-- GH#153: 深掘りの解説ノートを別系統の AI(Claude API)がレビューした結果(設計 06 §4)。
--   review_status   NULL = レビューしていない(無効・この migration より前の行)
--                   'clean' = 指摘なし / 'revised' = 指摘あり → 書き手が 1 回直した /
--                   'revise_failed' = 指摘あり・直すのに失敗(本文は直す前のまま)/
--                   'failed' = レビュー自体が失敗(未レビュー)/ 'skipped' = 月の上限額に達して省いた
--   review_findings 指摘の JSON 配列(clean は空配列。failed・skipped は NULL)。
--                   公開後も残す(1〜2 週間回して続けるかを決める材料)
--   review_cost_usd レビューの API 費用の見積もり(やり直しの分を含む。doctor #9 の当月コストに入る)
ALTER TABLE dig_results ADD COLUMN review_status TEXT
  CHECK (review_status IN ('clean', 'revised', 'revise_failed', 'failed', 'skipped'));
ALTER TABLE dig_results ADD COLUMN review_findings TEXT;
ALTER TABLE dig_results ADD COLUMN review_cost_usd REAL NOT NULL DEFAULT 0;
