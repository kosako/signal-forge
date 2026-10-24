-- GH#193: 感想の受信を冪等にする(設計 04 §1 の「再起動で二重処理しない」)。
-- poller は感想を記帳した後に getUpdates の offset を保存する。その間に止まる(または offset の
-- 保存がロック競合で失敗する。GH#181)と Telegram が同じ update を再配達し、同じ感想が 2 行になる。
-- 感想を書いたリプライの chat_id と message_id を記帳し、組で一意にする(message_id は chat 単位)。
-- poller は INSERT ... ON CONFLICT DO NOTHING で記帳し、記帳と処理済みの判定を同じ 1 文で確定する。
-- 019 より前の行は NULL のまま(UNIQUE は NULL 同士を別の値として扱うので衝突しない)。
ALTER TABLE item_notes ADD COLUMN chat_id INTEGER;
ALTER TABLE item_notes ADD COLUMN message_id INTEGER;
CREATE UNIQUE INDEX ux_item_notes_message ON item_notes(chat_id, message_id);

ALTER TABLE digest_notes ADD COLUMN chat_id INTEGER;
ALTER TABLE digest_notes ADD COLUMN message_id INTEGER;
CREATE UNIQUE INDEX ux_digest_notes_message ON digest_notes(chat_id, message_id);
