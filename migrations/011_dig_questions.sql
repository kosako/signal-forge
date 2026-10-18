-- GH#126: 深掘りカードへの質問と返事の台帳(ADR 0005 / 設計 06 §6)。
-- 1 リプライ = 1 行。返事は write-ahead で作る: 答え(または失敗)を先にここへ記帳し、
-- 送信できたら answer_message_id を記帳する。どこで中断しても、次の実行は未送信の行の
-- 送信から再開し、同じ質問の答えを作り直さない。
--   - status = 'pending'  : 答えを作る前
--   - status = 'answered' : 答えを作った(answer に本文)
--   - status = 'failed'   : 答えを作れなかった(error に理由。失敗の知らせを送る)
--   - answer_message_id IS NULL = 返事(または失敗の知らせ)が未送信
-- スレッド = どのカードへの質問か(dig_result_id)。カードへのリプライも、返事への
-- リプライも同じカードに帰属させ、それまでのやり取りを材料に含める。
-- kind は LLM がリプライを分けた結果(question / take / both)。take の記録(#106)が使う。
CREATE TABLE dig_questions (
  id                   INTEGER PRIMARY KEY,
  dig_result_id        INTEGER NOT NULL REFERENCES dig_results(id),
  question             TEXT NOT NULL,     -- owner のリプライ本文
  question_message_id  INTEGER NOT NULL,  -- そのリプライの message_id(返事はこれへのリプライで送る)
  status               TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'answered', 'failed')),
  kind                 TEXT CHECK (kind IN ('question', 'take', 'both')),
  answer               TEXT,
  error                TEXT,
  created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  answered_at          TEXT,
  answer_message_id    INTEGER,
  sent_at              TEXT
) STRICT;

CREATE INDEX ix_dig_questions_thread ON dig_questions(dig_result_id);
CREATE INDEX ix_dig_questions_pending ON dig_questions(id) WHERE status = 'pending';
CREATE INDEX ix_dig_questions_unsent ON dig_questions(id)
  WHERE status <> 'pending' AND answer_message_id IS NULL;
CREATE INDEX ix_dig_questions_answer_message ON dig_questions(answer_message_id)
  WHERE answer_message_id IS NOT NULL;
