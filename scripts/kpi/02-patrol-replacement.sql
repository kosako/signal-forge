-- 巡回代替日(夜アンケート質問1。設計 01 §7)
-- 「いいえ」= 巡回せずに済んだ日(代替成功)。日ごとの最新回答を採用。
-- 未回答の日はこの結果に現れない = 代替できなかった日として fail-closed 集計する
-- (分母は暦日。突合は週次レビュー側の手作業)
WITH latest AS (
  SELECT signal_date, value,
         row_number() OVER (PARTITION BY signal_date
                            ORDER BY created_at DESC, id DESC) AS rn
  FROM signals WHERE kind = 'survey_patrol'
)
SELECT signal_date,
       value                AS patrolled,      -- yes = 巡回した / no = しなかった
       (value = 'no')       AS replaced_day    -- 1 = 巡回代替に成功した日
FROM latest WHERE rn = 1
ORDER BY signal_date;
