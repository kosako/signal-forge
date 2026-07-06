import sqlite3
from pathlib import Path

import pytest

from app.db import apply_migrations, connect, insert_item_with_dedup


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    yield conn
    conn.close()


def insert(conn: sqlite3.Connection, **overrides):
    params = {
        "source_id": 1,
        "external_id": None,
        "url": "https://example.com/a",
        "canonical_url": "https://example.com/a",
        "title": "T",
        "author": None,
        "published_at": None,
        "raw_payload": None,
        "content_hash": "sha256:aaaa",
        "dedup_eligible": True,
    }
    params.update(overrides)
    return insert_item_with_dedup(conn, **params)


def test_first_insert_succeeds(conn: sqlite3.Connection) -> None:
    outcome = insert(conn)
    assert outcome.outcome == "inserted"
    assert outcome.item_id == 1


def test_same_canonical_url_is_ignored(conn: sqlite3.Connection) -> None:
    insert(conn)
    outcome = insert(conn, content_hash="sha256:bbbb")
    assert outcome.outcome == "ignored"
    assert outcome.item_id is None
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1


def test_same_source_external_id_is_ignored(conn: sqlite3.Connection) -> None:
    insert(conn, external_id="guid-1")
    outcome = insert(
        conn, external_id="guid-1", url="https://example.com/b", canonical_url="https://example.com/b"
    )
    assert outcome.outcome == "ignored"


def test_same_content_within_24h_becomes_duplicate(conn: sqlite3.Connection) -> None:
    insert(conn)
    outcome = insert(conn, url="https://mirror.example/a", canonical_url="https://mirror.example/a")
    assert outcome.outcome == "duplicate"
    row = conn.execute(
        "SELECT status, dup_of_item_id FROM items WHERE id = ?", (outcome.item_id,)
    ).fetchone()
    assert (row["status"], row["dup_of_item_id"]) == ("duplicate", 1)


def test_same_content_older_than_24h_not_duplicate(conn: sqlite3.Connection) -> None:
    insert(conn)
    conn.execute("UPDATE items SET fetched_at = '2026-01-01T00:00:00Z' WHERE id = 1")
    outcome = insert(conn, url="https://mirror.example/a", canonical_url="https://mirror.example/a")
    assert outcome.outcome == "inserted"


def test_ineligible_content_never_duplicated(conn: sqlite3.Connection) -> None:
    # manual 相当: 空テキストの content_hash が同じでも duplicate 化しない(設計 01 §5〜6)
    insert(conn, content_hash="sha256:empty", dedup_eligible=False)
    outcome = insert(
        conn,
        url="https://other.example/b",
        canonical_url="https://other.example/b",
        content_hash="sha256:empty",
        dedup_eligible=False,
    )
    assert outcome.outcome == "inserted"


def test_duplicate_parent_is_not_itself_a_duplicate(conn: sqlite3.Connection) -> None:
    insert(conn)  # id=1 親
    insert(conn, url="https://m1.example/a", canonical_url="https://m1.example/a")  # id=2 dup→1
    outcome = insert(conn, url="https://m2.example/a", canonical_url="https://m2.example/a")
    row = conn.execute(
        "SELECT dup_of_item_id FROM items WHERE id = ?", (outcome.item_id,)
    ).fetchone()
    assert row["dup_of_item_id"] == 1  # duplicate 行(id=2)は親にならない


def test_reinsert_after_dedup_is_idempotent(conn: sqlite3.Connection) -> None:
    insert(conn)
    insert(conn, url="https://mirror.example/a", canonical_url="https://mirror.example/a")
    outcome = insert(conn, url="https://mirror.example/a", canonical_url="https://mirror.example/a")
    assert outcome.outcome == "ignored"
    assert conn.execute("SELECT count(*) FROM items WHERE status='duplicate'").fetchone()[0] == 1
