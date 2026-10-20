-- GH#156: 主のエンジン(Codex)が失敗した LLM の呼び出しを、予備(Claude API)でやり直した記録
-- (設計 00 §5)。1 呼び出し = 1 行。予備が答えなかった呼び出し(予備も失敗)も、課金されうる
-- ので残す。月の上限の判定と doctor #9 の当月コストに使う(どちらも UTC の暦月)。
--   stage          analyze / trend / dig / answer(どの段の呼び出しか)
--   run_id         朝バッチの ULID(朝バッチの後の知らせで集計する。返事など朝バッチの外は NULL)
--   primary_error  主の失敗の種類(例外の型名。本文は載せない)。主を飛ばした回は 'skipped'
--                  (同じプロセスで主が続けて失敗した後は、主を呼ばずに予備へ行く)
--   ok             1 = 予備が答えた / 0 = 予備も失敗した
CREATE TABLE llm_fallback_calls (
  id             INTEGER PRIMARY KEY,
  created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  run_id         TEXT,
  stage          TEXT    NOT NULL CHECK (stage IN ('analyze', 'trend', 'dig', 'answer')),
  model          TEXT    NOT NULL,
  primary_error  TEXT    NOT NULL,
  ok             INTEGER NOT NULL CHECK (ok IN (0, 1)),
  input_tokens   INTEGER,
  output_tokens  INTEGER,
  cost_usd       REAL    NOT NULL DEFAULT 0
) STRICT;

CREATE INDEX ix_llm_fallback_calls_created ON llm_fallback_calls(created_at);
CREATE INDEX ix_llm_fallback_calls_run ON llm_fallback_calls(run_id) WHERE run_id IS NOT NULL;
