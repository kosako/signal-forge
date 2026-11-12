-- GH#409: 夜の一覧(まだ読んでいない記事)の行にも「👎 不要」を付ける(設計 04 §3・06 §2)。カードの
-- 無い記事(本文の無い X のブックマークなど)も不要にできるよう、押したカードの列(dig_result_id)を
-- 空にできるようにする。NULL = 一覧の行のボタンで不要にした(どのカードからでもない)。
-- 列の NOT NULL を外すには表の作り直しが要る(SQLite)。dig_dismissals を参照する表・トリガ・
-- ビュー・索引は無い。列・既定値・外部キー(items・dig_results)は 031 のまま。
CREATE TABLE dig_dismissals_new (
  item_id        INTEGER PRIMARY KEY REFERENCES items(id),
  dig_result_id  INTEGER REFERENCES dig_results(id),
  dismissed_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
) STRICT;

INSERT INTO dig_dismissals_new (item_id, dig_result_id, dismissed_at)
SELECT item_id, dig_result_id, dismissed_at FROM dig_dismissals;

DROP TABLE dig_dismissals;
ALTER TABLE dig_dismissals_new RENAME TO dig_dismissals;
