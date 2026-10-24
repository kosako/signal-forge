-- GH#178: 興味シグナルの取込の日次の上限([x_signals].daily_read_cap)は JST の暦日の上限で、
-- 同じ日の実行で共有する(x_signal_runs の当日の読取の合計を引いた残りだけを読む。設計 02 §2)。
-- 明示的な backfill(intel x-signals --backfill)は日次の上限の外の別枠なので、その行を見分けて
-- 通常の取込の使用量から外す。費用の集計(doctor #9)には従来どおり入れる。
-- backfill = 1 は backfill の取込。既存の行は 0(通常の取込)として扱う。
ALTER TABLE x_signal_runs
  ADD COLUMN backfill INTEGER NOT NULL DEFAULT 0 CHECK (backfill IN (0, 1));
