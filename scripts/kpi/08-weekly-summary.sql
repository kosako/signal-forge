-- 週次サマリ(直近7日・JST。週次レビュー §6 の 1〜3 をまとめて出す)
-- 母集団は配信済み digest(sent)。反応ゼロの配信日は反応率 0% の無効日として
-- SQL 内で判定される(fail-closed。手動補正は不要)。
-- 残る手作業は夜アンケート未回答日の突合のみ(kpi-queries.md 参照)
WITH latest AS (
  SELECT item_id, value, signal_date,
         row_number() OVER (PARTITION BY item_id, signal_date
                            ORDER BY created_at DESC, id DESC) AS rn
  FROM signals WHERE kind = 'reaction'
),
daily AS (
  SELECT digests.digest_date,
         coalesce(sum(latest.value IN ('useful', 'dig')), 0) AS positive,
         count(latest.item_id) AS reacted,
         (coalesce(sum(latest.value IN ('useful', 'dig')), 0) >= 3) AS kpi_met,
         (count(latest.item_id) = 0
          OR count(latest.item_id) * 1.0 / nullif(digests.item_count, 0) <= 0.30)
           AS invalid_day
  FROM digests
  LEFT JOIN latest
         ON latest.signal_date = digests.digest_date AND latest.rn = 1
  WHERE digests.delivery_status = 'sent'
    AND digests.digest_date >= date('now', '+9 hours', '-7 days')  -- JST 暦日
  GROUP BY digests.digest_date, digests.item_count
)
SELECT count(*)                                 AS delivered_days,
       coalesce(sum(reacted > 0), 0)            AS reacted_days,
       coalesce(sum(kpi_met), 0)                AS kpi_met_days,
       coalesce(sum(invalid_day), 0)            AS invalid_days,
       (coalesce(sum(invalid_day), 0) >= 3)     AS week_fail_closed
FROM daily;
