"""今の重点に当たる記事を選ぶ(GH#433 の 4 本目・設計 03 §4.1・§6)。

digest の段が、点を足すと上位に届きうる候補(app/ranking の focus_pool)の見出しと要約を LLM に渡し、
今の重点のどれかに当たる記事を選ばせる(prompts/focus-match-v001.md)。結果は focus_matches
(migration 042)に残し、ランキングが当たった記事の点に `[focus].score_bonus` を足す。分析の prompt
には混ぜない(混ぜると当たった記事の関心の点まで上がり、確かめの段がプロファイルに照らして直す。
11-20 の本番の写しの比較)。

- 重点の名前は LLM が外部の本文から作った文、証拠の例と記事の見出し・要約は外部の本文に由来する。
  どれもサニタイズして nonce の囲みの中の data として渡す。出力は Pydantic で検証し、渡した記事と
  話題の id にだけ写す(1 つの記事は最初に当てた 1 つの話題)。
- 付加機能なので、LLM の失敗は digest の段の失敗にしない。その日に選んだ行があればそれを使い、
  無ければ点を足さない。ログには件数と失敗の種類だけを書く。
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
from pathlib import Path

from pydantic import BaseModel

from app.analysis.llm import LLMEngine, LLMError, complete_with_retry
from app.focus import FocusTopicRef
from app.prompting import fill_template, sanitize

PROMPT_VERSION = "focus-match-v001"
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / f"{PROMPT_VERSION}.md"
MATCH_EFFORT = "medium"  # 候補は 60 件まで・1 日 1 回なので、分析(low)より深く読ませる

MAX_NAME = 30
MAX_EXAMPLE = 100
MAX_TITLE = 120
MAX_SUMMARY = 200
MAX_REASON = 60


class FocusMatch(BaseModel):
    item_id: int
    topic_id: int
    reason: str = ""  # 長さは保存の前に切る(長いだけで全体を落とさない)


class FocusMatches(BaseModel):
    matches: list[FocusMatch]


def build_prompt(topics: list[FocusTopicRef], pool: list[sqlite3.Row], nonce: str) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    topic_lines = []
    for topic in topics:
        line = f"id={topic.topic_id} 名前: {_line(topic.name, MAX_NAME, nonce)}"
        examples = [e for e in (_line(e, MAX_EXAMPLE, nonce) for e in topic.examples) if e]
        if examples:
            line += " / 例: " + " | ".join(examples)
        topic_lines.append(line)
    item_lines = [
        f"[{row['item_id']}] {_line(row['title_ja'] or row['title'], MAX_TITLE, nonce)}"
        f" / {_line(row['summary'], MAX_SUMMARY, nonce)}"
        for row in pool
    ]
    return fill_template(
        template,
        {
            "focus_topics": "\n".join(topic_lines) or "(なし)",
            "items": "\n".join(item_lines) or "(なし)",
            "schema": json.dumps(FocusMatches.model_json_schema(), ensure_ascii=False),
            "nonce": nonce,
        },
    )


def _line(value: str | None, max_length: int, nonce: str) -> str:
    """1 行に均す(改行・制御文字・nonce を除き、続く空白を 1 つに)。"""
    return " ".join(sanitize(value, max_length, nonce).split())


def select_focus_items(
    conn: sqlite3.Connection,
    engine: LLMEngine,
    logger: logging.Logger,
    *,
    digest_date: str,
    topics: list[FocusTopicRef],
    pool: list[sqlite3.Row],
    run_id: str | None = None,
) -> dict[int, int]:
    """今の重点に当たる記事(item_id → 話題の id)を選び、その日の行を入れ替える。

    重点か候補が無ければ LLM を呼ばず、その日の行を消して空を返す。LLM が失敗したら、その日に
    選んだ行のうち今の重点の話題のものを返す(行は書き換えない)。
    """
    topic_ids = {topic.topic_id for topic in topics}
    matches: dict[int, tuple[int, str]] = {}
    if topics and pool:
        prompt = build_prompt(topics, pool, secrets.token_hex(4))
        try:
            result = complete_with_retry(engine, prompt, FocusMatches)
        except LLMError as e:
            logger.warning(
                f"今の重点に当たる記事を選べない(その日の判定を使う): {type(e).__name__}",
                extra={"run_id": run_id, "stage": "digest"},
            )
            return stored_focus_items(conn, digest_date, topic_ids)
        pool_ids = {row["item_id"] for row in pool}
        for match in result.matches:
            if match.item_id in pool_ids and match.topic_id in topic_ids:
                matches.setdefault(
                    match.item_id, (match.topic_id, " ".join(match.reason.split())[:MAX_REASON])
                )
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM focus_matches WHERE digest_date = ?", (digest_date,))
        conn.executemany(
            "INSERT INTO focus_matches "
            "(digest_date, item_id, topic_id, reason, run_id, prompt_version) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (digest_date, item_id, topic_id, reason, run_id, PROMPT_VERSION)
                for item_id, (topic_id, reason) in matches.items()
            ],
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    logger.info(
        "今の重点に当たる記事を選んだ",
        extra={
            "run_id": run_id,
            "stage": "digest",
            "extra": {"pool": len(pool), "topics": len(topics), "matched": len(matches)},
        },
    )
    return {item_id: topic_id for item_id, (topic_id, _reason) in matches.items()}


def stored_focus_items(
    conn: sqlite3.Connection, digest_date: str, topic_ids: set[int]
) -> dict[int, int]:
    """その日に選んだ行のうち、指定の話題のもの(item_id → 話題の id)。"""
    return {
        row["item_id"]: row["topic_id"]
        for row in conn.execute(
            "SELECT item_id, topic_id FROM focus_matches WHERE digest_date = ? ORDER BY id",
            (digest_date,),
        )
        if row["topic_id"] in topic_ids
    }
