-- analyze-v002: 日本語見出し(設計 01 §3 / 03 §3.3)。
-- NULL = v001 世代の分析 or degraded 記録(LLM 不在でタイトルのみ)。
-- 表示側(digest / Telegram / Obsidian / 追い掛け)は title_ja 優先・無ければ items.title。
ALTER TABLE analyses ADD COLUMN title_ja TEXT;
