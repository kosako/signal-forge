-- フェーズ2境界判定(PROPOSAL §フェーズ2境界: 🔖累計8件 + 50%深掘り実施)
-- dig_total: item×日の最新 reaction が 🔖 の累計件数
-- followup は item ごとの最新回答を採用し、yes 率を出す
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
  FROM signals WHERE kind = 'followup_dig'
)
SELECT (SELECT count(*) FROM digs)                                   AS dig_total,
       (SELECT count(*) FROM latest_followup WHERE rn = 1)           AS followup_answered,
       (SELECT count(*) FROM latest_followup WHERE rn = 1
         AND value = 'yes')                                          AS followup_yes,
       round((SELECT count(*) FROM latest_followup WHERE rn = 1 AND value = 'yes') * 100.0
             / nullif((SELECT count(*) FROM latest_followup WHERE rn = 1), 0), 1)
                                                                     AS dig_execution_pct,
       ((SELECT count(*) FROM digs) >= 8
        AND (SELECT count(*) FROM latest_followup WHERE rn = 1 AND value = 'yes') * 2
            >= (SELECT count(*) FROM latest_followup WHERE rn = 1)) AS phase2_ready
;
