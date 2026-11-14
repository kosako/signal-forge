"""出来事の調査(GH#415。設計 06 §9)の依頼の台帳(news_investigations)の受け付け側。

poller(ヘッダの「🔎 N」のボタン)と起動係(answer_worker)が使う。app.investigate は Telegram の
組み立て(app.delivery.telegram)を読み込む。app.delivery は読み込むと poller を読み込むので、
poller が app.investigate を直に読むと循環する。台帳の受け付けの関数だけをここに分け、DB と
models だけに依存させる(取り出し・記帳・生成は app.investigate)。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Literal

from app.models import NewsEntry

RequestOutcome = Literal[
    "accepted", "requested", "running", "unsent", "done", "no_sources", "failed"
]


def request_investigation(
    conn: sqlite3.Connection,
    digest_id: int,
    event_index: int,
    entry: NewsEntry,
    *,
    callback_id: str | None = None,
) -> RequestOutcome:
    """出来事の調査を依頼する。同じ digest の同じ見出しの出来事は 1 回だけ作る。

    戻り値は accepted(受け付けた)・requested / running(受け付け済みで、待っている / 作っている)・
    unsent(作り終えたが、公開か送信がまだ)・done(カードを送った)・no_sources(材料の記事が無い)・
    failed(同じ callback の再配達で、その依頼は失敗して閉じている)。失敗した依頼は、もう一度押せば
    受け付け直す。unsent は作り直さず、台帳から公開・送信を進める。

    callback_id はヘッダのボタンを押した callback の id(GH#415 の 4 本目。手で頼む CLI は None)。
    受け付けたとき・受け付け済みと答えたときに行に残し、同じ callback の再配達では受け付け直さない
    (poller が答えた後、offset を保存する前に止まったとき。押し直した callback は id が違う)。
    呼び出し側はトランザクションの中で呼ぶ(状態の確認と反映の間に別の実行が書かない)。
    """
    if not entry.source_ids:
        return "no_sources"
    inserted = conn.execute(
        "INSERT INTO news_investigations (digest_id, event_index, label, summary, item_ids, "
        "request_callback_id) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (digest_id, label) DO NOTHING",
        (
            digest_id,
            event_index,
            entry.label,
            entry.summary,
            json.dumps(entry.source_ids),
            callback_id,
        ),
    ).rowcount
    if inserted:
        return "accepted"
    row = conn.execute(
        "SELECT id, status, message_id, request_callback_id FROM news_investigations"
        " WHERE digest_id = ? AND label = ?",
        (digest_id, entry.label),
    ).fetchone()
    if callback_id is not None and row["request_callback_id"] == callback_id:
        return _current_outcome(row)  # 同じ callback の再配達。受け付け直さない
    if callback_id is not None:
        conn.execute(
            "UPDATE news_investigations SET request_callback_id = ? WHERE id = ?",
            (callback_id, row["id"]),
        )
    if row["status"] == "failed":
        retried = conn.execute(
            "UPDATE news_investigations SET status = 'requested', "
            "requested_at = strftime('%Y-%m-%dT%H:%M:%SZ','now'), started_at = NULL, "
            "finished_at = NULL, failure = NULL WHERE id = ? AND status = 'failed'",
            (row["id"],),
        ).rowcount
        return "accepted" if retried else "requested"
    return _current_outcome(row)


def _current_outcome(row: sqlite3.Row) -> RequestOutcome:
    if row["status"] == "done" and row["message_id"] is None:
        return "unsent"
    return row["status"]


def investigated_labels(conn: sqlite3.Connection, digest_id: int) -> set[str]:
    """その digest で、調査を受け付けた(か、作った)出来事の見出し。ヘッダのボタンの ✓ に使う。"""
    return {
        row["label"]
        for row in conn.execute(
            "SELECT label FROM news_investigations WHERE digest_id = ? AND status != 'failed'",
            (digest_id,),
        )
    }


def has_investigation_requests(conn: sqlite3.Connection) -> bool:
    """作っていない依頼か、作り始めた印の残った依頼があるか。

    poller が `intel dig-now` を起動する合図(今すぐの深掘りの has_now_requests と同じ)。印の残った
    依頼は、作っている途中か、前の実行が途中で止まったもの。作っている途中なら、起動した dig-now は
    ロックが取れずに何もせず終わる。止まったものは、起動した dig-now が失敗として閉じて知らせる。
    """
    return (
        conn.execute(
            "SELECT 1 FROM news_investigations WHERE status IN ('requested', 'running') LIMIT 1"
        ).fetchone()
        is not None
    )


def has_unsent_investigations(conn: sqlite3.Connection) -> bool:
    """作り終えて、公開か送信がまだの調査があるか(中断した分の再開の合図)。"""
    return (
        conn.execute(
            "SELECT 1 FROM news_investigations WHERE status = 'done' AND message_id IS NULL "
            "LIMIT 1"
        ).fetchone()
        is not None
    )
