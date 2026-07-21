import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.analysis.analyzer import AnalysisBatch, ItemAnalysis
from app.config import AnalysisConfig, Config, ObsidianConfig, StorageConfig
from app.connectors.base import FetchResult, FetchStats, RawItem
from app.db import apply_migrations, connect
from app.digest.builder import today_jst
from app.http import HttpClient
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


def test_stage_failure_is_isolated_and_reported(conn, tmp_path) -> None:
    # プロファイル欠落 → analyze 段は失敗するが、digest(空)・purge まで進む
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
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1  # collect は成功
    digest = conn.execute("SELECT item_count FROM digests").fetchone()
    assert digest["item_count"] == 0  # 分析なしでも digest 段は空で完走


def test_analyze_unavailable_fails_stage_without_degraded(conn, tmp_path) -> None:
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
    # digest 段は空で完走(トレンドは付加機能として節省略・本体は止めない)
    assert conn.execute("SELECT item_count FROM digests").fetchone()["item_count"] == 0


def test_analyze_full_degradation_fails_stage_but_keeps_degraded_digest(conn, tmp_path) -> None:
    # transient 由来の全滅(FullDegradationError)も analyze 段の失敗になるが、
    # degraded 行は書き込み済みで digest は劣化配信のまま完走する(GH#74 案2の二重ガード)
    from app.analysis.llm import LLMError

    class AlwaysFailEngine:
        name = "codex-cli"
        model = "fake"

        def complete_structured(self, prompt, schema, timeout_s=120):
            raise LLMError("常に失敗")

    failures = run_pipeline(
        conn, make_config(tmp_path), {}, {"rss": StaticConnector()}, make_http(),
        lambda: AlwaysFailEngine(), logger, "01RUNID",
    )
    assert "analyze" in failures
    assert conn.execute("SELECT count(*) FROM analyses WHERE degraded=1").fetchone()[0] == 1
    digest = conn.execute("SELECT item_count, status FROM digests").fetchone()
    assert digest["status"] == "degraded" and digest["item_count"] == 1  # 劣化配信は維持


def test_export_failure_reported_but_purge_runs(conn, tmp_path) -> None:
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


def test_dig_stage_failure_is_isolated(conn, tmp_path) -> None:
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
