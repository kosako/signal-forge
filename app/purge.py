"""TTL purge — 朝バッチの backup の直前の段(設計 01 §6 の削除ジョブ)。

1. documents の期限切れ削除(expires_at <= now)
2. items.raw_payload の NULL 化は documents から独立して、fetched_at + ソース別 TTL
   超過の全行(status・documents の有無を問わない)に適用する
   (documents 起点だと documents を持たない items が漏れ、特に X の full_text の
   無期限残存は本文非保持ポリシー違反になる)
3. 削除行+NULL 化行の合計が 1,000 行を超えた回は VACUUM(空き回収の意図)
"""

from __future__ import annotations

import logging
import sqlite3

from app.config import Config

VACUUM_THRESHOLD_ROWS = 1000


def purge_expired(
    conn: sqlite3.Connection,
    config: Config,
    logger: logging.Logger,
    *,
    run_id: str | None = None,
) -> dict[str, int]:
    deleted = conn.execute(
        "DELETE FROM documents WHERE expires_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now')"
    ).rowcount

    nullified = conn.execute(
        """
        UPDATE items SET raw_payload = NULL
        WHERE raw_payload IS NOT NULL
          AND fetched_at <= strftime(
                '%Y-%m-%dT%H:%M:%SZ', 'now',
                '-' || (
                  SELECT CASE WHEN sources.type = 'x' THEN ? ELSE ? END
                  FROM sources WHERE sources.id = items.source_id
                ) || ' days'
              )
        """,
        (config.retention.x_cache_ttl_days, config.retention.cache_ttl_days),
    ).rowcount

    # x_signals の本文抜粋も X 由来の TTL キャッシュ(Issue #2 / GH#58 S1b)。
    # シグナル行(id・著者・時刻)は残し、本文だけ落とす
    excerpts_nullified = conn.execute(
        """
        UPDATE x_signals SET text_excerpt = NULL
        WHERE text_excerpt IS NOT NULL
          AND observed_at <= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-' || ? || ' days')
        """,
        (config.retention.x_cache_ttl_days,),
    ).rowcount

    # 設計 01 §6 の文言は「削除行 >1,000」だが、raw_payload の NULL 化も大きな空き
    # ページを生む(X full_text 等)ため、空き回収の意図に沿って合算で判定する
    if deleted + nullified + excerpts_nullified > VACUUM_THRESHOLD_ROWS:
        conn.execute("VACUUM")

    counts = {
        "documents_deleted": deleted,
        "raw_payload_nullified": nullified,
        "x_signal_excerpts_nullified": excerpts_nullified,
    }
    logger.info("purge 完了", extra={"run_id": run_id, "stage": "purge", "extra": counts})
    return counts
