-- ヘッダの今の重点を外す・戻すボタン(GH#433 の 3 本目・設計 04 §2・§3)。ボタンを付けて送ったヘッダの
-- 行にだけ、送ったときの digests.focus_json を写して残す。ボタンの N はこの写しの何番目かで読む
-- (--force で focus_json が変わっても、押したメッセージに載っていた話題を外す)。ボタンを付けない
-- ヘッダと記事の行は NULL。
ALTER TABLE digest_messages ADD COLUMN focus_json TEXT
  CHECK (focus_json IS NULL OR json_valid(focus_json));
