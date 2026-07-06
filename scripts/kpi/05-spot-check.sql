-- 👍校正スポットチェック(週次レビュー #7)
-- 1週間前の 👍 から無作為に2〜3件抜き、いま見ても価値があったかを再評価する
WITH latest AS (
  SELECT item_id, value, signal_date,
         row_number() OVER (PARTITION BY item_id, signal_date
                            ORDER BY created_at DESC, id DESC) AS rn
  FROM signals WHERE kind = 'reaction'
)
SELECT items.id, items.title, items.canonical_url, latest.signal_date
FROM latest
JOIN items ON items.id = latest.item_id
WHERE latest.rn = 1 AND latest.value = 'useful'
  AND latest.signal_date = date('now', '+9 hours', '-7 days')  -- JST 暦日
ORDER BY random()
LIMIT 3;
