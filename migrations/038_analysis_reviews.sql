-- 分析の確かめ(GH#423・設計 03 §3.6)の台帳。Codex が書いた分析を Claude API が確かめ、直すときは
-- 直した分析を analyses に新しい行として足す(item の最新の分析になり、ランキング・digest・トレンドが
-- そのまま使う)。Codex の行は書き換えずに残す。
--
-- analysis_review_calls は Claude API の呼び出しごとに 1 行(失敗も)。この機能の月の上限
-- ([analysis_review].monthly_cap_usd。UTC の暦月)と doctor #9 の当月の費用に数える。額は応答の
-- usage の実額で、応答の usage を受け取れず費用が分からない呼び出しはトークン数を空にし、課金されうる
-- 最大の額を積む(llm_fallback_calls と同じ。GH#241)。error_kind は失敗の種類だけで、応答の本文は
-- 書かない。
CREATE TABLE analysis_review_calls (
  id             INTEGER PRIMARY KEY,
  created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  run_id         TEXT,
  model          TEXT    NOT NULL,
  ok             INTEGER NOT NULL CHECK (ok IN (0, 1)),
  error_kind     TEXT,                                -- ok = 0 のときの失敗の種類(本文を含まない)
  item_count     INTEGER NOT NULL CHECK (item_count > 0),  -- 渡した分析の数
  input_tokens   INTEGER,
  output_tokens  INTEGER,
  cost_usd       REAL    NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
  CHECK ((ok = 1) = (error_kind IS NULL))
) STRICT;

CREATE INDEX ix_analysis_review_calls_created_at ON analysis_review_calls (created_at);

-- 確かめた分析ごとに 1 行。analysis_id は確かめた Codex の分析(1 つの分析は 1 回だけ確かめる)。
-- fixed のときは足した行を fixed_analysis_id に持ち、どの項目が変わったか(ItemAnalysis の名前)を
-- changed_fields に残す(直した数と項目を後から数えるため)。note は Claude が書いた直した理由の 1 文
-- (外部の記事に由来しうる文。配信・ノートには出さない)。
CREATE TABLE analysis_reviews (
  id                 INTEGER PRIMARY KEY,
  analysis_id        INTEGER NOT NULL UNIQUE REFERENCES analyses(id),
  item_id            INTEGER NOT NULL REFERENCES items(id),
  call_id            INTEGER NOT NULL REFERENCES analysis_review_calls(id),
  verdict            TEXT    NOT NULL CHECK (verdict IN ('ok', 'fixed')),
  fixed_analysis_id  INTEGER UNIQUE REFERENCES analyses(id),
  changed_fields     TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(changed_fields)),
  note               TEXT    NOT NULL DEFAULT '',
  created_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  CHECK ((verdict = 'fixed') = (fixed_analysis_id IS NOT NULL))
) STRICT;

CREATE INDEX ix_analysis_reviews_item ON analysis_reviews (item_id);
