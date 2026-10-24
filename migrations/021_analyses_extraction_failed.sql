-- GH#240: 本文の抽出に失敗した記事の分析を、本文を読めた分析と見分けられるようにする(設計 01 §3 /
-- 03 §2 / 04 §2・§7)。
-- 抽出に失敗した記事(documents.extraction_status='failed'、または documents 行が無い)は、本文なしで
-- 見出しとメタ情報だけを LLM に渡す。LLM が答えれば正常な分析(degraded=0)として保存されるので、
-- 「本文を読めていない」ことは degraded と別の印で持つ(degraded は LLM の失敗によるタイトルのみの記録で、
-- 全滅の判定と 🎯 の行の省略に使う。意味を混ぜない)。
-- 1 = 本文なしで LLM に渡した(degraded の行にも入力どおりに入れる)。documents は TTL で消えるので、
-- 分析の行に残す。021 より前の行は 0(記録なし)。
ALTER TABLE analyses ADD COLUMN extraction_failed INTEGER NOT NULL DEFAULT 0
  CHECK (extraction_failed IN (0,1));
