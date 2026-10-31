-- GH#349: 深掘りの候補が 1 日の上限に足りない日は、その日の digest の上位から埋める(設計 06 §3 の ③)。
-- ③ で掘った item は、生成物を dig_results へ write-ahead するのと同じトランザクションで
-- source = 'digest'・status = 'done' の行を足す(後で X のブックマークが付いても自動キューが
-- 掘り直さない・③ の除外を「キューに行があるか」で済ませる・夜の一覧と調べ直しの依頼を
-- 他の行と同じ規則で扱うため)。source の CHECK(migration 009 の 'bookmark' / 'request')を
-- 広げるには表の作り直しが要る(SQLite)。dig_queue を参照する表・トリガ・ビューは無い。
-- 列・既定値・外部キー(items へ ON DELETE CASCADE)・UNIQUE(item_id)・索引は 009・022 のまま。
CREATE TABLE dig_queue_new (
  id            INTEGER PRIMARY KEY,
  item_id       INTEGER NOT NULL UNIQUE REFERENCES items(id) ON DELETE CASCADE,
  status        TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'passed', 'done')),
  -- 初回投入の経路で以後不変: 'bookmark' = 新規 🔖 の自動投入 / 'request' = リプライ指定で
  -- 新規作成 / 'digest' = その日の digest の上位から選んで掘った(GH#349)
  source        TEXT NOT NULL CHECK (source IN ('bookmark', 'request', 'digest')),
  angle         TEXT,
  requested_at  TEXT,
  enqueued_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  backlog_op_question_id INTEGER  -- migration 022(GH#235)
) STRICT;

INSERT INTO dig_queue_new
  (id, item_id, status, source, angle, requested_at, enqueued_at, updated_at,
   backlog_op_question_id)
SELECT id, item_id, status, source, angle, requested_at, enqueued_at, updated_at,
       backlog_op_question_id
FROM dig_queue;

DROP TABLE dig_queue;
ALTER TABLE dig_queue_new RENAME TO dig_queue;

CREATE INDEX ix_dig_queue_status ON dig_queue(status);
