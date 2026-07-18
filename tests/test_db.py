import sqlite3
from pathlib import Path

import pytest

from app.db import MIGRATIONS_DIR, MigrationError, apply_migrations, connect, discover_migrations


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    yield conn
    conn.close()


def test_connect_sets_pragmas(tmp_path: Path) -> None:
    conn = connect(tmp_path / "intel.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL


def test_apply_migrations_records_ledger_and_seeds_manual(conn: sqlite3.Connection) -> None:
    ledger = conn.execute("SELECT version, filename FROM schema_migrations").fetchall()
    assert [(row["version"], row["filename"]) for row in ledger] == [
        (1, "001_init.sql"),
        (2, "002_analyses_title_ja.sql"),
        (3, "003_digests_trend_markdown.sql"),
        (4, "004_x_signals.sql"),
        (5, "005_item_notes.sql"),
        (6, "006_digest_notes.sql"),
        (7, "007_x_signals_item_id.sql"),
    ]
    manual = conn.execute("SELECT type, name, enabled FROM sources").fetchone()
    assert (manual["type"], manual["name"], manual["enabled"]) == ("manual", "manual", 1)


def test_apply_migrations_is_idempotent(conn: sqlite3.Connection, tmp_path: Path) -> None:
    assert apply_migrations(conn, tmp_path / "intel.db") == []
    assert conn.execute("SELECT count(*) FROM sources").fetchone()[0] == 1


def test_backup_created_before_apply(tmp_path: Path) -> None:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    assert (tmp_path / "intel.db.bak").exists()


def test_backup_is_consistent_snapshot_of_pre_apply_state(tmp_path: Path) -> None:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, content_hash) "
        "VALUES (1, 'https://a', 'https://a', 'sha256:x')"
    )
    other = tmp_path / "migrations"
    other.mkdir()
    applied = _copy_real_migrations(other)
    (other / f"{applied + 1:03d}_extra.sql").write_text(
        "CREATE TABLE extra (id INTEGER PRIMARY KEY) STRICT;"
    )
    apply_migrations(conn, db_path, other)

    bak = sqlite3.connect(tmp_path / "intel.db.bak")
    assert bak.execute("SELECT count(*) FROM items").fetchone()[0] == 1  # WAL 内容を含む
    tables = {row[0] for row in bak.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "extra" not in tables  # 適用「前」のスナップショット
    bak.close()


def test_strict_rejects_wrong_type(conn: sqlite3.Connection) -> None:
    # STRICT の型強制(数値変換できない TEXT は INTEGER 列に入らない)
    with pytest.raises(sqlite3.IntegrityError, match="cannot store TEXT"):
        conn.execute(
            "INSERT INTO items (source_id, url, canonical_url, content_hash) "
            "VALUES ('abc', 'https://a', 'https://a', 'sha256:x')"
        )


def test_unique_canonical_url_absorbs_duplicates(conn: sqlite3.Connection) -> None:
    insert = (
        "INSERT OR IGNORE INTO items (source_id, url, canonical_url, content_hash) "
        "VALUES (1, 'https://a', 'https://a', 'sha256:x')"
    )
    assert conn.execute(insert).rowcount == 1
    assert conn.execute(insert).rowcount == 0  # 冪等性契約(設計 02 §1)


def test_signals_reaction_requires_item_id(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO signals (kind, item_id, value, signal_date) "
            "VALUES ('reaction', NULL, 'useful', '2026-07-06')"
        )


def test_digest_messages_header_is_unique_per_digest(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO digests (digest_date, markdown, item_ids, item_count) "
        "VALUES ('2026-07-06', '# d', '[]', 0)"
    )
    conn.execute(
        "INSERT INTO digest_messages (digest_id, item_id, message_id) VALUES (1, NULL, 10)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO digest_messages (digest_id, item_id, message_id) VALUES (1, NULL, 11)"
        )


def test_documents_cascade_on_item_delete(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, content_hash) "
        "VALUES (1, 'https://a', 'https://a', 'sha256:x')"
    )
    conn.execute(
        "INSERT INTO documents (item_id, extraction_status, expires_at) "
        "VALUES (1, 'ok', '2026-08-01T00:00:00Z')"
    )
    conn.execute("DELETE FROM items WHERE id = 1")
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0


def test_discover_rejects_gap(tmp_path: Path) -> None:
    (tmp_path / "001_a.sql").write_text("SELECT 1;")
    (tmp_path / "003_b.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="連番"):
        discover_migrations(tmp_path)


def test_discover_rejects_bad_name(tmp_path: Path) -> None:
    (tmp_path / "1_a.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="NNN_name"):
        discover_migrations(tmp_path)


def test_renamed_applied_file_fails(tmp_path: Path) -> None:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    other = tmp_path / "migrations"
    other.mkdir()
    (other / "001_renamed.sql").write_text((MIGRATIONS_DIR / "001_init.sql").read_text())
    with pytest.raises(MigrationError, match="編集・改名は禁止"):
        apply_migrations(conn, db_path, other)


def test_failed_migration_rolls_back(tmp_path: Path) -> None:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    other = tmp_path / "migrations"
    other.mkdir()
    applied = _copy_real_migrations(other)
    bad_name = f"{applied + 1:03d}_bad.sql"
    (other / bad_name).write_text(
        "CREATE TABLE will_rollback (id INTEGER PRIMARY KEY) STRICT;\nINVALID SQL;"
    )
    with pytest.raises(MigrationError, match=bad_name):
        apply_migrations(conn, db_path, other)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "will_rollback" not in tables
    assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == applied


def _copy_real_migrations(other: Path) -> int:
    """実マイグレーション一式を複製し、適用済み件数(= 既存の最大版番号)を返す。"""
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    for f in files:
        (other / f.name).write_text(f.read_text())
    return len(files)
