import ast
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
    assert analysis.extraction_failed == 0  # migration 021 の既定(GH#240)


def test_extraction_failed_note_is_read_from_common_layer() -> None:
    # 本文を読めていない印(GH#240)は digest・Telegram・Items ノートの 3 つの段が使う。特定の段の
    # module に置かず共通の層(models.py)から読む(設計 00 §3)。Items ノートの書き出しが文言のため
    # だけに digest と ranking を読み込む依存を作らない
    app_dir = Path(__file__).resolve().parent.parent / "app"
    for rel in ("digest/builder.py", "delivery/telegram.py", "export/obsidian.py"):
        tree = ast.parse((app_dir / rel).read_text(encoding="utf-8"))
        sources = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and any(alias.name == "EXTRACTION_FAILED_NOTE" for alias in node.names)
        }
        assert sources == {"app.models"}, rel
    obsidian = ast.parse((app_dir / "export/obsidian.py").read_text(encoding="utf-8"))
    imported = {node.module for node in ast.walk(obsidian) if isinstance(node, ast.ImportFrom)}
    assert not {m for m in imported if m and m.startswith(("app.digest", "app.ranking"))}
