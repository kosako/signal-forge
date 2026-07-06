-- 日次 KPI(PROPOSAL §成功条件 / 設計 01 §7)
-- 母集団は配信済み digest(sent)。反応ゼロの配信日も行として現れ、
-- 反応率 0% の無効日として判定される(fail-closed)。
-- 反応は item×日の最新レコードを採用(押し直し許容)。
WITH latest AS (
  SELECT item_id, value, signal_date,
         row_number() OVER (PARTITION BY item_id, signal_date
                            ORDER BY created_at DESC, id DESC) AS rn
  FROM signals WHERE kind = 'reaction'
)
SELECT digests.digest_date,
       coalesce(sum(latest.value IN ('useful', 'dig')), 0)              AS positive,
       count(latest.item_id)                                            AS reacted,
       digests.item_count                                               AS delivered,
       round(count(latest.item_id) * 100.0
             / nullif(digests.item_count, 0), 1)                        AS reaction_rate_pct,
       (coalesce(sum(latest.value IN ('useful', 'dig')), 0) >= 3)       AS kpi_met,
       (count(latest.item_id) = 0
        OR count(latest.item_id) * 1.0 / nullif(digests.item_count, 0) <= 0.30)
                                                                        AS invalid_day
FROM digests
LEFT JOIN latest
       ON latest.signal_date = digests.digest_date AND latest.rn = 1
WHERE digests.delivery_status = 'sent'
GROUP BY digests.digest_date, digests.item_count
ORDER BY digests.digest_date;
