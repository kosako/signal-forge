-- 無効日3日連続の検知(設計 03 §6 / リスク R1)
-- 母集団は配信済み digest(sent)。反応ゼロの配信日も無効日として streak に入る
-- (reaction 起点だと反応ゼロの日が消え、R1 発動を見逃す)。
-- 3日連続を検知したら digest.max_items = 5 への削減を発動する(手順はテンプレート参照)
WITH latest AS (
  SELECT item_id, value, signal_date,
         row_number() OVER (PARTITION BY item_id, signal_date
                            ORDER BY created_at DESC, id DESC) AS rn
  FROM signals WHERE kind = 'reaction'
),
daily AS (
  SELECT digests.digest_date,
         (count(latest.item_id) = 0
          OR count(latest.item_id) * 1.0 / nullif(digests.item_count, 0) <= 0.30)
           AS invalid_day
  FROM digests
  LEFT JOIN latest
         ON latest.signal_date = digests.digest_date AND latest.rn = 1
  WHERE digests.delivery_status = 'sent'
  GROUP BY digests.digest_date, digests.item_count
),
streaks AS (
  SELECT digest_date, invalid_day,
         lag(invalid_day, 1) OVER (ORDER BY digest_date) AS prev1,
         lag(invalid_day, 2) OVER (ORDER BY digest_date) AS prev2
  FROM daily
)
SELECT digest_date AS third_day_of_streak
FROM streaks
WHERE invalid_day = 1 AND prev1 = 1 AND prev2 = 1
ORDER BY digest_date;
