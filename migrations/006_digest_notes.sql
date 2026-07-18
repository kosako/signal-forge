-- ダイジェスト(ヘッダメッセージ)へのリプライ = 日次感想(GH#96。item_notes の相似形)。
-- 「その日のダイジェスト全体への take」を自分のログとして恒久保存する。正本は本テーブルで、
-- Obsidian の Daily ノート(GENERATED 領域「## 感想」節)は DB の純関数として投影する。
-- KPI・シグナルには接続しない(活用は後決め = GH#96 の境界)。
CREATE TABLE digest_notes (
  id          INTEGER PRIMARY KEY,
  digest_id   INTEGER NOT NULL REFERENCES digests(id),
  note        TEXT    NOT NULL,
  created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  -- Obsidian へ反映済みか(export 段の追いパスの差分管理。NULL = 未反映)
  exported_at TEXT
) STRICT;
CREATE INDEX ix_digest_notes_digest ON digest_notes(digest_id);
CREATE INDEX ix_digest_notes_pending ON digest_notes(digest_id) WHERE exported_at IS NULL;
