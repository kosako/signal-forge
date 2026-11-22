-- 週の仮説の答えを使う(GH#440 の 2 本目・設計 03 §4.2・04 §3〜4)。
--
-- focus_topic_id は、今の重点ごとにコードが足す問い(「来週も重点のままでいい?」)の話題。❌ ちがう と
-- 答えると、ヘッダの 🎯 外すと同じく話題を外す。LLM が書いた問いには付けない(NULL)。
-- profile_line は、関心が移った(interest_shift)・飽きた(fatigue)の仮説に LLM が添えた、関心
-- プロファイルに足す 1 行の案。外部の本文に由来するので、⭕ そう と答えた後に案を送り、人が承認した
-- ときだけ足す。interest_shift は「↑ 上げる」、fatigue は「↓ 下げる」の節に入る。
ALTER TABLE weekly_questions ADD COLUMN focus_topic_id INTEGER REFERENCES focus_topics(id);
ALTER TABLE weekly_questions ADD COLUMN profile_line TEXT;

-- 関心プロファイルに足す案(問いごとに 1 つ)。⭕ そう を記録したときに行を作って送る。message_id は
-- 送った案のメッセージ(NULL = まだ送れていない。次に ⭕ そう を押したときに送り直す)。status は
-- proposed(承認待ち)・applied(足した)・declined(やめた)。section と line は送ったときの写しで、
-- 承認したときはこの写しを足す(問いの行を後から読み直さない)
CREATE TABLE weekly_profile_proposals (
  id           INTEGER PRIMARY KEY,
  question_id  INTEGER NOT NULL UNIQUE REFERENCES weekly_questions(id),
  section      TEXT    NOT NULL CHECK (section IN ('up', 'down')),
  line         TEXT    NOT NULL,
  status       TEXT    NOT NULL DEFAULT 'proposed'
               CHECK (status IN ('proposed', 'applied', 'declined')),
  message_id   INTEGER,
  created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  sent_at      TEXT,
  decided_at   TEXT,
  CHECK ((status = 'proposed') = (decided_at IS NULL))
) STRICT;
CREATE UNIQUE INDEX ux_weekly_profile_proposals_message ON weekly_profile_proposals(message_id)
  WHERE message_id IS NOT NULL;
