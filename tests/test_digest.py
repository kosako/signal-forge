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


def test_no_qualified_items_produces_empty_ok_digest(conn, tmp_path) -> None:
    add_analyzed(conn, "https://a/1", relevance=10)  # min_score 未満
    digest_id = generate_digest(conn, make_config(tmp_path), logger)
    row = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row["status"] == "ok"
    assert row["item_count"] == 0
    assert json.loads(row["item_ids"]) == []
    # min_score 未満の item は digested に遷移しない(analyzed のまま)
    assert conn.execute("SELECT status FROM items").fetchone()["status"] == "analyzed"


def test_all_degraded_produces_title_only_digest(conn, tmp_path) -> None:
    a = add_analyzed(conn, "https://a/1", relevance=0, degraded=1)
    b = add_analyzed(conn, "https://a/2", relevance=0, degraded=1)
    digest_id = generate_digest(conn, make_config(tmp_path), logger)
    row = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row["status"] == "degraded"
    assert json.loads(row["item_ids"]) == [a, b]
    assert "degraded" in row["markdown"]
    assert "推薦理由" not in row["markdown"]  # タイトルのみ


def test_mixed_degraded_and_ok_is_normal_digest(conn, tmp_path) -> None:
    add_analyzed(conn, "https://a/1", relevance=0, degraded=1)
    ok = add_analyzed(conn, "https://a/2", relevance=90)
    digest_id = generate_digest(conn, make_config(tmp_path), logger)
    row = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row["status"] == "ok"
    assert json.loads(row["item_ids"]) == [ok]
