import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import Config, DigestConfig, RankingConfig
from app.db import apply_migrations, connect
from app.ranking import rank_for_digest

logger = logging.getLogger("test-ranking")


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed-a')")  # id=2
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed-b')")  # id=3
    yield conn
    conn.close()


def iso_hours_ago(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_analyzed(
    conn,
    url,
    *,
    source_id=2,
    relevance=80,
    novelty="new",
    negative="[]",
    topic=None,
    published_hours_ago=1.0,
    degraded=0,
    title_ja=None,
):
    item_id = conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, title, published_at, content_hash, "
        "status) VALUES (?, ?, ?, ?, ?, 'sha256:x', 'analyzed')",
        (source_id, url, url, f"title {url}", iso_hours_ago(published_hours_ago)),
    ).lastrowid
    conn.execute(
        "INSERT INTO analyses (item_id, title_ja, summary, category, topic, relevance_score, "
        "novelty, negative_labels, recommendation_reason, implication, degraded, engine, model, "
        "prompt_version, input_hash) "
        "VALUES (?, ?, '要約', '課題直結', ?, ?, ?, ?, '理由', '示唆', ?, 'codex-cli', 'm', "
        "'analyze-v001', ?)",
        (item_id, title_ja, topic, relevance, novelty, negative, degraded, f"sha256:{url}"),
    )
    return item_id


def make_config(tmp_path, *, max_items=10, **ranking_kwargs) -> Config:
    return Config(
        config_dir=tmp_path,
        digest=DigestConfig(max_items=max_items),
        ranking=RankingConfig(**ranking_kwargs),
    )


def score_of(conn, item_id) -> float:
    return conn.execute(
        "SELECT total_score FROM analyses WHERE item_id = ?", (item_id,)
    ).fetchone()[0]


def test_score_formula_components(conn, tmp_path) -> None:
    fresh_new = add_analyzed(conn, "https://a/1", relevance=80)  # 80×1.0
    known = add_analyzed(conn, "https://a/2", relevance=80, novelty="known")  # 80×0.3=24
    negative2 = add_analyzed(conn, "https://a/3", relevance=80, negative='["tips","hype"]')  # 50
    day_old = add_analyzed(conn, "https://a/4", relevance=80, published_hours_ago=30)  # 75
    two_days = add_analyzed(conn, "https://a/5", relevance=80, published_hours_ago=60)  # 70

    rank_for_digest(conn, make_config(tmp_path))
    assert score_of(conn, fresh_new) == 80
    assert score_of(conn, known) == 24
    assert score_of(conn, negative2) == 50
    assert score_of(conn, day_old) == 75
    assert score_of(conn, two_days) == 70


def test_score_clamped_to_zero(conn, tmp_path) -> None:
    item = add_analyzed(
        conn, "https://a/1", relevance=10, novelty="known",
        negative='["tips","hype","宣伝"]',
    )
    rank_for_digest(conn, make_config(tmp_path))
    assert score_of(conn, item) == 0


def test_topic_dup_penalizes_all_but_best(conn, tmp_path) -> None:
    best = add_analyzed(conn, "https://a/1", relevance=90, topic="LLM評価")
    second = add_analyzed(conn, "https://a/2", relevance=80, topic=" LLM評価 ")  # 前後空白は一致
    other = add_analyzed(conn, "https://a/3", relevance=80, topic="別トピック")

    rank_for_digest(conn, make_config(tmp_path))
    assert score_of(conn, best) == 90
    assert score_of(conn, second) == 60  # 80 − 20
    assert score_of(conn, other) == 80


def test_min_score_gate(conn, tmp_path) -> None:
    add_analyzed(conn, "https://a/1", relevance=39)
    high = add_analyzed(conn, "https://a/2", relevance=80)
    selected = rank_for_digest(conn, make_config(tmp_path))
    assert [item.item_id for item in selected] == [high]


def test_max_items_and_source_cap_with_promotion(conn, tmp_path) -> None:
    # feed-a から5件(全て高スコア)、feed-b から1件(低め)
    a_items = [add_analyzed(conn, f"https://a/{i}", relevance=90 - i) for i in range(5)]
    b_item = add_analyzed(conn, "https://b/1", source_id=3, relevance=50)

    selected = rank_for_digest(conn, make_config(tmp_path, max_items=5, max_per_source=4))
    ids = [item.item_id for item in selected]
    assert len(ids) == 5
    assert ids[:4] == a_items[:4]  # feed-a は上限4件
    assert ids[4] == b_item  # 次点繰上げで feed-b が入る
    assert a_items[4] not in ids


def test_selection_ignores_non_analyzed_items(conn, tmp_path) -> None:
    add_analyzed(conn, "https://a/1")
    conn.execute("UPDATE items SET status = 'digested'")
    assert rank_for_digest(conn, make_config(tmp_path)) == []


def test_bookmark_items_excluded_from_ranking(conn, tmp_path) -> None:
    # ブックマーク保存アイテムは must-include の別枠であり ranking 不参加(GH#99)
    conn.execute(
        """INSERT INTO sources (type, name, config, enabled)
           VALUES ('x', 'x:bookmark', '{"mode": "bookmark"}', 0)"""
    )
    bm_source = conn.execute(
        "SELECT id FROM sources WHERE name = 'x:bookmark'"
    ).fetchone()["id"]
    add_analyzed(conn, "https://a/normal", relevance=50)
    bm = add_analyzed(conn, "https://a/bookmark", source_id=bm_source, relevance=99)
    # 合流ケース: RSS 由来 item にブックマークが紐付いた場合も除外(GH#99 レビュー指摘)
    merged = add_analyzed(conn, "https://a/merged-rss", relevance=95)
    for tweet_id, item_id in (("t1", bm), ("t2", merged)):
        conn.execute(
            "INSERT INTO x_signals (kind, tweet_id, item_id) VALUES ('bookmark', ?, ?)",
            (tweet_id, item_id),
        )

    selected = rank_for_digest(conn, make_config(tmp_path))
    assert [item.canonical_url for item in selected] == ["https://a/normal"]
