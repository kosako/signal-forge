-- GH#407: 深掘りカードの議論のモード(設計 06 §6)。カードの「💬 話す」で 1 つ開き、カードと返事への
-- リプライを議論の一手として受ける。「まとめて」で閉じ、議論の中の本人の発言を原文のまま take に残す。
--   dig_discussions.dig_result_id  議論しているカード
--   dig_discussions.started_at     「💬 話す」で開いた時刻(UTC)
--   dig_discussions.closed_at      「まとめて」を受け付けて閉じた時刻(UTC)。NULL = 議論中。まとめを
--                                  作れなかったら NULL に戻す(もう一度まとめられるように)
-- 1 枚のカードで同時に開く議論は 1 つ(部分 UNIQUE。再配達・二重押しで 2 つ開かない)。
CREATE TABLE dig_discussions (
  id             INTEGER PRIMARY KEY,
  dig_result_id  INTEGER NOT NULL REFERENCES dig_results(id),
  started_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  closed_at      TEXT
) STRICT;
CREATE UNIQUE INDEX ux_dig_discussions_open ON dig_discussions(dig_result_id)
  WHERE closed_at IS NULL;

-- 議論の一手とまとめは、質問と同じ台帳に積む(送信・再送・失敗の知らせ・1 日の上限を共有する)。
--   discussion_id    議論の中のリプライなら、その議論。NULL = 今までどおりの質問・感想・ボタン
--   discussion_wrap  1 = 「まとめて」(議論を閉じるまとめ)。まとめは議論の中の本人の発言を take に残す
ALTER TABLE dig_questions ADD COLUMN discussion_id INTEGER REFERENCES dig_discussions(id);
ALTER TABLE dig_questions ADD COLUMN discussion_wrap INTEGER NOT NULL DEFAULT 0
  CHECK (discussion_wrap IN (0, 1));
CREATE INDEX ix_dig_questions_discussion ON dig_questions(discussion_id)
  WHERE discussion_id IS NOT NULL;
