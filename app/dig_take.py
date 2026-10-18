"""深掘りカードへのリプライのうち take を記録する(GH#106 / 設計 06 §6 / ADR 0003)。

- take かどうかは、返事を作る LLM 呼び出しが返す分類(dig_questions.kind)で決める
  (GH#126。別の呼び出しを増やさない)。take / both が対象で、question は記録しない
- 記録は 2 つ: 解説ノートの末尾(take 領域)への追記と、followup_dig = yes(未消化一覧から
  消える = ブックマークの消化動線。GH#99 の既知境界)。記録済みかの正本は台帳
  (dig_questions.take_recorded_at)
- 書くのは本人のリプライの原文だけ(LLM の出力はノートに書かない。知見として確定するのは
  本人の take だけ = ADR 0003)。take の中身は KPI・ランキングに使わない(設計 06 §6)。
  followup_dig = yes は「深掘りした」の記録なので、🔖 の付いたアイテムでは KPI 06(フェーズ2
  境界の実施率)の分子に入る(🔖 の無いアイテムは KPI 06 が数えない)
- 返事を送れた行だけを記録する(返事が届く前に未消化一覧から消さない)
- 公開済みのノートは本人のもの(Obsidian で開いて編集していることもある)なので、**追記だけ**を
  行い、書き換えない。末尾への追記(O_APPEND)は今ある中身に触れないので、本人の編集を消す
  ことは無い(エディタが古い内容で保存し直したときに消えうるのは、こちらが足した take だけ)。
  frontmatter の status は書き換えない(1 行だけを書き換える手段はファイルの書き直ししか無いため。
  本人が手で take済 にしてよい)。追記には質問 id の印を付け、やり直しで 2 回書かない
- 本人がノートを動かした・消したときは、ノートへの追記だけを諦める(原文は台帳と item_notes に
  残る)。vault に書けない一時的な失敗(DeepDive の置き場ごと見えないときを含む)は記録しない
  まま残し、次の実行で再試行する。どの失敗もその 1 件に閉じ、後の take は記録する
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

from app.config import Config
from app.dig_answer import _deepdive_dir

_NOW_UTC = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"


class _NoteMissing(Exception):
    """解説ノートが無い・DeepDive の外を指す(本人が動かした・消した)。再試行しても直らない。"""


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
    """ノートの末尾に take を足す(今ある中身は書き換えない)。"""
    path = _note_path(deepdive_dir, row["note_name"])
    marker = f"<!-- signal-forge:take q={row['id']} -->"
    current = path.read_bytes()
    if marker.encode("utf-8") in current:
        return  # 前回の実行でノートには書けていた(台帳の記帳の前に止まった)
    heading = f"**take**({row['take_time']}・Telegram のリプライ)"
    block = f"\n{heading}\n\n{row['question']}\n\n{marker}\n"
    if current and not current.endswith(b"\n"):
        block = "\n" + block
    # O_APPEND: 書く位置は常にその時点の末尾。O_NOFOLLOW: 確かめた後に symlink へ
    # すり替えられても辿らない
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
    try:
        data = block.encode("utf-8")
        while data:
            data = data[os.write(fd, data):]
    finally:
        os.close(fd)


def _note_path(deepdive_dir: Path | None, note_name: str) -> Path:
    # 読む側(dig_answer._read_note)と同じく、DeepDive の外を指す名前・symlink は扱わない
    if deepdive_dir is None:
        raise _NoteMissing("vault が未設定")
    # DeepDive の置き場そのものが無いのは、vault が一時的に使えない(外付けの取り外しなど)と
    # みて再試行に回す(FileNotFoundError をそのまま上げる)。置き場があってノートだけ無いのは
    # 本人が動かした・消した
    base = deepdive_dir.resolve(strict=True)
    try:
        path = (deepdive_dir / f"{note_name}.md").resolve(strict=True)
    except FileNotFoundError as e:
        raise _NoteMissing("ノートが見つからない") from e
    if path.parent != base or not path.is_file():
        raise _NoteMissing("ノートが DeepDive の外を指す")
    return path


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
