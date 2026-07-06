-- ソース別・カテゴリ別の 👍∨🔖 率(週次ピボット判定の入力。設計 01 §7)
-- カテゴリは各 item の最新 analyses から取る
WITH latest AS (
  SELECT item_id, value, signal_date,
         row_number() OVER (PARTITION BY item_id, signal_date
                            ORDER BY created_at DESC, id DESC) AS rn
  FROM signals WHERE kind = 'reaction'
)
SELECT sources.type || ':' || sources.name AS source_label,
       analyses.category,
       count(*)                                        AS reacted,
       sum(latest.value IN ('useful', 'dig'))          AS positive,
       round(sum(latest.value IN ('useful', 'dig')) * 100.0 / count(*), 1) AS positive_pct
FROM latest
JOIN items   ON items.id = latest.item_id
JOIN sources ON sources.id = items.source_id
JOIN analyses ON analyses.id = (
  SELECT id FROM analyses WHERE item_id = items.id
  ORDER BY analyzed_at DESC, id DESC LIMIT 1
)
WHERE latest.rn = 1
GROUP BY source_label, analyses.category
ORDER BY positive_pct DESC, reacted DESC;
