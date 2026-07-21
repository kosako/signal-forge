-- GH#105: 朝の調査段の実行結果台帳(設計 06 §4〜5)。
-- 1 実行 = 1 行。ノート生成(vault 書込)と概要カード送信の間で落ちても、
-- 再実行が「生成済みを再生成せず・未送信カードだけ送る」ための正本。
--   - message_id IS NULL = カード未送信(次回 run / intel dig が送る)
--   - card_text は送信文の確定形を保存する(再送時に LLM を呼ばず同一文を送る)
-- 同一 item の再指定(done → request 復帰)は別の日に別行として積める(UNIQUE にしない。
-- ノート名は実行日プレフィクスで衝突しない)。
-- カードへのリプライの帰属(#106 の take 記録)は message_id → item_id で引く。
CREATE TABLE dig_results (
  id                INTEGER PRIMARY KEY,
  item_id           INTEGER NOT NULL REFERENCES items(id),
  run_id            TEXT,               -- 朝バッチの ULID(ログ⇔DB 突合用)
  note_name         TEXT NOT NULL,      -- 解説ノートのファイル名(拡張子なし)
  source_note_name  TEXT,               -- 原文/対訳ノート。NULL = 無し(ツイート本文のみ等)
  angle             TEXT,               -- 実行時に使った観点(NULL = 自動ブリーフ)
  card_text         TEXT NOT NULL,      -- 概要カードの確定送信文
  created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  message_id        INTEGER,            -- Telegram message_id(NULL = 未送信)
  sent_at           TEXT
) STRICT;

CREATE INDEX ix_dig_results_item ON dig_results(item_id);
CREATE INDEX ix_dig_results_pending ON dig_results(id) WHERE message_id IS NULL;
-- リプライ帰属用(owner DM 限定運用のため message_id 単独で引ける。digest_messages と同じ前提)
CREATE INDEX ix_dig_results_message ON dig_results(message_id) WHERE message_id IS NOT NULL;
