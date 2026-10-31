-- GH#340: collect・extract の段の上限で打ち切り、残りを次の実行へ回した記録(設計 02 §3・05 §8)。
-- 1 回の打ち切り = 1 行。週 1 回のソースの健康診断(GH#150)が、直近 7 日に打ち切った日を知らせる。
--   kind      rss_items    = RSS の 1 ソース 1 回の件数の上限([collect].rss_max_items)を超えた
--             collect_time = collect の段の時間の上限([collect].time_budget_s)に達した
--             extract_time = 本文抽出の段の時間の上限([extract].time_budget_s)に達した
--   source_id rss_items のときの対象のソース(段の打ち切りは NULL)
--   deferred  次の実行へ回した数(rss_items・extract_time = 記事、collect_time = ソース)
--   run_id    朝バッチの ULID(単独の intel collect / extract は NULL)
CREATE TABLE budget_cuts (
  id         INTEGER PRIMARY KEY,
  created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  run_id     TEXT,
  kind       TEXT    NOT NULL CHECK (kind IN ('rss_items', 'collect_time', 'extract_time')),
  source_id  INTEGER REFERENCES sources(id),
  deferred   INTEGER NOT NULL CHECK (deferred > 0),
  CHECK ((kind = 'rss_items') = (source_id IS NOT NULL))
) STRICT;

CREATE INDEX ix_budget_cuts_created ON budget_cuts(created_at);
