-- 無効日3日連続の検知(設計 03 §6 / リスク R1)
-- 3日連続を検知したら digest.max_items = 5 への削減を発動する(手順はテンプレート参照)
WITH latest AS (
  SELECT item_id, value, signal_date,
         row_number() OVER (PARTITION BY item_id, signal_date
                            ORDER BY created_at DESC, id DESC) AS rn
  FROM signals WHERE kind = 'reaction'
),
daily AS (
  SELECT latest.signal_date,
         (count(*) * 1.0 / nullif(digests.item_count, 0) <= 0.30) AS invalid_day
  FROM latest
  LEFT JOIN digests ON digests.digest_date = latest.signal_date
  WHERE latest.rn = 1
  GROUP BY latest.signal_date
),
streaks AS (
  SELECT signal_date, invalid_day,
         lag(invalid_day, 1) OVER (ORDER BY signal_date) AS prev1,
         lag(invalid_day, 2) OVER (ORDER BY signal_date) AS prev2
  FROM daily
)
SELECT signal_date AS third_day_of_streak
FROM streaks
WHERE invalid_day = 1 AND prev1 = 1 AND prev2 = 1
ORDER BY signal_date;
