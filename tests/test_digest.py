import json
import logging
import sqlite3
from pathlib import Path

import pytest

from app.config import Config, DigestConfig
from app.db import apply_migrations, connect
from app.digest import generate_digest
from tests.test_ranking import add_analyzed

logger = logging.getLogger("test-digest")


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed-a')")  # id=2
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed-b')")  # id=3
    yield conn
    conn.close()


def make_config(tmp_path, max_items=10) -> Config:
    return Config(config_dir=tmp_path, digest=DigestConfig(max_items=max_items))


# 全滅の判定は当日(JST)に分析された候補を見る(GH#175)。判定に関わるテストは当日と分析の
# 時刻を固定する(既定の「今」のままだと、分析と digest の生成が JST の 0 時をまたぐと食い違う)
TODAY = "2026-07-06"
TODAY_START = "2026-07-05T15:00:00Z"  # TODAY の JST 0:00
TODAY_AT = "2026-07-06T00:00:00Z"  # TODAY の JST 9:00
JUST_BEFORE_TODAY = "2026-07-05T14:59:59Z"  # 前日の JST 23:59:59
PAST = "2026-01-01T00:00:00Z"  # 選外で analyzed のまま残った過去の記事


@pytest.fixture
def fixed_today(monkeypatch) -> None:
    monkeypatch.setattr("app.digest.builder.today_jst", lambda: TODAY)


def set_analyzed_at(conn, analyzed_at: str, *item_ids: int) -> None:
    for item_id in item_ids:
        conn.execute(
            "UPDATE analyses SET analyzed_at = ? WHERE item_id = ?", (analyzed_at, item_id)
        )


def test_digest_generated_with_items_in_score_order(conn, tmp_path) -> None:
    low = add_analyzed(conn, "https://a/low", relevance=50)
    high = add_analyzed(conn, "https://a/high", relevance=90)

    digest_id = generate_digest(conn, make_config(tmp_path), logger, run_id="01RUN")
    row = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row["status"] == "ok"
    assert row["run_id"] == "01RUN"
    assert json.loads(row["item_ids"]) == [high, low]
    assert row["item_count"] == 2
    assert "推薦理由" in row["markdown"]
    assert "https://a/high" in row["markdown"]
    statuses = {r["status"] for r in conn.execute("SELECT status FROM items")}
    assert statuses == {"digested"}


def test_headline_prefers_title_ja_with_fallback(conn, tmp_path) -> None:
    # analyze-v002 の日本語見出しを優先。NULL(v001 世代)は原文に fallback
    add_analyzed(conn, "https://a/en", relevance=90, title_ja="日本語の見出し")
    add_analyzed(conn, "https://a/legacy", relevance=80)  # title_ja NULL

    digest_id = generate_digest(conn, make_config(tmp_path), logger)
    md = conn.execute("SELECT markdown FROM digests WHERE id = ?", (digest_id,)).fetchone()[0]
    assert "## 1. 日本語の見出し" in md
    assert "title https://a/en" not in md  # 原文タイトルは表示しない
    assert "## 2. title https://a/legacy" in md  # fallback


def test_second_run_skips_force_replaces(conn, tmp_path) -> None:
    add_analyzed(conn, "https://a/1", relevance=90)
    config = make_config(tmp_path)
    first = generate_digest(conn, config, logger)
    assert generate_digest(conn, config, logger) is None  # skip(冪等)

    assert first is not None
    new_item = add_analyzed(conn, "https://a/2", relevance=95)
    replaced = generate_digest(conn, config, logger, force=True)
    assert replaced is not None
    assert conn.execute("SELECT count(*) FROM digests").fetchone()[0] == 1  # 1日1行
    row = conn.execute("SELECT item_ids FROM digests").fetchone()
    ids = json.loads(row["item_ids"])
    assert new_item in ids  # 新 item を含む
    # 旧 digest 採用 item も候補に復帰して残る(--force で黙って消えない)
    assert len(ids) == 2


def test_force_replace_preserves_digest_notes_and_resets_delivery(conn, tmp_path) -> None:
    # GH#96(Codex must SF-PR97-001): 感想(digest_notes)が付いた digest の --force
    # 置換が FK 違反にならず、感想は同じ digest_date の感想として保持される
    add_analyzed(conn, "https://a/1", relevance=90)
    config = make_config(tmp_path)
    first = generate_digest(conn, config, logger)
    conn.execute(
        "INSERT INTO digest_notes (digest_id, note) VALUES (?, '全体の感想')", (first,)
    )
    conn.execute(
        "UPDATE digests SET delivery_status = 'sent', sent_at = 'x' WHERE id = ?", (first,)
    )
    conn.commit()

    replaced = generate_digest(conn, config, logger, force=True)
    assert replaced == first  # id 維持の上書き(参照の安定)
    note = conn.execute("SELECT digest_id, note FROM digest_notes").fetchone()
    assert (note["digest_id"], note["note"]) == (first, "全体の感想")  # 感想は保持
    row = conn.execute("SELECT delivery_status, sent_at FROM digests").fetchone()
    assert (row["delivery_status"], row["sent_at"]) == ("pending", None)  # 配信状態は初期化


def test_force_replace_then_deliver_resends_all(conn, tmp_path) -> None:
    # Codex should(PR #97): --force + deliver の契約(今の世代の台帳を空にして全通再送)を直接保護。
    # 前の世代の行は消さず、置換の印を付けて残す(リプライの帰属と感想の投影に使う。GH#190)
    from app.config import TelegramConfig
    from app.delivery import deliver_digest
    from tests.test_delivery import ApiRecorder

    item = add_analyzed(conn, "https://a/1", relevance=90)
    config = make_config(tmp_path)
    tg_config = Config(config_dir=tmp_path, telegram=TelegramConfig(owner_chat_id=777))
    digest_id = generate_digest(conn, config, logger)
    recorder = ApiRecorder()  # 2 回の配信で同じものを使う(message_id は chat の中で重ならない)
    deliver_digest(conn, tg_config, recorder.api(), digest_id, logger)
    assert conn.execute("SELECT count(*) FROM digest_messages").fetchone()[0] == 2  # ヘッダ+1件

    assert generate_digest(conn, config, logger, force=True) == digest_id
    deliver_digest(conn, tg_config, recorder.api(), digest_id, logger)
    assert len(recorder.requests) == 4  # ヘッダ + 全 item が再送される(skip しない)
    ledger = conn.execute(
        "SELECT item_id, message_id, superseded_at IS NOT NULL AS superseded "
        "FROM digest_messages ORDER BY id"
    ).fetchall()
    assert [tuple(r) for r in ledger] == [
        (None, 101, 1), (item, 102, 1),  # 前の世代(置換の印つきで残る)
        (None, 103, 0), (item, 104, 0),  # 今の世代
    ]
    row = conn.execute("SELECT delivery_status FROM digests").fetchone()
    assert row["delivery_status"] == "sent"


def test_no_qualified_items_produces_empty_ok_digest(conn, tmp_path) -> None:
    add_analyzed(conn, "https://a/1", relevance=10)  # min_score 未満
    digest_id = generate_digest(conn, make_config(tmp_path), logger)
    row = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row["status"] == "ok"
    assert row["item_count"] == 0
    assert json.loads(row["item_ids"]) == []
    # min_score 未満の item は digested に遷移しない(analyzed のまま)
    assert conn.execute("SELECT status FROM items").fetchone()["status"] == "analyzed"


def test_hold_if_empty_does_not_finalize_empty_digest(conn, tmp_path) -> None:
    # 分析の段が失敗した日の空は「該当なし」として確定しない(行を作らない。GH#191)
    from app.digest.builder import DigestHeldError

    add_analyzed(conn, "https://a/1", relevance=10)  # min_score 未満
    config = make_config(tmp_path)
    with pytest.raises(DigestHeldError):
        generate_digest(conn, config, logger, hold_if_empty=True)
    assert conn.execute("SELECT count(*) FROM digests").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM items").fetchone()["status"] == "analyzed"
    # 載せる記事があれば、分析の段が失敗した日でも作る(先の実行で分析を済ませた候補など)
    ok = add_analyzed(conn, "https://a/2", relevance=90)
    digest_id = generate_digest(conn, config, logger, hold_if_empty=True)
    row = conn.execute("SELECT item_ids FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert json.loads(row["item_ids"]) == [ok]


def test_hold_if_empty_on_force_keeps_existing_digest(conn, tmp_path) -> None:
    # --force の置換でも障害の空なら、旧採用 item の復帰と台帳の置換の印(GH#190)ごと戻し、
    # 既存の digest をそのまま残す(GH#191)
    from dataclasses import replace

    from app.config import RankingConfig
    from app.digest.builder import DigestHeldError

    item = add_analyzed(conn, "https://a/1", relevance=90)
    config = make_config(tmp_path)
    digest_id = generate_digest(conn, config, logger)
    conn.execute(
        "UPDATE digests SET delivery_status = 'sent', sent_at = 'x' WHERE id = ?", (digest_id,)
    )
    conn.execute(
        "INSERT INTO digest_messages (digest_id, item_id, message_id) VALUES (?, ?, 1)",
        (digest_id, item),
    )
    strict = replace(config, ranking=RankingConfig(min_score=101))  # 旧採用 item も載らない
    with pytest.raises(DigestHeldError):
        generate_digest(conn, strict, logger, force=True, hold_if_empty=True)
    row = conn.execute("SELECT id, item_ids, delivery_status FROM digests").fetchone()
    assert (row["id"], json.loads(row["item_ids"]), row["delivery_status"]) == (
        digest_id, [item], "sent",
    )
    assert conn.execute("SELECT status FROM items").fetchone()["status"] == "digested"
    ledger = conn.execute("SELECT superseded_at FROM digest_messages").fetchall()
    assert [r["superseded_at"] for r in ledger] == [None]  # 今の世代のまま


def test_all_degraded_produces_title_only_digest(conn, tmp_path, fixed_today) -> None:
    a = add_analyzed(conn, "https://a/1", relevance=0, degraded=1)
    b = add_analyzed(conn, "https://a/2", relevance=0, degraded=1)
    set_analyzed_at(conn, TODAY_AT, a, b)
    digest_id = generate_digest(conn, make_config(tmp_path), logger)
    row = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row["status"] == "degraded"
    assert json.loads(row["item_ids"]) == [a, b]
    assert "degraded" in row["markdown"]
    assert "推薦理由" not in row["markdown"]  # タイトルのみ


def test_all_extraction_failed_today_is_not_full_degradation(conn, tmp_path, fixed_today) -> None:
    # GH#240: 当日の候補がすべて本文の抽出に失敗していても、LLM が答えていれば全滅(degraded
    # 配信)ではない。通常の digest にし、どの記事にも要約・推薦理由と印を残す
    a = add_analyzed(conn, "https://a/1", relevance=90)
    b = add_analyzed(conn, "https://a/2", relevance=80)
    conn.execute("UPDATE analyses SET extraction_failed = 1")
    set_analyzed_at(conn, TODAY_AT, a, b)

    digest_id = generate_digest(conn, make_config(tmp_path), logger)

    row = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row["status"] == "ok"
    assert json.loads(row["item_ids"]) == [a, b]
    md = row["markdown"]
    assert "degraded" not in md  # 全滅の注記は付かない
    assert md.count("- ⚠️ 本文を読めていない(見出しだけで分析)") == 2
    assert md.count("\n要約\n") == 2  # 要約はそのまま
    assert md.count("**推薦理由**: 理由") == 2  # 理由・示唆もそのまま


def test_extraction_note_only_on_failed_item(conn, tmp_path) -> None:
    # 本文を読めた通常の記事の項目は変わらない(印は抽出に失敗した記事だけ。GH#240)
    failed = add_analyzed(conn, "https://a/failed", relevance=90)
    normal = add_analyzed(conn, "https://a/normal", relevance=80)
    conn.execute("UPDATE analyses SET extraction_failed = 1 WHERE item_id = ?", (failed,))

    digest_id = generate_digest(conn, make_config(tmp_path), logger)

    row = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert json.loads(row["item_ids"]) == [failed, normal]
    first, second = row["markdown"].split("## 2. ")
    assert "- ⚠️ 本文を読めていない(見出しだけで分析)" in first
    assert "本文を読めていない" not in second
    assert "- カテゴリ: 課題直結\n\n要約\n" in second


def test_mixed_degraded_and_ok_is_normal_digest(conn, tmp_path) -> None:
    add_analyzed(conn, "https://a/1", relevance=0, degraded=1)
    ok = add_analyzed(conn, "https://a/2", relevance=90)
    digest_id = generate_digest(conn, make_config(tmp_path), logger)
    row = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row["status"] == "ok"
    assert json.loads(row["item_ids"]) == [ok]


def test_degraded_judgement_ignores_bookmark_items(conn, tmp_path, fixed_today) -> None:
    # 非 degraded のブックマークが残っていても「候補全滅」の判定を歪めない(GH#99)
    conn.execute(
        """INSERT INTO sources (type, name, config, enabled)
           VALUES ('x', 'x:bookmark', '{"mode": "bookmark"}', 0)"""
    )
    bm_source = conn.execute(
        "SELECT id FROM sources WHERE name = 'x:bookmark'"
    ).fetchone()["id"]
    bm = add_analyzed(conn, "https://a/bm", source_id=bm_source, relevance=90)  # 非 degraded
    conn.execute(
        "INSERT INTO x_signals (kind, tweet_id, item_id) VALUES ('bookmark', 't1', ?)",
        (bm,),
    )
    a = add_analyzed(conn, "https://a/1", relevance=0, degraded=1)
    set_analyzed_at(conn, TODAY_AT, bm, a)

    digest_id = generate_digest(conn, make_config(tmp_path), logger)
    row = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row["status"] == "degraded"  # 通常候補は全滅 → degraded 配信
    assert json.loads(row["item_ids"]) == [a]  # ブックマークは載らない


def test_degraded_judgement_uses_latest_analysis(conn, tmp_path, fixed_today) -> None:
    # 全滅の判定はランキングと同じく最新の分析で行う(候補の条件は 1 か所。GH#112)。
    # 古い分析も当日の時刻にする(当日に分析された候補だけを見る判定で外れないように。GH#175)
    item = add_analyzed(conn, "https://a/1", relevance=0, degraded=1)
    set_analyzed_at(conn, TODAY_START, item)
    conn.execute(
        "INSERT INTO analyses (item_id, summary, category, relevance_score, novelty, "
        "negative_labels, recommendation_reason, implication, degraded, engine, model, "
        "prompt_version, input_hash, analyzed_at) "
        "VALUES (?, '要約', '課題直結', 90, 'new', '[]', '理由', '示唆', 0, 'codex-cli', 'm', "
        "'analyze-v001', 'sha256:re', ?)",
        (item, TODAY_AT),
    )

    digest_id = generate_digest(conn, make_config(tmp_path), logger)
    row = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row["status"] == "ok"  # 古い degraded の分析で全滅と判定しない
    assert json.loads(row["item_ids"]) == [item]


def test_all_degraded_today_ignores_past_leftovers(conn, tmp_path, fixed_today) -> None:
    # 閾値に届かず analyzed のまま残った過去の記事(非 degraded も)があっても、当日の分析が
    # 全滅なら当日の分だけでタイトルのみの劣化配信にする(GH#175。設計 00 §6)
    past_ok = add_analyzed(conn, "https://a/past-ok", relevance=10)  # 非 degraded・選外
    past_degraded = add_analyzed(conn, "https://a/past-degraded", relevance=0, degraded=1)
    set_analyzed_at(conn, PAST, past_ok)
    set_analyzed_at(conn, JUST_BEFORE_TODAY, past_degraded)  # 当日の範囲の下限の外
    a = add_analyzed(conn, "https://a/1", relevance=0, degraded=1)
    b = add_analyzed(conn, "https://a/2", relevance=0, degraded=1)
    set_analyzed_at(conn, TODAY_START, a)  # 当日の範囲の下限ちょうど
    set_analyzed_at(conn, TODAY_AT, b)

    digest_id = generate_digest(conn, make_config(tmp_path), logger)
    row = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row["status"] == "degraded"
    assert json.loads(row["item_ids"]) == [a, b]  # 当日の分だけ(過去の記事は載せない)
    assert "推薦理由" not in row["markdown"]  # タイトルのみ
    # 過去の記事は候補(analyzed)のまま残す(通常のランキングの日の扱いを変えない)
    statuses = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM items")}
    assert statuses[past_ok] == statuses[past_degraded] == "analyzed"


def test_no_analysis_today_is_not_full_degradation(conn, tmp_path, fixed_today) -> None:
    # 当日に分析が無い朝は全滅ではない。過去に残った degraded の記事だけで劣化配信
    # (「LLM 分析が失敗した」の表示)にせず、通常のランキングに任せる(GH#175)
    past = add_analyzed(conn, "https://a/past", relevance=0, degraded=1)
    set_analyzed_at(conn, PAST, past)

    digest_id = generate_digest(conn, make_config(tmp_path), logger)
    row = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row["status"] == "ok"
    assert row["item_count"] == 0  # min_score 未満は載せない(通常のランキング)
    assert conn.execute("SELECT status FROM items").fetchone()["status"] == "analyzed"


def test_partial_degradation_today_ranks_past_candidates_too(conn, tmp_path, fixed_today) -> None:
    # 当日が全滅でなければ従来どおり、過去の候補も含めて通常のランキング(GH#175 で変えない)
    past = add_analyzed(conn, "https://a/past", relevance=90)
    set_analyzed_at(conn, PAST, past)
    degraded = add_analyzed(conn, "https://a/1", relevance=0, degraded=1)
    ok = add_analyzed(conn, "https://a/2", relevance=80)
    set_analyzed_at(conn, TODAY_AT, degraded, ok)

    digest_id = generate_digest(conn, make_config(tmp_path), logger)
    row = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row["status"] == "ok"
    assert json.loads(row["item_ids"]) == [past, ok]


def link_bookmark(conn, item_id: int, tweet_id: str, observed_at: str) -> None:
    conn.execute(
        "INSERT INTO x_signals (kind, tweet_id, item_id, observed_at) "
        "VALUES ('bookmark', ?, ?, ?)",
        (tweet_id, item_id, observed_at),
    )


def test_bookmark_section_rendered_and_marked_digested(conn, tmp_path) -> None:
    # 🔖 保存節(GH#99): ランキング外の別枠として全件掲載・保存順(新しい順)・digested 化
    normal = add_analyzed(conn, "https://a/normal", relevance=90)
    bm_old = add_analyzed(conn, "https://b/old", relevance=10, title_ja="古い保存")
    bm_new = add_analyzed(conn, "https://b/new", relevance=10, title_ja="新しい保存")
    link_bookmark(conn, bm_old, "t1", "2026-07-10T00:00:00Z")
    link_bookmark(conn, bm_new, "t2", "2026-07-18T00:00:00Z")

    digest_id = generate_digest(conn, make_config(tmp_path), logger)
    row = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert json.loads(row["item_ids"]) == [normal]  # ランキングには混ざらない
    assert json.loads(row["bookmark_item_ids"]) == [bm_new, bm_old]  # 保存順(新しい順)
    assert "## 🔖 保存(2件)" in row["markdown"]
    assert "[新しい保存](https://b/new)" in row["markdown"]
    statuses = {r["status"] for r in conn.execute("SELECT status FROM items")}
    assert statuses == {"digested"}  # 節の掲載で digested(翌日は載らない)


def test_bookmark_section_caps_display_but_records_all(conn, tmp_path) -> None:
    ids = []
    for i in range(12):
        item = add_analyzed(conn, f"https://b/{i}", relevance=10)
        link_bookmark(conn, item, f"t{i}", f"2026-07-{i + 1:02d}T00:00:00Z")
        ids.append(item)

    digest_id = generate_digest(conn, make_config(tmp_path), logger)
    row = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert len(json.loads(row["bookmark_item_ids"])) == 12  # 記録は全件
    assert "…他 2 件" in row["markdown"]  # 表示は上限 10 + 件数


def test_force_replace_reincludes_bookmark_items(conn, tmp_path) -> None:
    bm = add_analyzed(conn, "https://b/1", relevance=10, title_ja="保存")
    link_bookmark(conn, bm, "t1", "2026-07-18T00:00:00Z")
    config = make_config(tmp_path)
    first = generate_digest(conn, config, logger)
    assert first is not None

    replaced = generate_digest(conn, config, logger, force=True)
    row = conn.execute("SELECT * FROM digests WHERE id = ?", (replaced,)).fetchone()
    assert json.loads(row["bookmark_item_ids"]) == [bm]  # 置換後も節から消えない


def test_bookmark_on_already_digested_item_still_listed(conn, tmp_path) -> None:
    # 合流ケース(GH#99 レビュー P2): 配信済み(digested)の既存 item に後から
    # ブックマークが付いても、保存節に一度は必ず載る(status 基準だと恒久に落ちる)
    config = make_config(tmp_path)
    old = add_analyzed(conn, "https://a/old", relevance=90)
    first = generate_digest(conn, config, logger)
    assert first is not None
    assert conn.execute(
        "SELECT status FROM items WHERE id = ?", (old,)
    ).fetchone()["status"] == "digested"

    link_bookmark(conn, old, "t1", "2026-07-18T00:00:00Z")  # 配信後にブックマーク
    conn.execute("DELETE FROM digests")  # 翌日相当(digest_date UNIQUE を回避)
    second = generate_digest(conn, config, logger)
    row = conn.execute("SELECT * FROM digests WHERE id = ?", (second,)).fetchone()
    assert json.loads(row["bookmark_item_ids"]) == [old]  # 保存節に載る

    conn.execute("DELETE FROM digests WHERE id <> ?", (second,))
    conn.execute("UPDATE digests SET digest_date = '2000-01-01'")  # さらに翌日相当
    third = generate_digest(conn, config, logger)
    row = conn.execute("SELECT * FROM digests WHERE id = ?", (third,)).fetchone()
    assert json.loads(row["bookmark_item_ids"]) == []  # 台帳掲載済み → 再掲しない


def test_bookmark_markdown_link_escapes_structure(conn, tmp_path) -> None:
    # 外部由来 title の []・URL の () で保存節の Markdown 構造を壊さない(レビュー P2)
    bm = add_analyzed(
        conn, "https://b/a_(1)", relevance=10, title_ja="題 [注記] あり"
    )
    link_bookmark(conn, bm, "t1", "2026-07-18T00:00:00Z")
    digest_id = generate_digest(conn, make_config(tmp_path), logger)
    md = conn.execute(
        "SELECT markdown FROM digests WHERE id = ?", (digest_id,)
    ).fetchone()[0]
    assert "- [題 \\[注記\\] あり](https://b/a_%281%29)" in md
