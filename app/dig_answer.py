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
- 書き込みを伴う操作(take の記録 GH#106・調べ直しの依頼 GH#137)は、返事を作る呼び出しの
  出力で決めない(GH#245)。本人の入力(リプライの本文と、返信先が失敗の知らせか)だけを渡す
  別の呼び出し(prompts/dig-intent-v001)で決める。返事の材料(外部由来のノート・元記事)の中の
  文が、本人の質問を take として記録させたり、調べ直しを積ませたりできないようにするため
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.analysis.llm import LLMEngine, complete_with_retry
from app.analysis.profile import load_interest_profile
from app.config import Config
from app.dig import DigInstruction, apply_instructions
from app.prompting import fill_template, sanitize_body
from app.vault_guard import VaultGuard, VaultStalled

PROMPT_VERSION = "dig-answer-v003"
INTENT_PROMPT_VERSION = "dig-intent-v001"  # 本人のリプライの意図の判定(GH#245)
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# 返事の目安はプロンプトで 1,500 字。スキーマの上限は余裕を持たせる(少し超えただけで
# 検証失敗 → 返事が届かない、を避ける。送る側が媒体の上限で切る)
ANSWER_MAX_CHARS = 3_000
QUESTION_MAX_CHARS = 2_000  # 1 リプライの上限(プロンプトに渡す長さ)
EXPLAINER_MAX_CHARS = 20_000  # 解説ノート(平均 3,500 字。本人の追記で伸びても抑える)
HISTORY_TURNS = 5  # 材料に含めるそれまでのやり取り(直近から)

FAILURE_TEXT = "⚠️ 答えを作れませんでした。時間をおいて、もう一度リプライしてください"
# それまでのやり取りで、答えを作れなかった回に返事の代わりに置く文(GH#236)
FAILED_TURN_TEXT = "(回答失敗: このときは答えを作れなかった)"
# 失敗の知らせへのリプライに、答えられなかった元の質問を添えるときの前置き(GH#236)
RETRY_LEAD = (
    "(このリプライは「答えを作れなかった」という知らせへの返信。"
    "答えられなかった私の前のリプライ(古い順):)"
)

# 調べ直しの依頼(GH#137)。観点は本人のリプライの原文(LLM の出力は使わない。材料の外部本文が
# 翌朝の深掘りの指定観点 = プロンプトの信頼される側に入らないように)。上限は調査段が観点に使う長さ
REDIG_ANGLE_MAX_CHARS = 500
REDIG_NOTICE = (
    "\n\n🔁 次の朝の深掘りで、このリプライを観点にして調べ直します"
    "(新しい解説ノートとカードで届きます)"
)
# 調べ直しの依頼を受けられないときの知らせ(GH#197)。調査段の材料(元記事の本文)は保存の期間を
# 過ぎると消え、取り直す経路を持たない(X の本文を保持しない方針。GH#2)ので、翌朝の実行を約束しない
REDIG_NO_MATERIAL_NOTICE = (
    "\n\n⚠️ 元記事の本文の保存の期間が過ぎて材料が無いため、この記事は調べ直せません"
)

# カードのボタンからの依頼(GH#141)。台帳の question にこの文を入れ、質問と同じ流れで答える
# (返事はカードへのリプライ。それまでのやり取りにもこの文で残る)
REQUESTS = {
    "summary": (
        "(📝 要約のボタン)元記事の要約を日本語で書いてほしい。元記事が日本語以外なら、和訳した"
        "要約にする。記事が何を主張し、何を根拠にしているかが分かるように、要点を 3〜5 個の"
        "短い箇条書きで。"
    ),
    "more": (
        "(📖 詳しくのボタン)元記事の中身を、概要カードより詳しく日本語で説明してほしい。記事の"
        "流れに沿って、主張・根拠・具体例を省きすぎずに。元記事が日本語以外なら日本語で。"
    ),
}

_NOW_UTC = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"

# (返信先 = 質問の message_id, プレーンテキスト) -> 送った message_id
SendReply = Callable[[int, str], int]


class DigAnswer(BaseModel):
    """返事の構造化出力(prompts/dig-answer-v003.md)。

    リプライの種類と調べ直しの依頼かは返させない(DigIntent で別に決める。GH#245)。
    """

    answer: str = Field(min_length=1, max_length=ANSWER_MAX_CHARS)


class DigIntent(BaseModel):
    """本人のリプライの意図(prompts/dig-intent-v001.md。GH#245)。

    kind は take の記録(#106)が使う。redig = 本人が観点を足して調べ直してほしいと求めている
    (GH#137。翌朝の深掘りのキューに積む)。判定に渡すのは本人の入力だけで、外部由来の材料と
    それから作った返事は渡さない。既定値を持たせない(書き漏らしを「依頼でない」と黙って受け付けて
    依頼を失わない。欠ければ検証の失敗として扱う)。
    """

    kind: Literal["question", "take", "both"]
    redig: bool


def enqueue_question(
    conn: sqlite3.Connection,
    dig_result_id: int,
    question: str,
    question_message_id: int,
    replied_message_id: int,
) -> int:
    """質問を台帳に積む(返事は answer_pending が作る)。トランザクションは呼び出し側が張る。

    replied_message_id = そのリプライの返信先(カード・返事・失敗の知らせ)。失敗の知らせへの
    リプライなら、答えられなかった元の質問に結び付ける(retry_of。GH#236)。
    """
    return conn.execute(
        "INSERT INTO dig_questions (dig_result_id, question, question_message_id, retry_of) "
        "VALUES (?, ?, ?, (SELECT id FROM dig_questions "
        "WHERE dig_result_id = ? AND answer_message_id = ? AND status = 'failed'))",
        (dig_result_id, question, question_message_id, dig_result_id, replied_message_id),
    ).lastrowid


def enqueue_request(conn: sqlite3.Connection, dig_result_id: int, request: str) -> bool:
    """カードのボタンからの依頼を台帳に積む(GH#141)。同じカードの同じボタンが既にあれば
    積まずに False を返す(再配達・二重押し。migration 014 の部分 UNIQUE)。"""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO dig_questions (dig_result_id, question, request) VALUES (?, ?, ?)",
        (dig_result_id, REQUESTS[request], request),
    )
    return cursor.rowcount == 1


def question_received(conn: sqlite3.Connection, question_message_id: int) -> bool:
    """そのリプライを質問として受け付け済みか(再配達で 2 回積まない。migration 012)。"""
    return (
        conn.execute(
            "SELECT 1 FROM dig_questions WHERE question_message_id = ?", (question_message_id,)
        ).fetchone()
        is not None
    )


def questions_on(conn: sqlite3.Connection, day: str) -> int:
    """その JST 暦日に受け付けた質問の数(1 日の上限の判定。created_at は UTC 保存)。"""
    return conn.execute(
        "SELECT count(*) AS n FROM dig_questions WHERE date(created_at, '+9 hours') = ?",
        (day,),
    ).fetchone()["n"]


def has_pending(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute("SELECT 1 FROM dig_questions WHERE status = 'pending' LIMIT 1").fetchone()
        is not None
    )


def has_unsent(conn: sqlite3.Connection) -> bool:
    """返事(または失敗の知らせ)を作ったが送れていない行があるか。"""
    return (
        conn.execute(
            "SELECT 1 FROM dig_questions "
            "WHERE status <> 'pending' AND answer_message_id IS NULL LIMIT 1"
        ).fetchone()
        is not None
    )


def answer_pending(
    conn: sqlite3.Connection,
    config: Config,
    engine_factory: Callable[[str], LLMEngine],
    send_reply: SendReply,
    logger: logging.Logger,
    vault: VaultGuard | None = None,
) -> int:
    """未処理の質問に古い順で答えて送り、送った通数(失敗の知らせを含む)を返す。

    1 件ずつ「答えを作る → 送る」を繰り返す(先の質問の返事を待たせない)。vault のノートは
    時間の上限つきで読み、vault が応答しないときはノートなしで答える(GH#145)。
    """
    vault = vault or VaultGuard()
    sender = _Sender(conn, send_reply, logger)
    sender.flush()  # 前回の実行で送れなかった返事から
    while (row := _next_pending(conn)) is not None:
        _answer_one(conn, config, engine_factory, row, logger, vault)
        sender.flush()
    return sender.sent


def _next_pending(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT q.id, q.dig_result_id, q.question, q.request, q.retry_of,
               r.item_id, r.note_name, r.source_note_name, r.card_text
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
    vault: VaultGuard,
) -> None:
    """1 件を pending から answered / failed へ必ず進める(残すと answer_pending が回り続ける)。"""
    log_extra = {"stage": "answer", "extra": {"question_id": row["id"]}}
    try:
        engine = engine_factory(config.dig.answer_reasoning_effort)
        intent = _intent(engine, config, row)
        prompt = build_answer_prompt(conn, config, row, vault)
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
    kind = intent.kind
    answer = result.answer
    # 調べ直しは深掘りの段が動いているときだけ受ける(daily_cap = 0 では翌朝に掘られない)
    redig = intent.redig and config.dig.daily_cap > 0
    if redig and not _has_dig_material(conn, config, row["item_id"]):
        # 材料(元記事の本文)が無ければ翌朝に掘れないので、積まずに理由を伝える(GH#197)
        redig = False
        answer += REDIG_NO_MATERIAL_NOTICE
        logger.info(
            "本文の保存の期間が過ぎて材料が無いため、調べ直しを積まない(理由を返事に添える)",
            extra=log_extra,
        )
    if redig and not _record_answer_with_redig(conn, row, kind, answer, logger, log_extra):
        redig = False
    if not redig:
        _record_answer(conn, row["id"], kind, answer, redig=False)
    logger.info(f"返事を作った(kind={kind}・調べ直し={redig})", extra=log_extra)


def _has_dig_material(conn: sqlite3.Connection, config: Config, item_id: int) -> bool:
    """調査段がその item を掘れるだけの材料を持つか(調査段の選定と同じ条件。GH#197)。"""
    # 呼び出し時に import する(deepdive → delivery → poller → 本モジュールの循環を避ける)
    from app.deepdive import item_has_material

    return item_has_material(conn, config, item_id)


def _intent(engine: LLMEngine, config: Config, row: sqlite3.Row) -> DigIntent:
    """take の記録・調べ直しの依頼を決める本人の意図(GH#245)。

    返事を作る呼び出し(外部由来の材料を読む)とは別に、本人の入力だけで判定する。同じエンジン・
    同じ effort で呼び、1 日の上限(answer_daily_cap は質問の数)と予備の台帳(段 answer)も
    返事と共有する。判定に失敗したら返事も作らず失敗として知らせる(依頼を黙って落とさない)。
    ボタンからの依頼は本人の take でも調べ直しの依頼でもないので、呼ばずに question にする
    (GH#141。GH#106 が記録しない)。
    """
    if row["request"] is not None:
        return DigIntent(kind="question", redig=False)
    return complete_with_retry(
        engine, build_intent_prompt(row), DigIntent, timeout_s=config.dig.timeout_s
    )


def _record_answer(
    conn: sqlite3.Connection, question_id: int, kind: str, answer: str, *, redig: bool
) -> None:
    conn.execute(
        f"UPDATE dig_questions SET status = 'answered', kind = ?, answer = ?, redig = ?, "
        f"answered_at = {_NOW_UTC} WHERE id = ?",
        (kind, answer, int(redig), question_id),
    )


def _record_answer_with_redig(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    kind: str,
    answer: str,
    logger: logging.Logger,
    log_extra: dict,
) -> bool:
    """返事(調べ直しの知らせ付き)と深掘りのキューへの依頼を 1 つのトランザクションで記録する。

    キューに積めたときだけ知らせを付ける(積めなかったのに「調べ直します」と送らない)。
    キューへの積み方は未消化一覧へのリプライ指定と同じ(dig.apply_instructions: queued へ戻し、
    requested_at を更新して優先し、観点を上書きする。source は変えない)。
    質問を受け付けた後に本人がそのアイテムをパス・指定し直していたら(返事を作る数分の間の
    未消化一覧への操作)、古い依頼で上書きせず、積まずに False を返す(GH#235)。
    """
    angle = row["question"].strip()[:REDIG_ANGLE_MAX_CHARS] or None
    try:
        conn.execute("BEGIN IMMEDIATE")  # 接続は autocommit
        if _operated_after(conn, row["item_id"], row["id"]):
            conn.execute("ROLLBACK")
            logger.info(
                "受け付けた後にパス・指定し直しがあったため、調べ直しを積まない(返事だけ送る)",
                extra=log_extra,
            )
            return False
        _record_answer(conn, row["id"], kind, answer + REDIG_NOTICE, redig=True)
        apply_instructions(
            conn,
            [DigInstruction(row["item_id"], "request", angle=angle)],
            from_backlog=False,
        )
        conn.execute("COMMIT")
    except sqlite3.Error as e:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        logger.error(
            f"調べ直しをキューに積めなかった(返事だけ送る): {type(e).__name__}: {e}",
            extra=log_extra,
        )
        return False
    return True


def _operated_after(conn: sqlite3.Connection, item_id: int, question_id: int) -> bool:
    """質問を受け付けた後に、本人が未消化一覧でそのアイテムをパス・指定し直したか(GH#235)。

    未消化一覧への操作は、反映した時点で受け付け済みだった質問の id の最大を印に残す
    (dig_queue.backlog_op_question_id。migration 022)。質問の id がそれ以下なら、質問は
    その操作より前に受け付けている。時刻は秒の単位で同じ秒の先後が分からないので使わない。
    先の調べ直しの依頼と朝の深掘りの done は印を変えない(本人の未消化一覧への操作ではない。
    続けて送った調べ直しや、掘った後の調べ直しは今どおり積む)。
    """
    queue = conn.execute(
        "SELECT backlog_op_question_id AS mark FROM dig_queue WHERE item_id = ?", (item_id,)
    ).fetchone()
    return queue is not None and queue["mark"] is not None and queue["mark"] >= question_id


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
        # 返信先: 質問は本人のリプライ、ボタンからの依頼はカード(GH#141)
        rows = self._conn.execute(
            """
            SELECT q.id, q.status, q.answer,
                   coalesce(q.question_message_id, r.message_id) AS reply_to
            FROM dig_questions q JOIN dig_results r ON r.id = q.dig_result_id
            WHERE q.status <> 'pending' AND q.answer_message_id IS NULL
            ORDER BY q.id
            """
        ).fetchall()
        for row in rows:
            text = row["answer"] if row["status"] == "answered" else FAILURE_TEXT
            try:
                message_id = self._send_reply(row["reply_to"], text)
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


def build_answer_prompt(
    conn: sqlite3.Connection, config: Config, row: sqlite3.Row, vault: VaultGuard | None = None
) -> str:
    vault = vault or VaultGuard()
    nonce = secrets.token_hex(4)
    template = (_PROMPTS_DIR / f"{PROMPT_VERSION}.md").read_text(encoding="utf-8")
    profile = load_interest_profile(config.profile_path).body
    question = sanitize_body(row["question"], nonce)[:QUESTION_MAX_CHARS]
    retried = _retried_questions(conn, row["retry_of"])
    if retried:
        question = "\n\n".join(
            [question, RETRY_LEAD]
            + [sanitize_body(q["question"], nonce)[:QUESTION_MAX_CHARS] for q in retried]
        )
    # ボタンからの依頼のやり直しは、材料もボタンと同じにする(元記事の本文。GH#141・GH#236)
    request = retried[0]["request"] if retried else row["request"]
    return fill_template(
        template,
        {
            "interest_profile": profile,
            "materials": (
                _request_materials(conn, config, row, nonce, vault)
                if request is not None
                else _materials(config, row, nonce, vault)
            ),
            "history": _history(conn, row["dig_result_id"], row["id"], nonce),
            "question": question,
            "schema": json.dumps(DigAnswer.model_json_schema(), ensure_ascii=False),
            "nonce": nonce,
        },
    )


_REPLIED_TO = {
    False: "深掘りの概要カード、またはそのカードへのあなたの返事",
    True: "「答えを作れなかった」という知らせ(私の前のリプライに答えられなかったときに送られる)",
}


def build_intent_prompt(row: sqlite3.Row) -> str:
    """本人のリプライの意図を分けるプロンプト(GH#245)。

    渡すのは本人の入力だけ: リプライの本文と、返信先が失敗の知らせか(本人の操作から分かること)。
    外部由来の材料(カード・ノート・元記事)と、それから作った返事(それまでのやり取り)は渡さない。
    失敗の知らせへのリプライでも元の質問は渡さない(take として書く原文・調べ直しの観点は今回の
    リプライの本文なので、判定も今回の本文だけで行う。元の質問は返事の材料にだけ渡す)。
    """
    nonce = secrets.token_hex(4)
    template = (_PROMPTS_DIR / f"{INTENT_PROMPT_VERSION}.md").read_text(encoding="utf-8")
    return fill_template(
        template,
        {
            "replied_to": _REPLIED_TO[row["retry_of"] is not None],
            "reply": sanitize_body(row["question"], nonce)[:QUESTION_MAX_CHARS],
            "schema": json.dumps(DigIntent.model_json_schema(), ensure_ascii=False),
            "nonce": nonce,
        },
    )


def _retried_questions(conn: sqlite3.Connection, retry_of: int | None) -> list[sqlite3.Row]:
    """失敗の知らせへのリプライが、やり直しを求めている元の質問(古い順。GH#236)。

    やり直しも失敗し、その知らせにまたリプライしたときは、最初の質問まで辿る(retry_of は常に
    前の行を指すので巡回しない)。最初の質問(ボタンからの依頼でありうる)は必ず残し、途中の
    やり直しは直近の分だけにする(合わせて HISTORY_TURNS 件まで。プロンプトを伸ばしすぎない)。
    """
    chain: list[sqlite3.Row] = []
    while retry_of is not None:
        row = conn.execute(
            "SELECT question, request, retry_of FROM dig_questions WHERE id = ?", (retry_of,)
        ).fetchone()
        chain.append(row)
        retry_of = row["retry_of"]
    chain.reverse()
    if len(chain) > HISTORY_TURNS:
        chain = [chain[0], *chain[len(chain) - HISTORY_TURNS + 1 :]]
    return chain


def _materials(config: Config, row: sqlite3.Row, nonce: str, vault: VaultGuard) -> str:
    """カード(台帳)+ 解説ノート + 原文/対訳ノート(vault)。

    ノート本文は公開時に台帳から消える(migration 010)ので vault から読む。本人が
    ノートを動かした・消した場合や vault が応答しない場合も、手元の材料で答えられるよう
    欠落は注記で済ませる。
    """
    deepdive_dir = _deepdive_dir(config)
    parts = ["## 概要カード", sanitize_body(row["card_text"], nonce)]

    explainer = _read_note_guarded(vault, deepdive_dir, row["note_name"])
    parts.append("## 解説ノート")
    if explainer is None:
        parts.append(_missing_note("解説ノート", vault))
    else:
        parts.append(_truncate(sanitize_body(explainer, nonce), EXPLAINER_MAX_CHARS))

    if row["source_note_name"] is not None:
        source = _read_note_guarded(vault, deepdive_dir, row["source_note_name"])
        parts.append("## 原文/対訳ノート")
        if source is None:
            parts.append(_missing_note("原文/対訳ノート", vault))
        else:
            parts.append(
                _truncate(sanitize_body(source, nonce), config.dig.answer_source_max_chars)
            )
    return "\n\n".join(parts)


def _request_materials(
    conn: sqlite3.Connection, config: Config, row: sqlite3.Row, nonce: str, vault: VaultGuard
) -> str:
    """カードのボタンからの依頼の材料(GH#141): カード + 元記事の本文。

    スマホの Telegram で完結させるため、元記事の本文は DB(抽出済みの documents)を第一にし、
    vault のノートには頼らない。保持期限を過ぎて DB に無いときだけ、原文/対訳ノート(vault)を使う。
    """
    parts = ["## 概要カード", sanitize_body(row["card_text"], nonce), "## 元記事の本文"]
    doc = conn.execute(
        "SELECT extracted_text FROM documents WHERE item_id = ? AND extracted_text IS NOT NULL",
        (row["item_id"],),
    ).fetchone()
    body = doc["extracted_text"] if doc is not None else None
    if body is None and row["source_note_name"] is not None:
        body = _read_note_guarded(vault, _deepdive_dir(config), row["source_note_name"])
    if body is None:
        parts.append("(元記事の本文が手元に無い。概要カードの範囲で答える)")
    else:
        parts.append(_truncate(sanitize_body(body, nonce), config.dig.answer_source_max_chars))
    return "\n\n".join(parts)


def _history(conn: sqlite3.Connection, dig_result_id: int, before_id: int, nonce: str) -> str:
    """同じカードへのそれまでのやり取り(直近 HISTORY_TURNS 件を古い順)。

    答えを作れなかった質問も、返事の代わりに決まった文を付けて含める(GH#236。失敗の知らせへの
    「もう一度」で、何をやり直すかが分かるように)。
    """
    rows = conn.execute(
        """
        SELECT question, status, answer FROM dig_questions
        WHERE dig_result_id = ? AND id < ? AND status IN ('answered', 'failed')
        ORDER BY id DESC LIMIT ?
        """,
        (dig_result_id, before_id, HISTORY_TURNS),
    ).fetchall()
    if not rows:
        return "(なし)"
    turns = []
    for r in reversed(rows):
        question = sanitize_body(r["question"], nonce)[:QUESTION_MAX_CHARS]
        if r["status"] == "answered":
            answer = sanitize_body(r["answer"], nonce)[:ANSWER_MAX_CHARS]
        else:
            answer = FAILED_TURN_TEXT
        turns.append(f"私: {question}\nあなた: {answer}")
    return "\n\n".join(turns)


def _deepdive_dir(config: Config) -> Path | None:
    # 呼び出し時に import する(deepdive → delivery → poller → 本モジュールの循環を避ける)
    from app.deepdive import DEEPDIVE_FOLDER

    if config.obsidian.vault_path is None:
        return None
    return Path(config.obsidian.vault_path).expanduser() / config.obsidian.folder / DEEPDIVE_FOLDER


def _read_note_guarded(
    vault: VaultGuard, deepdive_dir: Path | None, note_name: str
) -> str | None:
    """_read_note を時間の上限つきで(vault が応答しないときは None。理由は vault.stalled)。"""
    if deepdive_dir is None:
        return None
    try:
        return vault.call(lambda: _read_note(deepdive_dir, note_name))
    except VaultStalled:
        return None


def _missing_note(label: str, vault: VaultGuard) -> str:
    if vault.stalled:
        return f"({label}を読めない: vault が応答しない)"
    return f"({label}が見つからない)"


def _read_note(deepdive_dir: Path | None, note_name: str) -> str | None:
    # note_name は調査段が付けた名前(台帳由来)。念のため DeepDive の外を指すもの(`../` を
    # 含む名前や、外への symlink)は読まない。symlink を辿った後の実体の置き場所で判定する
    if deepdive_dir is None:
        return None
    try:
        base = deepdive_dir.resolve(strict=True)
        path = (deepdive_dir / f"{note_name}.md").resolve(strict=True)
        if path.parent != base or not path.is_file():
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n(…以降は長さ上限で切り詰め)"
