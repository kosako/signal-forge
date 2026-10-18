"""深掘りカードへのリプライのうち take を記録する(GH#106 / 設計 06 §6 / ADR 0003)。

- take かどうかは、返事を作る LLM 呼び出しが返す分類(dig_questions.kind)で決める
  (GH#126。別の呼び出しを増やさない)。take / both が対象で、question は記録しない
- 記録は 3 つ: 解説ノートの末尾(take 領域)への追記、ノートの status を take待ち → take済、
  followup_dig = yes(未消化一覧から消える = ブックマークの消化動線。GH#99 の既知境界)
- 書くのは本人のリプライの原文だけ(LLM の出力はノートに書かない。知見として確定するのは
  本人の take だけ = ADR 0003)。KPI・ランキングへは接続しない(設計 06 §6)
- 公開済みのノートは本人のものなので、末尾への追記と frontmatter の status 行の置き換えだけを
  行い、他の部分は書き換えない。追記には質問 id の印を付け、やり直しで 2 回書かない
- 本人がノートを動かした・消したときは、ノートへの記録だけを諦める(原文は台帳と
  item_notes に残る)。vault に書けない一時的な失敗は記録しないまま残し、次の実行で再試行する
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

from app.config import Config
from app.dig_answer import _deepdive_dir
from app.export.obsidian import atomic_write

_NOW_UTC = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"
_STATUS_BEFORE = "status: take待ち"
_STATUS_AFTER = "status: take済"


class _NoteMissing(Exception):
    """解説ノートが無い・DeepDive の外を指す(本人が動かした・消した)。再試行しても直らない。"""


def has_unrecorded_take(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM dig_questions WHERE status = 'answered' "
            "AND kind IN ('take', 'both') AND take_recorded_at IS NULL LIMIT 1"
        ).fetchone()
        is not None
    )


def record_takes(conn: sqlite3.Connection, config: Config, logger: logging.Logger) -> int:
    """未記録の take を古い順に記録し、記録した件数を返す(1 件の失敗は他を止めない)。"""
    rows = conn.execute(
        """
        SELECT q.id, q.question, date(q.created_at, '+9 hours') AS take_date,
               strftime('%Y-%m-%d %H:%M', q.created_at, '+9 hours') AS take_time,
               r.item_id, r.note_name
        FROM dig_questions q JOIN dig_results r ON r.id = q.dig_result_id
        WHERE q.status = 'answered' AND q.kind IN ('take', 'both')
          AND q.take_recorded_at IS NULL
        ORDER BY q.id
        """
    ).fetchall()
    recorded = 0
    for row in rows:
        log_extra = {"stage": "answer", "item_id": row["item_id"],
                     "extra": {"question_id": row["id"]}}
        try:
            _append_to_note(_deepdive_dir(config), row)
        except _NoteMissing:
            logger.warning(
                "解説ノートが見つからず take をノートに書けなかった(台帳と感想には残る)",
                extra=log_extra,
            )
        except OSError as e:
            logger.error(
                f"take をノートに書けなかった(次の実行で再試行): {type(e).__name__}: {e}",
                extra=log_extra,
            )
            continue
        try:
            # followup_dig と記録済みの印は同じトランザクションで(接続は autocommit)
            conn.execute("BEGIN IMMEDIATE")
            _record_followup(conn, row)
            conn.execute(
                f"UPDATE dig_questions SET take_recorded_at = {_NOW_UTC} WHERE id = ?",
                (row["id"],),
            )
            conn.execute("COMMIT")
        except sqlite3.Error as e:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            logger.error(
                f"take を台帳に記録できなかった(次の実行で再試行): {type(e).__name__}: {e}",
                extra=log_extra,
            )
            continue
        recorded += 1
        logger.info("take を記録した", extra=log_extra)
    return recorded


def _append_to_note(deepdive_dir: Path | None, row: sqlite3.Row) -> None:
    path = _note_path(deepdive_dir, row["note_name"])
    content = path.read_text(encoding="utf-8")
    marker = f"<!-- signal-forge:take q={row['id']} -->"
    if marker in content:
        return  # 前回の実行でノートには書けていた(台帳の記帳の前に止まった)
    content = _mark_taken(content)
    if not content.endswith("\n"):
        content += "\n"
    heading = f"**take**({row['take_time']}・Telegram のリプライ)"
    content += f"\n{heading}\n\n{row['question']}\n\n{marker}\n"
    atomic_write(path, content)


def _note_path(deepdive_dir: Path | None, note_name: str) -> Path:
    # 読む側(dig_answer._read_note)と同じく、DeepDive の外を指す名前・symlink は扱わない
    if deepdive_dir is None:
        raise _NoteMissing
    try:
        base = deepdive_dir.resolve(strict=True)
        path = (deepdive_dir / f"{note_name}.md").resolve(strict=True)
    except FileNotFoundError as e:
        raise _NoteMissing from e
    if path.parent != base or not path.is_file():
        raise _NoteMissing
    return path


def _mark_taken(content: str) -> str:
    """frontmatter の status 行だけを take済 にする(本人が書き換えた status はそのまま)。"""
    match = re.match(r"---\n(.*?\n)---\n", content, flags=re.DOTALL)
    if match is None:
        return content
    front = match.group(1)
    updated = re.sub(
        rf"^{re.escape(_STATUS_BEFORE)}$", _STATUS_AFTER, front, count=1, flags=re.MULTILINE
    )
    return content[: match.start(1)] + updated + content[match.end(1):]


def _record_followup(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """followup_dig = yes(既にあれば足さない)。signal_date は 🔖 を付けた日(設計 01 §7)。

    🔖 の無いアイテム(X のブックマーク・自動キュー)は take の日にする。
    """
    exists = conn.execute(
        "SELECT 1 FROM signals WHERE kind = 'followup_dig' AND item_id = ? AND value = 'yes'",
        (row["item_id"],),
    ).fetchone()
    if exists is not None:
        return
    dig = conn.execute(
        """
        SELECT signal_date FROM signals
        WHERE kind = 'reaction' AND item_id = ? AND value = 'dig'
        ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (row["item_id"],),
    ).fetchone()
    conn.execute(
        "INSERT INTO signals (kind, item_id, value, signal_date) "
        "VALUES ('followup_dig', ?, 'yes', ?)",
        (row["item_id"], dig["signal_date"] if dig is not None else row["take_date"]),
    )
