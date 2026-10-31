import fcntl
import json
import logging
import os
import re
import sqlite3
import tarfile
import threading
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.analysis.analyzer import AnalysisBatch, ItemAnalysis
from app.config import AnalysisConfig, Config, ObsidianConfig, StorageConfig
from app.connectors.base import FetchResult, FetchStats, RawItem
from app.db import apply_migrations, connect
from app.http import HttpClient
from app.jst import today_jst
from app.run import run_pipeline

logger = logging.getLogger("test-run")

LONG_BODY = "分析対象の本文。" * 60


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    conn.execute(
        "INSERT INTO sources (type, name, config) VALUES ('rss', 'feed-a', '{\"url\": \"https://a\"}')"
    )
    yield conn
    conn.close()


class StaticConnector:
    source_type = "rss"

    def fetch(self, source_row, cursor):
        item = RawItem(
            external_id="guid-1",
            url="https://example.com/article",
            title="十分に長い記事タイトルのサンプルです",
            author=None,
            published_at=datetime.now(UTC),
            raw_content=f"<p>{LONG_BODY}</p>",
            meta={},
        )
        return FetchResult(
            items=[item],
            next_cursor={"etag": None, "last_modified": None},
            stats=FetchStats(
                items_fetched=1,
                api_reads=0,
                estimated_cost_usd=0.0,
                http_status=200,
                duration_ms=1,
            ),
        )


class ScriptedEngine:
    name = "codex-cli"
    model = "fake"

    def __init__(self, conn):
        self._conn = conn

    def complete_structured(self, prompt, schema, timeout_s=120):
        ids = [r["id"] for r in self._conn.execute("SELECT id FROM items WHERE status='new'")]
        return AnalysisBatch(
            results=[
                ItemAnalysis(
                    item_id=i, title_ja="日本語見出し", summary="要約", relevance=90,
                    category="課題直結", negative=[], novelty="new", topic=f"t{i}",
                    reason="理由", implication="示唆",
                )
                for i in ids
            ]
        )


def make_config(tmp_path: Path) -> Config:
    profile = tmp_path / "interest-profile.md"
    profile.write_text("---\nupdated: 2026-07-06\n---\n# 関心\n")
    return Config(
        config_dir=tmp_path,
        obsidian=ObsidianConfig(vault_path=str(tmp_path / "vault")),
        analysis=AnalysisConfig(profile_path=str(profile)),
        storage=StorageConfig(backup_dir=str(tmp_path / "backups")),
    )


def make_http() -> HttpClient:
    return HttpClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(404)),
        resolve_host=lambda host: ["93.184.216.34"],
        sleep=lambda _s: None,
    )


# 前の段が失敗した日も purge・backup が実際に走ったことを、結果で確かめる(GH#257)。
# 失敗の一覧に名前が無いことだけでは、段を飛ばしても通ってしまう。
# purge は SQLite の現在時刻と比べるので、期限は実時計に左右されない遠い過去・未来に固定する
EXPIRED_AT = "2000-01-01T00:00:00Z"
LIVE_UNTIL = "2999-12-31T00:00:00Z"
BACKUP_DAY = "2026-07-06"  # backup の世代名(JST の日付)も固定する


@pytest.fixture
def ttl_documents(conn, monkeypatch) -> tuple[int, int]:
    """期限切れと期限内の本文(documents)を 1 件ずつ用意し、(期限切れ, 期限内) の item_id を返す。

    item は配信済み(digested)にして、collect から dig までの段の対象にしない。
    """
    monkeypatch.setattr("app.backup.today_jst", lambda: BACKUP_DAY)
    ids = []
    for key, expires_at in (("expired", EXPIRED_AT), ("live", LIVE_UNTIL)):
        url = f"https://ttl.example/{key}"
        item_id = conn.execute(
            "INSERT INTO items (source_id, url, canonical_url, content_hash, status) "
            "VALUES (1, ?, ?, ?, 'digested')",
            (url, url, f"sha256:{key}"),
        ).lastrowid
        conn.execute(
            "INSERT INTO documents (item_id, extracted_text, extraction_status, expires_at) "
            "VALUES (?, '本文', 'ok', ?)",
            (item_id, expires_at),
        )
        ids.append(item_id)
    return ids[0], ids[1]


def assert_purged_then_backed_up(conn, tmp_path, ttl_documents) -> None:
    """purge 段が期限切れだけを消し、同じ実行の backup 段がその結果を写したことを確かめる。"""
    expired_id, live_id = ttl_documents

    def seeded_documents(db) -> set[int]:
        rows = db.execute(
            "SELECT item_id FROM documents WHERE item_id IN (?, ?)", (expired_id, live_id)
        )
        return {row[0] for row in rows}

    assert seeded_documents(conn) == {live_id}  # purge: 期限切れは消え、期限内は残る
    archive = tmp_path / "backups" / f"intel-{BACKUP_DAY.replace('-', '')}.tar.gz"
    with tarfile.open(archive) as tar:
        tar.extract("intel.db", path=tmp_path / "restore", filter="data")
    restored = sqlite3.connect(tmp_path / "restore" / "intel.db")
    try:
        assert seeded_documents(restored) == {live_id}  # backup: purge の後のスナップショット
    finally:
        restored.close()


def test_full_pipeline_end_to_end(conn, tmp_path) -> None:
    config = make_config(tmp_path)
    failures = run_pipeline(
        conn, config, {}, {"rss": StaticConnector()}, make_http(),
        lambda: ScriptedEngine(conn), logger, "01RUNID",
    )
    assert failures == []

    digest = conn.execute("SELECT * FROM digests").fetchone()
    assert digest["run_id"] == "01RUNID"  # run_id が digests を貫通
    assert digest["item_count"] == 1
    analysis = conn.execute("SELECT run_id FROM analyses").fetchone()
    assert analysis["run_id"] == "01RUNID"  # analyses も貫通
    item = conn.execute("SELECT status FROM items").fetchone()
    assert item["status"] == "digested"
    daily = tmp_path / "vault" / "SignalForge" / "Daily" / f"{today_jst()}.md"
    assert daily.exists()
    assert list((tmp_path / "vault" / "SignalForge" / "Items").glob("*.md"))
    assert list((tmp_path / "backups").glob("intel-*.tar.gz"))  # backup 段(設計 05 §5)


def test_rerun_is_idempotent(conn, tmp_path) -> None:
    config = make_config(tmp_path)
    args = (conn, config, {}, {"rss": StaticConnector()}, make_http())
    assert run_pipeline(*args, lambda: ScriptedEngine(conn), logger, "01RUN1") == []
    assert run_pipeline(*args, lambda: ScriptedEngine(conn), logger, "01RUN2") == []
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM digests").fetchone()[0] == 1


def test_stage_failure_is_isolated_and_reported(conn, tmp_path, ttl_documents) -> None:
    # プロファイル欠落 → analyze 段は失敗するが、digest・purge まで進む
    config = Config(
        config_dir=tmp_path,
        obsidian=ObsidianConfig(vault_path=str(tmp_path / "vault")),
        storage=StorageConfig(backup_dir=str(tmp_path / "backups")),
    )
    failures = run_pipeline(
        conn, config, {}, {"rss": StaticConnector()}, make_http(),
        lambda: ScriptedEngine(conn), logger, "01RUNID",
    )
    assert "analyze" in failures
    assert "purge" not in failures
    collected = conn.execute(
        "SELECT count(*) FROM items WHERE url = 'https://example.com/article'"
    ).fetchone()[0]
    assert collected == 1  # collect は成功(ttl_documents が用意した 2 件は数えない)
    # 分析なしの空は障害の空。digest 段は確定せずに完走し、export も当日分を書かずに通る(GH#191)
    assert "digest" not in failures and "export" not in failures
    assert conn.execute("SELECT count(*) FROM digests").fetchone()[0] == 0
    assert_purged_then_backed_up(conn, tmp_path, ttl_documents)


def test_analyze_unavailable_fails_stage_without_degraded(conn, tmp_path, ttl_documents) -> None:
    # 環境障害(codex 不在 = #72)は silent full-degradation でなく analyze 段の失敗になる
    # (failures 非空は CLI の非0終了・失敗通知の入口 = app/run.py docstring。GH#74)。
    # 後続の段は継続する
    from app.analysis.llm import LLMUnavailableError

    class UnavailableEngine:
        name = "codex-cli"
        model = "fake"

        def complete_structured(self, prompt, schema, timeout_s=120):
            raise LLMUnavailableError("codex 起動失敗: [Errno 2] No such file or directory")

    failures = run_pipeline(
        conn, make_config(tmp_path), {}, {"rss": StaticConnector()}, make_http(),
        lambda: UnavailableEngine(), logger, "01RUNID",
    )
    assert "analyze" in failures
    assert "purge" not in failures and "backup" not in failures  # 後続の段は継続
    # degraded を1件も書かない(修復後の再実行がそのまま再分析になる)
    assert conn.execute("SELECT count(*) FROM analyses").fetchone()[0] == 0
    # 空の digest は確定しない(修復後の同じ日の再実行で作る。GH#191)。トレンドは付加機能として
    # 節省略・本体は止めない
    assert "digest" not in failures
    assert conn.execute("SELECT count(*) FROM digests").fetchone()[0] == 0
    assert_purged_then_backed_up(conn, tmp_path, ttl_documents)


def use_fake_telegram(monkeypatch) -> list[httpx.Request]:
    """朝バッチの Telegram を MockTransport に差し替え、送った request の一覧を返す。"""
    from app import run as run_module
    from app.delivery import TelegramApi

    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent)}})

    monkeypatch.setattr(
        run_module, "create_api",
        lambda env: TelegramApi("tok", transport=httpx.MockTransport(handler)),
    )
    return sent


def with_telegram(config: Config) -> Config:
    from dataclasses import replace

    from app.config import TelegramConfig

    return replace(config, telegram=TelegramConfig(owner_user_id=1, owner_chat_id=42))


def test_empty_digest_after_analyze_failure_is_delivered_on_same_day_rerun(
    conn, tmp_path, monkeypatch
) -> None:
    # 分析の障害で載せる記事が無い日は、空の digest を確定せず送らない。復旧後の同じ日の
    # 再実行で、回復した分析の結果を作って配信する(GH#191)
    from app.analysis.llm import LLMUnavailableError

    class UnavailableEngine:
        name = "codex-cli"
        model = "fake"

        def complete_structured(self, prompt, schema, timeout_s=120):
            raise LLMUnavailableError("codex 起動失敗")

    sent = use_fake_telegram(monkeypatch)
    args = (
        conn, with_telegram(make_config(tmp_path)), {"TELEGRAM_BOT_TOKEN": "tok"},
        {"rss": StaticConnector()}, make_http(),
    )
    failures = run_pipeline(*args, lambda: UnavailableEngine(), logger, "01RUN1")
    assert failures == ["analyze"]  # export・deliver は当日分を扱わず、失敗にしない
    assert conn.execute("SELECT count(*) FROM digests").fetchone()[0] == 0
    assert sent == []  # 空のダイジェストを送らない
    assert failures.digest_held  # 失敗通知は同じ日の再走を案内する

    assert run_pipeline(*args, lambda: ScriptedEngine(conn), logger, "01RUN2") == []
    digest = conn.execute("SELECT run_id, item_count, delivery_status FROM digests").fetchone()
    assert tuple(digest) == ("01RUN2", 1, "sent")
    assert sent  # 回復した分析の結果が同じ日に届く


def test_date_change_after_digest_is_not_taken_as_held(conn, tmp_path, monkeypatch) -> None:
    # analyze 段が失敗しても載せる記事があれば digest を作る。その後に JST の日付が変わっても
    # 保留と取り違えない(deliver を黙って省かず、通知も「作れなかった」と言わない。GH#191)
    from app import run as run_module
    from app.analysis.llm import LLMUnavailableError
    from tests.test_ranking import add_analyzed

    class UnavailableEngine:
        name = "codex-cli"
        model = "fake"

        def complete_structured(self, prompt, schema, timeout_s=120):
            raise LLMUnavailableError("codex 起動失敗")

    carried = add_analyzed(conn, "https://carried.example/1", source_id=1, relevance=90)
    day = {"today": "2026-07-06"}
    monkeypatch.setattr("app.digest.builder.today_jst", lambda: "2026-07-06")
    monkeypatch.setattr(run_module, "today_jst", lambda: day["today"])
    generate_digest = run_module.generate_digest

    def generate_then_cross_midnight(*args, **kwargs):
        digest_id = generate_digest(*args, **kwargs)
        day["today"] = "2026-07-07"  # digest 段の直後に JST の 0 時を越える
        return digest_id

    monkeypatch.setattr(run_module, "generate_digest", generate_then_cross_midnight)
    sent = use_fake_telegram(monkeypatch)
    failures = run_pipeline(
        conn, with_telegram(make_config(tmp_path)), {"TELEGRAM_BOT_TOKEN": "tok"},
        {"rss": StaticConnector()}, make_http(), lambda: UnavailableEngine(), logger, "01RUN",
    )
    digest = conn.execute("SELECT digest_date, item_ids, delivery_status FROM digests").fetchone()
    assert tuple(digest) == ("2026-07-06", f"[{carried}]", "pending")  # 作った digest は残る
    assert not failures.digest_held
    # 日付をまたいだ扱いは従来どおり段の失敗として知らせる(保留として黙って省かない)
    assert "deliver" in failures
    assert sent == []
    assert failures.deliver_date is None  # 送る digest を引けていない(送り直し方を添えない)


def test_failed_delivery_keeps_digest_date_across_midnight(conn, tmp_path, monkeypatch) -> None:
    # deliver 段の失敗の後、失敗通知まで(dig・purge・backup)に JST の 0 時を越えても、
    # 送り直し方の日付は送ろうとした digest の日付のまま(GH#247)
    from app import run as run_module
    from app.delivery import DeliveryError

    day = {"today": "2026-07-06"}
    monkeypatch.setattr("app.digest.builder.today_jst", lambda: "2026-07-06")
    monkeypatch.setattr(run_module, "today_jst", lambda: day["today"])

    def fail_then_cross_midnight(*args, **kwargs):
        day["today"] = "2026-07-07"  # 配信の失敗の後に JST の 0 時を越える
        raise DeliveryError("Telegram sendMessage が失敗: HTTP 400")

    monkeypatch.setattr(run_module, "deliver_digest", fail_then_cross_midnight)
    use_fake_telegram(monkeypatch)
    failures = run_pipeline(
        conn, with_telegram(make_config(tmp_path)), {"TELEGRAM_BOT_TOKEN": "tok"},
        {"rss": StaticConnector()}, make_http(), lambda: ScriptedEngine(conn), logger, "01RUN",
    )
    assert "deliver" in failures
    assert failures.deliver_date == "2026-07-06"


def test_no_qualified_items_digest_is_finalized_as_before(conn, tmp_path, monkeypatch) -> None:
    # 分析が通って載せる記事が無い日は、今までどおり「該当なし」で確定して配信し、
    # 同じ日の再実行でも作り直さない(障害の空とは区別する。GH#191)
    class LowRelevanceEngine(ScriptedEngine):
        def complete_structured(self, prompt, schema, timeout_s=120):
            batch = super().complete_structured(prompt, schema, timeout_s)
            return AnalysisBatch(
                results=[result.model_copy(update={"relevance": 0}) for result in batch.results]
            )

    sent = use_fake_telegram(monkeypatch)
    args = (
        conn, with_telegram(make_config(tmp_path)), {"TELEGRAM_BOT_TOKEN": "tok"},
        {"rss": StaticConnector()}, make_http(),
    )
    failures = run_pipeline(*args, lambda: LowRelevanceEngine(conn), logger, "01RUN1")
    assert failures == []
    assert not failures.digest_held
    digest = conn.execute(
        "SELECT run_id, status, item_count, delivery_status, markdown FROM digests"
    ).fetchone()
    assert tuple(digest)[:4] == ("01RUN1", "ok", 0, "sent")
    assert "基準(min_score)を満たすアイテムがありませんでした" in digest["markdown"]
    delivered = len(sent)

    assert run_pipeline(*args, lambda: ScriptedEngine(conn), logger, "01RUN2") == []
    rows = conn.execute("SELECT run_id, item_count FROM digests").fetchall()
    assert [tuple(row) for row in rows] == [("01RUN1", 0)]  # 作り直さない
    assert len(sent) == delivered  # 送り直さない


def test_analyze_full_degradation_fails_stage_but_keeps_degraded_digest(
    conn, tmp_path, monkeypatch
) -> None:
    # transient 由来の全滅(FullDegradationError)も analyze 段の失敗になるが、
    # degraded 行は書き込み済みで digest は劣化配信のまま完走する(GH#74 案2の二重ガード)
    from app import run as run_module
    from app.analysis.llm import LLMError

    class AlwaysFailEngine:
        name = "codex-cli"
        model = "fake"

        def complete_structured(self, prompt, schema, timeout_s=120):
            raise LLMError("常に失敗")

    # 全滅の判定は当日(JST)に分析された候補を見る(GH#175)。analyze 段は分析の時刻を既定の
    # 「今」で書くので、digest 段の直前に当日の時刻へ置き換え、当日も固定する(段の間で JST の
    # 0 時をまたいでも食い違わないように)
    today = "2026-07-06"
    monkeypatch.setattr("app.digest.builder.today_jst", lambda: today)
    monkeypatch.setattr(run_module, "today_jst", lambda: today)  # 生成済みの確認・export の当日
    generate_digest = run_module.generate_digest

    def generate_digest_analyzed_today(db, *args, **kwargs):
        db.execute("UPDATE analyses SET analyzed_at = '2026-07-06T00:00:00Z'")  # today の JST 9:00
        return generate_digest(db, *args, **kwargs)

    monkeypatch.setattr(run_module, "generate_digest", generate_digest_analyzed_today)

    failures = run_pipeline(
        conn, make_config(tmp_path), {}, {"rss": StaticConnector()}, make_http(),
        lambda: AlwaysFailEngine(), logger, "01RUNID",
    )
    assert "analyze" in failures
    assert conn.execute("SELECT count(*) FROM analyses WHERE degraded=1").fetchone()[0] == 1
    digest = conn.execute("SELECT item_count, status FROM digests").fetchone()
    assert digest["status"] == "degraded" and digest["item_count"] == 1  # 劣化配信は維持


def test_export_failure_reported_but_purge_runs(conn, tmp_path, ttl_documents) -> None:
    profile = tmp_path / "interest-profile.md"
    profile.write_text("---\nupdated: 2026-07-06\n---\n# 関心\n")
    config = Config(  # vault 未設定 → export 失敗
        config_dir=tmp_path,
        analysis=AnalysisConfig(profile_path=str(profile)),
        storage=StorageConfig(backup_dir=str(tmp_path / "backups")),
    )
    failures = run_pipeline(
        conn, config, {}, {"rss": StaticConnector()}, make_http(),
        lambda: ScriptedEngine(conn), logger, "01RUNID",
    )
    assert failures == ["export"]
    assert_purged_then_backed_up(conn, tmp_path, ttl_documents)


def test_deliver_skipped_when_telegram_unconfigured(conn, tmp_path) -> None:
    config = make_config(tmp_path)
    failures = run_pipeline(
        conn, config, {}, {"rss": StaticConnector()}, make_http(),
        lambda: ScriptedEngine(conn), logger, "01RUNID",
    )
    assert failures == []
    # deliver 段は skip され、digest は未配信のまま(検知は doctor #4 の責務)
    assert conn.execute("SELECT delivery_status FROM digests").fetchone()[0] == "pending"


def test_deliver_runs_when_telegram_configured(conn, tmp_path, monkeypatch) -> None:
    import httpx

    from app import run as run_module
    from app.config import TelegramConfig
    from app.delivery import TelegramApi

    def fake_create_api(env):
        assert env["TELEGRAM_BOT_TOKEN"] == "tok"
        return TelegramApi(
            "tok",
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(
                    200, json={"ok": True, "result": {"message_id": 1}}
                )
            ),
        )

    monkeypatch.setattr(run_module, "create_api", fake_create_api)
    base = make_config(tmp_path)
    config = Config(
        config_dir=base.config_dir,
        obsidian=base.obsidian,
        analysis=base.analysis,
        storage=base.storage,
        telegram=TelegramConfig(owner_user_id=1, owner_chat_id=42),
    )
    failures = run_pipeline(
        conn, config, {"TELEGRAM_BOT_TOKEN": "tok"}, {"rss": StaticConnector()}, make_http(),
        lambda: ScriptedEngine(conn), logger, "01RUNID",
    )
    assert failures == []
    row = conn.execute("SELECT delivery_status, sent_at FROM digests").fetchone()
    assert row["delivery_status"] == "sent"
    assert row["sent_at"] is not None


def test_run_id_in_json_log_lines(conn, tmp_path) -> None:
    from app.logs import setup_logging

    log = setup_logging(tmp_path / "logs", "morning.log")
    config = make_config(tmp_path)
    run_pipeline(
        conn, config, {}, {"rss": StaticConnector()}, make_http(),
        lambda: ScriptedEngine(conn), log, "01RUNID",
    )
    logging.shutdown()
    lines = [
        json.loads(line)
        for line in (tmp_path / "logs" / "morning.log").read_text().splitlines()
    ]
    stages = {line["stage"] for line in lines if line["run_id"] == "01RUNID"}
    # 主要ステージのログに run_id が貫通している
    assert {"collect", "extract", "analyze", "digest", "export", "purge", "run"} <= stages


def test_x_signals_stage_absent_when_cap_zero(conn, tmp_path) -> None:
    # 既定(cap 0 = 無効)では x_signals 段を配線しない(挙動不変)
    failures = run_pipeline(
        conn, make_config(tmp_path), {}, {"rss": StaticConnector()}, make_http(),
        lambda: ScriptedEngine(conn), logger, "01RUNID",
    )
    assert failures == []
    assert conn.execute("SELECT count(*) FROM x_signal_runs").fetchone()[0] == 0


def test_x_signals_stage_fails_loud_when_enabled_but_unconfigured(conn, tmp_path) -> None:
    # cap > 0(取込を意図)なのに client_id / token 未設定は段の失敗(黙って skip しない)
    from dataclasses import replace

    from app.config import XSignalsConfig

    config = replace(make_config(tmp_path), x_signals=XSignalsConfig(daily_read_cap=15))
    failures = run_pipeline(
        conn, config, {}, {"rss": StaticConnector()}, make_http(),
        lambda: ScriptedEngine(conn), logger, "01RUNID",
    )
    assert "x_signals" in failures
    assert "digest" not in failures  # 後続の段は継続


def test_dig_stage_failure_is_isolated(conn, tmp_path, ttl_documents) -> None:
    # 材料不足のリプライ指定は dig 段の失敗になる(GH#105)が、後続の段は継続する
    item_id = conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, content_hash) "
        "VALUES (1, 'https://q/1', 'https://q/1', 'sha256:q')"
    ).lastrowid
    conn.execute(
        "INSERT INTO dig_queue (item_id, status, source, requested_at) "
        "VALUES (?, 'queued', 'request', '2026-07-21T00:00:00Z')",
        (item_id,),
    )
    failures = run_pipeline(
        conn, make_config(tmp_path), {}, {"rss": StaticConnector()}, make_http(),
        lambda: ScriptedEngine(conn), logger, "01RUNID",
    )
    assert "dig" in failures
    assert "purge" not in failures and "backup" not in failures
    assert_purged_then_backed_up(conn, tmp_path, ttl_documents)


def test_dig_stage_yields_without_failure_while_another_dig_runs(conn, tmp_path) -> None:
    # 手動の intel dig が調査段を進めている間は、朝バッチの dig 段は待たずに何もしないで後続の段へ
    # 進み、段の失敗にもしない(GH#196)。走れば失敗になる材料不足のリプライ指定で確かめる
    import fcntl

    item_id = conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, content_hash) "
        "VALUES (1, 'https://q/1', 'https://q/1', 'sha256:q')"
    ).lastrowid
    conn.execute(
        "INSERT INTO dig_queue (item_id, status, source, requested_at) "
        "VALUES (?, 'queued', 'request', '2026-07-21T00:00:00Z')",
        (item_id,),
    )
    with (tmp_path / "intel.db.dig.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        failures = run_pipeline(
            conn, make_config(tmp_path), {}, {"rss": StaticConnector()}, make_http(),
            lambda: ScriptedEngine(conn), logger, "01RUNID",
        )
    assert failures == []
    assert conn.execute(
        "SELECT status FROM dig_queue WHERE item_id = ?", (item_id,)
    ).fetchone()["status"] == "queued"  # 先の実行に任せ、キューに触れない


def test_dig_stage_absent_when_cap_zero(conn, tmp_path) -> None:
    from dataclasses import replace

    from app.config import DigConfig

    item_id = conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, content_hash) "
        "VALUES (1, 'https://q/1', 'https://q/1', 'sha256:q')"
    ).lastrowid
    conn.execute(
        "INSERT INTO dig_queue (item_id, status, source, requested_at) "
        "VALUES (?, 'queued', 'request', '2026-07-21T00:00:00Z')",
        (item_id,),
    )
    config = replace(make_config(tmp_path), dig=DigConfig(daily_cap=0))
    failures = run_pipeline(
        conn, config, {}, {"rss": StaticConnector()}, make_http(),
        lambda: ScriptedEngine(conn), logger, "01RUNID",
    )
    assert failures == []  # 段ごと省かれ、材料不足でも失敗しない


def test_pipeline_runs_same_stages_as_cli(conn, tmp_path, monkeypatch) -> None:
    # 朝バッチの analyze・digest・dig 段は、単独の CLI と同じ段の組み立てと実行の口を呼ぶ
    # (GH#206)。朝バッチは run_id と、run_pipeline に渡された主の作り方を渡す
    from app import run as run_module

    calls: dict[str, dict] = {}

    def spy(name, returned):
        def stage(db, config, env, stage_logger, **kwargs):
            calls[name] = kwargs
            return returned

        return stage

    monkeypatch.setattr(
        run_module, "analyze_stage", spy("analyze", {"analyzed": 0, "degraded": 0})
    )
    monkeypatch.setattr(run_module, "digest_stage", spy("digest", None))
    monkeypatch.setattr(run_module, "dig_stage", spy("dig", True))
    primary = ScriptedEngine(conn)
    run_pipeline(
        conn, make_config(tmp_path), {}, {"rss": StaticConnector()}, make_http(),
        lambda: primary, logger, "01RUNID", force_digest=True,
    )
    assert list(calls) == ["analyze", "digest", "dig"]
    assert all(kwargs["run_id"] == "01RUNID" for kwargs in calls.values())
    assert calls["analyze"]["primary"]("low") is primary
    assert calls["digest"]["primary"]("low") is primary
    assert (calls["digest"]["force"], calls["digest"]["hold_if_empty"]) == (True, False)
    assert set(calls["dig"]) == {"run_id"}  # dig の主は config から effort 可変で作る


@pytest.mark.parametrize("review", [True, False])
def test_dig_stage_wires_review_engine_from_config(conn, tmp_path, monkeypatch, review) -> None:
    # 深掘りのレビュー(GH#153)は [dig].review のときだけ。キーは .env の dict から読む
    from app import deepdive
    from app.analysis.llm import AnthropicApiEngine, LLMUnavailableError
    from app.config import DigConfig
    from app.run import dig_stage

    captured: dict = {}
    monkeypatch.setattr(
        deepdive, "run_dig_stage", lambda *args, **kwargs: captured.update(kwargs)
    )
    config = Config(config_dir=tmp_path, dig=DigConfig(review=review))
    dig_stage(conn, config, {"INTEL_ANTHROPIC_API_KEY": "k"}, logger, run_id="run-1")
    factory = captured["review_engine_factory"]
    if not review:
        assert factory is None
        return
    engine = factory()
    assert isinstance(engine, AnthropicApiEngine) and engine.model == "claude-opus-5-5"
    dig_stage(conn, config, {}, logger, run_id="run-2")
    with pytest.raises(LLMUnavailableError):
        captured["review_engine_factory"]()


def test_fallback_answers_when_codex_fails_and_is_noticed(conn, tmp_path, monkeypatch) -> None:
    # 主(Codex)が起動できなくても、予備(GH#156)が判定する。記録は答えたエンジンで、
    # 朝バッチの後に 1 通知らせる
    from dataclasses import replace

    from app import run as run_module
    from app.analysis.llm import LLMUnavailableError, LLMUsage
    from app.config import LLMFallbackConfig, TelegramConfig

    class Down:
        name = "codex-cli"
        model = "fake"

        def complete_structured(self, prompt, schema, timeout_s=120):
            raise LLMUnavailableError("codex 起動失敗")

    class Claude(ScriptedEngine):
        name = "anthropic-api"
        model = "claude-sonnet-5-5"
        last_usage = LLMUsage(1000, 100, 0.01)

    monkeypatch.setattr(
        "app.analysis.fallback.create_anthropic_engine", lambda settings, env: Claude(conn)
    )
    config = replace(make_config(tmp_path), llm_fallback=LLMFallbackConfig(engine="anthropic-api"))
    failures = run_pipeline(
        conn, config, {}, {"rss": StaticConnector()}, make_http(),
        lambda: Down(), logger, "01RUNFB",
    )
    assert "analyze" not in failures
    analysis = conn.execute("SELECT engine, model, degraded FROM analyses").fetchone()
    assert tuple(analysis) == ("anthropic-api", "claude-sonnet-5-5", 0)
    # 記事が 1 件なのでトレンドは LLM を呼ばない(配線は下の digest_stage のテスト)
    ledger = conn.execute("SELECT stage, run_id FROM llm_fallback_calls").fetchall()
    assert [(row["stage"], row["run_id"]) for row in ledger] == [("analyze", "01RUNFB")]

    sent: list[tuple[int, str]] = []

    class Api:
        def send_message(self, chat_id, text, **kwargs):
            sent.append((chat_id, text))
            return 1

    monkeypatch.setattr(run_module, "create_api", lambda env: Api())
    with_telegram = replace(config, telegram=TelegramConfig(owner_user_id=1, owner_chat_id=10))
    run_module.notify_llm_fallback(
        conn, with_telegram, {"TELEGRAM_BOT_TOKEN": "t"}, "01RUNFB", logger
    )
    ((chat_id, text),) = sent
    assert chat_id == 10
    assert "Claude API で代行" in text and "記事の判定: 1 回" in text


def test_fallback_notice_is_quiet_when_unused_or_off(conn, tmp_path, monkeypatch) -> None:
    from dataclasses import replace

    from app import run as run_module
    from app.config import LLMFallbackConfig, TelegramConfig

    sent: list[str] = []  # 送信の失敗は握られるので、例外でなく記録で確かめる

    class Api:
        def send_message(self, chat_id, text, **kwargs):
            sent.append(text)
            return 1

    monkeypatch.setattr(run_module, "create_api", lambda env: Api())
    conn.execute(
        "INSERT INTO llm_fallback_calls (run_id, stage, model, primary_error, ok)"
        " VALUES ('01USED', 'analyze', 'm', 'LLMError', 1)"
    )
    config = replace(
        make_config(tmp_path), telegram=TelegramConfig(owner_user_id=1, owner_chat_id=10)
    )
    env = {"TELEGRAM_BOT_TOKEN": "t"}
    run_module.notify_llm_fallback(conn, config, env, "01USED", logger)  # 予備は無効
    enabled = replace(config, llm_fallback=LLMFallbackConfig(engine="anthropic-api"))
    run_module.notify_llm_fallback(conn, enabled, env, "01OTHER", logger)  # この回は使っていない
    assert sent == []
    run_module.notify_llm_fallback(conn, enabled, env, "01USED", logger)  # 対照: 送る
    assert len(sent) == 1


def test_fallback_notice_send_failure_is_only_logged(conn, tmp_path, monkeypatch, caplog) -> None:
    from dataclasses import replace

    from app import run as run_module
    from app.config import LLMFallbackConfig, TelegramConfig

    conn.execute(
        "INSERT INTO llm_fallback_calls (run_id, stage, model, primary_error, ok)"
        " VALUES ('01RUN', 'dig', 'm', 'LLMError', 1)"
    )

    class Api:
        def send_message(self, chat_id, text, **kwargs):
            raise RuntimeError("送れない")

    monkeypatch.setattr(run_module, "create_api", lambda env: Api())
    config = replace(
        make_config(tmp_path),
        telegram=TelegramConfig(owner_user_id=1, owner_chat_id=10),
        llm_fallback=LLMFallbackConfig(engine="anthropic-api"),
    )
    run_module.notify_llm_fallback(conn, config, {"TELEGRAM_BOT_TOKEN": "t"}, "01RUN", logger)
    assert "予備の利用の知らせを送れない" in caplog.text


def test_dig_stage_engine_falls_back_when_enabled(conn, tmp_path, monkeypatch) -> None:
    from app import deepdive
    from app.analysis.fallback import FallbackEngine
    from app.analysis.llm import CodexCliEngine
    from app.config import LLMFallbackConfig
    from app.run import dig_stage

    captured: dict = {}
    monkeypatch.setattr(
        deepdive, "run_dig_stage", lambda *args, **kwargs: captured.update(args=args)
    )
    off = Config(config_dir=tmp_path)
    dig_stage(conn, off, {}, logger, run_id="run-1")
    assert isinstance(captured["args"][2]("medium"), CodexCliEngine)
    on = Config(config_dir=tmp_path, llm_fallback=LLMFallbackConfig(engine="anthropic-api"))
    dig_stage(conn, on, {}, logger, run_id="run-2")
    assert isinstance(captured["args"][2]("medium"), FallbackEngine)


def test_trend_engine_falls_back_when_enabled(conn, tmp_path, monkeypatch) -> None:
    from app.analysis import trend
    from app.analysis.fallback import FallbackEngine
    from app.config import LLMFallbackConfig
    from app.run import digest_stage

    engines: list = []

    def capture(conn, engine, logger, run_id=None):
        engines.append(engine)
        return None

    monkeypatch.setattr(trend, "synthesize_trend", capture)
    primary = ScriptedEngine(conn)
    for config in (
        make_config(tmp_path),
        Config(
            config_dir=tmp_path,
            obsidian=ObsidianConfig(vault_path=str(tmp_path / "vault")),
            llm_fallback=LLMFallbackConfig(engine="anthropic-api"),
        ),
    ):
        digest_stage(
            conn, config, {}, logger, run_id="run-1", primary=lambda _effort: primary,
            force=True,
        )
    assert engines[0] is primary  # 予備が無効なら主のまま
    assert isinstance(engines[1], FallbackEngine) and engines[1]._primary is primary


_HANG_GUARD_SECONDS = 30  # 並行のテストが止まったときだけ効く上限(正否の判定には使わない)


def test_overlapping_analyze_waits_and_keeps_daily_limit(tmp_path, caplog) -> None:
    # GH#334: 朝バッチと intel analyze が重なっても、後の実行は先の実行を待ち、先が分析した
    # 件数を引いた残りの枠だけを分析する(日次の上限を超えず、同じ item を 2 回 LLM に渡さない)。
    # 先の実行を LLM の応答待ちで止め、その間に後の実行を走らせる。後の実行がロックで待ち始めた
    # (修正前は事前選別を済ませて LLM を呼ぶ)のを合図に先の実行を進める。正否は時間に頼らない
    from app.analysis.analyzer import ANALYZE_LOCK_SUFFIX
    from app.run import analyze_stage

    db_path = tmp_path / "intel.db"
    setup = connect(db_path)
    apply_migrations(setup, db_path)
    setup.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed-a')")
    source_id = setup.execute("SELECT id FROM sources WHERE name = 'feed-a'").fetchone()["id"]

    def add_item(n: int) -> int:
        url = f"https://a.example/{n}"
        return setup.execute(
            "INSERT INTO items (source_id, url, canonical_url, title, content_hash) "
            "VALUES (?, ?, ?, '十分に長い記事タイトルのサンプル', ?)",
            (source_id, url, url, f"sha256:{n}"),
        ).lastrowid

    first_items = [add_item(n) for n in range(2)]
    base = make_config(tmp_path)
    config = Config(
        config_dir=tmp_path,
        analysis=AnalysisConfig(profile_path=base.analysis.profile_path, daily_llm_limit=3),
    )
    first_in_llm = threading.Event()
    release_first = threading.Event()
    lock_wait = threading.Event()  # 後の実行がロックで待ち始めた
    llm_while_held = threading.Event()  # 先の実行が止まっている間に後の実行が LLM を呼んだ
    second_reached = threading.Event()  # 上の 2 つのどちらか(後の実行が判断の所まで来た)
    sent: dict[str, list[int]] = {"first": [], "second": []}

    class PausingEngine:
        name = "codex-cli"
        model = "fake"

        def complete_structured(self, prompt, schema, timeout_s=120):
            ids = [int(i) for i in re.findall(r'<item id="(\d+)">', prompt)]
            name = threading.current_thread().name
            sent[name] += ids
            if name == "first":
                first_in_llm.set()
                assert release_first.wait(_HANG_GUARD_SECONDS)
            elif not release_first.is_set():
                llm_while_held.set()
                second_reached.set()
            return AnalysisBatch(
                results=[
                    ItemAnalysis(
                        item_id=i, title_ja="見出し", summary="要約", relevance=90,
                        category="課題直結", negative=[], novelty="new", topic=f"t{i}",
                        reason="理由", implication="示唆",
                    )
                    for i in ids
                ]
            )

    class LockWaitSignal(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.getMessage() == "別の分析が実行中のため、終わるのを待つ":
                lock_wait.set()
                second_reached.set()

    results: dict[str, object] = {}

    def run() -> None:
        name = threading.current_thread().name
        thread_conn = connect(db_path)
        try:
            results[name] = analyze_stage(
                thread_conn, config, {}, logger, run_id=name,
                primary=lambda _effort: PausingEngine(),
            )
        except BaseException as e:  # スレッドの例外は本体で検査する
            results[name] = e
        finally:
            thread_conn.close()

    first = threading.Thread(target=run, name="first", daemon=True)
    second = threading.Thread(target=run, name="second", daemon=True)
    signal = LockWaitSignal()
    logger.addHandler(signal)
    lock_path = tmp_path / ("intel.db" + ANALYZE_LOCK_SUFFIX)
    try:
        with caplog.at_level(logging.INFO, logger="test-run"):
            first.start()
            assert first_in_llm.wait(_HANG_GUARD_SECONDS)
            # 先の実行は LLM の応答待ちの間もロックを握っている(別の fd では取れない)
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
            later_items = [add_item(n) for n in range(2, 4)]  # 先の実行の後に届いた記事
            second.start()
            assert second_reached.wait(_HANG_GUARD_SECONDS)
    finally:
        # 途中の assert で抜けても先の実行を止めたままにせず、起動したスレッドの終わりを待つ
        release_first.set()
        for thread in (first, second):
            if thread.ident is not None:  # 起動前のスレッドは join できない
                thread.join(_HANG_GUARD_SECONDS)
        logger.removeHandler(signal)
    assert not first.is_alive() and not second.is_alive()

    assert not llm_while_held.is_set()
    assert lock_wait.is_set()
    assert results == {
        "first": {"analyzed": 2, "degraded": 0},
        "second": {"analyzed": 1, "degraded": 0},  # 上限 3 − 先の 2 = 残り 1
    }
    assert sent["first"] == first_items
    assert len(sent["second"]) == 1 and sent["second"][0] in later_items
    assert setup.execute("SELECT count(*) FROM analyses").fetchone()[0] == 3
    statuses = dict(setup.execute("SELECT id, status FROM items").fetchall())
    assert sorted(statuses.values()) == ["analyzed", "analyzed", "analyzed", "skipped"]
    # 両方の実行が終われば、ロックは放されている
    fd = os.open(lock_path, os.O_WRONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)
    setup.close()
