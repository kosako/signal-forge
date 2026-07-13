-- X 興味シグナル(探索: 🔖ブックマーク・❤️いいね・🔁自RT。ADR 0001 / GH#58 S1b)。
-- Telegram 反応(活用)の signals とは出所が違うため分けて記録する(ADR 0001)。
-- tweet_id ベース(ブックマーク等は collect 経路に乗っていない tweet が普通のため
-- items を参照しない)。同じ tweet の再観測は UNIQUE + INSERT OR IGNORE で吸収。
CREATE TABLE x_signals (
  id               INTEGER PRIMARY KEY,
  kind             TEXT NOT NULL CHECK (kind IN ('bookmark','like','self_rt')),
  tweet_id         TEXT NOT NULL,
  author_id        TEXT,
  author_username  TEXT,
  -- 本文抜粋は TTL キャッシュのみ(purge が retention.x_cache_ttl_days で NULL 化。
  -- X 由来本文を恒久保存しない = Issue #2 の確定ポリシー)
  text_excerpt     TEXT,
  tweet_created_at TEXT,
  -- bookmarked_at 等は API が提供しないため、初回観測時刻を直近性の代理にする(GH#58)
  observed_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  run_id           TEXT,
  UNIQUE (kind, tweet_id)
) STRICT;
CREATE INDEX ix_x_signals_kind_observed ON x_signals(kind, observed_at);

-- 取込 run のコスト台帳(collect_runs は sources 行が前提のため独立させる。
-- doctor #9 の当月コスト集計が合算する)
CREATE TABLE x_signal_runs (
  id                 INTEGER PRIMARY KEY,
  started_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  status             TEXT NOT NULL CHECK (status IN ('ok','failed')),
  api_reads          INTEGER NOT NULL DEFAULT 0,
  estimated_cost_usd REAL    NOT NULL DEFAULT 0.0,
  new_signals        INTEGER NOT NULL DEFAULT 0,
  error              TEXT,
  run_id             TEXT
) STRICT;
