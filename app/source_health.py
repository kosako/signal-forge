"""情報ソースの健康(設計 02 §1〜3: 機械が見つけ、人が判断する。GH#124 / GH#150)。

- source_failure_alerts: 連続で失敗しているソース(doctor #14・watchdog・週次の健康診断が使う)
- source_health_report: 週 1 回の健康診断の本文(poller の内蔵タイマーが日曜の夜に送る)。
  週次レビュー(plan §6)の「ソースの棚卸し」を、人が思い出さなくても始められるようにする

読み取りだけ。ソースを止めたり config の URL を書き換えたりはしない(判断は人。設計 02 §3)。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from app.config import Config
from app.jst import JST

# consecutive_failures がこの回数に達したら人に知らせる(設計 02 §2・§3)。
# X = doctor #14 と watchdog / RSS = doctor #14 のみ(一時障害が多く約1週間で「死亡疑い」)
X_FAILURE_ALERT = 3
RSS_FAILURE_ALERT = 7
ALERT_ERROR_MAX_CHARS = 200  # 知らせる文に載せる直近エラーの上限(URL 込みで1〜2行)

REPORT_WEEKDAY = 6  # 健康診断を送る曜日(datetime.weekday() の日曜)。週次レビューの週末に合わせる
QUIET_DAYS = 14  # これだけ新しい記事が入らないソースを知らせる(週刊のフィードも 2 回分は待つ)
RATE_DAYS = 28  # 👍・🔖 の率を見る配信の期間(直近 4 週)
RATE_MIN_DELIVERED = 10  # 率を出す最低の配信件数(少ないと率がぶれる)
RATE_SHOW = 3  # 率の低い方から並べるソースの数


def source_failure_alerts(
    conn: sqlite3.Connection, source_type: str, threshold: int
) -> list[str]:
    """enabled なソースのうち threshold 回以上連続で失敗しているものを 1 件 1 行で返す。

    doctor #14・watchdog・週次の健康診断用の平文(HTML エスケープは送る側)。
    """
    rows = conn.execute(
        "SELECT name, consecutive_failures, last_error FROM sources "
        "WHERE enabled = 1 AND type = ? AND consecutive_failures >= ? ORDER BY id",
        (source_type, threshold),
    ).fetchall()
    alerts = []
    for row in rows:
        # 改行は空白に寄せて 1 件 1 行を保つ(例外の文言は複数行になりうる)
        error = " ".join((row["last_error"] or "不明").split())
        if len(error) > ALERT_ERROR_MAX_CHARS:
            error = error[: ALERT_ERROR_MAX_CHARS - 1] + "…"
        alerts.append(
            f"「{_one_line(row['name'])}」が {row['consecutive_failures']} 回連続で失敗"
            f"(直近: {error})"
        )
    return alerts


def source_health_report(conn: sqlite3.Connection, config: Config, today: str) -> str:
    """週 1 回の健康診断の本文(平文。HTML エスケープは送る側)。today は JST の暦日。

    何も無い週も送る(棚卸しの合図を兼ねる)。X を止めている間は X のソースを見ない
    (回数も新着も進まないので、止める前の状態で知らせ続けない)。
    """
    types = ["rss", "x"] if config.x.enabled else ["rss"]
    findings: list[str] = []
    for source_type in types:
        findings += [f"⚠️ {alert}" for alert in source_failure_alerts(conn, source_type, 1)]
    findings += [
        f"↪️ 「{_one_line(row['name'])}」のフィードが移転している(移転先: {row['moved_url']})"
        for row in _moved(conn, types)
    ]
    findings += [
        f"💤 「{_one_line(row['name'])}」に {QUIET_DAYS} 日間、新しい記事が入っていない"
        for row in _quiet(conn, types, today)
    ]
    rates = [
        f"・「{_one_line(row['name'])}」{100 * row['positive'] / row['delivered']:.0f}%"
        f"(👍・🔖 {row['positive']} / 配信 {row['delivered']})"
        for row in _lowest_rates(conn, today)
    ]
    lines = ["🩺 ソースの健康診断(週 1 回)", ""]
    lines += findings or ["✅ 失敗・移転・新着なしのソースはありません"]
    lines += [
        "",
        f"📊 👍・🔖 の率が低いソース(直近 {RATE_DAYS // 7} 週・配信 {RATE_MIN_DELIVERED} 件以上)",
    ]
    lines += rates or ["(対象のソースなし)"]
    lines += [
        "",
        "止める・URL を直すかはあなたが決めます(自動では変えません。手順は runbook §12)",
    ]
    return "\n".join(lines)


def _moved(conn: sqlite3.Connection, types: list[str]) -> list[sqlite3.Row]:
    marks = ",".join("?" * len(types))
    return conn.execute(
        f"SELECT name, moved_url FROM sources WHERE enabled = 1 AND type IN ({marks}) "
        "AND moved_url IS NOT NULL ORDER BY id",
        types,
    ).fetchall()


def _quiet(conn: sqlite3.Connection, types: list[str], today: str) -> list[sqlite3.Row]:
    """QUIET_DAYS の間に新しい記事が入っていないソース(登録して間もないソースは除く)。"""
    since = _utc_since(today, QUIET_DAYS)
    marks = ",".join("?" * len(types))
    return conn.execute(
        f"""
        SELECT s.name FROM sources s
        WHERE s.enabled = 1 AND s.type IN ({marks}) AND s.created_at < ?
          AND NOT EXISTS (
            SELECT 1 FROM items i WHERE i.source_id = s.id AND i.fetched_at >= ?
          )
        ORDER BY s.id
        """,
        (*types, since, since),
    ).fetchall()


def _lowest_rates(conn: sqlite3.Connection, today: str) -> list[sqlite3.Row]:
    """直近 RATE_DAYS の配信での 👍・🔖 の率が低いソース(KPI 04 をソース単位にしたもの)。

    母集団は配信済みの digest の item(無反応の配信も分母に入る)。反応は item ごとの最新。
    """
    since = (datetime.fromisoformat(today) - timedelta(days=RATE_DAYS)).strftime("%Y-%m-%d")
    return conn.execute(
        """
        WITH delivered AS (
          SELECT DISTINCT CAST(je.value AS INTEGER) AS item_id
          FROM digests, json_each(digests.item_ids) AS je
          WHERE digests.delivery_status = 'sent' AND digests.digest_date > ?
        ),
        latest AS (
          SELECT item_id, value,
                 row_number() OVER (PARTITION BY item_id ORDER BY created_at DESC, id DESC) AS rn
          FROM signals WHERE kind = 'reaction'
        )
        SELECT s.name, count(*) AS delivered,
               coalesce(sum(latest.value IN ('useful', 'dig')), 0) AS positive
        FROM delivered
        JOIN items i ON i.id = delivered.item_id
        JOIN sources s ON s.id = i.source_id
        LEFT JOIN latest ON latest.item_id = delivered.item_id AND latest.rn = 1
        WHERE s.enabled = 1
        GROUP BY s.id
        HAVING count(*) >= ?
        ORDER BY 1.0 * positive / count(*), count(*) DESC, s.id
        LIMIT ?
        """,
        (since, RATE_MIN_DELIVERED, RATE_SHOW),
    ).fetchall()


def _utc_since(today: str, days: int) -> str:
    """JST の暦日 today の days 日前の 0 時を、items.fetched_at と比べられる UTC の文字列に。"""
    start = datetime.fromisoformat(today).replace(tzinfo=JST) - timedelta(days=days)
    return start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _one_line(text: str) -> str:
    return " ".join(text.split())
