-- 出来事の調査(GH#415 の 3 本目)の X の検索の費用の台帳。この機能の月の上限
-- ([investigate].x_monthly_cap_usd。UTC の暦月)と doctor #9 の当月の費用に数える(ADR 0006)。
-- 検索を送るたびに 1 行(失敗も)。読取の数は課金されうる数で、分からないときは多めに数える。
-- 送る前に上限の件数で pending の行を書き(予約)、応答の後に ok / failed と実際の数に直す。送った後に
-- 実行が止まっても、pending の行が上限の件数のまま残って月の合計に数えられる(上限を超えない)。
-- searched_at は予約の時刻で、応答の後に応答を受けた時刻に直す(予約と送信の間に月が変わったら、
-- 送った後の月に数える)。上限の判定では、前の月の pending の行も今の月に数える。
-- 検索の語は外部由来の題から LLM が作ったもの(検査済み)。どの語で読んだかを見返すために残す。
CREATE TABLE investigation_x_reads (
  id                  INTEGER PRIMARY KEY,
  investigation_id    INTEGER NOT NULL REFERENCES news_investigations(id),
  searched_at         TEXT    NOT NULL,                         -- UTC の ISO 8601(上の説明)
  query               TEXT    NOT NULL,
  status              TEXT    NOT NULL CHECK (status IN ('pending', 'ok', 'failed')),
  api_reads           INTEGER NOT NULL CHECK (api_reads >= 0),
  estimated_cost_usd  REAL    NOT NULL CHECK (estimated_cost_usd >= 0)
) STRICT;

CREATE INDEX ix_investigation_x_reads_searched_at ON investigation_x_reads (searched_at);
