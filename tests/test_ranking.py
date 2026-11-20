import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import Config, DigestConfig, FocusConfig, RankingConfig
from app.db import apply_migrations, connect
from app.ranking import focus_pool, rank_for_digest

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


def make_config(
    tmp_path, *, max_items=10, score_bonus=15.0, bonus_items=5, **ranking_kwargs
) -> Config:
    return Config(
        config_dir=tmp_path,
        digest=DigestConfig(max_items=max_items),
        ranking=RankingConfig(**ranking_kwargs),
        focus=FocusConfig(enabled=True, score_bonus=score_bonus, bonus_items=bonus_items),
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



# --- 今の重点の点(GH#433 の 4 本目) ---


def test_focus_bonus_lifts_items_near_the_cutoff(conn, tmp_path) -> None:
    # 重点に当たる記事の点に 15 を足す。上位の少し下の記事だけが上がり、並びは足した後の点
    top = [add_analyzed(conn, f"https://b/{i}", relevance=90 - i, source_id=3) for i in range(3)]
    near = add_analyzed(conn, "https://a/near", relevance=80)  # 80 + 15 = 95
    far = add_analyzed(conn, "https://a/far", relevance=60)  # 60 + 15 = 75 は 88 に届かない

    config = make_config(tmp_path, max_items=3)
    selected = rank_for_digest(conn, config, focus_items={near: 7, far: 7})
    assert [item.item_id for item in selected] == [near, top[0], top[1]]
    assert [item.score for item in selected] == [95.0, 90.0, 89.0]
    assert [item.focus_topic_id for item in selected] == [7, None, None]
    assert score_of(conn, far) == 75  # total_score も足した点(選外も書く)

    # 重点が無ければ点の順のまま
    assert [i.item_id for i in rank_for_digest(conn, config)] == top
    assert score_of(conn, near) == 80


def test_focus_bonus_keeps_order_beyond_100_and_respects_min_score(conn, tmp_path) -> None:
    # 足して 100 を超えても、足す前の差で並べる(total_score は 100 で止める)。min_score は足す前の
    # 点で見る(30 + 15 = 45 でも、30 は 40 未満なので入れない)
    lower = add_analyzed(conn, "https://a/1", relevance=88)
    higher = add_analyzed(conn, "https://a/2", relevance=95)
    weak = add_analyzed(conn, "https://a/3", relevance=30)

    selected = rank_for_digest(
        conn, make_config(tmp_path), focus_items={lower: 1, higher: 1, weak: 1}
    )
    assert [item.item_id for item in selected] == [higher, lower]
    assert [item.score for item in selected] == [100.0, 100.0]
    assert score_of(conn, weak) == 30  # 足していない


def test_focus_bonus_goes_to_the_highest_bonus_items_only(conn, tmp_path) -> None:
    # 点を足すのは重点の記事のうち点の高い bonus_items 件まで。0 なら足さない
    focus = [add_analyzed(conn, f"https://a/{i}", relevance=70 - i) for i in range(3)]
    items = dict.fromkeys(focus, 1)
    rank_for_digest(conn, make_config(tmp_path, bonus_items=2), focus_items=items)
    assert [score_of(conn, i) for i in focus] == [85, 84, 68]
    rank_for_digest(conn, make_config(tmp_path, bonus_items=0), focus_items=items)
    assert [score_of(conn, i) for i in focus] == [70, 69, 68]
    rank_for_digest(conn, make_config(tmp_path, score_bonus=0.0), focus_items=items)
    assert [score_of(conn, i) for i in focus] == [70, 69, 68]


def test_focus_pool_asks_only_items_the_bonus_can_lift(conn, tmp_path) -> None:
    # 聞くのは、足す前の点が「点の順の最後の記事 − score_bonus」以上の記事(上位も含む)と、
    # --force で置き換える digest の記事。degraded は聞かない
    top = [add_analyzed(conn, f"https://b/{i}", relevance=90 - i, source_id=3) for i in range(2)]
    near = add_analyzed(conn, "https://a/near", relevance=76)  # 89 − 15 = 74 以上
    add_analyzed(conn, "https://a/far", relevance=73)
    add_analyzed(conn, "https://a/degraded", relevance=88, degraded=1)
    digested = add_analyzed(conn, "https://a/digested", relevance=80)
    conn.execute("UPDATE items SET status = 'digested' WHERE id = ?", (digested,))

    config = make_config(tmp_path, max_items=2)
    assert [row["item_id"] for row in focus_pool(conn, config)] == [*top, near]
    with_replaced = focus_pool(conn, config, include_item_ids=[digested])
    assert [row["item_id"] for row in with_replaced] == [*top, digested, near]

    # 上位が埋まらない日は min_score 以上をすべて聞く
    roomy = make_config(tmp_path, max_items=10)
    assert len(focus_pool(conn, roomy)) == 4  # 90・89・76・73(degraded と 40 未満なし)
