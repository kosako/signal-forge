-- 今の重点で X の検索の枠を 1 本入れ替える案(GH#433 の 6 本目・ADR 0001 の追記・設計 02 §2・03 §4.1)。
--
-- 話題が今の重点に入ると、受信デーモンが朝のダイジェストの後に案を 1 通送る。人が「N と入れ替える」を
-- 押したときだけ、朝の収集が config の [x_search].slots の N 本目を、この行の query に差し替えて使う。
-- 話題が重点から外れたら(🎯 外すを含む)差し替えをやめ、朝の focus 段が reverted にする。
--
-- activated_at は案を出した重点の入り(focus_topics.activated_at の写し)。同じ入りには 1 回だけ聞き、
-- 外れてまた入ったら聞き直す。query は話題の検索の語(外部の本文から LLM が作った語)から作り、
-- config の枠と同じ検査を通したもの。人が見て承認するのはこの文で、収集はこの文を送る。
-- shown_slots は案を送ったときに並べた config の枠の写し(JSON の配列)。承認のときに今の枠と
-- 食い違えば承認せず、今の枠で案を送り直す(message_id を空にする)。
-- slot と replaced_query は承認したときの写し(どの枠の、どの文と入れ替えたか)。config の枠の文が
-- 変わっていたら差し替えない。status は proposed(案を作った・送った)・approved(入れ替え中)・
-- declined(入れない)・expired(答える前に重点から外れた)・reverted(元に戻した)。
CREATE TABLE focus_x_slots (
  id              INTEGER PRIMARY KEY,
  topic_id        INTEGER NOT NULL REFERENCES focus_topics(id),
  activated_at    TEXT    NOT NULL,
  query           TEXT    NOT NULL,
  status          TEXT    NOT NULL DEFAULT 'proposed'
                  CHECK (status IN ('proposed', 'approved', 'declined', 'expired', 'reverted')),
  slot            INTEGER CHECK (slot BETWEEN 1 AND 3),
  replaced_query  TEXT,
  message_id      INTEGER,
  shown_slots     TEXT    CHECK (shown_slots IS NULL OR json_valid(shown_slots)),
  created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  sent_at         TEXT,
  decided_at      TEXT,
  reverted_at     TEXT,
  UNIQUE (topic_id, activated_at),
  CHECK ((status IN ('approved', 'reverted')) = (slot IS NOT NULL AND replaced_query IS NOT NULL)),
  CHECK ((status = 'reverted') = (reverted_at IS NOT NULL)),
  CHECK ((message_id IS NULL) = (shown_slots IS NULL))
) STRICT;
CREATE UNIQUE INDEX ux_focus_x_slots_message ON focus_x_slots(message_id)
  WHERE message_id IS NOT NULL;
-- 入れ替え中は同時に 1 本だけ(ADR 0001 の 3 本の枠のうち 1 本)
CREATE UNIQUE INDEX ux_focus_x_slots_approved ON focus_x_slots(status) WHERE status = 'approved';
-- 答えを待つ案も同時に 1 つだけ
CREATE UNIQUE INDEX ux_focus_x_slots_proposed ON focus_x_slots(status) WHERE status = 'proposed';
