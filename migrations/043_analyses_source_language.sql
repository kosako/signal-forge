-- GH#439: 日本語にして出す記事に、元の記事の言語を示す(設計 01 §3 / 03 §3.3 / 04 §2・§7)。
-- 分析の時に、LLM に渡した本文(無ければ見出し)から、コードで言語を決めて残す(app/language.py の
-- detect_language。prompt は変えない)。documents は TTL で消えるので、分析の行に残す。
-- ja / en / zh / ko / other。NULL = 決められなかった(本文も見出しも無い、日本語の混じった文、
-- 漢字だけの短い文など)か、043 より前の行。配信とノートは ja と NULL には印を出さない。
ALTER TABLE analyses ADD COLUMN source_language TEXT
  CHECK (source_language IN ('ja', 'en', 'zh', 'ko', 'other'));
