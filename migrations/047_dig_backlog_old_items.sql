-- 夜の一覧の「🗂 古い記事」の枠(GH#449・設計 06 §2・04 §3)。
--
-- old_item_ids は、その 1 通の古い記事の枠に載せた item の id(JSON の配列。枠が無い 1 通は NULL)。
-- 「まとめて不要」のボタン(b:0:z)を押したとき、押した 1 通に載せた古い記事だけを不要にするのに使う
-- (callback_data と押された盤面の文言だけを信じない)。
ALTER TABLE dig_backlog_messages ADD COLUMN old_item_ids TEXT
  CHECK (old_item_ids IS NULL OR json_valid(old_item_ids));
