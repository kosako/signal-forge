# 設計 01: データモデル(SQLite 正本)

ステータス: v1(2026-07-05)/ 上位文書: [../PROPOSAL.md](../PROPOSAL.md)(齟齬時は PROPOSAL が優先)/ 対象読者: 実装者(本人)

## 1. 前提と共通規約

- DB は SQLite 単一ファイル(`~/.local/share/intel/intel.db` を既定、config.toml で変更可)。ORM は使わず `sqlite3` + 薄いリポジトリ層。
- **全テーブル STRICT**。理由: 型の暗黙変換によるサイレント破損を防ぐ(SQLite 3.37+ 必須。Python 3.12 同梱で充足)。
- 接続時 PRAGMA(全プロセス共通、`app/db.py` の接続ファクトリで一元設定):
  - `journal_mode=WAL` / `busy_timeout=5000` / `foreign_keys=ON` / `synchronous=NORMAL`
  - 理由: 朝バッチと受信デーモンの**2プロセスが同一 DB に並行書き込み**するため、WAL + busy_timeout が必須。
- 時刻の規約: **タイムスタンプ列はすべて UTC の ISO 8601 TEXT**(`YYYY-MM-DDTHH:MM:SSZ`、`strftime('%Y-%m-%dT%H:%M:%SZ','now')`)。**暦日列(`digest_date` / `signal_date`)のみ JST の `YYYY-MM-DD`**。理由: KPI の「日」は生活時間(JST)で判定するが、比較・演算は UTC が安全なため役割で分ける。
- JSON を持つ TEXT 列は `json_valid()` CHECK を付ける。理由: 壊れた JSON の混入を書き込み時点で弾く。

## 2. ER 図(text)

```text
sources 1 ──< collect_runs                     (コスト・実行ログ)
sources 1 ──< items ──1 documents              (本文キャッシュ、TTL付き)
                │ ├──< analyses                (LLM分析。再分析で複数行)
                │ ├──< signals                 (kind='reaction'/'followup_dig' は item_id 必須)
                │ ├──< item_notes              (感想 = Telegram リプライ。恒久保存。GH#92)
                │ └── dup_of_item_id ──> items (自己参照: 24h dedup の親)
signals(kind='survey_*') … item_id NULL(日単位の回答)
digests ──(item_ids JSON)──> items             (結合表なし。json_each で展開)
digests 1 ──< digest_messages >── items        (送信済み Telegram message_id。部分再送用)
digests 1 ──< digest_notes                     (日次感想 = ヘッダへのリプライ。GH#96)
telegram_state                                 (getUpdates offset 等の単一行KV)
schema_migrations                              (マイグレーション台帳。ランナーが管理)
```

digests↔items に KPI 用の結合表を作らない理由: 参照は表示と手動 KPI 集計のみで、`json_each(digests.item_ids)` で足りる(結合表はフェーズ2の拡張点)。`digest_messages` は KPI 用でなく配信の部分再送判定専用の台帳(設計 04 §2)。

## 3. DDL(migrations/001_init.sql)

```sql
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
  consecutive_failures INTEGER NOT NULL DEFAULT 0,  -- collect 成功で 0 リセット、失敗で +1(死活閾値の判定材料。設計 02)
  created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
) STRICT;

CREATE TABLE items (
  id             INTEGER PRIMARY KEY,
  source_id      INTEGER NOT NULL REFERENCES sources(id),
  external_id    TEXT,                    -- X: tweet id / RSS: entry guid / manual: NULL
  url            TEXT    NOT NULL,        -- 取得時の生URL(表示・再訪用)
  canonical_url  TEXT    NOT NULL,        -- §4 の規則で正規化。配信・ノートにはこちらのみ使う
  title          TEXT,
  author         TEXT,
  published_at   TEXT,
  fetched_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  raw_payload    TEXT    CHECK (raw_payload IS NULL OR json_valid(raw_payload)),
                                           -- JSON 形・サイズ上限・purge は §6 参照

  content_hash   TEXT    NOT NULL,        -- §5 の規則
  status         TEXT    NOT NULL DEFAULT 'new'
                 CHECK (status IN ('new','skipped','analyzed','digested','duplicate','error')),
  skip_reason    TEXT,                     -- status='skipped' 時のみ。事前選別の規則名(設計 03 §2)。合成値を status に入れない
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
  expires_at        TEXT NOT NULL            -- §6: 挿入時に TTL から確定
) STRICT;
CREATE INDEX ix_documents_expires ON documents(expires_at);

CREATE TABLE analyses (
  id                    INTEGER PRIMARY KEY,
  item_id               INTEGER NOT NULL REFERENCES items(id),
  run_id                TEXT,               -- 朝バッチの ULID(設計 05 §3。ログ⇔DB 突合用)
  summary               TEXT    NOT NULL,   -- 日本語要約(恒久保存の正本)
  category              TEXT    NOT NULL,   -- 主カテゴリ1つ。週次ピボット(ソース別・カテゴリ別 👍∨🔖率)用
  topic                 TEXT,               -- 重複判定用の正規化トピックラベル(設計 03 §3.3。degraded 時は NULL 可)
  relevance_score       REAL    NOT NULL CHECK (relevance_score BETWEEN 0 AND 100),
  novelty               TEXT    NOT NULL CHECK (novelty IN ('new','update','known')),  -- 設計 03 §3.3 の enum をそのまま保存
  negative_labels       TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(negative_labels)),  -- 03 の negative 配列。ペナルティ計算は ranking/
  total_score           REAL    CHECK (total_score IS NULL OR total_score BETWEEN 0 AND 100),
                                            -- 生産者は app/ranking/(式 v1、設計 03 §6)。analyze 段の INSERT 時は NULL、
                                            -- digest 直前に決定的に算出して UPDATE で書き戻す
  recommendation_reason TEXT    NOT NULL,   -- 推薦理由(PROPOSAL の必須出力)
  implication           TEXT    NOT NULL,   -- 「自分への示唆」(同上)
  degraded              INTEGER NOT NULL DEFAULT 0 CHECK (degraded IN (0,1)),  -- タイトルのみ分析
  engine                TEXT    NOT NULL CHECK (engine IN ('codex-cli','claude-cli','anthropic-api')),
  model                 TEXT    NOT NULL,
  prompt_version        TEXT    NOT NULL,   -- prompts/ のファイル名に対応(例 'rank-v3')
  input_hash            TEXT    NOT NULL,   -- 実際にLLMへ渡した入力の sha256
  input_tokens          INTEGER,
  output_tokens         INTEGER,
  estimated_cost_usd    REAL    NOT NULL DEFAULT 0,  -- サブスク枠実行時は 0(クォータは doctor で別掲)
  analyzed_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  title_ja              TEXT                          -- 日本語見出し(002 で追加のため末尾列。
                                                      -- NULL = v001 世代 / degraded。表示側は
                                                      -- title_ja 優先・無ければ items.title)
) STRICT;
CREATE UNIQUE INDEX ux_analyses_idem ON analyses(item_id, prompt_version, input_hash);
CREATE INDEX ix_analyses_item ON analyses(item_id, analyzed_at);

CREATE TABLE signals (
  id          INTEGER PRIMARY KEY,
  kind        TEXT    NOT NULL CHECK (kind IN
                ('reaction','survey_patrol','survey_read_min','followup_dig')),
  item_id     INTEGER REFERENCES items(id),
  value       TEXT    NOT NULL,
  signal_date TEXT    NOT NULL,             -- JST 'YYYY-MM-DD'。§7 の帰属規則
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
-- 複合 UNIQUE では一意にならず重複挿入できてしまう。部分 UNIQUE index 2本で担保する
-- (message_kind 列の追加案より、列と挿入コードの変更が不要な index 2本の方がシンプル)。
CREATE UNIQUE INDEX ux_digest_messages_item   ON digest_messages(digest_id, item_id)
  WHERE item_id IS NOT NULL;
CREATE UNIQUE INDEX ux_digest_messages_header ON digest_messages(digest_id)
  WHERE item_id IS NULL;

-- 感想(Telegram 配信の記事メッセージへのリプライ。GH#92 / migration 005)。
-- 「その場で書いた自分の take」= 知見の種(ADR 0003)。自分の文章のため Issue #2
-- (X 本文の TTL 制約)の対象外で恒久保存する。正本は本テーブル、Obsidian の item ノート
-- (GENERATED 領域「## 感想」節)は DB の純関数として投影。KPI には接続しない
-- (reaction を自動記帳しない = 測定定義の汚染防止)。
CREATE TABLE item_notes (
  id          INTEGER PRIMARY KEY,
  item_id     INTEGER NOT NULL REFERENCES items(id),
  note        TEXT    NOT NULL,
  created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  exported_at TEXT                          -- Obsidian へ反映済みか(NULL = 未反映)
) STRICT;
CREATE INDEX ix_item_notes_item ON item_notes(item_id);
CREATE INDEX ix_item_notes_pending ON item_notes(item_id) WHERE exported_at IS NULL;

-- 日次感想(ダイジェストのヘッダメッセージへのリプライ。GH#96 / migration 006)。
-- item_notes の相似形。正本は本テーブル、Obsidian の Daily ノート(GENERATED 領域
-- 「## 感想」節)は DB の純関数として投影。KPI・シグナル未接続(活用は後決め)。
CREATE TABLE digest_notes (
  id          INTEGER PRIMARY KEY,
  digest_id   INTEGER NOT NULL REFERENCES digests(id),
  note        TEXT    NOT NULL,
  created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  exported_at TEXT                          -- Obsidian へ反映済みか(NULL = 未反映)
) STRICT;
CREATE INDEX ix_digest_notes_digest ON digest_notes(digest_id);
CREATE INDEX ix_digest_notes_pending ON digest_notes(digest_id) WHERE exported_at IS NULL;

-- manual ソースは1行固定(手動URL投入の親)
INSERT INTO sources (type, name) VALUES ('manual', 'manual');
```

共通語彙6テーブルに加える運用系テーブルは `collect_runs`(X 従量コストと doctor 月次見込みに必要な読み取り件数の時系列)、`telegram_state`(getUpdates offset の永続化)、`digest_messages`(部分再送の判定台帳)、`item_notes`(記事感想。GH#92 / migration 005)、`digest_notes`(日次感想。GH#96 / migration 006)、`dig_queue`(深掘りキュー。GH#104 / migration 009。§7 末尾)、`dig_backlog_messages`(未消化一覧のリプライ先台帳。同)の7つ。認可拒否 update の記録はテーブル化せず poller.log への WARN 1行とする(設計 04 §6。1人運用で照会頻度がほぼゼロのデータにマイグレーションを増やさない)。

### items.status の状態遷移

| status | 意味 | 遷移契機(書き手) |
|---|---|---|
| `new` | 収集直後。手動URL投入も `new` | collect / 受信デーモン(manual) |
| `skipped` | 事前選別で除外。規則名は `skip_reason` に記録 | analyze Stage 1(設計 03 §2) |
| `duplicate` | 24h 内容 dedup の子(§6)。以後の段の対象外 | collect(挿入直後) |
| `analyzed` | LLM 分析済み(degraded 含む) | analyze Stage 2 |
| `digested` | 当日ダイジェストに採用 | digest |
| `error` | scheme 不正 URL(§4)・処理不能 | 各段 |

## 4. canonical_url 正規化規則(u1)

正規化はコード内の定数ルール(バージョン `u1`)として実装し、**挿入前に1回だけ計算、以後不変**。理由: UNIQUE キーの遡及再計算は dedup 衝突と履歴断絶を生むため、ルール改定時は新規挿入分から適用し混在を許容する(`u1|` 等のプレフィックスは付けず、必要になったら列追加で対応)。

適用順:

0. **scheme allowlist(全ソース共通)**: scheme が `http` / `https` 以外の URL(`javascript:` 等)はアイテムごと reject(`status='error'`)。manual 投入経路だけでなく X・RSS 由来にも適用する。
1. 短縮 URL 展開: X は API の `entities.expanded_url` を使う(HTTP 不要)。RSS で `t.co` / `bit.ly` 等の既知短縮ドメインのみ 1 回 GET で最終 URL に解決。それ以外はリダイレクト解決しない。理由: 全 URL の解決はコストと失敗点を増やす。**例外: manual(Telegram 投入。plan #18)は展開しない** — 受信デーモンの外向き HTTP を Telegram API のみに保つ(SSRF 面と失敗点を増やさない)。短縮 URL の手動投入は collect 由来と canonical が割れて重複しうるが、頻度・実害とも小さく許容する。
2. scheme・host を小文字化。デフォルトポート(:80/:443)除去。scheme の書き換え(http→https)はしない。
3. `twitter.com` / `mobile.twitter.com` → `x.com` に統一。X ポストの canonical は `https://x.com/i/web/status/{id}` 形式で組み立てる。理由: screen_name 変更に対して不変。
4. fragment(`#...`)除去。
5. クエリ: トラッキング系パラメータを削除(`utm_*, fbclid, gclid, yclid, igshid, mc_cid, mc_eid, ref, ref_src, s, t`)。残りはキー昇順にソート。
6. path 末尾の `/` を除去(root `/` は除く)。

配信・Obsidian ノートに載せる URL は常に `canonical_url`(LLM 出力の URL は使わない。PROPOSAL §プロンプトインジェクション対策)。

## 5. content_hash 規則(h1)

- 入力テキスト: `title + "\n" + 収集時点で得られる本文`(X: full_text / RSS: summary または content / manual: 空)。抽出後の再計算はしない。理由: ハッシュ可変にすると dedup 判定が時点依存になる。
- **manual は正規化テキストが空になる**(URL のみ投入でタイトル・本文が無い)ため、content_hash は記録するが 24h 内容 dedup の対象外とする(§6。空ハッシュ同士の一致で無関係な別 URL が duplicate 化するのを防ぐ)。
- 正規化: NFKC → 小文字化 → URL 文字列除去(`https?://\S+`。X の t.co 混入対策)→ 連続空白を 1 個に → 前後 trim。
- 値: `sha256:` + hex(sha256(`"h1|"` + 正規化テキスト))。`h1` はルールバージョンで、改定時の新旧混在を識別する。

## 6. 重複排除(2層)と本文キャッシュ TTL

### 恒久・厳密 dedup(冪等性の担保)

- `UNIQUE(canonical_url)` と `UNIQUE(source_id, external_id)` に対し `INSERT OR IGNORE`。collect の再実行・カーソル巻き戻し・同一 URL の再投入は無害に落ちる。

### 24h 内容 dedup(横断シンジケーション対策)

- 挿入成功直後に同一 `content_hash` で `fetched_at` が直近 24 時間内の既存アイテム(status ≠ 'duplicate')を検索し、あれば新しい方を `status='duplicate'`, `dup_of_item_id=既存id` にする。duplicate は extract/analyze/digest の対象外。
- **スキップ条件**: content_hash の正規化テキスト(§5)が**空または 20 字未満**のアイテムは、24h 内容 dedup の親にも子にもしない(manual の URL のみ投入・本文なし RSS entry 等)。manual の挿入直後の重複判定は **canonical_url のみ**で行う(設計 02 §4)。理由: 実質空のハッシュ同士が一致し、別 URL のアイテムまで duplicate 化される誤爆を防ぐ。
- 24h に限る理由: 古い記事の再掲は「再浮上」として分析する価値が残るのと、`(content_hash, fetched_at)` 索引の走査を常に安価に保つため。

### items.raw_payload の JSON 形(collect → extraction の受け渡し)

collect と extraction は DB 経由でのみ受け渡す(設計 00 §3)ため、コネクタが返す `RawItem.raw_content`(RSS の content/summary 等。設計 02 §1)の永続先を本列に定義する:

```json
{"raw_content": "…ソース同梱本文。無ければキー省略…",
 "meta": {"…ソース固有情報(RT種別等)。RawItem.meta をそのまま…"},
 "x": {"…X のみ: API レスポンス該当部(full_text 等)…"}}
```

- **サイズ上限**: `raw_content` は 50,000 字で切り詰めて保存(巨大な全文フィードで DB を肥大化させない。本文の正本は documents 側)。
- **purge 対象**: raw_payload は本文キャッシュと同じ TTL で NULL 化する(下記削除ジョブ 2.)。恒久保存の対象ではない。

### 本文キャッシュ TTL と削除ジョブ

- `documents.expires_at` は**挿入時に確定**する: RSS/manual は `extracted_at + cache_ttl_days`(config、既定 30 日)、X 由来は `x_cache_ttl_days`(**7 日で確定。Day 0 の Developer Agreement 確認(2026-07-06、Issue #2)より: 規約に一律の保存期間上限は存在せず、義務は「X 側の削除・変更との同期」。TTL 7日は削除・非公開化が最長7日で自動的にキャッシュから消える同期実装として妥当**)。理由: purge 側が `expires_at <= now` の比較だけで済み、ソース種別の分岐を持たない。
- **削除リクエストの即時対応(Developer Agreement IV.B)**: X または投稿者から特定コンテンツの削除要求を受けたら **24時間以内**に消す義務がある(TTL では代替できない)。MVP は手動対応で、手順は本節に完結させる: 該当 item の `documents` 行 DELETE + `raw_payload` NULL 化 + Obsidian ノートの削除(X 由来は本文を置かないためメタのみ)。個人用途で発生確率は低く、専用コマンドは作らない。
- 削除ジョブは `intel run` の最終ステップ(単独実行は `intel doctor --purge` 相当のサブコマンドでも可):
  1. `DELETE FROM documents WHERE expires_at <= now`
  2. `raw_payload` の NULL 化は **documents から独立して**行う: `items.fetched_at + TTL <= now` の**全行**(documents の有無・status を問わない。duplicate・skipped・抽出未実行の行も対象)の `raw_payload` を NULL 化。TTL はソース別(X: `x_cache_ttl_days` / RSS・manual: `cache_ttl_days`)。理由: documents.expires_at 起点にすると documents 行を持たない items が purge から漏れる。特に X は full_text の無期限残存が本文非保持ポリシー違反になる(恒久保存は `analyses.summary` + メタ + URL のみ)。TTL 超過残存は doctor #11 が検査する(設計 05 §2)
  3. 削除行と `raw_payload` NULL 化行の合算が 1,000 行を超えた回は `VACUUM`(NULL 化も大きな空きを生むため合算で判定)。理由: auto_vacuum のブートストラップ順序の罠を避け、DB は高々百 MB 級なので都度 VACUUM で十分。

## 7. signals の表現(4ボタン+夜アンケート+追い掛け)

追記専用(UPDATE/DELETE しない)の単一テーブルに `kind × value` で持つ。理由: ボタン種別ごとの列やテーブルを分けるより、KPI のアドホック集計(手動運用)と将来のフィードバックループ入力に単一の時系列が最も扱いやすい。

| kind | value | item_id | signal_date の帰属 |
|---|---|---|---|
| `reaction` | `useful`(👍)/ `dig`(🔖)/ `known`(✅)/ `noise`(👎) | 必須 | そのアイテムを配信した digest_date |
| `survey_patrol` | `yes` / `no`(巡回した?) | NULL | 質問対象日。翌日 12:00 JST までの遅延回答も対象日に帰属 |
| `survey_read_min` | 分数の整数文字列 | NULL | 同上 |
| `followup_dig` | `yes` / `no`(深掘りした?) | 必須(🔖済みアイテム) | 🔖を付けた日。**MVP で送信・記録する**(🔖翌日夜に追い掛け1通。設計 04 §4。フェーズ2境界の「50%深掘り」判定に必要) |

- 同一アイテムへの再タップは行を追加し、**集計は created_at 最新の行を採用**。理由: 上書きせず履歴を残すと、👍校正(遅延スポットチェック)の乖離分析材料になる。
- 受信デーモンは callback の `from.id` を allowlist 照合してから書き込む(認可は設計 04 Telegram の責務。本テーブルは認可済みデータのみ格納)。
### X 興味シグナル(x_signals・migration 004 + 007。GH#58 S1b / ADR 0001 / GH#99)

Telegram 反応(**活用** = 提示済みへの評価)とは出所が違う**探索**シグナル(X 上の自分の行動)を別テーブルで持つ(ADR 0001「探索と活用を区別」)。like / self_rt は tweet_id ベースで items を参照しない(collect 経路に乗っていない tweet が普通)。**bookmark だけは保存アイテムとして items にも登録し、`item_id`(migration 007)で紐付ける**(GH#99。取込時に item 化。外部リンク付きはリンク先を canonical_url に、リンクなしはツイート URL。帰属先は enabled=0 の sources 行 `x:bookmark` = mode "bookmark")。

- `kind`: `bookmark`(🔖・強)/ `like`(❤️・弱)/ `self_rt`(🔁・強。RT 元ツイートの id・著者を記録)
- `item_id`(NULL 可): bookmark の保存アイテム参照(GH#99)。like / self_rt と item 化導入前の既存 bookmark 行は NULL(過去分は `--backfill --kind bookmark` の再読取で遡って紐付く)
- `UNIQUE (kind, tweet_id)` + INSERT OR IGNORE で再観測を吸収(差分検出は「新→旧に読み既知 id 到達で打ち切り」。bookmarks / liked_tweets は since_id 非対応のため)
- `observed_at` = 初回観測時刻。`bookmarked_at` 等を API が提供しないため、**直近性の代理**とする(cold start の --backfill 一括取込分は「直近」として扱えない制約を含む)
- `text_excerpt`(≤280字)は **TTL キャッシュのみ**(purge が `x_cache_ttl_days` で NULL 化。§6 の X 本文非保持ポリシー準拠)。恒久保存は id・著者・時刻
- 取込コストは `x_signal_runs`(collect_runs は sources 行が前提のため独立の台帳)に記録し、doctor #9 が合算する(§9)

- 日次 KPI(👍∨🔖 ≥3)の集計クエリ例(手動):

```sql
WITH latest AS (
  SELECT item_id, value, signal_date,
         row_number() OVER (PARTITION BY item_id, signal_date
                            ORDER BY created_at DESC) AS rn
  FROM signals WHERE kind = 'reaction')
SELECT signal_date,
       sum(value IN ('useful','dig'))                AS positive,
       count(*)                                      AS reacted,
       (SELECT item_count FROM digests d
         WHERE d.digest_date = latest.signal_date)   AS delivered  -- 反応率の分母
FROM latest WHERE rn = 1 GROUP BY signal_date;
```

### 深掘りキュー(dig_queue / dig_backlog_messages・migration 009。GH#104 / 設計 06 §2〜3)

夜の入口の永続化。新規 🔖 の item 化時に無条件で `dig_queue` へ積み(backfill 再読取は対象外)、未消化一覧への自由文リプライ(番号 + 観点 / パス)が upsert する。`source` は**初回投入の経路で以後不変**(自動投入 = `bookmark` / リプライで新規作成 = `request`。明示指定の有無は `requested_at` と `status` で導出する)。朝の調査段(GH#105)が `status='queued'` から日次上限件数を選んで実行し `done` にする。

- `item_id` UNIQUE(1 item 1 行。状態遷移で履歴を持たない — 観点は最新だけあれば調査に足りる)、items へ ON DELETE CASCADE
- `status`: `queued` / `passed`(深掘り対象外。未消化一覧の表示には影響しない)/ `done`。パス済みへの再ブックマークでは復帰させない(明示指示を自動投入が覆さない)。リプライの再指定(request)だけが `queued` へ戻す
- `angle` = 観点の自由文(リプライから番号・パス構文を除いた残り)。`requested_at` = 明示指定時刻(調査段の優先順位① > 新着 🔖 順)
- `dig_backlog_messages` は一覧メッセージの message_id 台帳(リプライ先の認識。owner DM 限定のため message_id 単独で一意 = digest_messages と同じ前提)

## 8. digests

- 1 暦日(JST)1 行、`digest_date` UNIQUE。再生成は同日行を置換(`INSERT OR REPLACE`)ではなく、生成済みなら skip を既定とし `--force` で置換。理由: 朝バッチの多重起動(launchd のまとめ実行)で二重配信しないための冪等キー。
- `item_ids` は配信順の JSON 配列。KPI の分母・ソース別ピボットは `json_each(item_ids)` で items に結合する。
- `bookmark_item_ids`(migration 008。GH#99)は「🔖 保存」節に載せたブックマーク item の JSON 配列(保存順)。ランキング外の must-include 別枠で、`item_ids` とは重ならない。KPI の分母(item_count / json_each(item_ids))には含めない。
- `delivery_status` で watchdog(digest 未生成・未送検知)が判定する。`status='degraded'` は LLM 全滅時のタイトルのみ配信。

## 9. コスト記録

- LLM: `analyses.engine / model / input_tokens / output_tokens / estimated_cost_usd`。サブスク枠(codex-cli / claude-cli)は 0 を記録し、トークン数だけ残す(API 切替時の試算材料。クォータ消費率は doctor が別途表示)。
- X API: `collect_runs.api_reads × 単価(config、Day 0 実測値)` を `estimated_cost_usd` に記録。S1b の興味シグナル取込は `x_signal_runs` に同形で記録(GH#58)。
- `intel doctor` の月次見込み = 当月 `sum(analyses.estimated_cost_usd) + sum(collect_runs.estimated_cost_usd) + sum(x_signal_runs.estimated_cost_usd)` の日割り外挿。

## 10. マイグレーション運用

- `migrations/NNN_name.sql`(3桁ゼロ埋め連番・欠番禁止)。自前ランナー(`app/db.py`)が `schema_migrations(version INTEGER PRIMARY KEY, filename TEXT, applied_at TEXT)` を台帳に、未適用分を番号順に **1 ファイル = 1 トランザクション**(`BEGIN IMMEDIATE`)で適用する。
- CLI 起動時(collect/run 等)に自動適用、`intel doctor` は未適用の有無を表示。理由: 単一ユーザー・単一ホストでは適用忘れによる稼働停止の方が怖い。
- **forward-only(down なし)**。適用前に DB ファイルを `intel.db.bak` にコピーしてから適用。理由: 個人運用ではロールバック SQL の保守より単一ファイルコピー復元が確実。
- 適用済みファイルは編集禁止。修正・変更は必ず新番号のファイルで行う。

## 11. 拡張点(実装しない。必要時にマイグレーション追加)

- FTS5 による全文検索(documents に外部コンテンツテーブル)/ digest_items 結合表 / signals の Obsidian 反映用カラム / ランキング自動調整の特徴量列 / `analyses.entities`。いずれもフェーズ2の判断まで作らない。
