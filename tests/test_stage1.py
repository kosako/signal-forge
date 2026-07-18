import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.analysis.stage1 import preselect
from app.config import AnalysisConfig, Config
from app.db import apply_migrations, connect

logger = logging.getLogger("test-stage1")


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed-a')")  # id=2
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed-b')")  # id=3
    conn.execute("INSERT INTO sources (type, name) VALUES ('x', 'home-tl')")  # id=4
    yield conn
    conn.close()


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def add_item(conn, url, *, source_id=2, title="十分な長さのタイトル", published=None, payload=None):
    return conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, title, published_at, content_hash, "
        "raw_payload) VALUES (?, ?, ?, ?, ?, 'sha256:x', ?)",
        (source_id, url, url, title, published, json.dumps(payload) if payload else None),
    ).lastrowid


def make_config(tmp_path, **analysis_kwargs) -> Config:
    return Config(config_dir=tmp_path, analysis=AnalysisConfig(**analysis_kwargs))


def skip_reasons(conn) -> dict[str, str | None]:
    return {
        r["canonical_url"]: (r["status"], r["skip_reason"])
        for r in conn.execute("SELECT canonical_url, status, skip_reason FROM items")
    }


def test_blocklist_by_domain_and_keyword(conn, tmp_path) -> None:
    now = iso(datetime.now(UTC))
    add_item(conn, "https://spam.example/a", published=now)
    add_item(conn, "https://sub.spam.example/b", published=now)
    add_item(conn, "https://ok.example/c", title="宣伝: 限定セール", published=now)
    add_item(conn, "https://ok.example/d", published=now)

    config = make_config(tmp_path, block_domains=("spam.example",), block_keywords=("宣伝",))
    result = preselect(conn, config, logger)
    counts = result.counts
    assert counts["blocklist"] == 3
    assert counts["selected"] == 1
    assert len(result.selected_ids) == 1  # 採用集合は戻り値が正本
    rows = skip_reasons(conn)
    assert rows["https://spam.example/a"] == ("skipped", "blocklist")
    assert rows["https://sub.spam.example/b"] == ("skipped", "blocklist")
    assert rows["https://ok.example/c"] == ("skipped", "blocklist")
    assert rows["https://ok.example/d"] == ("new", None)


def test_stale_rule_spares_manual_and_null_published(conn, tmp_path) -> None:
    old = iso(datetime.now(UTC) - timedelta(hours=100))
    add_item(conn, "https://a.example/old", published=old)
    add_item(conn, "https://a.example/manual-old", source_id=1, published=old)  # manual
    add_item(conn, "https://a.example/unknown", published=None)

    counts = preselect(conn, make_config(tmp_path), logger).counts
    assert counts["stale"] == 1
    rows = skip_reasons(conn)
    assert rows["https://a.example/old"] == ("skipped", "stale")
    assert rows["https://a.example/manual-old"] == ("new", None)
    assert rows["https://a.example/unknown"] == ("new", None)


def test_x_noise_rules(conn, tmp_path) -> None:
    now = iso(datetime.now(UTC))
    add_item(
        conn, "https://x.com/i/web/status/1", source_id=4, title="返信",
        published=now, payload={"meta": {"is_reply": True}},
    )
    add_item(conn, "https://x.com/i/web/status/2", source_id=4, title="短い", published=now)
    add_item(
        conn, "https://x.com/i/web/status/3", source_id=4, title="短くてもリンクあり http://a",
        published=now,
    )
    add_item(
        conn, "https://x.com/i/web/status/4", source_id=4,
        title="二十文字の閾値を確実に超える十分に長い本文を持つ通常のポストです", published=now,
    )
    add_item(conn, "https://rss.example/short", title="短い", published=now)  # rss は対象外

    counts = preselect(conn, make_config(tmp_path), logger).counts
    assert counts["x_noise"] == 2
    rows = skip_reasons(conn)
    assert rows["https://x.com/i/web/status/1"] == ("skipped", "x_noise")
    assert rows["https://x.com/i/web/status/2"] == ("skipped", "x_noise")
    assert rows["https://x.com/i/web/status/3"] == ("new", None)
    assert rows["https://x.com/i/web/status/4"] == ("new", None)
    assert rows["https://rss.example/short"] == ("new", None)


def test_daily_cap_round_robin_keeps_source_diversity(conn, tmp_path) -> None:
    base = datetime.now(UTC)
    for i in range(6):
        published = iso(base - timedelta(hours=i))
        add_item(conn, f"https://a.example/{i}", source_id=2, published=published)
    for i in range(2):
        published = iso(base - timedelta(hours=i))
        add_item(conn, f"https://b.example/{i}", source_id=3, published=published)

    result = preselect(conn, make_config(tmp_path, daily_llm_limit=4), logger)
    assert result.counts["selected"] == 4
    assert result.counts["daily_cap"] == 4
    survivors = {
        r["canonical_url"]
        for r in conn.execute("SELECT canonical_url FROM items WHERE status='new'")
    }
    # ラウンドロビンで両ソースから2件ずつ(published_at 降順)
    assert survivors == {
        "https://a.example/0", "https://a.example/1",
        "https://b.example/0", "https://b.example/1",
    }
    assert len(result.selected_ids) == 4


def test_daily_cap_limit_one_takes_globally_freshest(conn, tmp_path) -> None:
    base = datetime.now(UTC)
    add_item(conn, "https://a.example/older", source_id=2, published=iso(base - timedelta(hours=5)))
    add_item(conn, "https://b.example/newest", source_id=3, published=iso(base))

    result = preselect(conn, make_config(tmp_path, daily_llm_limit=1), logger)
    survivor = conn.execute("SELECT canonical_url FROM items WHERE status='new'").fetchone()
    # ソースの登録順でなく published_at 降順が優先される(境界件数でも降順維持)
    assert survivor["canonical_url"] == "https://b.example/newest"
    assert result.counts["selected"] == 1


def test_failed_extraction_items_not_excluded(conn, tmp_path) -> None:
    now = iso(datetime.now(UTC))
    item_id = add_item(conn, "https://a.example/failed", published=now)
    conn.execute(
        "INSERT INTO documents (item_id, extraction_status, expires_at) "
        "VALUES (?, 'failed', '2026-08-01T00:00:00Z')",
        (item_id,),
    )
    result = preselect(conn, make_config(tmp_path), logger)
    assert result.counts["selected"] == 1  # degraded 分析に回る(除外しない)
    assert result.selected_ids == [item_id]


def test_bookmark_items_bypass_stale_noise_and_cap(conn, tmp_path) -> None:
    # ブックマーク保存アイテムは本人意思: stale / x_noise / daily_cap を適用しない(GH#99)
    conn.execute(
        """INSERT INTO sources (type, name, config, enabled)
           VALUES ('x', 'x:bookmark', '{"mode": "bookmark"}', 0)"""
    )
    bm_source = conn.execute(
        "SELECT id FROM sources WHERE name = 'x:bookmark'"
    ).fetchone()["id"]
    old = iso(datetime.now(UTC) - timedelta(hours=200))  # 72h 超(通常は stale)
    bm_stale = add_item(conn, "https://x.com/i/web/status/1", source_id=bm_source,
                        title=None, published=old, payload={"raw_content": "短い"})
    bm_block = add_item(conn, "https://spam.example/x", source_id=bm_source,
                        title=None, published=old)
    fresh = add_item(conn, "https://ok.example/a", published=iso(datetime.now(UTC)))

    config = make_config(tmp_path, block_domains=("spam.example",), daily_llm_limit=1)
    result = preselect(conn, config, logger)

    # cap(1件)は通常ソースにのみ効き、ブックマークは cap 外で必ず採用
    assert bm_stale in result.selected_ids
    assert fresh in result.selected_ids
    # blocklist(明示 NG)はブックマークにも適用
    assert skip_reasons(conn)["https://spam.example/x"] == ("skipped", "blocklist")
    assert bm_block not in result.selected_ids
