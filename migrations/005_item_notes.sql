-- Telegram 配信の記事メッセージへのリプライ = 感想キャプチャ(GH#92)。
-- 感想は「その場で書いた自分の take」= 知見の種(ADR 0003)。自分の文章のため
-- Issue #2(X 本文の TTL 制約)の対象外で恒久保存する。正本は本テーブルで、
-- Obsidian の item ノート(GENERATED 領域「## 感想」節)は DB の純関数として投影する。
-- KPI には接続しない(reaction を自動記帳しない = 測定定義の汚染防止)。
CREATE TABLE item_notes (
  id          INTEGER PRIMARY KEY,
  item_id     INTEGER NOT NULL REFERENCES items(id),
  note        TEXT    NOT NULL,
  created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  -- Obsidian へ反映済みか(export 段の追加パスの差分管理。NULL = 未反映)
  exported_at TEXT
) STRICT;
CREATE INDEX ix_item_notes_item ON item_notes(item_id);
CREATE INDEX ix_item_notes_pending ON item_notes(item_id) WHERE exported_at IS NULL;
