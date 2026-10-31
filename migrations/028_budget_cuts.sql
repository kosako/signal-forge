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
-- 同じソースの同じ記事を次の実行で読み直すときは、解決し直さずに既知の判定と正規化に使う。
-- ほかのソースと同じ先の記事は保存で無視されるので、結果を残さないと毎回の解決で段の時間の
-- 上限を使い切り、残りが進まない。使い回しを同じ記事に限るのは、短縮 URL の転送先が後で変わった
-- とき、新しい記事を古い先との重複として捨てないため(新しい記事は解決し直す)。
--   source_id     記事を取ったソース
--   entry_key     記事の識別子。'guid:' + guid、無ければ 'url:' + 元の URL(種別で名前空間を
--                 分ける。URL と同じ文字列の guid の記事が、guid の無い記事の結果を使わない)
--   url           解決の前の短縮 URL(前後の空白は除く)。同じ記事でも URL が変われば解決し直す
--   canonical_url 解決した先を u1 で正規化した URL(items.canonical_url と同じ形)。NULL は確定した
--                 正規化のエラー(解決先を分解できない等。記事は error の行になり、次の実行は
--                 HTTP の前にその行で取り込み済みと分かる)
--   resolved_at   解決した時刻。purge が 30 日より前の行を消す(フィードに残る期間を超えたら要らない)
-- 通信の失敗(一時的)は残さず、次の実行で解決し直す。
CREATE TABLE short_url_resolutions (
  source_id     INTEGER NOT NULL REFERENCES sources(id),
  entry_key     TEXT    NOT NULL,
  url           TEXT    NOT NULL,
  canonical_url TEXT,
  resolved_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  PRIMARY KEY (source_id, entry_key)
) STRICT;

CREATE INDEX ix_short_url_resolutions_resolved ON short_url_resolutions(resolved_at);
