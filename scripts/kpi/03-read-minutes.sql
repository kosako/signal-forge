-- 読了時間の分布(夜アンケート質問2。10〜15分仮説との乖離を見る)
WITH latest AS (
  SELECT signal_date, value,
         row_number() OVER (PARTITION BY signal_date
                            ORDER BY created_at DESC, id DESC) AS rn
  FROM signals WHERE kind = 'survey_read_min'
)
SELECT value AS minutes, count(*) AS days
FROM latest WHERE rn = 1
GROUP BY value
ORDER BY CAST(value AS INTEGER);
