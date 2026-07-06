"""TTL purge — 朝バッチの最終ステップ(設計 01 §6 の削除ジョブ)。

1. documents の期限切れ削除(expires_at <= now)
2. items.raw_payload の NULL 化は documents から独立して、fetched_at + ソース別 TTL
   超過の全行(status・documents の有無を問わない)に適用する
   (documents 起点だと documents を持たない items が漏れ、特に X の full_text の
   無期限残存は本文非保持ポリシー違反になる)
3. 削除行が 1,000 行を超えた回は VACUUM
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

    if deleted > VACUUM_THRESHOLD_ROWS:
        conn.execute("VACUUM")

    counts = {"documents_deleted": deleted, "raw_payload_nullified": nullified}
    logger.info("purge 完了", extra={"run_id": run_id, "stage": "purge", "extra": counts})
    return counts
