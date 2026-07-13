import logging
import sqlite3
from pathlib import Path

import pytest

from app.analysis.llm import LLMError
from app.analysis.trend import TrendSummary, TrendTheme, synthesize_trend
from app.config import Config
from app.db import apply_migrations, connect

logger = logging.getLogger("test-trend")


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed-a')")  # id=2
    yield conn
    conn.close()


def add_analyzed(conn, url, *, topic, title_ja, degraded=0, relevance=80) -> int:
    item_id = conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, title, content_hash, status) "
        "VALUES (2, ?, ?, ?, 'sha256:x', 'analyzed')",
        (url, url, f"t {url}"),
    ).lastrowid
    conn.execute(
        "INSERT INTO analyses (item_id, title_ja, summary, category, topic, relevance_score, "
        "novelty, negative_labels, recommendation_reason, implication, degraded, engine, model, "
        "prompt_version, input_hash) "
        "VALUES (?, ?, '要約', '課題直結', ?, ?, 'new', '[]', '理由', '示唆', ?, 'codex-cli', "
        "'m', 'analyze-v001', ?)",
        (item_id, title_ja, topic, relevance, degraded, f"sha256:{url}"),
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


def cfg(tmp_path) -> Config:
    return Config(config_dir=tmp_path)


def seed_six(conn) -> list[int]:
    return [
        add_analyzed(conn, f"u{i}", topic=f"topic{i % 2}", title_ja=f"見出し{i}")
        for i in range(6)
    ]


def test_renders_grounded_themes(conn, tmp_path) -> None:
    ids = seed_six(conn)
    result = TrendSummary(
        themes=[
            TrendTheme(label="AI セキュリティ", summary="権限と秘匿が話題。", item_ids=ids[:2]),
            TrendTheme(label="エージェント設計", summary="設計論が複数。", item_ids=ids[2:4]),
        ]
    )
    md = synthesize_trend(conn, cfg(tmp_path), FakeEngine(result), logger)
    assert md is not None
    assert md.startswith("## 今日のトレンド")
    assert "**AI セキュリティ** — 権限と秘匿が話題。" in md
    assert "**エージェント設計** — 設計論が複数。" in md


def test_too_few_items_returns_none(conn, tmp_path) -> None:
    # MIN_ITEMS 未満(5件未満)ではトレンドをまとめない
    add_analyzed(conn, "only", topic="t", title_ja="1件だけ")
    result = TrendSummary(themes=[TrendTheme(label="x", summary="y", item_ids=[1])])
    assert synthesize_trend(conn, cfg(tmp_path), FakeEngine(result), logger) is None


def test_llm_failure_returns_none(conn, tmp_path) -> None:
    seed_six(conn)
    # 付加機能なので LLM 失敗は節省略(None)。例外は投げない
    md = synthesize_trend(conn, cfg(tmp_path), FakeEngine(LLMError("boom")), logger)
    assert md is None


def test_ungrounded_and_hallucinated_dropped(conn, tmp_path) -> None:
    ids = seed_six(conn)
    result = TrendSummary(
        themes=[
            TrendTheme(label="実在", summary="接地あり。", item_ids=[ids[0]]),
            TrendTheme(label="幻", summary="存在しない id のみ。", item_ids=[999999]),
        ]
    )
    md = synthesize_trend(conn, cfg(tmp_path), FakeEngine(result), logger)
    assert "**実在**" in md
    assert "**幻**" not in md  # 実在 id を含まないテーマは捨てる


def test_degraded_items_excluded(conn, tmp_path) -> None:
    # degraded 分析はトレンド対象から除外 → 有効5件未満なら None
    for i in range(6):
        add_analyzed(conn, f"d{i}", topic="t", title_ja=f"d{i}", degraded=1)
    result = TrendSummary(themes=[TrendTheme(label="x", summary="y", item_ids=[1])])
    assert synthesize_trend(conn, cfg(tmp_path), FakeEngine(result), logger) is None
