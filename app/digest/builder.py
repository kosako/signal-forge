"""digest 生成(設計 01 §8 / 03 §6)。

- 1 暦日(JST)1 行。生成済みなら skip、--force で置換(digest_date UNIQUE が冪等キー)
- 通常: ランキング選抜(min_score 未満は載せない。0 件なら「該当なし」)
- degraded: 当日候補の分析が LLM 全滅(全件 degraded)のとき、タイトルのみで
  最大 max_items 件(min_score は適用しない — 無配信より劣化配信。設計 00 §6)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta, timezone

from app.config import Config
from app.ranking import RankedItem, rank_for_digest

JST = timezone(timedelta(hours=9))


def today_jst() -> str:
    return datetime.now(UTC).astimezone(JST).strftime("%Y-%m-%d")


def generate_digest(
    conn: sqlite3.Connection,
    config: Config,
    logger: logging.Logger,
    *,
    run_id: str | None = None,
    force: bool = False,
) -> int | None:
    """当日ダイジェストを生成し digests.id を返す(skip 時は None)。"""
    digest_date = today_jst()
    existing = conn.execute(
        "SELECT id, item_ids FROM digests WHERE digest_date = ?", (digest_date,)
    ).fetchone()
    if existing and not force:
        logger.info(
            "digest 生成済みのため skip(置換は --force)",
            extra={"run_id": run_id, "stage": "digest"},
        )
        return None

    # 置換の準備(旧採用 item の復帰)から新 digest 確定までを同一トランザクションで行う
    conn.execute("BEGIN IMMEDIATE")
    try:
        if existing:
            # --force 置換: 旧 digest 採用 item を候補(analyzed)に戻してから再ランキング
            # (戻さないと旧採用分が新 digest から黙って消える)。配信台帳も作り直す
            old_ids = json.loads(existing["item_ids"])
            if old_ids:
                placeholders = ",".join("?" * len(old_ids))
                conn.execute(
                    f"UPDATE items SET status = 'analyzed' "
                    f"WHERE id IN ({placeholders}) AND status = 'digested'",
                    old_ids,
                )
            conn.execute("DELETE FROM digest_messages WHERE digest_id = ?", (existing["id"],))
            conn.execute("DELETE FROM digests WHERE id = ?", (existing["id"],))

        # 全候補の最新分析が degraded なら通常ランキングを使わない(min_score=0 等の
        # 許容設定でも degraded 配信の契約を守る)
        ranked = _all_degraded_candidates(conn, config)
        degraded_digest = bool(ranked)
        if not degraded_digest:
            ranked = rank_for_digest(conn, config)

        markdown = _render_markdown(digest_date, ranked, degraded=degraded_digest)
        item_ids = [item.item_id for item in ranked]
        digest_id = conn.execute(
            """
            INSERT INTO digests (digest_date, run_id, status, markdown, item_ids, item_count)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                digest_date,
                run_id,
                "degraded" if degraded_digest else "ok",
                markdown,
                json.dumps(item_ids),
                len(item_ids),
            ),
        ).lastrowid
        for item_id in item_ids:
            conn.execute("UPDATE items SET status = 'digested' WHERE id = ?", (item_id,))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise

    logger.info(
        "digest 生成",
        extra={
            "run_id": run_id,
            "stage": "digest",
            "extra": {"items": len(item_ids), "degraded": degraded_digest},
        },
    )
    return digest_id


def _all_degraded_candidates(conn: sqlite3.Connection, config: Config) -> list[RankedItem]:
    """LLM 全滅判定: 候補全件の最新分析が degraded なら、その候補をタイトルのみで返す。"""
    rows = conn.execute(
        """
        SELECT items.id AS item_id, items.source_id, items.title, items.canonical_url,
               sources.type || ':' || sources.name AS source_label,
               analyses.id AS analysis_id, analyses.degraded
        FROM items
        JOIN sources ON sources.id = items.source_id
        JOIN analyses ON analyses.id = (
            SELECT id FROM analyses WHERE item_id = items.id
            ORDER BY analyzed_at DESC, id DESC LIMIT 1
        )
        WHERE items.status = 'analyzed'
        ORDER BY items.id
        """
    ).fetchall()
    if not rows or any(row["degraded"] == 0 for row in rows):
        return []
    return [
        RankedItem(
            item_id=row["item_id"],
            analysis_id=row["analysis_id"],
            source_id=row["source_id"],
            source_label=row["source_label"],
            title=row["title"],
            canonical_url=row["canonical_url"],
            score=0.0,
            summary="",
            category="その他",
            recommendation_reason="",
            implication="",
            degraded=True,
        )
        for row in rows[: config.digest.max_items]
    ]


def _render_markdown(digest_date: str, items: list[RankedItem], *, degraded: bool) -> str:
    lines = [f"# 朝のダイジェスト {digest_date}", ""]
    if degraded:
        lines.append("> ⚠️ LLM 分析が失敗したため、タイトルのみの degraded 配信です。")
        lines.append("")
    if not items:
        lines.append("本日は基準(min_score)を満たすアイテムがありませんでした。")
        return "\n".join(lines)

    for position, item in enumerate(items, start=1):
        # 見出しは日本語タイトル優先(analyze-v002)。degraded / v001 世代は原文に fallback
        title = item.title_ja or item.title or "(タイトルなし)"
        lines.append(f"## {position}. {title}")
        lines.append(f"- ソース: {item.source_label} / スコア: {item.score:.0f}")
        lines.append(f"- URL: {item.canonical_url}")  # DB 正規 URL のみ(LLM 出力 URL 不使用)
        if not degraded:
            lines.append(f"- カテゴリ: {item.category}")
            lines.append("")
            lines.append(item.summary)
            lines.append("")
            lines.append(f"**推薦理由**: {item.recommendation_reason}")
            lines.append(f"**示唆**: {item.implication}")
        lines.append("")
    return "\n".join(lines)
