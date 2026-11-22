"""今の重点で X の検索の枠を 1 本入れ替える(GH#433 の 6 本目)。

ADR 0001 の追記・設計 02 §2・03 §4.1。


- 話題が今の重点に入ると、受信デーモンが朝のダイジェストを送った後に案を 1 通送る(next_proposal)。
  同じ重点の入り(focus_topics.activated_at)には 1 回だけ聞く。答えを待つ案も、入れ替え中の枠も、
  同時に 1 つだけ
- 人が「N と入れ替える」を押したときだけ入れ替える(decide)。朝の収集は effective_slots で、config の
  `[x_search].slots` の N 本目を案の検索に差し替える。枠の数は変えないので、1 日に読む上限
  (`[x_search].daily_read_cap`)とその分け方も変わらない
- 話題が重点から外れたら(🎯 外すを含む)、差し替えをやめる。朝の focus 段が revert_stale で
  記録を閉じる
  (承認した枠は reverted、答える前の案は expired)
- 検索の語は外部の本文から LLM が作った語(focus_topics.terms)。clean_terms を通し、config の枠と同じ
  検査(送る文の長さ・括弧と引用符の対応)を通した文だけを案にする。人が見て承認するのも、収集が送るのも、
  この文(台帳の query)
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime

from app.config import X_SEARCH_QUERY_MAX, Config, _parens_balanced, x_search_query

# 答えの記号(callback_data の 3 つ目)。数字は入れ替える枠の番号
DECLINE = "n"
REVERT = "r"


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_query(terms: Sequence[str]) -> str | None:
    """話題の検索の語から X の検索の文を作る(語を引用符で囲み OR でつなぐ)。作れなければ None。

    語は clean_terms で 1 行にし、演算子と引用・括弧を外し直す(台帳の値をそのまま外へ出さない)。
    config の枠と同じく、自動で足す除外句を含めた長さと、括弧・引用符の対応を確かめる。
    """
    from app.focus import clean_terms

    cleaned = clean_terms([t for t in terms if isinstance(t, str)])
    if not cleaned:
        return None
    query = " OR ".join(f'"{term}"' for term in cleaned)
    if len(x_search_query(query)) > X_SEARCH_QUERY_MAX or not _parens_balanced(query):
        return None
    return query


def _query_ok(query: str) -> bool:
    return bool(query.strip()) and (
        len(x_search_query(query)) <= X_SEARCH_QUERY_MAX and _parens_balanced(query)
    )


def current_replacement(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """入れ替え中の枠(approved の行)と、その話題の今の状態。無ければ None。"""
    return conn.execute(
        "SELECT f.*, t.name AS topic_name, t.status AS topic_status, "
        "t.activated_at AS topic_activated_at "
        "FROM focus_x_slots f JOIN focus_topics t ON t.id = f.topic_id "
        "WHERE f.status = 'approved'"
    ).fetchone()


def effective_slots(
    conn: sqlite3.Connection, config: Config, logger: logging.Logger | None = None
) -> tuple[str, ...]:
    """朝の収集が使う検索の枠。入れ替え中の枠があり、その話題が今も重点なら、その 1 本を差し替える。

    差し替えないのは、[focus] が off・入れ替え中の枠が無い・話題が重点でない(外れた・外した・重点に
    入り直した)・config の N 本目の文が承認したときと違う・案の文が検査を通らない・同じ文の枠が既に
    ある、のどれか。読むだけで、記録は閉じない(閉じるのは focus 段の revert_stale)。
    """
    slots = tuple(config.x_search.slots)
    if not config.focus.enabled or not slots:
        return slots
    row = current_replacement(conn)
    if row is None:
        return slots
    index = (row["slot"] or 0) - 1
    usable = (
        row["topic_status"] == "active"
        and row["topic_activated_at"] == row["activated_at"]
        and 0 <= index < len(slots)
        and slots[index] == row["replaced_query"]
        and _query_ok(row["query"])
        and row["query"] not in slots
    )
    if not usable:
        return slots
    if logger is not None:
        logger.info(
            "X の検索の枠の 1 本を今の重点の検索に入れ替えた",
            extra={"stage": "collect", "extra": {"slot": row["slot"], "topic_id": row["topic_id"]}},
        )
    return slots[:index] + (row["query"],) + slots[index + 1 :]


def revert_stale(conn: sqlite3.Connection, now: datetime) -> tuple[list[int], list[int]]:
    """重点から外れた話題の記録を閉じる。(元に戻した行の id, 答える前に閉じた案の id)を返す。

    朝の focus 段が refresh_status の後に呼ぶ。外れた = 話題の状態が active でないか、重点に
    入り直して activated_at が案のものと違う。承認した枠は reverted、答える前の案は expired にする。
    """
    stamp = _iso(now)
    stale = (
        "SELECT f.id FROM focus_x_slots f JOIN focus_topics t ON t.id = f.topic_id "
        "WHERE f.status = ? AND (t.status != 'active' OR t.activated_at IS NOT f.activated_at)"
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        reverted = [r["id"] for r in conn.execute(stale, ("approved",))]
        expired = [r["id"] for r in conn.execute(stale, ("proposed",))]
        conn.executemany(
            "UPDATE focus_x_slots SET status = 'reverted', reverted_at = ? WHERE id = ?",
            [(stamp, i) for i in reverted],
        )
        conn.executemany(
            "UPDATE focus_x_slots SET status = 'expired', decided_at = ? WHERE id = ?",
            [(stamp, i) for i in expired],
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return reverted, expired


_PROPOSAL_SQL = (
    "SELECT f.*, t.name AS topic_name FROM focus_x_slots f "
    "JOIN focus_topics t ON t.id = f.topic_id WHERE f.id = ?"
)


def next_proposal(
    conn: sqlite3.Connection, config: Config, now: datetime
) -> sqlite3.Row | None:
    """送る案(まだ送っていない proposed の行)。要らなければ None。

    入れ替え中の枠があれば出さない(1 本だけ)。答えを待つ案があれば、送れていないときだけそれを返す
    (送り直し)。無ければ、今の重点(重みの高い順)のうち、今の入りにまだ聞いていない話題で、検索の文を
    作れる最初の 1 つの案を作る。受信デーモンが毎周期呼ぶので、作る必要があるときだけ書く。
    """
    if not config.focus.enabled or not config.x_search.slots:
        return None
    if conn.execute("SELECT 1 FROM focus_x_slots WHERE status = 'approved'").fetchone():
        return None
    pending = conn.execute(
        "SELECT id, message_id FROM focus_x_slots WHERE status = 'proposed'"
    ).fetchone()
    if pending is not None:
        if pending["message_id"] is None:
            return conn.execute(_PROPOSAL_SQL, (pending["id"],)).fetchone()
        return None
    from app.focus import active_focus

    for entry in active_focus(conn, config, now=now):
        topic = conn.execute(
            "SELECT activated_at, terms FROM focus_topics WHERE id = ? AND status = 'active'",
            (entry.topic_id,),
        ).fetchone()
        if topic is None or not topic["activated_at"]:
            continue
        asked = conn.execute(
            "SELECT 1 FROM focus_x_slots WHERE topic_id = ? AND activated_at = ?",
            (entry.topic_id, topic["activated_at"]),
        ).fetchone()
        if asked is not None:
            continue
        query = build_query(_terms(topic["terms"]))
        if query is None or query in config.x_search.slots:
            continue
        proposal_id = _insert_proposal(conn, entry.topic_id, topic["activated_at"], query)
        if proposal_id is not None:
            return conn.execute(_PROPOSAL_SQL, (proposal_id,)).fetchone()
        return None
    return None


def _terms(value: str | None) -> list[str]:
    import json

    try:
        terms = json.loads(value or "[]")
    except ValueError:
        return []
    return terms if isinstance(terms, list) else []


def _insert_proposal(
    conn: sqlite3.Connection, topic_id: int, activated_at: str, query: str
) -> int | None:
    """案の行を作る。重なった呼び出しで先に入れ替え中・答え待ちの行ができていたら作らない。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        busy = conn.execute(
            "SELECT 1 FROM focus_x_slots WHERE status IN ('approved', 'proposed') "
            "OR (topic_id = ? AND activated_at = ?)",
            (topic_id, activated_at),
        ).fetchone()
        proposal_id = None
        if busy is None:
            proposal_id = conn.execute(
                "INSERT INTO focus_x_slots (topic_id, activated_at, query) VALUES (?, ?, ?)",
                (topic_id, activated_at, query),
            ).lastrowid
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return proposal_id


def record_sent(conn: sqlite3.Connection, proposal_id: int, message_id: int) -> None:
    conn.execute(
        "UPDATE focus_x_slots SET message_id = ?, sent_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
        "WHERE id = ? AND message_id IS NULL",
        (message_id, proposal_id),
    )
    conn.commit()


def decide(
    conn: sqlite3.Connection,
    config: Config,
    proposal_id: int,
    message_id: int,
    code: str,
    now: datetime,
) -> tuple[str, sqlite3.Row | None]:
    """案のボタンの答えを記録する。(結果, 記録の後の行)を返す。

    呼び出し側のトランザクションの中で使う。結果は approved(入れ替えた)・declined(入れない)・
    reverted(元に戻した)・expired(答える前に重点から外れていた)・already(同じ答えが記録済み)・
    closed(閉じた案への入れ替え・入れない)・not_approved
    (入れ替えていない案の元に戻す)・no_slot(その番号の枠が無い)・missing(押されたメッセージと結び
    付かない)。押されたメッセージが、その案として送ったメッセージであることを確かめる。
    """
    row = conn.execute(
        "SELECT f.*, t.status AS topic_status, t.activated_at AS topic_activated_at "
        "FROM focus_x_slots f JOIN focus_topics t ON t.id = f.topic_id "
        "WHERE f.id = ? AND f.message_id = ?",
        (proposal_id, message_id),
    ).fetchone()
    if row is None:
        return "missing", None
    stamp = _iso(now)
    status = row["status"]
    if code == REVERT:
        if status == "reverted":
            outcome = "already"
        elif status != "approved":
            outcome = "not_approved"
        else:
            conn.execute(
                "UPDATE focus_x_slots SET status = 'reverted', reverted_at = ? WHERE id = ?",
                (stamp, proposal_id),
            )
            outcome = "reverted"
    elif code == DECLINE:
        if status == "declined":
            outcome = "already"
        elif status != "proposed":
            outcome = "closed"
        else:
            conn.execute(
                "UPDATE focus_x_slots SET status = 'declined', decided_at = ? WHERE id = ?",
                (stamp, proposal_id),
            )
            outcome = "declined"
    else:
        slot = int(code)
        slots = config.x_search.slots
        if status == "approved":
            outcome = "already"
        elif status != "proposed":
            outcome = "closed"
        elif row["topic_status"] != "active" or row["topic_activated_at"] != row["activated_at"]:
            conn.execute(
                "UPDATE focus_x_slots SET status = 'expired', decided_at = ? WHERE id = ?",
                (stamp, proposal_id),
            )
            outcome = "expired"
        elif not 1 <= slot <= len(slots):
            outcome = "no_slot"
        else:
            conn.execute(
                "UPDATE focus_x_slots SET status = 'approved', slot = ?, replaced_query = ?, "
                "decided_at = ? WHERE id = ?",
                (slot, slots[slot - 1], stamp, proposal_id),
            )
            outcome = "approved"
    return outcome, conn.execute(_PROPOSAL_SQL, (proposal_id,)).fetchone()


def render_state(conn: sqlite3.Connection, config: Config) -> list[str]:
    """`intel focus` に出す X の検索の枠の状態(読むだけ)。"""
    lines = []
    row = current_replacement(conn)
    if row is not None:
        day = datetime.strptime(row["decided_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        state = "入れ替え中" if effective_slots(conn, config) != config.x_search.slots else (
            "入れ替え中のはずだが、使っていない(重点から外れた・config の枠が変わった)"
        )
        lines.append(
            f"  {row['slot']} 本目を {_one_line(row['query'])} に{state}"
            f"(話題: {_one_line(row['topic_name'])}・{day.month}/{day.day} に承認)"
        )
    pending = conn.execute(
        "SELECT f.message_id, t.name FROM focus_x_slots f JOIN focus_topics t ON t.id = f.topic_id "
        "WHERE f.status = 'proposed'"
    ).fetchone()
    if pending is not None:
        sent = "送って答えを待っている" if pending["message_id"] is not None else "まだ送れていない"
        lines.append(f"  案: {_one_line(pending['name'])}({sent})")
    return lines or ["  入れ替えなし"]


def _one_line(value: str) -> str:
    return " ".join(str(value).split())
