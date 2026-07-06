-- 設計 01 §3 の DDL(正本: docs/design/01-data-model.md)。
-- トランザクション制御はランナー側(app/db.py。BEGIN IMMEDIATE / COMMIT)。

CREATE TABLE sources (
  id              INTEGER PRIMARY KEY,
  type            TEXT    NOT NULL CHECK (type IN ('x','rss','manual')),
  name            TEXT    NOT NULL UNIQUE,
  config          TEXT    CHECK (config IS NULL OR json_valid(config)),   -- feed URL 等
  cursor          TEXT    CHECK (cursor IS NULL OR json_valid(cursor)),   -- since_id / etag / modified
  enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
  last_success_at TEXT,
  last_error_at   TEXT,
  last_error      TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,  -- collect 成功で 0 リセット、失敗で +1(設計 02)
  created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
) STRICT;

CREATE TABLE items (
  id             INTEGER PRIMARY KEY,
  source_id      INTEGER NOT NULL REFERENCES sources(id),
  external_id    TEXT,                    -- X: tweet id / RSS: entry guid / manual: NULL
  url            TEXT    NOT NULL,        -- 取得時の生URL(表示・再訪用)
  canonical_url  TEXT    NOT NULL,        -- 設計 01 §4 の規則で正規化。配信・ノートにはこちらのみ
  title          TEXT,
  author         TEXT,
  published_at   TEXT,
  fetched_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  raw_payload    TEXT    CHECK (raw_payload IS NULL OR json_valid(raw_payload)),
                                           -- JSON 形・サイズ上限・purge は設計 01 §6
  content_hash   TEXT    NOT NULL,        -- 設計 01 §5 の規則
  status         TEXT    NOT NULL DEFAULT 'new'
                 CHECK (status IN ('new','skipped','analyzed','digested','duplicate','error')),
  skip_reason    TEXT,                     -- status='skipped' 時のみ。事前選別の規則名(設計 03 §2)
  dup_of_item_id INTEGER REFERENCES items(id)
) STRICT;
CREATE UNIQUE INDEX ux_items_canonical ON items(canonical_url);
CREATE UNIQUE INDEX ux_items_src_ext   ON items(source_id, external_id)
  WHERE external_id IS NOT NULL;
CREATE INDEX ix_items_hash_time ON items(content_hash, fetched_at);  -- 24h dedup 走査用
CREATE INDEX ix_items_status    ON items(status);

CREATE TABLE documents (
  id                INTEGER PRIMARY KEY,
  item_id           INTEGER NOT NULL UNIQUE REFERENCES items(id) ON DELETE CASCADE,
  extracted_text    TEXT,
  language          TEXT,
  extraction_status TEXT NOT NULL CHECK (extraction_status IN ('ok','partial','failed')),
  extraction_error  TEXT,
  extracted_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  expires_at        TEXT NOT NULL            -- 設計 01 §6: 挿入時に TTL から確定
) STRICT;
CREATE INDEX ix_documents_expires ON documents(expires_at);

CREATE TABLE analyses (
  id                    INTEGER PRIMARY KEY,
  item_id               INTEGER NOT NULL REFERENCES items(id),
  run_id                TEXT,               -- 朝バッチの ULID(設計 05 §3。ログ⇔DB 突合用)
  summary               TEXT    NOT NULL,   -- 日本語要約(恒久保存の正本)
  category              TEXT    NOT NULL,   -- 主カテゴリ1つ。週次ピボット用
  topic                 TEXT,               -- 重複判定用の正規化トピックラベル(設計 03 §3.3)
  relevance_score       REAL    NOT NULL CHECK (relevance_score BETWEEN 0 AND 100),
  novelty               TEXT    NOT NULL CHECK (novelty IN ('new','update','known')),
  negative_labels       TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(negative_labels)),
  total_score           REAL    CHECK (total_score IS NULL OR total_score BETWEEN 0 AND 100),
                                            -- 生産者は app/ranking/。analyze 段の INSERT 時は NULL
  recommendation_reason TEXT    NOT NULL,   -- 推薦理由(PROPOSAL の必須出力)
  implication           TEXT    NOT NULL,   -- 「自分への示唆」(同上)
  degraded              INTEGER NOT NULL DEFAULT 0 CHECK (degraded IN (0,1)),
  engine                TEXT    NOT NULL CHECK (engine IN ('codex-cli','claude-cli','anthropic-api')),
  model                 TEXT    NOT NULL,
  prompt_version        TEXT    NOT NULL,   -- prompts/ のファイル名に対応
  input_hash            TEXT    NOT NULL,   -- 実際にLLMへ渡した入力の sha256
  input_tokens          INTEGER,
  output_tokens         INTEGER,
  estimated_cost_usd    REAL    NOT NULL DEFAULT 0,  -- サブスク枠実行時は 0
  analyzed_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
) STRICT;
CREATE UNIQUE INDEX ux_analyses_idem ON analyses(item_id, prompt_version, input_hash);
CREATE INDEX ix_analyses_item ON analyses(item_id, analyzed_at);

CREATE TABLE signals (
  id          INTEGER PRIMARY KEY,
  kind        TEXT    NOT NULL CHECK (kind IN
                ('reaction','survey_patrol','survey_read_min','followup_dig')),
  item_id     INTEGER REFERENCES items(id),
  value       TEXT    NOT NULL,
  signal_date TEXT    NOT NULL,             -- JST 'YYYY-MM-DD'。帰属規則は設計 01 §7
  created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  CHECK ((kind IN ('reaction','followup_dig')) = (item_id IS NOT NULL)),
  CHECK (kind <> 'reaction'      OR value IN ('useful','dig','known','noise')),
  CHECK (kind <> 'survey_patrol' OR value IN ('yes','no')),
  CHECK (kind <> 'followup_dig'  OR value IN ('yes','no'))
) STRICT;
CREATE INDEX ix_signals_item      ON signals(item_id);
CREATE INDEX ix_signals_kind_date ON signals(kind, signal_date);

CREATE TABLE digests (
  id              INTEGER PRIMARY KEY,
  digest_date     TEXT    NOT NULL UNIQUE,  -- JST。1日1件
  run_id          TEXT,                     -- 朝バッチの ULID(設計 05 §3)
  generated_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  status          TEXT    NOT NULL DEFAULT 'ok' CHECK (status IN ('ok','degraded')),
  markdown        TEXT    NOT NULL,
  item_ids        TEXT    NOT NULL CHECK (json_valid(item_ids)),  -- 配信順の items.id 配列
  item_count      INTEGER NOT NULL,
  delivery_status TEXT    NOT NULL DEFAULT 'pending'
                  CHECK (delivery_status IN ('pending','sent','failed')),
  sent_at         TEXT
) STRICT;

CREATE TABLE collect_runs (
  id                 INTEGER PRIMARY KEY,
  source_id          INTEGER NOT NULL REFERENCES sources(id),
  started_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  finished_at        TEXT,
  status             TEXT    NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running','ok','error')),
  items_fetched      INTEGER NOT NULL DEFAULT 0,
  api_reads          INTEGER NOT NULL DEFAULT 0,   -- X の課金対象読み取り件数
  estimated_cost_usd REAL    NOT NULL DEFAULT 0,   -- api_reads × Day 0 実測単価
  error              TEXT
) STRICT;
CREATE INDEX ix_collect_runs_src ON collect_runs(source_id, started_at);

-- Telegram 受信デーモンの状態(getUpdates offset 等)。単一行 KV
CREATE TABLE telegram_state (
  key        TEXT NOT NULL PRIMARY KEY,     -- 例 'getupdates_offset'
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
) STRICT;

-- 配信済み Telegram メッセージの台帳(送信途中失敗時の部分再送用。設計 04 §2)
CREATE TABLE digest_messages (
  id         INTEGER PRIMARY KEY,
  digest_id  INTEGER NOT NULL REFERENCES digests(id),
  item_id    INTEGER REFERENCES items(id),  -- NULL = ヘッダメッセージ
  message_id INTEGER NOT NULL,              -- Telegram の message_id
  sent_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
) STRICT;
-- SQLite の UNIQUE index は NULL 同士を別値として扱うため、ヘッダ行(item_id IS NULL)は
-- 複合 UNIQUE では一意にならない。部分 UNIQUE index 2本で担保する(設計 01 §3)。
CREATE UNIQUE INDEX ux_digest_messages_item   ON digest_messages(digest_id, item_id)
  WHERE item_id IS NOT NULL;
CREATE UNIQUE INDEX ux_digest_messages_header ON digest_messages(digest_id)
  WHERE item_id IS NULL;

-- manual ソースは1行固定(手動URL投入の親)
INSERT INTO sources (type, name) VALUES ('manual', 'manual');
