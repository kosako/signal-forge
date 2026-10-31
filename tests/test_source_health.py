"""週 1 回のソースの健康診断(GH#150 / 設計 02 §1〜3)。"""

import json
import sqlite3
from pathlib import Path

import pytest

from app.config import Config, XConfig
from app.db import apply_migrations, connect
from app.source_health import source_health_report

TODAY = "2026-10-25"  # 日曜(JST)
OLD = "2026-01-01T00:00:00Z"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    yield conn
    conn.close()


def config(tmp_path: Path, *, x_enabled: bool = True) -> Config:
    return Config(config_dir=tmp_path, x=XConfig(enabled=x_enabled))


def add_source(
    conn, name, *, type_="rss", enabled=1, failures=0, error=None, moved=None, created=OLD
) -> int:
    return conn.execute(
        "INSERT INTO sources (type, name, enabled, consecutive_failures, last_error, moved_url, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (type_, name, enabled, failures, error, moved, created),
    ).lastrowid


def add_item(conn, source_id: int, url: str, fetched_at: str) -> int:
    return conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, content_hash, fetched_at) "
        "VALUES (?, ?, ?, 'sha256:x', ?)",
        (source_id, url, url, fetched_at),
    ).lastrowid


def test_report_lists_failing_moved_and_quiet_sources(conn, tmp_path) -> None:
    failing = add_source(conn, "feed-fail", failures=2, error="HTTP 500:\nhttps://a/feed")
    add_item(conn, failing, "https://a/1", "2026-10-24T00:00:00Z")
    moved = add_source(conn, "feed-moved", moved="https://new.example/feed")
    add_item(conn, moved, "https://m/1", "2026-10-24T00:00:00Z")
    quiet = add_source(conn, "feed-quiet")
    add_item(conn, quiet, "https://q/1", "2026-10-05T00:00:00Z")  # 20 日前が最後
    ok = add_source(conn, "feed-ok")
    add_item(conn, ok, "https://o/1", "2026-10-24T00:00:00Z")
    add_source(conn, "feed-new", created="2026-10-22T00:00:00Z")  # 登録して間もない
    add_source(conn, "feed-off", enabled=0, failures=9, moved="https://x/y")  # 人が止めた
    x_home = add_source(conn, "x:home", type_="x", failures=4, error="HTTP 429")
    add_item(conn, x_home, "https://x.com/1", "2026-10-24T00:00:00Z")

    report = source_health_report(conn, config(tmp_path), TODAY)

    assert report.startswith("🩺 ソースの健康診断")
    assert "⚠️ 「feed-fail」が 2 回連続で失敗(直近: HTTP 500: https://a/feed)" in report
    assert "⚠️ 「x:home」が 4 回連続で失敗(直近: HTTP 429)" in report
    assert "↪️ 「feed-moved」のフィードが移転している(移転先: https://new.example/feed)" in report
    assert "💤 「feed-quiet」に 14 日間、新しい記事が入っていない" in report
    for name in ("feed-ok", "feed-new", "feed-off", "manual"):
        assert f"「{name}」" not in report
    assert "✅" not in report


def test_report_skips_x_while_x_is_stopped(conn, tmp_path) -> None:
    # X を止めている間は回数も新着も進まないので、止める前の状態で知らせ続けない
    add_source(conn, "x:home", type_="x", failures=4, error="HTTP 429")
    report = source_health_report(conn, config(tmp_path, x_enabled=False), TODAY)
    assert "x:home" not in report


def test_report_is_sent_even_when_all_healthy(conn, tmp_path) -> None:
    ok = add_source(conn, "feed-ok")
    add_item(conn, ok, "https://o/1", "2026-10-24T00:00:00Z")
    report = source_health_report(conn, config(tmp_path), TODAY)
    assert "✅ 失敗・移転・新着なしのソースはありません" in report
    assert "(対象のソースなし)" in report
    assert "runbook §12" in report


def deliver(conn, digest_date: str, item_ids: list[int]) -> None:
    conn.execute(
        "INSERT INTO digests (digest_date, status, markdown, item_ids, item_count, "
        "delivery_status) VALUES (?, 'ok', 'md', ?, ?, 'sent')",
        (digest_date, json.dumps(item_ids), len(item_ids)),
    )


def react(conn, item_id: int, value: str, signal_date: str) -> None:
    conn.execute(
        "INSERT INTO signals (kind, item_id, value, signal_date) VALUES ('reaction', ?, ?, ?)",
        (item_id, value, signal_date),
    )


def test_rates_list_lowest_sources_with_enough_deliveries(conn, tmp_path) -> None:
    low = add_source(conn, "low")
    high = add_source(conn, "high")
    few = add_source(conn, "few")
    old = add_source(conn, "old")
    items: dict[str, list[int]] = {}
    for name, source_id, count in (("low", low, 10), ("high", high, 10), ("few", few, 9),
                                   ("old", old, 10)):
        items[name] = [
            add_item(conn, source_id, f"https://{name}/{n}", "2026-10-20T00:00:00Z")
            for n in range(count)
        ]
    deliver(conn, "2026-10-20", items["low"] + items["high"] + items["few"])
    deliver(conn, "2026-09-20", items["old"])  # 4 週より前の配信は数えない
    react(conn, items["low"][0], "useful", "2026-10-20")
    react(conn, items["low"][1], "useful", "2026-10-20")
    react(conn, items["low"][1], "known", "2026-10-21")  # 最新の反応で数える(訂正)
    for item_id in items["high"][:5]:
        react(conn, item_id, "dig", "2026-10-20")
    react(conn, items["few"][0], "noise", "2026-10-20")

    report = source_health_report(conn, config(tmp_path), TODAY)

    low_line = "・「low」10%(👍・🔖 1 / 配信 10)"
    high_line = "・「high」50%(👍・🔖 5 / 配信 10)"
    assert low_line in report and high_line in report
    assert report.index(low_line) < report.index(high_line)  # 低い順
    assert "「few」" not in report  # 配信が少ないと率がぶれるので出さない
    assert "「old」" not in report


def test_rates_skip_manual_and_stopped_x(conn, tmp_path) -> None:
    # 手動の投入と、止めている X は見直しの対象でないので、率の枠を使わない
    x_home = add_source(conn, "x:home", type_="x")
    feed = add_source(conn, "feed")
    manual = conn.execute("SELECT id FROM sources WHERE type = 'manual'").fetchone()[0]
    ids = []
    for source_id, name in ((x_home, "x"), (feed, "feed"), (manual, "manual")):
        ids += [
            add_item(conn, source_id, f"https://{name}/{n}", "2026-10-20T00:00:00Z")
            for n in range(10)
        ]
    deliver(conn, "2026-10-20", ids)

    stopped = source_health_report(conn, config(tmp_path, x_enabled=False), TODAY)
    assert "「feed」0%" in stopped
    assert "「x:home」" not in stopped and "「manual」" not in stopped
    running = source_health_report(conn, config(tmp_path), TODAY)
    assert "「x:home」0%" in running and "「manual」" not in running


def test_new_source_failing_or_moved_is_reported(conn, tmp_path) -> None:
    # 登録して間もないソースも、失敗・移転はすぐ知らせる(除くのは「新着なし」だけ)
    add_source(conn, "fresh-fail", failures=1, error="HTTP 404", created="2026-10-24T00:00:00Z")
    add_source(conn, "fresh-moved", moved="https://n/feed", created="2026-10-24T00:00:00Z")
    report = source_health_report(conn, config(tmp_path), TODAY)
    assert "「fresh-fail」が 1 回連続で失敗" in report
    assert "「fresh-moved」のフィードが移転している" in report
    assert "💤" not in report


@pytest.mark.parametrize(
    ("last_fetched", "quiet"),
    [("2026-10-10T15:00:00Z", False), ("2026-10-10T14:59:59Z", True)],
    ids=["at-boundary", "just-before"],
)
def test_quiet_boundary_is_jst_midnight_14_days_ago(conn, tmp_path, last_fetched, quiet) -> None:
    # 2026-10-25(JST)の 14 日前の 0 時 = 2026-10-11 00:00 JST = 2026-10-10T15:00:00Z(以後は新着)
    source_id = add_source(conn, "feed")
    add_item(conn, source_id, "https://f/1", last_fetched)
    report = source_health_report(conn, config(tmp_path), TODAY)
    assert ("💤 「feed」" in report) is quiet


def add_cut(conn, kind: str, created_at: str, deferred: int, source_id=None) -> None:
    conn.execute(
        "INSERT INTO budget_cuts (created_at, kind, source_id, deferred) VALUES (?, ?, ?, ?)",
        (created_at, kind, source_id, deferred),
    )


def test_report_lists_budget_cut_days_of_last_7_days(conn, tmp_path) -> None:
    # 段の上限で打ち切った日(GH#340)。2026-10-25(JST)を含む直近 7 日 = 2026-10-19 00:00 JST
    # (2026-10-18T15:00:00Z)から。日は JST の暦日で数える
    add_cut(conn, "collect_time", "2026-10-18T14:59:59Z", 9)  # 期間の前(数えない)
    add_cut(conn, "collect_time", "2026-10-18T15:00:00Z", 3)  # JST 10-19
    add_cut(conn, "collect_time", "2026-10-20T22:00:00Z", 5)  # JST 10-21
    add_cut(conn, "extract_time", "2026-10-24T21:00:00Z", 40)  # JST 10-25
    big = add_source(conn, "feed-big")
    add_cut(conn, "rss_items", "2026-10-23T20:00:00Z", 12, big)  # JST 10-24 の朝
    add_cut(conn, "rss_items", "2026-10-24T01:00:00Z", 30, big)  # 同じ JST 10-24
    off = add_source(conn, "feed-off", enabled=0)  # 人が止めたソースは知らせない
    add_cut(conn, "rss_items", "2026-10-24T01:00:00Z", 7, off)

    report = source_health_report(conn, config(tmp_path), TODAY)

    assert (
        "⏱️ 収集の段が時間の上限(600 秒)で打ち切られた(直近 7 日のうち 2 日。"
        "最も多い回は 5 ソースを次の実行へ回した)"
    ) in report
    assert (
        "⏱️ 本文抽出の段が時間の上限(600 秒)で打ち切られた(直近 7 日のうち 1 日。"
        "最も多い回は 40 件を次の実行へ回した)"
    ) in report
    assert (
        "📦 「feed-big」が 1 回の上限(100 件)を超えた(直近 7 日のうち 1 日。"
        "最も多い回は 30 件を次の実行へ回した)"
    ) in report
    assert "feed-off" not in report
    assert "✅" not in report


def test_report_without_budget_cuts_has_no_cut_lines(conn, tmp_path) -> None:
    add_cut(conn, "extract_time", "2026-10-18T14:59:59Z", 9)  # 先週のぶんは載せない
    report = source_health_report(conn, config(tmp_path), TODAY)
    assert "⏱️" not in report and "📦" not in report


@pytest.mark.parametrize(
    ("digest_date", "counted"), [("2026-09-28", True), ("2026-09-27", False)]
)
def test_rate_window_is_last_28_days(conn, tmp_path, digest_date, counted) -> None:
    # 2026-10-25 の 28 日前(2026-09-27)の配信は数えず、翌日からは数える
    source_id = add_source(conn, "feed")
    ids = [add_item(conn, source_id, f"https://f/{n}", "2026-09-27T00:00:00Z") for n in range(10)]
    deliver(conn, digest_date, ids)
    report = source_health_report(conn, config(tmp_path), TODAY)
    assert ("「feed」0%" in report) is counted
