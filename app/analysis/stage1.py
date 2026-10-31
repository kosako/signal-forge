"""分析 Stage 1: 事前選別(安価・機械的・LLM 不使用。設計 03 §2)。

`status='new'` の items に4規則を上から順に適用し、除外は `status='skipped'` +
`skip_reason`(規則名)で記録する(黙って脱落させない)。
extraction_status=failed は除外しない(本文なしでタイトル+メタだけの分析に回る。GH#240)。
まだ抽出を試していない(documents の行が無い)記事は、手で入れた manual を除いて選ばず、new の
まま次の抽出を待たせる(本文抽出の時間の上限で回した記事・抽出の段が落ちた日の記事を、本文なしで
分析して抽出の機会を失わない。朝バッチも単独の intel analyze も同じ規則。GH#340)。
duplicate は挿入時 dedup(設計 01 §6)で除外済みのため対象にしない。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from app.config import Config
from app.jst import jst_day_bounds_utc, today_jst

# 規則3: X の低情報量判定(正規化テキストがこの字数未満かつリンク無し)
X_NOISE_MIN_CHARS = 20


@dataclass(frozen=True)
class PreselectResult:
    counts: dict[str, int]  # {"selected": n, "blocklist": n, ...}
    selected_ids: list[int]  # LLM に渡す items.id(選抜集合の正本。再クエリしない)


def preselect(
    conn: sqlite3.Connection,
    config: Config,
    logger: logging.Logger,
    *,
    run_id: str | None = None,
) -> PreselectResult:
    """4規則を適用し、件数と採用 items.id を返す。

    採用集合は本戻り値が正本。Stage 2 が `status='new'` を再クエリすると、
    選別後に受信デーモンが追加した manual item が規則を通らず混入するため。
    抽出を待つ記事(manual 以外で documents の行が無い)は対象にせず、規則も件数も当てない。
    抽出に失敗した記事は documents に failed の行があるので、今までどおり本文なしで選ぶ。
    """
    rows = conn.execute(
        """
        SELECT items.*, sources.type AS source_type,
               EXISTS (
                 SELECT 1 FROM x_signals
                 WHERE x_signals.kind = 'bookmark' AND x_signals.item_id = items.id
               ) AS is_bookmarked
        FROM items
        JOIN sources ON sources.id = items.source_id
        WHERE items.status = 'new'
          AND (sources.type = 'manual'
               OR EXISTS (SELECT 1 FROM documents WHERE documents.item_id = items.id))
        ORDER BY items.id
        """
    ).fetchall()

    counts = {"selected": 0, "blocklist": 0, "stale": 0, "x_noise": 0, "daily_cap": 0}
    survivors: list[sqlite3.Row] = []
    bookmarks: list[sqlite3.Row] = []
    for row in rows:
        if row["is_bookmarked"]:
            # ブックマークは本人意思の保存アイテム(GH#99)。manual と同じ理屈で
            # stale(過去ツイートが本体)・x_noise を適用しない。blocklist(明示 NG)
            # だけは通す。判定は x_signals の紐付け(既存 item への合流でも失われない)
            reason = _rule_blocklist(row, config)
            if reason:
                _skip(conn, row["id"], reason)
                counts[reason] += 1
            else:
                bookmarks.append(row)
            continue
        reason = _rule_blocklist(row, config) or _rule_stale(row, config) or _rule_x_noise(row)
        if reason:
            _skip(conn, row["id"], reason)
            counts[reason] += 1
        else:
            survivors.append(row)

    # ブックマークは daily_cap の外で必ず採用(件数は x_signals の日次 cap /
    # backfill --limit で既に有界)。cap は従来どおり他ソースにのみ適用する。
    # cap は JST の暦日の上限で、同じ日の実行がすでに分析したぶんを引いた残りだけを
    # 採用する(GH#178。再走や単独の intel analyze で上限を超えない)
    limit = max(0, config.analysis.daily_llm_limit - analyzed_on(conn, today_jst()))
    selected = bookmarks + _rule_daily_cap(survivors, limit)
    selected_ids = [row["id"] for row in selected]
    selected_id_set = set(selected_ids)
    for row in survivors:
        if row["id"] not in selected_id_set:
            _skip(conn, row["id"], "daily_cap")
            counts["daily_cap"] += 1
    counts["selected"] = len(selected)

    logger.info("事前選別 完了", extra={"run_id": run_id, "stage": "analyze", "extra": counts})
    return PreselectResult(counts=counts, selected_ids=selected_ids)


def analyzed_on(conn: sqlite3.Connection, day: str) -> int:
    """JST の暦日 day に分析した件数(日次上限の使用量。GH#178)。

    台帳は analyses(analyzed_at は UTC。暦日の境界はトレンド・digest と同じ)。degraded の
    行も数える(LLM には渡している)。ブックマークに紐付く item は日次上限の外なので数えない
    (判定は事前選別と同じ x_signals の紐付け)。
    """
    start, end = jst_day_bounds_utc(day)
    return conn.execute(
        """
        SELECT count(*) AS n FROM analyses
        WHERE analyzed_at >= ? AND analyzed_at < ?
          AND NOT EXISTS (
            SELECT 1 FROM x_signals
            WHERE x_signals.kind = 'bookmark' AND x_signals.item_id = analyses.item_id
          )
        """,
        (start, end),
    ).fetchone()["n"]


def _skip(conn: sqlite3.Connection, item_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE items SET status = 'skipped', skip_reason = ? WHERE id = ?", (reason, item_id)
    )


def _rule_blocklist(row: sqlite3.Row, config: Config) -> str | None:
    """規則1: 静的 NG リスト(ドメイン一致・タイトル部分一致)。"""
    host = urlsplit(row["canonical_url"]).hostname or ""
    for domain in config.analysis.block_domains:
        if host == domain or host.endswith("." + domain):
            return "blocklist"
    title = row["title"] or ""
    for keyword in config.analysis.block_keywords:
        if keyword in title:
            return "blocklist"
    return None


def _rule_stale(row: sqlite3.Row, config: Config) -> str | None:
    """規則2: 鮮度 72h 超(manual は本人意思を優先して適用外)。

    published_at 不明(NULL)は判定不能なので除外しない(黙って落とす方向に倒さない)。
    """
    if row["source_type"] == "manual" or row["published_at"] is None:
        return None
    cutoff_ok = row["published_at"] >= _hours_ago_iso(config.analysis.freshness_hours)
    return None if cutoff_ok else "stale"


def _hours_ago_iso(hours: int) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rule_x_noise(row: sqlite3.Row) -> str | None:
    """規則3: X の低情報量パターン(リプライ・自RT・短文でリンク無し)。

    meta のキー(reply / self_retweet)は X ホームTL コネクタ(app/connectors/x.py の
    _to_raw_item)が書く契約。reply はコネクタが残した自分宛のリプライ、self_retweet は自分の RT。
    他人の RT(meta.retweet)は元ツイに正規化した通常のポストとして残す(設計 02 §2)。
    """
    if row["source_type"] != "x":
        return None
    meta = {}
    if row["raw_payload"]:
        meta = json.loads(row["raw_payload"]).get("meta", {})
    if meta.get("reply") or meta.get("self_retweet"):
        return "x_noise"
    body = ""
    if row["raw_payload"]:
        body = json.loads(row["raw_payload"]).get("raw_content", "") or ""
    text = f"{row['title'] or ''}{body}"
    if len(text.strip()) < X_NOISE_MIN_CHARS and "http" not in text:
        return "x_noise"
    return None


def _rule_daily_cap(rows: list[sqlite3.Row], limit: int) -> list[sqlite3.Row]:
    """規則4: 日次上限。published_at 降順・ソース間ラウンドロビンで多様性を保って採用。

    limit は当日の残り(日次上限 − 同じ日にすでに分析した件数。GH#178)。

    まず全体を published_at 降順(NULL は最後)に並べ、その順でソース列とソースの
    巡回順を作る(新しい item を持つソースから先に採用され、境界件数でも降順が保たれる)。
    """
    if len(rows) <= limit:
        return rows
    ordered = sorted(rows, key=lambda r: r["published_at"] or "", reverse=True)
    by_source: dict[int, list[sqlite3.Row]] = {}
    for row in ordered:
        by_source.setdefault(row["source_id"], []).append(row)

    selected: list[sqlite3.Row] = []
    queues = list(by_source.values())  # 挿入順 = 最新 item の新しいソース順
    while len(selected) < limit and any(queues):
        for queue in queues:
            if queue and len(selected) < limit:
                selected.append(queue.pop(0))
        queues = [q for q in queues if q]
    return selected
