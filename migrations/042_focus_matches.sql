-- 今の重点に当たる記事(GH#433 の 4 本目・設計 03 §4.1・§6)。digest の段が、点を足すと上位に届きうる
-- 候補の見出しと要約を LLM に渡し(prompts/focus-match-v001.md)、今の重点のどれかに当たる記事を選ばせる。
-- ランキングは、当たった記事の点に [focus].score_bonus を足す(点の高い [focus].bonus_items 件まで)。
--
-- digest の暦日(JST)と記事ごとに 1 行。当たらなかった記事は行を作らない。同じ日に digest を作り直す
-- (--force)ときは、その日の行を消して選び直す。選び直しの LLM が失敗したときは、その日の行を使う。
-- reason は当たった理由の一言(LLM が外部の本文から書いた文。配信・ノートには出さない)。
CREATE TABLE focus_matches (
  id              INTEGER PRIMARY KEY,
  digest_date     TEXT    NOT NULL,   -- JST の暦日(この日の digest の順位に使った)
  item_id         INTEGER NOT NULL REFERENCES items(id),
  topic_id        INTEGER NOT NULL REFERENCES focus_topics(id),
  reason          TEXT    NOT NULL DEFAULT '',
  run_id          TEXT,
  prompt_version  TEXT    NOT NULL,
  created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  UNIQUE (digest_date, item_id)
) STRICT;
