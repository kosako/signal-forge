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
    assert row["dig_execution_pct"] == 50.0  # 分母は dig_total(未回答は未実施扱い)
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


def add_sent_digest(conn, digest_date: str, item_ids: list[int]) -> None:
    conn.execute(
        "INSERT INTO digests (digest_date, markdown, item_ids, item_count, delivery_status) "
        "VALUES (?, '# md', ?, ?, 'sent')",
        (digest_date, json.dumps(item_ids), len(item_ids)),
    )


def add_delivered_item(conn, url: str) -> int:
    item_id = conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, title, content_hash, status) "
        "VALUES (2, ?, ?, 't', 'h', 'digested')",
        (url, url),
    ).lastrowid
    conn.execute(
        "INSERT INTO analyses (item_id, summary, category, relevance_score, novelty, "
        "recommendation_reason, implication, engine, model, prompt_version, input_hash) "
        "VALUES (?, 's', '課題直結', 80, 'new', 'r', 'i', 'codex-cli', 'm', 'v1', ?)",
        (item_id, url),
    )
    return item_id


def test_zero_reaction_delivered_day_is_invalid(conn) -> None:
    # 反応ゼロの配信日が集計から消えない(fail-closed。Codex must 2/3 の回帰)
    item = add_delivered_item(conn, "https://a/quiet")
    add_sent_digest(conn, "2026-07-01", [item])

    rows = conn.execute((KPI_DIR / "01-daily-kpi.sql").read_text(encoding="utf-8")).fetchall()
    quiet = next(r for r in rows if r["digest_date"] == "2026-07-01")
    assert quiet["reacted"] == 0
    assert quiet["invalid_day"] == 1


def test_invalid_streak_detects_three_zero_reaction_days(conn) -> None:
    for n, day in enumerate(("2026-07-01", "2026-07-02", "2026-07-03")):
        item = add_delivered_item(conn, f"https://a/quiet{n}")
        add_sent_digest(conn, day, [item])

    rows = conn.execute(
        (KPI_DIR / "07-invalid-streak.sql").read_text(encoding="utf-8")
    ).fetchall()
    assert [r["third_day_of_streak"] for r in rows] == ["2026-07-03"]


def test_source_rates_include_unreacted_delivered_items(conn) -> None:
    # 無反応の配信 item も分母に入る(Codex must 4 の回帰)
    item = add_delivered_item(conn, "https://a/unreacted")
    add_sent_digest(conn, "2026-07-01", [item])

    row = conn.execute(
        (KPI_DIR / "04-source-category-rates.sql").read_text(encoding="utf-8")
    ).fetchone()
    assert row["delivered"] == 5  # seed 4件 + 無反応1件
    assert row["reacted"] == 4
    assert row["positive"] == 3
    assert row["positive_pct"] == 60.0  # 3/5(反応済みのみ分母なら 75%)


def test_phase2_not_ready_with_unanswered_digs(conn) -> None:
    # 🔖 8件+回答1件(yes)では進出条件を満たさない(Codex must 1 の回帰)
    for n in range(7):  # seed に dig 2件あり → 追加7件で dig_total 9
        item = add_delivered_item(conn, f"https://a/dig{n}")
        conn.execute(
            "INSERT INTO signals (kind, item_id, value, signal_date) "
            "VALUES ('reaction', ?, 'dig', '2026-07-01')",
            (item,),
        )
    row = conn.execute(
        (KPI_DIR / "06-phase2-boundary.sql").read_text(encoding="utf-8")
    ).fetchone()
    assert row["dig_total"] == 9
    assert row["followup_yes"] == 1
    assert row["dig_execution_pct"] == round(100.0 / 9, 1)  # 分母は dig_total
    assert row["phase2_ready"] == 0

    # 過半の 🔖 に yes が付けば進出条件を満たす
    for _n in range(4):
        item = conn.execute(
            "SELECT item_id FROM signals WHERE kind='reaction' AND value='dig' "
            "AND item_id NOT IN (SELECT item_id FROM signals WHERE kind='followup_dig') "
            "LIMIT 1"
        ).fetchone()["item_id"]
        conn.execute(
            "INSERT INTO signals (kind, item_id, value, signal_date) "
            "VALUES ('followup_dig', ?, 'yes', '2026-07-02')",
            (item,),
        )
    row = conn.execute(
        (KPI_DIR / "06-phase2-boundary.sql").read_text(encoding="utf-8")
    ).fetchone()
    assert row["followup_yes"] == 5
    assert row["phase2_ready"] == 1  # 9件中5件 yes(>=50%)


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
