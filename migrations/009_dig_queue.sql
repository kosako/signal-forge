-- GH#104: 深掘りキュー(設計 06 §2〜3)— 夜の入口。
-- 新規 🔖 は無条件でキュー入り(source='bookmark')、一覧へのリプライ指定は
-- source='request'。実行(status='done' への遷移)は朝の調査段(GH#105)が担う。
-- パスは深掘りキューからの除外のみで、未消化一覧の表示には影響しない。
CREATE TABLE dig_queue (
  id            INTEGER PRIMARY KEY,
  item_id       INTEGER NOT NULL UNIQUE REFERENCES items(id) ON DELETE CASCADE,
  status        TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'passed', 'done')),
  source        TEXT NOT NULL CHECK (source IN ('bookmark', 'request')),
  -- 観点(interest angle)。リプライの自由文から番号・パス構文を除いた残り全文
  angle         TEXT,
  -- ユーザーが明示指定した時刻。調査段の優先順位①(NULL = 自動キューのみ)
  requested_at  TEXT,
  enqueued_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
) STRICT;

CREATE INDEX ix_dig_queue_status ON dig_queue(status);

-- 未消化 🔖 一覧メッセージの台帳(リプライ先の認識用。owner DM 限定運用のため
-- message_id 単独で一意。digest_messages と同じ前提)
CREATE TABLE dig_backlog_messages (
  message_id  INTEGER PRIMARY KEY,
  sent_date   TEXT NOT NULL
) STRICT;
