-- GH#126: 同じリプライを 2 回受け付けない(質問の受付の冪等化)。
-- poller は台帳に積んだ後に getUpdates の offset を保存する。その間に止まると Telegram が
-- 同じ update を再配達し、同じ質問に 2 回答えて送ってしまう(返事の二重生成・二重送信と
-- 1 日の上限の余分な消費)。message_id は owner chat 内で一意で、質問は owner chat からしか
-- 受け付けないので、question_message_id 単独で一意にできる。
CREATE UNIQUE INDEX ux_dig_questions_question_message ON dig_questions(question_message_id);
