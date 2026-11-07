-- GH#388: 今すぐの深掘り(設計 06 §3)。未消化一覧のボタンで頼んだ深掘りを、poller が別プロセスで
-- 起動する `intel dig-now` が朝を待たずに作る。
--   dig_queue.now_requested_at  今すぐの依頼を受け付けた時刻。NULL = 依頼なし。作り終えたとき、
--                               作れずに明朝の深掘りへ回したとき、本人がパスしたときに NULL に戻す
--   dig_queue.now_started_at    `intel dig-now` が作り始めた時刻(1 回押して 1 回だけ作るための印)。
--                               印が残ったまま次の実行が見つけたら、前の実行が途中で止まったとみなし、
--                               作り直さずに明朝の深掘りへ回す(LLM を繰り返し呼ばない)。作っている間は
--                               朝の選定がこの行を選ばない(本人がパスしても、作り終えるまで残す)
--   dig_queue.now_callback_id   今すぐのボタンの callback のうち、最後に受け付けた(受け付け済み・作成中と
--                               答えたものを含む)id。依頼を閉じても残し、
--                               同じ callback の再配達を新しい依頼として受け付けない
--   dig_results.on_demand       1 = 今すぐの依頼で作った。朝の 1 日の上限([dig].daily_cap)に数えない
ALTER TABLE dig_queue ADD COLUMN now_requested_at TEXT;
ALTER TABLE dig_queue ADD COLUMN now_started_at TEXT;
ALTER TABLE dig_queue ADD COLUMN now_callback_id TEXT;
ALTER TABLE dig_results ADD COLUMN on_demand INTEGER NOT NULL DEFAULT 0
  CHECK (on_demand IN (0, 1));
