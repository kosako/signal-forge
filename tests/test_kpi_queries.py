"""scripts/kpi/*.sql の実行検証(スキーマとの整合が壊れたら CI で気付く)。"""

import json
import sqlite3
from pathlib import Path

import pytest

from app.db import apply_migrations, connect

KPI_DIR = Path(__file__).resolve().parent.parent / "scripts" / "kpi"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    seed(conn)
    yield conn
    conn.close()


def seed(conn: sqlite3.Connection) -> None:
    """全クエリが最低1行のデータに触れる最小セット。"""
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed-a')")  # id=2
    items = []
    for n in range(4):
        items.append(
            conn.execute(
                "INSERT INTO items (source_id, url, canonical_url, title, content_hash, "
                "status) VALUES (2, ?, ?, ?, 'h', 'digested')",
                (f"https://a/{n}", f"https://a/{n}", f"title {n}"),
            ).lastrowid
        )
        conn.execute(
            "INSERT INTO analyses (item_id, summary, category, relevance_score, novelty, "
            "recommendation_reason, implication, engine, model, prompt_version, input_hash) "
            "VALUES (?, 's', '課題直結', 80, 'new', 'r', 'i', 'codex-cli', 'm', 'v1', ?)",
            (items[-1], f"h{n}"),
        )
    conn.execute(
        "INSERT INTO digests (digest_date, markdown, item_ids, item_count, delivery_status) "
        "VALUES (date('now'), '# md', ?, 4, 'sent')",
        (json.dumps(items),),
    )
    reactions = [("useful", items[0]), ("dig", items[1]), ("dig", items[2]), ("noise", items[3])]
    for value, item_id in reactions:
        conn.execute(
            "INSERT INTO signals (kind, item_id, value, signal_date) "
            "VALUES ('reaction', ?, ?, date('now'))",
            (item_id, value),
        )
    conn.execute(
        "INSERT INTO signals (kind, value, signal_date) "
        "VALUES ('survey_patrol', 'no', date('now'))"
    )
    conn.execute(
        "INSERT INTO signals (kind, value, signal_date) "
        "VALUES ('survey_read_min', '15', date('now'))"
    )
    conn.execute(
        "INSERT INTO signals (kind, item_id, value, signal_date) "
        "VALUES ('followup_dig', ?, 'yes', date('now'))",
        (items[1],),
    )


@pytest.mark.parametrize("sql_path", sorted(KPI_DIR.glob("*.sql")), ids=lambda p: p.name)
def test_query_executes(conn, sql_path: Path) -> None:
    rows = conn.execute(sql_path.read_text(encoding="utf-8")).fetchall()
    assert isinstance(rows, list)  # 構文・スキーマ整合の検証(結果の意味は下の個別テスト)


def test_daily_kpi_values(conn) -> None:
    row = conn.execute((KPI_DIR / "01-daily-kpi.sql").read_text(encoding="utf-8")).fetchone()
    assert row["positive"] == 3  # useful 1 + dig 2
    assert row["reacted"] == 4
    assert row["delivered"] == 4
    assert row["kpi_met"] == 1
    assert row["invalid_day"] == 0  # 反応率 100%


def test_patrol_replacement_values(conn) -> None:
    row = conn.execute(
        (KPI_DIR / "02-patrol-replacement.sql").read_text(encoding="utf-8")
    ).fetchone()
    assert row["patrolled"] == "no"
    assert row["replaced_day"] == 1


def test_phase2_boundary_values(conn) -> None:
    row = conn.execute(
        (KPI_DIR / "06-phase2-boundary.sql").read_text(encoding="utf-8")
    ).fetchone()
    assert row["dig_total"] == 2
    assert row["followup_answered"] == 1
    assert row["followup_yes"] == 1
    assert row["dig_execution_pct"] == 100.0
    assert row["phase2_ready"] == 0  # 累計8件未満


def test_weekly_summary_values(conn) -> None:
    row = conn.execute(
        (KPI_DIR / "08-weekly-summary.sql").read_text(encoding="utf-8")
    ).fetchone()
    assert row["delivered_days"] == 1
    assert row["reacted_days"] == 1
    assert row["kpi_met_days"] == 1
    assert row["invalid_days"] == 0
    assert row["week_fail_closed"] == 0


def test_retap_uses_latest_reaction(conn) -> None:
    # items[3](noise)を useful に押し直すと positive が増える(最新採用の検証)
    item = conn.execute(
        "SELECT item_id FROM signals WHERE value = 'noise'"
    ).fetchone()["item_id"]
    conn.execute(
        "INSERT INTO signals (kind, item_id, value, signal_date) "
        "VALUES ('reaction', ?, 'useful', date('now'))",
        (item,),
    )
    row = conn.execute((KPI_DIR / "01-daily-kpi.sql").read_text(encoding="utf-8")).fetchone()
    assert row["positive"] == 4
    assert row["reacted"] == 4  # 行は増えない(item×日の最新のみ)
