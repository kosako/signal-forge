"""深掘りカードへの質問に答える(GH#126 / ADR 0005 / 設計 06 §6)。

- 答えを作る部分は操作面に依存しない。送信は呼び出し側が send_reply として渡す
  (今は Telegram。操作するアプリが替わっても本モジュールはそのまま使う。ADR 0005)。
  send_reply にはプレーンテキストを渡し、整形・エスケープは送る側の責務
- 実行主体 = 使い捨て `codex exec`・LLM ツールなし(調査段と同じ隔離レール。設計 06 §4)。
  材料(カード・解説ノート・原文/対訳ノート・それまでのやり取り)はプロンプトで渡し、
  送信は呼び出し側(Python)が行う。PROPOSAL §フェーズ2境界の安全条件を構造的に満たす
- write-ahead(migration 011): 答え(または失敗)を台帳に記帳してから送り、送れたら
  answer_message_id を記帳する。途中で止まっても、次の実行は未送信の行の送信から再開し、
  同じ質問の答えを作り直さない
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.analysis.analyzer import sanitize_body
from app.analysis.llm import LLMEngine, complete_with_retry
from app.analysis.profile import load_interest_profile
from app.config import Config
from app.deepdive import DEEPDIVE_FOLDER

PROMPT_VERSION = "dig-answer-v001"
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# 返事の目安はプロンプトで 1,500 字。スキーマの上限は余裕を持たせる(少し超えただけで
# 検証失敗 → 返事が届かない、を避ける。送る側が媒体の上限で切る)
ANSWER_MAX_CHARS = 3_000
QUESTION_MAX_CHARS = 2_000  # 1 リプライの上限(プロンプトに渡す長さ)
EXPLAINER_MAX_CHARS = 20_000  # 解説ノート(平均 3,500 字。本人の追記で伸びても抑える)
HISTORY_TURNS = 5  # 材料に含めるそれまでのやり取り(直近から)

FAILURE_TEXT = "⚠️ 答えを作れませんでした。時間をおいて、もう一度リプライしてください"

_NOW_UTC = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"

# (返信先 = 質問の message_id, プレーンテキスト) -> 送った message_id
SendReply = Callable[[int, str], int]


class DigAnswer(BaseModel):
    """返事の構造化出力(prompts/dig-answer-v001.md)。kind は take の記録(#106)が使う。"""

    kind: Literal["question", "take", "both"]
    answer: str = Field(min_length=1, max_length=ANSWER_MAX_CHARS)


def answer_pending(
    conn: sqlite3.Connection,
    config: Config,
    engine_factory: Callable[[str], LLMEngine],
    send_reply: SendReply,
    logger: logging.Logger,
) -> int:
    """未処理の質問に古い順で答えて送り、送った通数(失敗の知らせを含む)を返す。

    1 件ずつ「答えを作る → 送る」を繰り返す(先の質問の返事を待たせない)。
    """
    sender = _Sender(conn, send_reply, logger)
    sender.flush()  # 前回の実行で送れなかった返事から
    while (row := _next_pending(conn)) is not None:
        _answer_one(conn, config, engine_factory, row, logger)
        sender.flush()
    return sender.sent


def _next_pending(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT q.id, q.dig_result_id, q.question,
               r.note_name, r.source_note_name, r.card_text
        FROM dig_questions q JOIN dig_results r ON r.id = q.dig_result_id
        WHERE q.status = 'pending'
        ORDER BY q.id LIMIT 1
        """
    ).fetchone()


def _answer_one(
    conn: sqlite3.Connection,
    config: Config,
    engine_factory: Callable[[str], LLMEngine],
    row: sqlite3.Row,
    logger: logging.Logger,
) -> None:
    """1 件を pending から answered / failed へ必ず進める(残すと answer_pending が回り続ける)。"""
    log_extra = {"stage": "answer", "extra": {"question_id": row["id"]}}
    try:
        engine = engine_factory(config.dig.answer_reasoning_effort)
        prompt = build_answer_prompt(conn, config, row)
        result = complete_with_retry(engine, prompt, DigAnswer, timeout_s=config.dig.timeout_s)
    except Exception as e:
        # 想定外の例外も 1 件に閉じ、失敗として本人に知らせる(返事を待たせたままにしない)
        conn.execute(
            f"UPDATE dig_questions SET status = 'failed', error = ?, answered_at = {_NOW_UTC} "
            "WHERE id = ?",
            (f"{type(e).__name__}: {e}"[:500], row["id"]),
        )
        conn.commit()
        logger.error(f"返事を作れなかった: {type(e).__name__}: {e}", extra=log_extra)
        return
    conn.execute(
        f"UPDATE dig_questions SET status = 'answered', kind = ?, answer = ?, "
        f"answered_at = {_NOW_UTC} WHERE id = ?",
        (result.kind, result.answer, row["id"]),
    )
    conn.commit()
    logger.info(f"返事を作った(kind={result.kind})", extra=log_extra)


class _Sender:
    """未送信の返事を送る。送信に 1 度失敗したら、この実行では以後送らない。

    送信先の障害中に同じ行へ再送を繰り返さないため。未送信の行は台帳に残り、
    次の実行の最初の flush で送る。
    """

    def __init__(
        self, conn: sqlite3.Connection, send_reply: SendReply, logger: logging.Logger
    ) -> None:
        self._conn = conn
        self._send_reply = send_reply
        self._logger = logger
        self._broken = False
        self.sent = 0

    def flush(self) -> None:
        if self._broken:
            return
        rows = self._conn.execute(
            """
            SELECT id, status, answer, question_message_id FROM dig_questions
            WHERE status <> 'pending' AND answer_message_id IS NULL
            ORDER BY id
            """
        ).fetchall()
        for row in rows:
            text = row["answer"] if row["status"] == "answered" else FAILURE_TEXT
            try:
                message_id = self._send_reply(row["question_message_id"], text)
            except Exception as e:
                self._broken = True
                self._logger.error(
                    f"返事の送信に失敗(次の実行で再送): {type(e).__name__}: {e}",
                    extra={"stage": "answer", "extra": {"question_id": row["id"]}},
                )
                return
            self._conn.execute(
                f"UPDATE dig_questions SET answer_message_id = ?, sent_at = {_NOW_UTC} "
                "WHERE id = ?",
                (message_id, row["id"]),
            )
            self._conn.commit()
            self.sent += 1


def build_answer_prompt(conn: sqlite3.Connection, config: Config, row: sqlite3.Row) -> str:
    nonce = secrets.token_hex(4)
    template = (_PROMPTS_DIR / f"{PROMPT_VERSION}.md").read_text(encoding="utf-8")
    profile = load_interest_profile(config.profile_path).body
    question = sanitize_body(row["question"], nonce)[:QUESTION_MAX_CHARS]
    return _fill_template(
        template,
        {
            "interest_profile": profile,
            "materials": _materials(config, row, nonce),
            "history": _history(conn, row["dig_result_id"], row["id"], nonce),
            "question": question,
            "schema": json.dumps(DigAnswer.model_json_schema(), ensure_ascii=False),
            "nonce": nonce,
        },
    )


def _materials(config: Config, row: sqlite3.Row, nonce: str) -> str:
    """カード(台帳)+ 解説ノート + 原文/対訳ノート(vault)。

    ノート本文は公開時に台帳から消える(migration 010)ので vault から読む。本人が
    ノートを動かした・消した場合も、手元の材料で答えられるよう欠落は注記で済ませる。
    """
    deepdive_dir = _deepdive_dir(config)
    parts = ["## 概要カード", sanitize_body(row["card_text"], nonce)]

    explainer = _read_note(deepdive_dir, row["note_name"])
    parts.append("## 解説ノート")
    if explainer is None:
        parts.append("(解説ノートが見つからない)")
    else:
        parts.append(_truncate(sanitize_body(explainer, nonce), EXPLAINER_MAX_CHARS))

    if row["source_note_name"] is not None:
        source = _read_note(deepdive_dir, row["source_note_name"])
        parts.append("## 原文/対訳ノート")
        if source is None:
            parts.append("(原文/対訳ノートが見つからない)")
        else:
            parts.append(
                _truncate(sanitize_body(source, nonce), config.dig.answer_source_max_chars)
            )
    return "\n\n".join(parts)


def _history(conn: sqlite3.Connection, dig_result_id: int, before_id: int, nonce: str) -> str:
    """同じカードへのそれまでのやり取り(返事のあるものだけ。直近 HISTORY_TURNS 件を古い順)。"""
    rows = conn.execute(
        """
        SELECT question, answer FROM dig_questions
        WHERE dig_result_id = ? AND id < ? AND status = 'answered'
        ORDER BY id DESC LIMIT ?
        """,
        (dig_result_id, before_id, HISTORY_TURNS),
    ).fetchall()
    if not rows:
        return "(なし)"
    turns = []
    for r in reversed(rows):
        question = sanitize_body(r["question"], nonce)[:QUESTION_MAX_CHARS]
        answer = sanitize_body(r["answer"], nonce)[:ANSWER_MAX_CHARS]
        turns.append(f"私: {question}\nあなた: {answer}")
    return "\n\n".join(turns)


def _deepdive_dir(config: Config) -> Path | None:
    if config.obsidian.vault_path is None:
        return None
    return Path(config.obsidian.vault_path).expanduser() / config.obsidian.folder / DEEPDIVE_FOLDER


def _read_note(deepdive_dir: Path | None, note_name: str) -> str | None:
    # note_name は調査段が付けた名前(台帳由来)。念のため DeepDive の外を指す名前は読まない
    if deepdive_dir is None or Path(note_name).name != note_name:
        return None
    try:
        return (deepdive_dir / f"{note_name}.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n(…以降は長さ上限で切り詰め)"


def _fill_template(template: str, mapping: dict[str, str]) -> str:
    """単一パス置換(調査段と同じ規約。入力由来の {{nonce}} 等を再展開しない)。"""
    return re.sub(r"\{\{(\w+)\}\}", lambda m: mapping[m.group(1)], template)
