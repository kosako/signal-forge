-- 週の仮説の問い(GH#440・設計 03 §4.2・04 §4)。日曜の朝バッチ(weekly 段)が、その週の行動の集計と
-- 材料から LLM に仮説を書かせ(prompts/weekly-v001.md)、日曜の夜に受信デーモンが 1 つずつ送る。
-- 夜のアンケートの 2 問(巡回した?・読むのにかかった時間)に代わる問いで、答えは次の週の材料にする。
--
-- week_date は作った日(JST の日曜)。その日の行があれば、同じ週にもう作らない。
-- question は LLM が外部の本文(記事の見出し・投稿)をもとに書いた文。送るときに 1 行に潰して escape する。
-- evidence_json は根拠にした材料の写し([{"kind": "tg_dig", "title": "…"}, {"kind": "stat"}] の形。
-- title は外部の本文)。送るときに、種類ごとの件数と見出しの例を出す。
-- message_id は送った Telegram のメッセージ(NULL = まだ送っていない)。ボタンの照合とリプライの帰属に使う。
CREATE TABLE weekly_questions (
  id              INTEGER PRIMARY KEY,
  week_date       TEXT    NOT NULL,   -- JST の暦日(作った日曜)
  position        INTEGER NOT NULL CHECK (position >= 1),
  kind            TEXT    NOT NULL CHECK (kind IN
                    ('interest_shift','fatigue','habit','source','focus','patrol','other')),
  question        TEXT    NOT NULL,
  evidence_json   TEXT    NOT NULL DEFAULT '[]',
  run_id          TEXT,
  prompt_version  TEXT    NOT NULL,
  created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  message_id      INTEGER,
  sent_at         TEXT,
  UNIQUE (week_date, position)
) STRICT;
CREATE UNIQUE INDEX ux_weekly_questions_message ON weekly_questions(message_id)
  WHERE message_id IS NOT NULL;

-- ボタンの答え。追記だけで、押し直しは新しい行にして最新を採る(signals と同じ。誤タップを直せて、
-- 履歴も残る)。yes = そう / no = ちがう / unsure = どちらとも
CREATE TABLE weekly_answers (
  id           INTEGER PRIMARY KEY,
  question_id  INTEGER NOT NULL REFERENCES weekly_questions(id),
  value        TEXT    NOT NULL CHECK (value IN ('yes','no','unsure')),
  created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
) STRICT;
CREATE INDEX ix_weekly_answers_question ON weekly_answers(question_id, created_at);

-- 問いへのリプライ(補足)。同じリプライの再配達は (chat_id, message_id) で 1 回だけ記録する(item_notes と同じ。
-- GH#193)。本人の文章なので、次の週の材料に入れる
CREATE TABLE weekly_question_notes (
  id           INTEGER PRIMARY KEY,
  question_id  INTEGER NOT NULL REFERENCES weekly_questions(id),
  note         TEXT    NOT NULL,
  chat_id      INTEGER,
  message_id   INTEGER,
  created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
) STRICT;
CREATE UNIQUE INDEX ux_weekly_question_notes_message
  ON weekly_question_notes(chat_id, message_id);
