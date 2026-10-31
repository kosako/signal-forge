"""TTL purge — 朝バッチの backup の直前の段(設計 01 §6 の削除ジョブ)。

1. documents の期限切れ削除(expires_at <= now)
2. items.raw_payload の NULL 化は documents から独立して、本文キャッシュの取得時刻 +
   ソース別 TTL 超過の全行(status・documents の有無を問わない)に適用する。取得時刻は
   payload の cached_at があればそれ、無ければ fetched_at(GH#332)
   (documents 起点だと documents を持たない items が漏れ、特に X の full_text の
   無期限残存は本文非保持ポリシー違反になる)
3. 削除行+NULL 化行の合計が 1,000 行を超えた回は VACUUM(空き回収の意図)
"""

from __future__ import annotations

import logging
import sqlite3

from app.config import Config

VACUUM_THRESHOLD_ROWS = 1000

# items.raw_payload の TTL 超過の条件(パラメータは x_cache_ttl_days, cache_ttl_days)。
# TTL の起点は本文キャッシュを得た時刻: payload の cached_at があればそれ、無ければ
# fetched_at(初回の取込の時刻)。cached_at は TTL 切れの item に全文を書き戻したときだけ
# 付く(GH#332)。fetched_at を動かさないのは、新着の判定(source_health)と 24h dedup が
# 初回の取込の時刻を前提にするため。purge の NULL 化と doctor #11 が同じ条件を使う。
# cached_at が異常な値(文字列でない・"%Y-%m-%dT%H:%M:%SZ" の実在する日時でない・未来)なら
# 起点を空文字にして期限切れとして扱う(X の本文を長く残す向きに外さない。設計 01 §6)。
# 形の検査は、修飾子 '+0 days' で正規化を強いた strftime の値との一致で行う。修飾子が無いと
# 時の 24("…T24:30:00Z")をそのまま返す日があり(SQLite 3.53 で月末の 3 日以外)、
# TTL の内の不正な時刻が残る。正規化すると
# 翌日へ繰り上がって一致しない(2/30・平年の 2/29 も繰り上がり、時の 25・分の 60・秒の 60・
# 月日の 00 は NULL)。一致した値は正規の形なので、未来の検査は文字列の比較でよい
RAW_PAYLOAD_EXPIRED = """
    raw_payload IS NOT NULL
    AND (
      CASE
        WHEN json_type(raw_payload, '$.cached_at') IS NULL THEN fetched_at
        WHEN json_type(raw_payload, '$.cached_at') = 'text'
         AND strftime('%Y-%m-%dT%H:%M:%SZ', json_extract(raw_payload, '$.cached_at'),
                      '+0 days')
             = json_extract(raw_payload, '$.cached_at')
         AND json_extract(raw_payload, '$.cached_at')
             <= strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
        THEN json_extract(raw_payload, '$.cached_at')
        ELSE ''
      END
    ) <= strftime(
          '%Y-%m-%dT%H:%M:%SZ', 'now',
          '-' || (
            SELECT CASE WHEN sources.type = 'x' THEN ? ELSE ? END
            FROM sources WHERE sources.id = items.source_id
          ) || ' days'
        )
"""

# x_signals の本文抜粋の TTL 超過の条件(パラメータは x_cache_ttl_days)。purge の NULL 化と
# doctor #11 の残存の集計が同じ条件を使う(GH#250)
X_SIGNAL_EXCERPT_EXPIRED = (
    "text_excerpt IS NOT NULL"
    " AND observed_at <= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-' || ? || ' days')"
)


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
        f"UPDATE items SET raw_payload = NULL WHERE {RAW_PAYLOAD_EXPIRED}",
        (config.retention.x_cache_ttl_days, config.retention.cache_ttl_days),
    ).rowcount

    # x_signals の本文抜粋も X 由来の TTL キャッシュ(Issue #2 / GH#58 S1b)。
    # シグナル行(id・著者・時刻)は残し、本文だけ落とす
    excerpts_nullified = conn.execute(
        f"UPDATE x_signals SET text_excerpt = NULL WHERE {X_SIGNAL_EXCERPT_EXPIRED}",
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
