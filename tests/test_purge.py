import logging
import sqlite3
from pathlib import Path

import pytest

from app.config import Config
from app.db import apply_migrations, connect
from app.purge import purge_expired

logger = logging.getLogger("test-purge")


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed-a')")  # id=2
    conn.execute("INSERT INTO sources (type, name) VALUES ('x', 'home-tl')")  # id=3
    yield conn
    conn.close()


def add_item(conn, url, *, source_id, fetched_days_ago, payload='{"raw_content": "x"}'):
    return conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, content_hash, raw_payload, fetched_at) "
        "VALUES (?, ?, ?, 'sha256:x', ?, strftime('%Y-%m-%dT%H:%M:%SZ','now', ?))",
        (source_id, url, url, payload, f"-{fetched_days_ago} days"),
    ).lastrowid


def add_document(conn, item_id, *, expires_days: int):
    sign = "+" if expires_days >= 0 else ""
    conn.execute(
        "INSERT INTO documents (item_id, extraction_status, expires_at) "
        "VALUES (?, 'ok', strftime('%Y-%m-%dT%H:%M:%SZ','now', ?))",
        (item_id, f"{sign}{expires_days} days"),
    )


def test_expired_documents_deleted(conn, tmp_path) -> None:
    live = add_item(conn, "https://a/live", source_id=2, fetched_days_ago=1)
    dead = add_item(conn, "https://a/dead", source_id=2, fetched_days_ago=40)
    add_document(conn, live, expires_days=10)
    add_document(conn, dead, expires_days=-1)

    counts = purge_expired(conn, Config(config_dir=tmp_path), logger)
    assert counts["documents_deleted"] == 1
    remaining = [r["item_id"] for r in conn.execute("SELECT item_id FROM documents")]
    assert remaining == [live]


def test_raw_payload_nullified_by_source_ttl(conn, tmp_path) -> None:
    rss_fresh = add_item(conn, "https://a/1", source_id=2, fetched_days_ago=10)  # 30日未満
    rss_old = add_item(conn, "https://a/2", source_id=2, fetched_days_ago=31)
    x_fresh = add_item(conn, "https://x/1", source_id=3, fetched_days_ago=6)  # 7日未満
    x_old = add_item(conn, "https://x/2", source_id=3, fetched_days_ago=8)
    # documents を持たない・status を問わない行も対象(duplicate)
    dup_old = add_item(conn, "https://a/3", source_id=2, fetched_days_ago=31)
    conn.execute("UPDATE items SET status='duplicate' WHERE id = ?", (dup_old,))

    counts = purge_expired(conn, Config(config_dir=tmp_path), logger)
    assert counts["raw_payload_nullified"] == 3
    payloads = {
        r["id"]: r["raw_payload"] for r in conn.execute("SELECT id, raw_payload FROM items")
    }
    assert payloads[rss_fresh] is not None
    assert payloads[x_fresh] is not None
    assert payloads[rss_old] is None
    assert payloads[x_old] is None
    assert payloads[dup_old] is None


def test_vacuum_triggered_by_nullified_rows(conn, tmp_path, monkeypatch) -> None:
    import app.purge as purge_module

    monkeypatch.setattr(purge_module, "VACUUM_THRESHOLD_ROWS", 2)
    for i in range(3):
        add_item(conn, f"https://a/{i}", source_id=2, fetched_days_ago=31)

    class SpyConn:
        def __init__(self, inner):
            self._inner = inner
            self.sqls: list[str] = []

        def execute(self, sql, *args):
            self.sqls.append(sql)
            return self._inner.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    spy = SpyConn(conn)
    counts = purge_expired(spy, Config(config_dir=tmp_path), logger)
    assert counts["raw_payload_nullified"] == 3
    assert any(sql.strip() == "VACUUM" for sql in spy.sqls)  # NULL 化のみでも発動


def test_purge_is_idempotent(conn, tmp_path) -> None:
    item = add_item(conn, "https://a/1", source_id=2, fetched_days_ago=31)
    add_document(conn, item, expires_days=-1)
    purge_expired(conn, Config(config_dir=tmp_path), logger)
    counts = purge_expired(conn, Config(config_dir=tmp_path), logger)
    assert counts == {"documents_deleted": 0, "raw_payload_nullified": 0}
