-- 今日の IT ニュース(GH#413)。冒頭の節を出来事の単位にし、出来事ごとに報じた記事のリンクを付ける。
-- {"events": [{"label": 見出し, "summary": 要約, "item_ids": [リンクにする items.id]}]}(出来事の大きい順)。
-- URL は持たず、配信のときに items の正規 URL から引く(LLM の出力の URL は使わない)。
-- trend_markdown(003)にはリンクを含まないプレーンテキストを今までどおり残す。
-- NULL = 構造なし(節なし、またはこの列より前の digest。前の digest は trend_markdown の文で出す)。
ALTER TABLE digests ADD COLUMN trend_json TEXT CHECK (trend_json IS NULL OR json_valid(trend_json));
