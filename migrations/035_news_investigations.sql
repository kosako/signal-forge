-- 出来事の調査(GH#415)。朝のダイジェストの「📰 今日の IT ニュース」の出来事 1 つを、報じた記事を
-- まとめて材料にして調べる(設計 06 §9)。深掘り(dig_queue・dig_results)は記事 1 本が単位で、同じ日に
-- 2 回掘らない規則やノートの名前が記事に結び付くので、混ぜずに別の表に持つ。
-- 1 つの digest の同じ見出しの出来事は 1 回だけ調べる(UNIQUE。続けて押しても 2 つ作らない)。
-- 状態: requested(依頼を受けた)→ running(作っている)→ done(ノートとカードを作った)/ failed。
-- 公開と送信は done の行を published_at・message_id で進める(深掘りのカードと同じ流れ)。
CREATE TABLE news_investigations (
  id             INTEGER PRIMARY KEY,
  digest_id      INTEGER NOT NULL REFERENCES digests(id),
  event_index    INTEGER NOT NULL CHECK (event_index >= 1),       -- 依頼したときの出来事の番号
  label          TEXT    NOT NULL,                                -- 出来事の見出し(依頼したときの写し)
  summary        TEXT    NOT NULL,                                -- 出来事の要約(同上)
  item_ids       TEXT    NOT NULL CHECK (json_valid(item_ids)),   -- 手元の材料(出来事を報じた items.id)
  status         TEXT    NOT NULL DEFAULT 'requested'
                 CHECK (status IN ('requested', 'running', 'done', 'failed')),
  requested_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  started_at     TEXT,
  finished_at    TEXT,
  failure        TEXT,           -- 失敗の種類(例外の型名など。外部由来の本文は残さない)
  note_name      TEXT,
  note_markdown  TEXT,           -- 公開するまでの本文(公開したら NULL。恒久の置き場はノート)
  card_text      TEXT,
  published_at   TEXT,
  message_id     INTEGER,
  sent_at        TEXT,
  UNIQUE (digest_id, label)
) STRICT;

CREATE INDEX ix_news_investigations_status ON news_investigations (status);
