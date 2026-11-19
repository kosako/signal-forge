-- 今の重点(GH#433・設計 03 §4.1)の台帳。直近の興味シグナルを話題に振り分け、話題ごとの重みで
-- 「今の重点」を決める。重みは focus_evidence から毎回計算する(種類の重み × 新しさの減衰)。
--
-- focus_topics は話題ごとに 1 行。name と terms は LLM が外部の本文(X の投稿など)から作った文なので、
-- 表示・prompt・検索では untrusted として扱う。status は重みで出入りし(candidate ⇄ active)、
-- dismissed は人が外した話題(2 本目で入れる。重みが上がっても戻さない)。
CREATE TABLE focus_topics (
  id              INTEGER PRIMARY KEY,
  name            TEXT    NOT NULL UNIQUE,
  terms           TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(terms)),  -- 検索の語(最大 3)
  status          TEXT    NOT NULL DEFAULT 'candidate'
                  CHECK (status IN ('candidate', 'active', 'dismissed')),
  created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  activated_at    TEXT,   -- 最後に重点に入った時刻
  deactivated_at  TEXT,   -- 最後に重点から外れた時刻
  dismissed_at    TEXT,
  CHECK ((status = 'dismissed') = (dismissed_at IS NOT NULL))
) STRICT;

-- 振り分けたシグナルごとに 1 行。signal_kind と signal_ref で元の行(x_signals・signals・
-- item_notes・news_investigations の id)を指し、同じシグナルは 1 回だけ振り分ける。どの話題にも
-- 当てはまらなかったシグナルも topic_id を空にして残す(次の実行で振り分け直さない)。
-- subject はシグナルが向いた先を、寄せる前の形で持つ(X のシグナルは 'tweet:<tweet id>'、反応と感想は
-- 'item:<items.id>'、調査は 'investigation:<id>')。重みは、重みの計算のたびに同じ投稿の記事へ寄せて、
-- 記事・投稿ごとに一番強いシグナル 1 つだけを数える(1 本の記事への 👍・🔖・感想の重なりで、その
-- 記事だけの話題が重点に入らないように。寄せ方は app/focus.py の _subject_sql)。
-- observed_at は元のシグナルの時刻(新しさの減衰に使う。元の行が消えても重みを計算できるよう写す)。
CREATE TABLE focus_evidence (
  id           INTEGER PRIMARY KEY,
  signal_kind  TEXT    NOT NULL CHECK (signal_kind IN (
                 'x_bookmark', 'x_like', 'x_self_rt', 'tg_useful', 'tg_dig', 'note',
                 'investigation')),
  signal_ref   INTEGER NOT NULL,
  topic_id     INTEGER REFERENCES focus_topics(id),
  subject      TEXT    NOT NULL,
  observed_at  TEXT    NOT NULL,
  assigned_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  run_id       TEXT,
  UNIQUE (signal_kind, signal_ref)
) STRICT;

CREATE INDEX ix_focus_evidence_topic ON focus_evidence (topic_id, observed_at);

-- 投稿を記事に寄せるとき(app/focus.py の _tweet_item_sql)に、同じ投稿のシグナルを tweet_id で引く。
-- 既存の UNIQUE (kind, tweet_id) は kind が先頭なので、tweet_id だけの検索に使えない
CREATE INDEX ix_x_signals_tweet ON x_signals (tweet_id);

-- X の投稿として取り込んだ記事を、投稿の id(items.external_id)で引く(同じ _tweet_item_sql と
-- _subject_sql)。既存の ux_items_src_ext は source_id が先頭なので、external_id だけの検索に使えない
CREATE INDEX ix_items_external_id ON items (external_id) WHERE external_id IS NOT NULL;
