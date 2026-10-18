-- GH#106: take の記録(設計 06 §6)。返事を作る LLM が take / both と分けたリプライを、
-- 解説ノートの take 領域への追記・ノートの status(take待ち → take済)・followup_dig に記録する。
-- take_recorded_at IS NULL = 未記録。記録は返事を送った後に行い、途中で止まっても次の
-- `intel answer` の実行で記録し直す(ノートへの追記は質問 id の印で 2 回書かない)。
ALTER TABLE dig_questions ADD COLUMN take_recorded_at TEXT;

CREATE INDEX ix_dig_questions_take_unrecorded ON dig_questions(id)
  WHERE status = 'answered' AND kind IN ('take', 'both') AND take_recorded_at IS NULL;
