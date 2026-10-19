-- GH#137: カードへのリプライで「観点を足して調べ直してほしい」と求められたか(設計 06 §6)。
-- 1 = 返事を作る LLM が調べ直しの依頼と分け、深掘りのキュー(dig_queue)に積んだ。
-- 観点は本人のリプライの原文(dig_queue.angle)。0 = 依頼でない・積まなかった。
ALTER TABLE dig_questions ADD COLUMN redig INTEGER NOT NULL DEFAULT 0 CHECK (redig IN (0, 1));
