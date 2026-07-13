import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import Config, XSignalsConfig
from app.db import apply_migrations, connect
from app.insights import author_insights, render_insights, topic_insights

NOW = datetime(2026, 7, 14, 0, 0, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    yield conn
    conn.close()


def make_config(tmp_path: Path, **kwargs) -> Config:
    return Config(config_dir=tmp_path, x_signals=XSignalsConfig(**kwargs))


def add_signal(
    conn, kind, tweet_id, *, author_id=None, username=None, days_ago=0, excerpt=None
):
    conn.execute(
        "INSERT INTO x_signals (kind, tweet_id, author_id, author_username, text_excerpt, "
        "observed_at) VALUES (?, ?, ?, ?, ?, "
        "strftime('%Y-%m-%dT%H:%M:%SZ', '2026-07-14T00:00:00Z', '-' || ? || ' days'))",
        (kind, tweet_id, author_id, username, excerpt, days_ago),
    )


def test_author_scores_use_kind_weight_and_decay(conn, tmp_path) -> None:
    config = make_config(tmp_path)  # 🔖3.0 / ❤️1.0 / 半減期 14 日
    add_signal(conn, "bookmark", "b1", author_id="u1", username="alice")
    add_signal(conn, "like", "l1", author_id="u2", username="bob")
    add_signal(conn, "bookmark", "b2", author_id="u2", username="bob", days_ago=28)
    insights = author_insights(conn, config, now=NOW)
    scores = {i.author: i.score for i in insights}
    assert scores["@alice"] == pytest.approx(3.0)  # 当日 = 減衰なし
    # bob = ❤️今日(1.0)+ 🔖28日前(3.0 × 0.5^2 = 0.75)
    assert scores["@bob"] == pytest.approx(1.75)
    assert insights[0].author == "@alice"  # スコア降順
    assert insights[0].counts == {"bookmark": 1}
    assert insights[0].last_observed == "2026-07-14"


def test_author_without_id_excluded_and_id_fallback(conn, tmp_path) -> None:
    add_signal(conn, "self_rt", "s1")  # author 不明 → 著者集計から除外
    add_signal(conn, "bookmark", "b1", author_id="u9")  # username 無し → id 表示
    insights = author_insights(conn, make_config(tmp_path), now=NOW)
    assert [i.author for i in insights] == ["u9"]


def test_topic_insights_join_analyzed_items_only(conn, tmp_path) -> None:
    conn.execute("INSERT INTO sources (type, name) VALUES ('x', 'x:home')")  # id=2
    for tweet_id, topic, degraded in (("t1", "MCP", 0), ("t2", "agents", 1)):
        item_id = conn.execute(
            "INSERT INTO items (source_id, url, canonical_url, external_id, content_hash) "
            "VALUES (2, 'https://x.com/i/status/' || ?1, 'https://x.com/i/status/' || ?1, "
            "?1, 'sha256:' || ?1)",
            (tweet_id,),
        ).lastrowid
        conn.execute(
            "INSERT INTO analyses (item_id, summary, category, topic, relevance_score, "
            "novelty, negative_labels, recommendation_reason, implication, degraded, "
            "engine, model, prompt_version, input_hash) "
            "VALUES (?, 's', 'その他', ?, 50, 'new', '[]', 'r', 'i', ?, "
            "'codex-cli', 'm', 'v', 'h')",
            (item_id, topic, degraded),
        )
    add_signal(conn, "bookmark", "t1", author_id="u1")
    add_signal(conn, "bookmark", "t2", author_id="u1")  # degraded 分析 → 除外
    add_signal(conn, "like", "t9", author_id="u1")  # items に無い → 除外
    topics = topic_insights(conn, make_config(tmp_path), now=NOW)
    assert [(t.topic, t.signals) for t in topics] == [("MCP", 1)]
    assert topics[0].score == pytest.approx(3.0)


def test_render_empty_and_smoke(conn, tmp_path) -> None:
    config = make_config(tmp_path)
    assert "シグナルなし" in render_insights(conn, config, now=NOW)
    add_signal(
        conn, "bookmark", "b1", author_id="u1", username="alice", excerpt="MCP の認可設計"
    )
    add_signal(conn, "like", "l1", author_id="u1", username="alice", excerpt=None)
    text = render_insights(conn, config, now=NOW)
    assert "@alice" in text
    assert "MCP の認可設計" in text
    assert "TTL 切れ" in text  # excerpt NULL は明示
    assert "常連著者" in text and "頻出テーマ" in text
    assert "提案" not in text  # 読取専用(提案は S2 の領分)
