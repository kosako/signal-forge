-- GH#99: X ブックマークの item 化 — シグナル行から保存アイテムへの紐付け。
-- NULL = 未 item 化(bookmark 以外の kind、および item 化前の既存行)。
ALTER TABLE x_signals ADD COLUMN item_id INTEGER REFERENCES items(id);

CREATE INDEX ix_x_signals_item ON x_signals(item_id);
