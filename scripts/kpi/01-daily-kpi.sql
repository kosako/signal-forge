-- 日次 KPI(PROPOSAL §成功条件 / 設計 01 §7)
-- 反応は item×日の最新レコードを採用(押し直し許容)。
-- kpi_met: 👍∨🔖 が3件以上 / invalid_day: 反応率 ≤30%(分母 = 配信件数)
WITH latest AS (
  SELECT item_id, value, signal_date,
         row_number() OVER (PARTITION BY item_id, signal_date
                            ORDER BY created_at DESC, id DESC) AS rn
  FROM signals WHERE kind = 'reaction'
)
SELECT latest.signal_date,
       sum(latest.value IN ('useful', 'dig'))                        AS positive,
       count(*)                                                      AS reacted,
       digests.item_count                                            AS delivered,
       round(count(*) * 100.0 / nullif(digests.item_count, 0), 1)    AS reaction_rate_pct,
       (sum(latest.value IN ('useful', 'dig')) >= 3)                 AS kpi_met,
       (count(*) * 1.0 / nullif(digests.item_count, 0) <= 0.30)      AS invalid_day
FROM latest
LEFT JOIN digests ON digests.digest_date = latest.signal_date
WHERE latest.rn = 1
GROUP BY latest.signal_date
ORDER BY latest.signal_date;
