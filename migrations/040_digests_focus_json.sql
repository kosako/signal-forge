-- 朝のダイジェストの冒頭の「🎯 今の重点」(GH#433・設計 03 §4.1・04 §2)。digest 段が、作ったときの
-- 今の重点([focus].enabled のとき)を写して残す。Telegram のヘッダと Daily ノートが出す。
-- 形は {"topics": [{"topic_id": 3, "name": "...", "subjects": 4}, ...]}。name は外部の本文から LLM が
-- 作った名前なので、出す側で escape する。重点が無い日と、[focus] が off の日は NULL。
ALTER TABLE digests ADD COLUMN focus_json TEXT CHECK (focus_json IS NULL OR json_valid(focus_json));
