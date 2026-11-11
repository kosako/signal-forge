-- GH#403: 深掘りカードの「👎 不要」(設計 04 §3・06 §5.3)。押した item は未消化一覧と日曜の
-- 読んだのまとめ確認に載せない。反応(signals)には書かない(ダイジェストの 👎 とは別の操作で、
-- 🔖 の数・ソースの健康診断・KPI の定義を変えない。ユーザーの判断)。取り消すと行を消す。
--   item_id        不要にした item(1 item に 1 行)
--   dig_result_id  押したカード(どのカードから不要にしたかの記録)
--   dismissed_at   押した時刻(UTC)
CREATE TABLE dig_dismissals (
  item_id        INTEGER PRIMARY KEY REFERENCES items(id),
  dig_result_id  INTEGER NOT NULL REFERENCES dig_results(id),
  dismissed_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
) STRICT;
