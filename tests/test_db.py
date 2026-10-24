import contextlib
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.db import MIGRATIONS_DIR, MigrationError, apply_migrations, connect, discover_migrations

_HANG_GUARD_SECONDS = 30  # 並行のテストが止まったときだけ効く上限(正否の判定には使わない)


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
        (8, "008_digests_bookmark_item_ids.sql"),
        (9, "009_dig_queue.sql"),
        (10, "010_dig_results.sql"),
        (11, "011_dig_questions.sql"),
        (12, "012_dig_questions_unique_message.sql"),
        (13, "013_dig_questions_take_recorded.sql"),
        (14, "014_dig_questions_card_requests.sql"),
        (15, "015_dig_questions_redig.sql"),
        (16, "016_sources_moved_url.sql"),
        (17, "017_dig_results_review.sql"),
        (18, "018_llm_fallback_calls.sql"),
        (19, "019_notes_received_message.sql"),
        (20, "020_dig_questions_retry.sql"),
        (21, "021_analyses_extraction_failed.sql"),
        (23, "023_x_signal_runs_backfill.sql"),
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


def test_concurrent_apply_runs_each_migration_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # GH#200: 2 つの起動が重なっても、同じ migration を 2 回流さずに両方が成功する。
    # 先の方を「未適用を確定した後・適用の前」(.bak の取得)で止め、その間に後の方を走らせる。
    # 後の方が排他を待ちに入る(修正前は台帳を読んで .bak まで進む)のを合図に先の方を進める。
    # 正否は時間に頼らない(上限は止まったときに suite を止めないためだけのもの)
    from app import db

    db_path = tmp_path / "intel.db"
    connect(db_path).close()
    first_paused = threading.Event()
    second_arrived = threading.Event()
    resume_first = threading.Event()
    backups: list[str] = []
    results: dict[str, object] = {}
    real_backup, real_lock = db._backup, db._migration_lock

    def paused_backup(conn: sqlite3.Connection, path: Path) -> None:
        name = threading.current_thread().name
        backups.append(name)
        if name == "first":
            first_paused.set()
            assert resume_first.wait(_HANG_GUARD_SECONDS)
        else:
            second_arrived.set()
        real_backup(conn, path)

    @contextlib.contextmanager
    def observed_lock(path: Path) -> Iterator[None]:
        if threading.current_thread().name == "second":
            second_arrived.set()
        with real_lock(path):
            yield

    monkeypatch.setattr(db, "_backup", paused_backup)
    monkeypatch.setattr(db, "_migration_lock", observed_lock)

    def run() -> None:
        name = threading.current_thread().name
        conn = connect(db_path)
        try:
            results[name] = apply_migrations(conn, db_path)
        except Exception as e:  # スレッドの例外を本体の assert で見せる
            results[name] = e
        finally:
            conn.close()

    first = threading.Thread(target=run, name="first", daemon=True)
    second = threading.Thread(target=run, name="second", daemon=True)
    try:
        first.start()
        assert first_paused.wait(_HANG_GUARD_SECONDS)
        second.start()
        assert second_arrived.wait(_HANG_GUARD_SECONDS)
    finally:
        # 途中の assert で抜けても先の方を止めたままにせず、起動したスレッドの終わりを待つ
        resume_first.set()
        for thread in (first, second):
            if thread.ident is not None:  # 起動前のスレッドは join できない
                thread.join(_HANG_GUARD_SECONDS)
    assert not first.is_alive() and not second.is_alive()

    all_files = [m.filename for m in discover_migrations(MIGRATIONS_DIR)]
    assert results == {"first": all_files, "second": []}  # 後の方は台帳を読み直して何もしない
    assert backups == ["first"]  # .bak は先の方の適用前の 1 回だけ(後の方が上書きしない)
    conn = connect(db_path)
    ledger = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    versions = [row[0] for row in ledger]
    assert versions == list(range(1, len(all_files) + 1))
    conn.close()


def _copy_real_migrations(other: Path) -> int:
    """実マイグレーション一式を複製し、適用済み件数(= 既存の最大版番号)を返す。"""
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    for f in files:
        (other / f.name).write_text(f.read_text())
    return len(files)


def test_card_requests_migration_keeps_questions_and_enforces_rules(tmp_path: Path) -> None:
    # GH#141(migration 014): 表を作り直しても既存の質問は残り、ボタンの依頼の決まりが効く
    before = tmp_path / "before"
    before.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(path.name[:3]) <= 13:
            (before / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    db = tmp_path / "intel.db"
    conn = connect(db)
    apply_migrations(conn, db, before)
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed')")
    conn.execute(
        "INSERT INTO items (id, source_id, url, canonical_url, content_hash) "
        "VALUES (1, 2, 'https://a/1', 'https://a/1', 'sha256:x')"
    )
    conn.execute(
        "INSERT INTO dig_results (id, item_id, note_name, card_text, message_id) "
        "VALUES (1, 1, 'n', 'カード', 700)"
    )
    conn.execute(
        "INSERT INTO dig_questions (dig_result_id, question, question_message_id, status, kind, "
        "answer, answer_message_id, take_recorded_at) "
        "VALUES (1, '質問', 81, 'answered', 'take', '返事', 82, '2026-10-19T00:00:00Z')"
    )

    apply_migrations(conn, db)

    row = conn.execute("SELECT * FROM dig_questions").fetchone()
    assert (row["question"], row["question_message_id"], row["request"], row["kind"]) == (
        "質問", 81, None, "take"
    )
    assert row["take_recorded_at"] == "2026-10-19T00:00:00Z"
    conn.execute(
        "INSERT INTO dig_questions (dig_result_id, question, request) VALUES (1, '要約', 'summary')"
    )
    with pytest.raises(sqlite3.IntegrityError):  # 同じカードの同じボタンは 1 行だけ
        conn.execute(
            "INSERT INTO dig_questions (dig_result_id, question, request) "
            "VALUES (1, '要約', 'summary')"
        )
    with pytest.raises(sqlite3.IntegrityError):  # 本人のリプライでもボタンでもない行は作れない
        conn.execute("INSERT INTO dig_questions (dig_result_id, question) VALUES (1, '?')")
    with pytest.raises(sqlite3.IntegrityError):  # 再配達の二重受付は従来どおり防ぐ
        conn.execute(
            "INSERT INTO dig_questions (dig_result_id, question, question_message_id) "
            "VALUES (1, '質問', 81)"
        )
    conn.close()



def test_x_signal_runs_backfill_migration_defaults_existing_runs_to_daily(tmp_path: Path) -> None:
    # GH#178(migration 023): 既存の取込の行は通常の取込(backfill = 0)になり、0 / 1 以外は入らない
    before = tmp_path / "before"
    before.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(path.name[:3]) <= 22:
            (before / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    db = tmp_path / "intel.db"
    conn = connect(db)
    apply_migrations(conn, db, before)
    conn.execute("INSERT INTO x_signal_runs (status, api_reads) VALUES ('ok', 7)")

    apply_migrations(conn, db)

    assert conn.execute("SELECT backfill FROM x_signal_runs").fetchone()["backfill"] == 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE x_signal_runs SET backfill = 2")
    conn.close()


def test_redig_migration_defaults_existing_questions_to_zero(tmp_path: Path) -> None:
    # GH#137(migration 015): 既存の質問は「調べ直しの依頼でない」になり、0 / 1 以外は入らない
    before = tmp_path / "before"
    before.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(path.name[:3]) <= 14:
            (before / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    db = tmp_path / "intel.db"
    conn = connect(db)
    apply_migrations(conn, db, before)
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed')")
    conn.execute(
        "INSERT INTO items (id, source_id, url, canonical_url, content_hash) "
        "VALUES (1, 2, 'https://a/1', 'https://a/1', 'sha256:x')"
    )
    conn.execute(
        "INSERT INTO dig_results (id, item_id, note_name, card_text, message_id) "
        "VALUES (1, 1, 'n', 'カード', 700)"
    )
    conn.execute(
        "INSERT INTO dig_questions (dig_result_id, question, question_message_id) "
        "VALUES (1, '質問', 81)"
    )

    apply_migrations(conn, db)

    assert conn.execute("SELECT redig FROM dig_questions").fetchone()["redig"] == 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE dig_questions SET redig = 2")
    conn.close()
