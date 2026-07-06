-- ソース別・カテゴリ別の 👍∨🔖 率(週次ピボット判定の入力。設計 01 §7〜8)
-- 母集団は配信済み digest の item_ids(無反応の配信 item も分母に入る。
-- 反応済みだけを分母にすると低率ソースの棚卸し判断を誤る)。
-- カテゴリは各 item の最新 analyses から取る。
WITH delivered AS (
  SELECT CAST(je.value AS INTEGER) AS item_id, digests.digest_date
  FROM digests, json_each(digests.item_ids) AS je
  WHERE digests.delivery_status = 'sent'
),
latest AS (
  SELECT item_id, value, signal_date,
         row_number() OVER (PARTITION BY item_id, signal_date
                            ORDER BY created_at DESC, id DESC) AS rn
  FROM signals WHERE kind = 'reaction'
)
SELECT sources.type || ':' || sources.name AS source_label,
       analyses.category,
       count(*)                                                  AS delivered,
       count(latest.item_id)                                     AS reacted,
       coalesce(sum(latest.value IN ('useful', 'dig')), 0)       AS positive,
       round(coalesce(sum(latest.value IN ('useful', 'dig')), 0) * 100.0
             / count(*), 1)                                      AS positive_pct
FROM delivered
JOIN items   ON items.id = delivered.item_id
JOIN sources ON sources.id = items.source_id
JOIN analyses ON analyses.id = (
  SELECT id FROM analyses WHERE item_id = items.id
  ORDER BY analyzed_at DESC, id DESC LIMIT 1
)
LEFT JOIN latest
       ON latest.item_id = delivered.item_id
      AND latest.signal_date = delivered.digest_date
      AND latest.rn = 1
GROUP BY source_label, analyses.category
ORDER BY positive_pct DESC, delivered DESC;
