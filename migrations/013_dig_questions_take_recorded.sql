-- GH#106: take の記録(設計 06 §6)。返事を作る LLM が take / both と分けたリプライを、
-- 解説ノートの take 領域への追記と followup_dig に記録する(ノートは書き直さない。frontmatter の
-- status も変えない)。take が付いたかの正本はこの列。
-- take_recorded_at IS NULL = 未記録。記録は返事を送れた後(answer_message_id がある行)に行い、
-- 途中で止まっても次の `intel answer` の実行で記録し直す(ノートへの追記は見出しの記録番号 =
-- 質問 id で見分け、2 回書かない。途中まで書けていたら続きだけを足す)。
ALTER TABLE dig_questions ADD COLUMN take_recorded_at TEXT;

CREATE INDEX ix_dig_questions_take_unrecorded ON dig_questions(id)
  WHERE status = 'answered' AND kind IN ('take', 'both') AND answer_message_id IS NOT NULL
    AND take_recorded_at IS NULL;
