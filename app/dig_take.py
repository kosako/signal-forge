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
  本人が手で take済 にしてよい)。追記の見出しに記録番号(質問 id)を入れ、やり直しで 2 回書かない
  (途中まで書いて止まっていたら続きだけを足す)。確かめた DeepDive の置き場を fd で押さえて書く
- 本人がノートを動かした・消したときは、ノートへの追記だけを諦める(原文は台帳と item_notes に
  残る)。vault に書けない一時的な失敗(DeepDive の置き場ごと見えないときを含む)は記録しない
  まま残し、次の実行で再試行する。どの失敗もその 1 件に閉じ、後の take は記録する
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import sqlite3
import stat
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
    """ノートの末尾に take を足す(今ある中身は書き換えない)。

    確かめた DeepDive の置き場を fd で押さえ、その中のノートを開いて読み書きする(確かめた後に
    置き場や途中のフォルダが symlink にすり替えられても、DeepDive の外には書かない)。
    """
    if deepdive_dir is None:
        raise _NoteMissing("vault が未設定")
    # DeepDive の置き場そのものが無いのは、vault が一時的に使えない(外付けの取り外しなど)と
    # みて再試行に回す(FileNotFoundError をそのまま上げる)。置き場があってノートだけ無いのは
    # 本人が動かした・消した
    base = deepdive_dir.resolve(strict=True)
    name = f"{row['note_name']}.md"
    if os.sep in name or name.startswith("."):
        raise _NoteMissing("ノート名が DeepDive の外を指す")
    dir_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if _fd_path(dir_fd) != str(base):
            raise OSError(errno.ESTALE, "DeepDive の置き場が確かめたときと違う", str(base))
        try:
            fd = os.open(name, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW, dir_fd=dir_fd)
        except FileNotFoundError as e:
            raise _NoteMissing("ノートが見つからない") from e
        except OSError as e:
            if e.errno == errno.ELOOP:  # ノートが symlink(DeepDive の外を指しうる)
                raise _NoteMissing("ノートが symlink") from e
            raise
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise _NoteMissing("ノートが通常のファイルでない")
            data = _remaining(_read_all(fd), _take_block(row))
            # O_APPEND: 書く位置は常にその時点の末尾(今ある中身には触れない)
            while data:
                data = data[os.write(fd, data):]
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


def _take_block(row: sqlite3.Row) -> bytes:
    """ノートに足す take の節。見出しの記録番号(質問 id)で、書いたかどうかを見分ける。

    印に HTML コメントを使わない: 途中まで書いて止まると閉じていない `<!--` が残り、Obsidian で
    その後ろが全部隠れるため。見出しの断片が残っても、ただの文字列で済む。
    """
    heading = f"**take**({row['take_time']}・Telegram のリプライ・記録番号 {row['id']})"
    return f"\n{heading}\n\n{row['question']}\n".encode()


def _remaining(current: bytes, block: bytes) -> bytes:
    """block のうち、まだノートに無い部分を返す(途中まで書いて止まったときは続きだけ)。"""
    anchor = block.split(b"\n", 2)[1]  # 見出しの行
    i = current.find(anchor)
    if i != -1:
        written = current[i:]
        expected = block[block.index(anchor):]
        if expected.startswith(written):
            return expected[len(written):]  # 途中まで書けていた: 続きだけを足す
        return b""  # 書き終えている(後ろに本人が書き足した・書き換えたものは触らない)
    # 見出しの途中で止まっていた: ノートの末尾が block の先頭と重なる分は書かない
    for k in range(min(len(current), len(block) - 1), 2, -1):
        if current.endswith(block[:k]):
            return block[k:]
    if current and not current.endswith(b"\n"):
        return b"\n" + block
    return block


def _read_all(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while chunk := os.read(fd, 65536):
        chunks.append(chunk)
    return b"".join(chunks)


def _fd_path(fd: int) -> str:
    """開いた fd の実際のパス(macOS の F_GETPATH)。"""
    raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
    return os.fsdecode(raw.split(b"\0", 1)[0])


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
