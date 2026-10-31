-- GH#335: 未消化一覧へのリプライ(深掘りの指定)の受信を冪等にする(設計 04 §1 の「再起動で二重処理しない」)。
-- poller は指定を反映した後に確認を返信し、getUpdates の offset を保存する。その間に止まる(または
-- offset の保存がロック競合で失敗する。GH#181)と Telegram が同じ update を再配達する。指定をやり直すと
-- requested_at が動いて生成中の深掘りの compare-and-set が外れ、掘り終えた done の行も queued に戻る。
-- 指定を反映したリプライの chat_id と message_id を記帳し、組で一意にする(message_id は chat 単位)。
-- poller は INSERT ... ON CONFLICT DO NOTHING の記帳と指定の反映を同じトランザクションで行い、
-- 記帳と処理済みの判定を同じ 1 文で確定する(感想の migration 019 と同じ形)。本人が別のメッセージで
-- 指定し直したときは message_id が違うので受け付ける。番号が無く案内だけを返したリプライは記帳しない。
CREATE TABLE dig_backlog_replies (
  chat_id      INTEGER NOT NULL,
  message_id   INTEGER NOT NULL,
  received_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  PRIMARY KEY (chat_id, message_id)
) STRICT;
