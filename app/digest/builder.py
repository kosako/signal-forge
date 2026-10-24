"""digest 生成(設計 01 §8 / 03 §6)。

- 1 暦日(JST)1 行。生成済みなら skip、--force で置換(digest_date UNIQUE が冪等キー)
- 通常: ランキング選抜(min_score 未満は載せない。0 件なら「該当なし」)
- degraded: 当日候補(当日に分析された候補)の分析が LLM 全滅(全件 degraded)のとき、
  当日候補をタイトルのみで最大 max_items 件(min_score は適用しない — 無配信より劣化配信。
  設計 00 §6)。閾値に届かず analyzed のまま残る過去の記事は判定にも劣化配信にも入れない(GH#175)
- 障害の空(GH#191): 分析の段が失敗した日(hold_if_empty)に載せる記事が無ければ(ランキングも
  劣化配信も 0 件)、「該当なし」として確定せず DigestHeldError を送出する(行を作らない)。確定すると
  復旧後の同じ日の再実行が skip し、回復した分析の結果が届かないため
"""

from __future__ import annotations

import json
import logging
import sqlite3

from app.config import Config
from app.jst import jst_day_bounds_utc, today_jst
from app.models import EXTRACTION_FAILED_NOTE
from app.ranking import RankedItem, digest_candidates, rank_for_digest

# 🔖 保存節(GH#99)の表示上限。超過は件数のみ示す(全件は Obsidian の Items ノートへ)
BOOKMARK_SECTION_MAX = 10


class DigestHeldError(RuntimeError):
    """分析の段が失敗した日に載せる記事が無かった(GH#191)。障害の空なので digest を確定しない。

    送出の前に同じトランザクションを ROLLBACK する(--force の旧採用 item の復帰も戻り、
    既存の digest はそのまま残る)。
    """


def generate_digest(
    conn: sqlite3.Connection,
    config: Config,
    logger: logging.Logger,
    *,
    run_id: str | None = None,
    force: bool = False,
    trend_markdown: str | None = None,
    hold_if_empty: bool = False,
) -> int | None:
    """当日ダイジェストを生成し digests.id を返す(skip 時は None)。

    trend_markdown: 「今日のトレンド」節(GH#75)。付加機能なので None 可(節を省く)。
    hold_if_empty: 分析の段が失敗した日。載せる記事が無ければ DigestHeldError(GH#191)。
    """
    digest_date = today_jst()
    # 置換の準備(旧採用 item の復帰)から新 digest 確定までを同一トランザクションで行う。
    # existing の読みもロック取得後に行う(取得前の読みだと、並行実行時に stale な
    # item_ids を基準に置換してしまう。id 維持の UPDATE 化で UNIQUE 衝突による
    # 検出も効かないため、読み自体をロック内に置く。Codex must)
    conn.execute("BEGIN IMMEDIATE")
    # ログは try の外で出す(COMMIT 後の try 内でログ例外が起きると、終了済み
    # トランザクションへの ROLLBACK が元例外をマスクする。Codex should)
    digest_id: int | None = None
    try:
        existing = conn.execute(
            "SELECT id, item_ids, bookmark_item_ids FROM digests WHERE digest_date = ?",
            (digest_date,),
        ).fetchone()
        skipped = bool(existing and not force)
        if skipped:
            conn.execute("COMMIT")  # 変更なしで閉じる
        else:
            if existing:
                # --force 置換: 旧 digest 採用 item(🔖保存節の分も)を候補(analyzed)に
                # 戻してから再ランキング(戻さないと旧採用分が新 digest から黙って消える)。
                # 配信台帳も作り直す
                old_ids = json.loads(existing["item_ids"]) + json.loads(
                    existing["bookmark_item_ids"] or "[]"
                )
                if old_ids:
                    placeholders = ",".join("?" * len(old_ids))
                    conn.execute(
                        f"UPDATE items SET status = 'analyzed' "
                        f"WHERE id IN ({placeholders}) AND status = 'digested'",
                        old_ids,
                    )
                conn.execute("DELETE FROM digest_messages WHERE digest_id = ?", (existing["id"],))
                # digests 行は DELETE せず id を維持して下の UPDATE で上書きする。
                # digest_notes(日次感想 = 恒久記録。GH#96)が digest_id を参照しており、
                # DELETE は FK 違反(CASCADE にすると感想が消える)。置換後も同じ
                # digest_date への感想として意味が保たれるため id 維持が正しい

            # 当日候補の最新分析がすべて degraded なら通常ランキングを使わない(min_score=0 等の
            # 許容設定でも degraded 配信の契約を守る)
            ranked = _all_degraded_candidates(conn, config, digest_date)
            degraded_digest = bool(ranked)
            if not degraded_digest:
                ranked = rank_for_digest(conn, config)
            if hold_if_empty and not ranked:
                # 分析の障害の空を「該当なし」として確定しない(GH#191)。下の except が
                # ROLLBACK して送出する(--force の旧採用 item の復帰も戻す)
                raise DigestHeldError(
                    "analyze 段の失敗で載せる記事が無いため、当日の digest を確定しない"
                )

            # 🔖 保存節(GH#99): ランキング外の must-include 別枠。台帳(bookmark_item_ids)
            # 未掲載のブックマーク item を保存順(新しい順)で全件載せる。リンク列で
            # 分析に依存しないため degraded 配信でも付ける
            bookmarks = _pending_bookmark_items(
                conn, exclude_digest_id=existing["id"] if existing else None
            )
            # トレンド節は degraded 配信では付けない(分析が無いため意味を成さない)
            trend = None if degraded_digest else trend_markdown
            markdown = _render_markdown(
                digest_date, ranked, degraded=degraded_digest, trend=trend, bookmarks=bookmarks
            )
            item_ids = [item.item_id for item in ranked]
            bookmark_item_ids = [row["item_id"] for row in bookmarks]
            if existing:
                # --force 置換は id 維持の上書き。配信状態は新規行と同じ初期値へ戻す
                # (戻さないと置換後の deliver が「配信済み」として skip する)
                conn.execute(
                    """
                    UPDATE digests SET
                        run_id = ?,
                        generated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now'),
                        status = ?, markdown = ?, item_ids = ?, item_count = ?,
                        delivery_status = 'pending', sent_at = NULL, trend_markdown = ?,
                        bookmark_item_ids = ?
                    WHERE id = ?
                    """,
                    (
                        run_id,
                        "degraded" if degraded_digest else "ok",
                        markdown,
                        json.dumps(item_ids),
                        len(item_ids),
                        trend,
                        json.dumps(bookmark_item_ids),
                        existing["id"],
                    ),
                )
                digest_id = existing["id"]
            else:
                digest_id = conn.execute(
                    """
                    INSERT INTO digests
                        (digest_date, run_id, status, markdown, item_ids, item_count,
                         trend_markdown, bookmark_item_ids)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        digest_date,
                        run_id,
                        "degraded" if degraded_digest else "ok",
                        markdown,
                        json.dumps(item_ids),
                        len(item_ids),
                        trend,
                        json.dumps(bookmark_item_ids),
                    ),
                ).lastrowid
            for item_id in item_ids + bookmark_item_ids:
                conn.execute("UPDATE items SET status = 'digested' WHERE id = ?", (item_id,))
            conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise

    if skipped:
        logger.info(
            "digest 生成済みのため skip(置換は --force)",
            extra={"run_id": run_id, "stage": "digest"},
        )
        return None

    logger.info(
        "digest 生成",
        extra={
            "run_id": run_id,
            "stage": "digest",
            "extra": {
                "items": len(item_ids),
                "bookmarks": len(bookmark_item_ids),
                "degraded": degraded_digest,
            },
        },
    )
    return digest_id


def _pending_bookmark_items(
    conn: sqlite3.Connection, *, exclude_digest_id: int | None = None
) -> list[sqlite3.Row]:
    """🔖 保存節に未掲載のブックマーク保存アイテムを保存順(新しい順)で返す(GH#99)。

    掲載済み判定は item の status でなく **bookmark_item_ids 台帳**で行う。配信済み
    (digested)の既存 item に後からブックマークが付く合流ケースでも、保存節・export に
    必ず一度は現れるため(status 基準だと恒久に落ちる。Codex レビュー P2)。
    --force 置換では自分自身の旧台帳を除外して再導出する(exclude_digest_id)。
    status は analyzed / digested のみ(分析前・blocklist skip・error は掲載しない)。
    """
    return conn.execute(
        """
        SELECT items.id AS item_id, items.title, items.canonical_url,
               (SELECT a.title_ja FROM analyses AS a WHERE a.item_id = items.id
                ORDER BY a.analyzed_at DESC, a.id DESC LIMIT 1) AS title_ja,
               (SELECT MAX(observed_at) FROM x_signals
                WHERE x_signals.kind = 'bookmark' AND x_signals.item_id = items.id
               ) AS marked_at
        FROM items
        WHERE items.status IN ('analyzed', 'digested')
          AND EXISTS (
            SELECT 1 FROM x_signals
            WHERE x_signals.kind = 'bookmark' AND x_signals.item_id = items.id
          )
          AND NOT EXISTS (
            SELECT 1
            FROM digests, json_each(digests.bookmark_item_ids) AS listed
            WHERE digests.bookmark_item_ids IS NOT NULL
              AND digests.id <> COALESCE(?, -1)
              AND listed.value = items.id
          )
        ORDER BY marked_at DESC, items.id DESC
        """,
        (exclude_digest_id,),
    ).fetchall()


def _all_degraded_candidates(
    conn: sqlite3.Connection, config: Config, digest_date: str
) -> list[RankedItem]:
    """LLM 全滅判定: 当日候補全件の最新分析が degraded なら、当日候補をタイトルのみで返す。

    候補はランキングと同じ集合(digest_candidates。ブックマーク紐付けの item を含まない。GH#99)。
    当日候補 = そのうち最新の分析が digest_date(JST)に行われたもの。閾値に届かなかった過去の
    記事は analyzed のまま候補に残るので、候補全体で見ると当日の全滅が隠れる(GH#175)。
    当日候補が無い(当日の分析が無い)ときは全滅としない(通常のランキングに任せる)。
    """
    start, end = jst_day_bounds_utc(digest_date)
    rows = [row for row in digest_candidates(conn) if start <= row["analyzed_at"] < end]
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


def _md_link(title: str, url: str) -> str:
    """Markdown リンクを安全に組む(外部由来 title の []・URL の () で構造を壊さない)。"""
    safe_title = title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    safe_url = url.replace("(", "%28").replace(")", "%29")
    return f"[{safe_title}]({safe_url})"


def _render_markdown(
    digest_date: str,
    items: list[RankedItem],
    *,
    degraded: bool,
    trend: str | None = None,
    bookmarks: list[sqlite3.Row] = (),
) -> str:
    lines = [f"# 朝のダイジェスト {digest_date}", ""]
    if degraded:
        lines.append("> ⚠️ LLM 分析が失敗したため、タイトルのみの degraded 配信です。")
        lines.append("")
    if trend:  # 「今日のトレンド」節を全体感として冒頭に(GH#75)
        lines.append(trend)
        lines.append("")
    if bookmarks:  # 🔖 保存節(must-include の別枠。GH#99)
        lines.append(f"## 🔖 保存({len(bookmarks)}件)")
        for row in bookmarks[:BOOKMARK_SECTION_MAX]:
            title = row["title_ja"] or row["title"] or row["canonical_url"]
            lines.append(f"- {_md_link(title, row['canonical_url'])}")
        if len(bookmarks) > BOOKMARK_SECTION_MAX:
            lines.append(
                f"- …他 {len(bookmarks) - BOOKMARK_SECTION_MAX} 件(Items ノートに保存済み)"
            )
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
            if item.extraction_failed and not item.degraded:
                lines.append(f"- {EXTRACTION_FAILED_NOTE}")
            lines.append("")
            lines.append(item.summary)
            lines.append("")
            lines.append(f"**推薦理由**: {item.recommendation_reason}")
            lines.append(f"**示唆**: {item.implication}")
        lines.append("")
    return "\n".join(lines)
