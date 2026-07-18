import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.analysis.llm import LLMError
from app.analysis.trend import TrendSummary, TrendTheme, synthesize_trend
from app.db import apply_migrations, connect

logger = logging.getLogger("test-trend")


def _now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed-a')")  # id=2
    yield conn
    conn.close()


def add_analyzed(
    conn, url, *, topic, title_ja, degraded=0, relevance=80, status="analyzed", analyzed_at=None
) -> int:
    item_id = conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, title, content_hash, status) "
        "VALUES (2, ?, ?, ?, 'sha256:x', ?)",
        (url, url, f"t {url}", status),
    ).lastrowid
    conn.execute(
        "INSERT INTO analyses (item_id, title_ja, summary, category, topic, relevance_score, "
        "novelty, negative_labels, recommendation_reason, implication, degraded, engine, model, "
        "prompt_version, input_hash, analyzed_at) "
        "VALUES (?, ?, '要約', '課題直結', ?, ?, 'new', '[]', '理由', '示唆', ?, 'codex-cli', "
        "'m', 'analyze-v001', ?, ?)",
        (item_id, title_ja, topic, relevance, degraded, f"sha256:{url}", analyzed_at or _now_utc()),
    )
    return item_id


class FakeEngine:
    name = "fake"

    def __init__(self, result_or_exc):
        self._r = result_or_exc

    def complete_structured(self, prompt, schema, timeout_s=120):
        if isinstance(self._r, Exception):
            raise self._r
        return self._r


def seed_six(conn) -> list[int]:
    return [
        add_analyzed(conn, f"u{i}", topic=f"topic{i % 2}", title_ja=f"見出し{i}")
        for i in range(6)
    ]


def test_renders_grounded_themes_plaintext(conn) -> None:
    ids = seed_six(conn)
    result = TrendSummary(
        themes=[
            TrendTheme(label="AI セキュリティ", summary="権限と秘匿が話題。", item_ids=ids[:2]),
            TrendTheme(label="エージェント設計", summary="設計論が複数。", item_ids=ids[2:4]),
        ]
    )
    md = synthesize_trend(conn, FakeEngine(result), logger)
    assert md is not None
    assert md.startswith("今日のトレンド")  # プレーンテキスト(markup なし)
    assert "・AI セキュリティ — 権限と秘匿が話題。" in md
    assert "・エージェント設計 — 設計論が複数。" in md
    assert "**" not in md and "##" not in md  # markup を含まない(consumer 側で整形)


def test_summary_truncated_to_bound(conn) -> None:
    ids = seed_six(conn)
    long_summary = "あ" * 300
    result = TrendSummary(
        themes=[TrendTheme(label="長文", summary=long_summary, item_ids=[ids[0]])]
    )
    md = synthesize_trend(conn, FakeEngine(result), logger)
    assert "…" in md  # 切り詰められた
    assert len(md) < 300  # ヘッダ 1 通が肥大しない


def test_prompt_compliant_summary_not_truncated(conn) -> None:
    # prompt(v002)の指示上限 160 字を守った要約は文中で切らない(GH#78)。
    # MAX_SUMMARY はあくまで指示無視時の安全網
    ids = seed_six(conn)
    compliant_summary = "い" * 159 + "。"
    result = TrendSummary(
        themes=[TrendTheme(label="遵守", summary=compliant_summary, item_ids=[ids[0]])]
    )
    md = synthesize_trend(conn, FakeEngine(result), logger)
    assert compliant_summary in md  # 全文がそのまま載る
    assert "…" not in md


def test_too_few_items_returns_none(conn) -> None:
    add_analyzed(conn, "only", topic="t", title_ja="1件だけ")
    result = TrendSummary(themes=[TrendTheme(label="x", summary="y", item_ids=[1])])
    assert synthesize_trend(conn, FakeEngine(result), logger) is None


def test_llm_failure_returns_none(conn) -> None:
    seed_six(conn)
    md = synthesize_trend(conn, FakeEngine(LLMError("boom")), logger)
    assert md is None  # 付加機能なので節省略・例外は投げない


def test_ungrounded_hallucinated_and_empty_dropped(conn) -> None:
    ids = seed_six(conn)
    result = TrendSummary(
        themes=[
            TrendTheme(label="実在", summary="接地あり。", item_ids=[ids[0]]),
            TrendTheme(label="幻", summary="存在しない id のみ。", item_ids=[999999]),
            TrendTheme(label="", summary="ラベル空。", item_ids=[ids[1]]),
            TrendTheme(label="要約空", summary="   ", item_ids=[ids[2]]),
        ]
    )
    md = synthesize_trend(conn, FakeEngine(result), logger)
    assert "・実在 — 接地あり。" in md
    assert "幻" not in md  # 実在 id を含まないテーマは捨てる
    assert "ラベル空" not in md
    assert "要約空" not in md


def test_degraded_items_excluded(conn) -> None:
    for i in range(6):
        add_analyzed(conn, f"d{i}", topic="t", title_ja=f"d{i}", degraded=1)
    result = TrendSummary(themes=[TrendTheme(label="x", summary="y", item_ids=[1])])
    assert synthesize_trend(conn, FakeEngine(result), logger) is None


def test_digested_items_included(conn) -> None:
    # --force 再生成時、当日 digest 済み(status='digested')の分析も拾う(status で絞らない)
    ids = [
        add_analyzed(conn, f"g{i}", topic="t", title_ja=f"g{i}", status="digested")
        for i in range(6)
    ]
    themes = [TrendTheme(label="採用", summary="digested 済み。", item_ids=ids)]
    md = synthesize_trend(conn, FakeEngine(TrendSummary(themes=themes)), logger)
    assert md is not None and "・採用 — digested 済み。" in md


def test_prior_day_analyses_excluded(conn) -> None:
    # 前日(JST)以前に分析されたものは当日トレンドに混ざらない(analyzed_at の下限)
    for i in range(6):
        add_analyzed(
            conn, f"old{i}", topic="t", title_ja=f"old{i}", analyzed_at="2020-01-01T00:00:00Z"
        )
    result = TrendSummary(themes=[TrendTheme(label="x", summary="y", item_ids=[1])])
    assert synthesize_trend(conn, FakeEngine(result), logger) is None


def test_trend_candidates_exclude_bookmark_items(conn) -> None:
    # ブックマークは過去の任意時点の内容のため「今日のトレンド」に混ぜない(GH#99)
    from app.analysis.trend import _fetch_today_items

    conn.execute(
        """INSERT INTO sources (type, name, config, enabled)
           VALUES ('x', 'x:bookmark', '{"mode": "bookmark"}', 0)"""
    )
    bm_source = conn.execute(
        "SELECT id FROM sources WHERE name = 'x:bookmark'"
    ).fetchone()["id"]
    add_analyzed(conn, "https://a/normal", topic="t1", title_ja="通常")
    bm_item = conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, content_hash, status) "
        "VALUES (?, 'https://a/bm', 'https://a/bm', 'sha256:b', 'analyzed')",
        (bm_source,),
    ).lastrowid
    conn.execute(
        "INSERT INTO analyses (item_id, title_ja, summary, category, topic, relevance_score, "
        "novelty, negative_labels, recommendation_reason, implication, degraded, engine, model, "
        "prompt_version, input_hash) "
        "VALUES (?, 'BM', '要約', '課題直結', 't1', 90, 'new', '[]', '理由', '示唆', 0, "
        "'codex-cli', 'm', 'analyze-v001', 'sha256:bm')",
        (bm_item,),
    )
    ids = [row["item_id"] for row in _fetch_today_items(conn)]
    assert bm_item not in ids
