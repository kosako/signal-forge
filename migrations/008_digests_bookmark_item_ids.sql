-- GH#99: digest の「🔖 保存」節 — ランキング外の must-include 別枠として
-- その日の digest に載せたブックマーク item の JSON 配列(保存順)。
ALTER TABLE digests ADD COLUMN bookmark_item_ids TEXT
  CHECK (bookmark_item_ids IS NULL OR json_valid(bookmark_item_ids));
