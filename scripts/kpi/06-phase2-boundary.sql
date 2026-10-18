-- フェーズ2境界判定(PROPOSAL §フェーズ2境界: 🔖累計8件 + その50%以上を実際に深掘り)
-- dig_total: item×日の最新 reaction が 🔖 の累計件数
-- 実施率の分母は dig_total(未回答の 🔖 は「未実施」扱い = fail-closed。
-- 回答済みだけを分母にすると 8件中1件回答 yes でも進出条件を満たしてしまう)
-- 分子も 🔖 の付いたアイテムに限る(take の記録 GH#106 は X のブックマークや自動キューの
-- アイテムにも followup_dig=yes を付けるため。分母に無いものを数えない)
WITH latest_reaction AS (
  SELECT item_id, value, signal_date,
         row_number() OVER (PARTITION BY item_id, signal_date
                            ORDER BY created_at DESC, id DESC) AS rn
  FROM signals WHERE kind = 'reaction'
),
digs AS (
  SELECT item_id, signal_date FROM latest_reaction WHERE rn = 1 AND value = 'dig'
),
latest_followup AS (
  SELECT item_id, value,
         row_number() OVER (PARTITION BY item_id
                            ORDER BY created_at DESC, id DESC) AS rn
  FROM signals WHERE kind = 'followup_dig' AND item_id IN (SELECT item_id FROM digs)
)
SELECT (SELECT count(*) FROM digs)                                   AS dig_total,
       (SELECT count(*) FROM latest_followup WHERE rn = 1)           AS followup_answered,
       (SELECT count(*) FROM latest_followup WHERE rn = 1
         AND value = 'yes')                                          AS followup_yes,
       round((SELECT count(*) FROM latest_followup WHERE rn = 1 AND value = 'yes') * 100.0
             / nullif((SELECT count(*) FROM digs), 0), 1)            AS dig_execution_pct,
       ((SELECT count(*) FROM digs) >= 8
        AND (SELECT count(*) FROM latest_followup WHERE rn = 1 AND value = 'yes') * 2
            >= (SELECT count(*) FROM digs))                          AS phase2_ready
;
