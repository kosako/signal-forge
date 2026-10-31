-- GH#336: take の受付と追い掛けの回答(followup_dig)の順序の印(設計 06 §6)。
-- take の記録は、返事を送れた後か、vault に書けなかったときは次の実行で行う。その前に本人が
-- 追い掛けで「いいえ」に答えていたら、古い take の記録で「はい」を足して上書きしない。
-- 時刻(秒の単位)では同じ秒の先後が分からないので、signals の id を目盛りにする(GH#235 と同じ考え方)。
-- received_signal_id = 質問を受け付けた時点で記帳済みだった signals.id の最大(signals がまだ
-- 無ければ 0)。最新の followup_dig の id がこれより大きければ、take の受付の後の回答とみなす。
-- この migration より前の行は、受付の時刻より前に記帳された signals の id の最大で埋める
-- (同じ秒の signals は受付の後とみなし、本人の回答を残す)。
ALTER TABLE dig_questions ADD COLUMN received_signal_id INTEGER;

UPDATE dig_questions SET received_signal_id = (
  SELECT coalesce(max(s.id), 0) FROM signals s WHERE s.created_at < dig_questions.created_at
);
