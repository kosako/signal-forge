"""深掘りカードへのリプライのうち take を記録する(GH#106 / 設計 06 §6 / ADR 0003)。

- take かどうかは、返事を作る LLM 呼び出しが返す分類(dig_questions.kind)で決める
  (GH#126。別の呼び出しを増やさない)。take / both が対象で、question は記録しない
- 記録は 3 つ: 解説ノートの末尾(take 領域)への追記、ノートの status を take待ち → take済、
  followup_dig = yes(未消化一覧から消える = ブックマークの消化動線。GH#99 の既知境界)
- 書くのは本人のリプライの原文だけ(LLM の出力はノートに書かない。知見として確定するのは
  本人の take だけ = ADR 0003)。take の中身は KPI・ランキングに使わない(設計 06 §6)。
  followup_dig = yes は「深掘りした」の記録なので、🔖 の付いたアイテムでは KPI 06(フェーズ2
  境界の実施率)の分子に入る(🔖 の無いアイテムは KPI 06 が数えない)
- 返事を送れた行だけを記録する(返事が届く前に未消化一覧から消さない)
- 公開済みのノートは本人のものなので、末尾への追記と frontmatter の status 行の置き換えだけを
  行い、他の部分は書き換えない。追記には質問 id の印を付け、やり直しで 2 回書かない。読んだ後に
  本人がノートを保存していたら置き換えず、次の実行でやり直す(本人の編集を上書きしない)
- 本人がノートを動かした・消した・UTF-8 で読めない形にしたときは、ノートへの記録だけを諦める
  (ファイルは変えない。原文は台帳と item_notes に残る)。vault に書けない一時的な失敗は記録しない
  まま残し、次の実行で再試行する。どの失敗もその 1 件に閉じ、後の take は記録する
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
from pathlib import Path

from app.config import Config
from app.dig_answer import _deepdive_dir

_NOW_UTC = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"
_STATUS_BEFORE = "status: take待ち"
_STATUS_AFTER = "status: take済"


class _NoteMissing(Exception):
    """解説ノートが無い・DeepDive の外を指す・UTF-8 で読めない。再試行しても直らない。"""


class _NoteChanged(Exception):
    """読んだ後に本人がノートを保存した(置き換えると本人の編集が消える)。次の実行でやり直す。"""


def has_unrecorded_take(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM dig_questions WHERE status = 'answered' "
            "AND kind IN ('take', 'both') AND answer_message_id IS NOT NULL "
            "AND take_recorded_at IS NULL LIMIT 1"
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
          AND q.answer_message_id IS NOT NULL AND q.take_recorded_at IS NULL
        ORDER BY q.id
        """
    ).fetchall()
    recorded = 0
    for row in rows:
        log_extra = {"stage": "answer", "item_id": row["item_id"],
                     "extra": {"question_id": row["id"]}}
        try:
            _append_to_note(_deepdive_dir(config), row)
        except _NoteMissing as e:
            logger.warning(
                f"解説ノートに take を書けなかった({e}。台帳と感想には残る)", extra=log_extra
            )
        except _NoteChanged:
            logger.warning(
                "読んだ後に解説ノートが保存されたので、take の追記を次の実行に回す", extra=log_extra
            )
            continue
        except Exception as e:  # vault に書けない一時的な失敗など。この 1 件に閉じる
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
    original = path.read_bytes()
    try:
        content = original.decode("utf-8")
    except UnicodeDecodeError as e:
        raise _NoteMissing("UTF-8 で読めない") from e
    marker = f"<!-- signal-forge:take q={row['id']} -->"
    if marker in content:
        return  # 前回の実行でノートには書けていた(台帳の記帳の前に止まった)
    content = _mark_taken(content)
    if not content.endswith("\n"):
        content += "\n"
    heading = f"**take**({row['take_time']}・Telegram のリプライ)"
    content += f"\n{heading}\n\n{row['question']}\n\n{marker}\n"
    _replace_if_unchanged(path, original, content)


def _replace_if_unchanged(path: Path, original: bytes, content: str) -> None:
    """一時ファイルに書いてから置き換える。置き換える直前に読み直し、読んだときから変わって
    いれば置き換えない(本人の保存を上書きしない。export の atomic_write と同じ書き方に、
    この確認を足したもの)。確認と置き換えの間の短い隙間は残る。"""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        if path.read_bytes() != original:
            raise _NoteChanged
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _note_path(deepdive_dir: Path | None, note_name: str) -> Path:
    # 読む側(dig_answer._read_note)と同じく、DeepDive の外を指す名前・symlink は扱わない
    if deepdive_dir is None:
        raise _NoteMissing("vault が未設定")
    try:
        base = deepdive_dir.resolve(strict=True)
        path = (deepdive_dir / f"{note_name}.md").resolve(strict=True)
    except FileNotFoundError as e:
        raise _NoteMissing("ノートが見つからない") from e
    if path.parent != base or not path.is_file():
        raise _NoteMissing("ノートが DeepDive の外を指す")
    return path


def _mark_taken(content: str) -> str:
    """frontmatter の status 行だけを take済 にする(本人が書き換えた status はそのまま)。

    frontmatter は先頭の `---` 行から次の `---` 行まで(空の frontmatter も同じ規則。本文の
    水平線を frontmatter の終わりと取り違えない)。
    """
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return content
    for i in range(1, len(lines)):
        line = lines[i].rstrip("\r\n")
        if line == "---":
            return content  # frontmatter に take待ち の status 行が無い
        if line == _STATUS_BEFORE:
            lines[i] = _STATUS_AFTER + lines[i][len(_STATUS_BEFORE):]
            return "".join(lines)
    return content  # 閉じの `---` が無い = frontmatter ではない


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
