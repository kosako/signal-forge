-- GH#399: 週 1 回(日曜の夜)の読んだのまとめ確認(設計 04 §4・06 §2)。未消化のうち、Telegram の
-- 感想(item_notes)がある item を 1 通に並べ、行ごとと全部の「✅ 読んだ」のボタンを付ける。
--   message_id  送った 1 通(owner DM 限定運用のため単独で一意。dig_backlog_messages と同じ前提)
--   sent_date   送った JST の日付
--   item_ids    その 1 通に載せた item の id(JSON の配列。載せた順)。ボタンはこの中の item だけを
--               受け付け、盤面もこの順で作り直す
CREATE TABLE read_confirm_messages (
  message_id  INTEGER PRIMARY KEY,
  sent_date   TEXT NOT NULL,
  item_ids    TEXT NOT NULL CHECK (json_valid(item_ids) AND json_type(item_ids) = 'array')
) STRICT;
