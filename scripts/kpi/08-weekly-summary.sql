-- 週次サマリ(直近7日。週次レビュー §6 の 1〜3 をまとめて出す)
-- 有効測定日 = ダイジェストが配信された日(delivery_status='sent')
-- 注意: 反応ゼロの配信日は daily に現れない = 反応率 0% の無効日として手動で数える
-- (SQL は反応のあった日のみ集計する。fail-closed の突合はテンプレートの手順)
WITH latest AS (
  SELECT item_id, value, signal_date,
         row_number() OVER (PARTITION BY item_id, signal_date
                            ORDER BY created_at DESC, id DESC) AS rn
  FROM signals WHERE kind = 'reaction'
),
daily AS (
  SELECT latest.signal_date,
         sum(latest.value IN ('useful', 'dig')) AS positive,
         count(*) AS reacted,
         digests.item_count AS delivered,
         (sum(latest.value IN ('useful', 'dig')) >= 3) AS kpi_met,
         (count(*) * 1.0 / nullif(digests.item_count, 0) <= 0.30) AS invalid_day
  FROM latest
  LEFT JOIN digests ON digests.digest_date = latest.signal_date
  WHERE latest.rn = 1 AND latest.signal_date >= date('now', '-7 days')
  GROUP BY latest.signal_date
)
SELECT (SELECT count(*) FROM digests
         WHERE delivery_status = 'sent'
           AND digest_date >= date('now', '-7 days'))  AS delivered_days,
       (SELECT count(*) FROM daily)                    AS reacted_days,
       (SELECT count(*) FROM daily WHERE kpi_met)      AS kpi_met_days,
       (SELECT count(*) FROM daily WHERE invalid_day)  AS invalid_days,
       (SELECT count(*) FROM daily WHERE invalid_day) >= 3 AS week_fail_closed
;
