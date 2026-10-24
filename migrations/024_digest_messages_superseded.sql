-- GH#190: --force の置換で、送ったメッセージの帰属(どのメッセージがどの item か)を消さない
-- (設計 01 §3 / 04 §2・§5・§7)。
-- 置換はこれまで当日の digest_messages の行を消していた。そのため Telegram に残る前のメッセージへの
-- リプライが感想として記録されず、再選抜で外れた記事の未投影の感想も投影先を失っていた。
-- 置換では行を消さず、superseded_at(置換した時刻)を入れて残す。NULL = 今の配信の世代。
-- 部分再送の判定と一意の制約は今の世代の行だけを見る(置換の後の deliver は従来どおり全通を
-- 送り直す)。リプライの帰属と感想の投影は、前の世代の行も含めて見る。
-- この migration より前の行は NULL(今の世代)。それまでの置換で消えた行は戻らない。
ALTER TABLE digest_messages ADD COLUMN superseded_at TEXT;

DROP INDEX ux_digest_messages_item;
DROP INDEX ux_digest_messages_header;
CREATE UNIQUE INDEX ux_digest_messages_item   ON digest_messages(digest_id, item_id)
  WHERE item_id IS NOT NULL AND superseded_at IS NULL;
CREATE UNIQUE INDEX ux_digest_messages_header ON digest_messages(digest_id)
  WHERE item_id IS NULL AND superseded_at IS NULL;
