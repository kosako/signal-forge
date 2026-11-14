-- 出来事の調査をヘッダのボタンで頼む(GH#415 の 4 本目)。
-- digest_messages.news_json = ヘッダに「🔎 N」のボタンを付けて送ったときの今日の IT ニュースの写し
-- (digests.trend_json と同じ形)。ボタンの N はこの写しの何件目かで読む。--force の置換で trend_json が
-- 変わっても、押したメッセージに載っていた出来事を調べる(前の世代の行も写しを残す)。ボタンを付けない
-- ヘッダと記事の行は NULL。
ALTER TABLE digest_messages ADD COLUMN news_json TEXT
  CHECK (news_json IS NULL OR json_valid(news_json));

-- 依頼を受け付けた(か、受け付け済みと答えた)callback の id。poller が答えた後、offset を保存する前に
-- 止まって同じ callback が再配達されたとき、失敗して閉じた依頼を受け付け直さない(今すぐの深掘りの
-- dig_queue.now_callback_id と同じ考え)。押し直した callback は id が違うので受け付け直す。
ALTER TABLE news_investigations ADD COLUMN request_callback_id TEXT;
