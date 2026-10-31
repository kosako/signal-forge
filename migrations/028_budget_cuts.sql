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

-- 短縮 URL の解決の結果(GH#340)。collect が短縮 URL(u1 手順1。設計 01 §4)を解決したら残し、
-- 次の実行では解決し直さずに既知の判定と正規化に使う。ほかのソースと同じ先の記事は保存で
-- 無視されるので、結果を残さないと毎回の解決で段の時間の上限を使い切り、残りが進まない。
--   url           解決の前の短縮 URL(記事の元の URL。前後の空白は除く)
--   canonical_url 解決した先を u1 で正規化した URL(items.canonical_url と同じ形)
--   resolved_at   解決した時刻。purge が 30 日より前の行を消す(フィードに残る期間を超えたら要らない)
-- 解決できなかった URL(通信の失敗・解決先が分解できない)は残さない。
CREATE TABLE short_url_resolutions (
  url           TEXT    NOT NULL PRIMARY KEY,
  canonical_url TEXT    NOT NULL,
  resolved_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
) STRICT;

CREATE INDEX ix_short_url_resolutions_resolved ON short_url_resolutions(resolved_at);
