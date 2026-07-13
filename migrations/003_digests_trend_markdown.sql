-- 今日の全体トレンドまとめ(GH#75)。ヘッダメッセージ + Obsidian markdown 冒頭に載せる
-- 日本語 markdown。その日の分析を topic ヒントで 3〜5 テーマへ束ねた俯瞰。
-- NULL = トレンド無し(synthesis 失敗 / 対象少数 / 旧世代 digest / degraded 配信)。
ALTER TABLE digests ADD COLUMN trend_markdown TEXT;
