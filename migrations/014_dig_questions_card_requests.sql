-- GH#141: 深掘りカードのボタン(📝 要約・📖 詳しく)からの依頼も、質問と同じ台帳に積む。
-- ボタンには本人のメッセージが無いので question_message_id を NULL 可にする(返事はカードへの
-- リプライで送る = dig_results.message_id)。request = ボタンの種類(NULL = 本人のリプライ)。
-- 同じカードの同じボタンは 1 回だけ積む(Telegram の再配達・二重押しで 2 回作らない)。
-- NOT NULL を外すには表の作り直しが要る(SQLite)。dig_questions を参照する表は無い。
CREATE TABLE dig_questions_new (
  id                   INTEGER PRIMARY KEY,
  dig_result_id        INTEGER NOT NULL REFERENCES dig_results(id),
  question             TEXT NOT NULL,     -- owner のリプライ本文(ボタンなら決まった依頼文)
  question_message_id  INTEGER,           -- そのリプライの message_id(ボタンは NULL)
  request              TEXT CHECK (request IN ('summary', 'more')),
  status               TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'answered', 'failed')),
  kind                 TEXT CHECK (kind IN ('question', 'take', 'both')),
  answer               TEXT,
  error                TEXT,
  created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  answered_at          TEXT,
  answer_message_id    INTEGER,
  sent_at              TEXT,
  take_recorded_at     TEXT,
  CHECK ((request IS NULL) = (question_message_id IS NOT NULL))
) STRICT;

INSERT INTO dig_questions_new
  (id, dig_result_id, question, question_message_id, request, status, kind, answer, error,
   created_at, answered_at, answer_message_id, sent_at, take_recorded_at)
SELECT id, dig_result_id, question, question_message_id, NULL, status, kind, answer, error,
       created_at, answered_at, answer_message_id, sent_at, take_recorded_at
FROM dig_questions;

DROP TABLE dig_questions;
ALTER TABLE dig_questions_new RENAME TO dig_questions;

CREATE INDEX ix_dig_questions_thread ON dig_questions(dig_result_id);
CREATE INDEX ix_dig_questions_pending ON dig_questions(id) WHERE status = 'pending';
CREATE INDEX ix_dig_questions_unsent ON dig_questions(id)
  WHERE status <> 'pending' AND answer_message_id IS NULL;
CREATE INDEX ix_dig_questions_answer_message ON dig_questions(answer_message_id)
  WHERE answer_message_id IS NOT NULL;
CREATE UNIQUE INDEX ux_dig_questions_question_message ON dig_questions(question_message_id);
CREATE INDEX ix_dig_questions_take_unrecorded ON dig_questions(id)
  WHERE status = 'answered' AND kind IN ('take', 'both') AND answer_message_id IS NOT NULL
    AND take_recorded_at IS NULL;
CREATE UNIQUE INDEX ux_dig_questions_request ON dig_questions(dig_result_id, request)
  WHERE request IS NOT NULL;
