import sqlite3
from pathlib import Path

import pytest

from app.db import apply_migrations, connect
from app.models import Analysis, Item, Source


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    yield conn
    conn.close()


def test_source_from_row(conn: sqlite3.Connection) -> None:
    source = Source.from_row(conn.execute("SELECT * FROM sources WHERE name='manual'").fetchone())
    assert source.type == "manual"
    assert source.enabled == 1
    assert source.cursor is None
    assert source.consecutive_failures == 0


def test_source_from_row_without_later_column(conn: sqlite3.Connection) -> None:
    # 後の migration の列(既定値あり)が無い行は既定値で読む。既定値の無い列は必須のまま
    row = conn.execute(
        "SELECT id, type, name, config, cursor, enabled, last_success_at, last_error_at, "
        "last_error, consecutive_failures, created_at FROM sources WHERE name='manual'"
    ).fetchone()
    assert Source.from_row(row).moved_url is None
    missing_required = conn.execute("SELECT id, type FROM sources WHERE name='manual'").fetchone()
    with pytest.raises(IndexError):
        Source.from_row(missing_required)


def test_item_from_row_round_trip(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, title, content_hash, raw_payload) "
        "VALUES (1, 'https://A', 'https://a', 'T', 'sha256:x', '{\"meta\": {}}')"
    )
    item = Item.from_row(conn.execute("SELECT * FROM items").fetchone())
    assert item.canonical_url == "https://a"
    assert item.status == "new"
    assert item.dup_of_item_id is None
    assert "T" in str(item.fetched_at) or item.fetched_at.endswith("Z")  # UTC ISO 8601


def test_analysis_from_row_covers_all_columns(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, content_hash) "
        "VALUES (1, 'https://a', 'https://a', 'sha256:x')"
    )
    conn.execute(
        "INSERT INTO analyses (item_id, summary, category, relevance_score, novelty, "
        "recommendation_reason, implication, engine, model, prompt_version, input_hash) "
        "VALUES (1, '要約', 'AI', 85.0, 'new', '理由', '示唆', 'codex-cli', 'gpt-5.5', "
        "'analyze-v001', 'sha256:in')"
    )
    analysis = Analysis.from_row(conn.execute("SELECT * FROM analyses").fetchone())
    assert analysis.relevance_score == 85.0
    assert analysis.negative_labels == "[]"
    assert analysis.total_score is None
    assert analysis.degraded == 0
    assert analysis.estimated_cost_usd == 0
