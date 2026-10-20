-- GH#150: フィードの恒久移転(301/308)を記録する(設計 02 §3)。
-- moved_url = 直近の成功した取得で恒久移転を経由したときの移転先(リダイレクトを辿った最終の URL)。
-- NULL = 移転していない(移転なしで取れたら消える)。sources.config の URL の書き換えは
-- 人が行う(自動では書き換えない)。週次のソースの健康診断がこれを読んで知らせる。
ALTER TABLE sources ADD COLUMN moved_url TEXT;
