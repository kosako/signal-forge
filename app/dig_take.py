"""深掘りカードへのリプライのうち take を記録する(GH#106 / 設計 06 §6 / ADR 0003)。

- take かどうかは、本人の入力だけを渡す意図の判定の LLM 呼び出しが返す分類
  (dig_questions.kind)で決める(GH#245。外部由来の材料を読む返事の呼び出しの出力では
  決めない)。take / both が対象で、question は記録しない
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
  (途中まで書いて止まっていたら続きだけを足す)。書き込み先は vault から 1 段ずつ symlink を
  辿らずに開く
- 本人がノートを動かした・消したときは、ノートへの追記だけを諦める(原文は台帳と item_notes に
  残る)。vault に書けない一時的な失敗(DeepDive の置き場ごと見えないときを含む)は記録しない
  まま残し、次の実行で再試行する。時間の上限を超えたとき(下)を除き、どの失敗もその 1 件に
  閉じ、後の take は記録する
- vault への追記には時間の上限を付ける(macOS の確認待ちなどで止まったまま返事の処理を
  止めない。GH#145)。上限を超えたら、この実行では残りの take を記録せず次の実行に回す
  (止まった追記が後から終わっても、節の記録番号で 2 回は書かない)
"""

from __future__ import annotations

import errno
import logging
import os
import sqlite3
import stat

from app.config import Config
from app.vault_dir import VaultPathError, folder_dir
from app.vault_guard import VaultGuard, VaultStalled

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


def record_takes(
    conn: sqlite3.Connection,
    config: Config,
    logger: logging.Logger,
    vault: VaultGuard | None = None,
) -> int:
    """未記録の take を古い順に記録し、記録した件数を返す(1 件の失敗は他を止めない)。"""
    vault = vault or VaultGuard()
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
            vault.call(lambda row=row: _append_to_note(config, row))
        except VaultStalled as e:
            logger.error(
                f"vault が応答しないので take の記録をこの実行では止める(次の実行で再試行): {e}",
                extra=log_extra,
            )
            break
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


def _append_to_note(config: Config, row: sqlite3.Row) -> None:
    """ノートの末尾に take を足す(今ある中身は書き換えない)。"""
    name = f"{row['note_name']}.md"
    if os.sep in name or name.startswith("."):
        raise _NoteMissing("ノート名が DeepDive の外を指す")
    dir_fd = _open_deepdive(config)
    try:
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
            data = _remaining(_read_all(fd), _take_body(row))
            # O_APPEND: 書く位置は常にその時点の末尾(今ある中身には触れない)
            while data:
                data = data[os.write(fd, data):]
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


def _open_deepdive(config: Config) -> int:
    """DeepDive の置き場を fd で開く。本人が設定した vault を起点に、その下のフォルダを 1 段ずつ
    symlink を辿らずに開く(途中のフォルダが外への symlink でも、外の置き場を基準にしない)。

    vault そのものが無い・途中のフォルダが無いのは、vault が一時的に使えない(外付けの取り外し
    など)とみて再試行に回す(FileNotFoundError をそのまま上げる)。途中が symlink・フォルダで
    ないとき(置き場の名前が不正なときを含む)は、ノートへの追記を諦める。開き方はノートの出力・
    返事の材料の読み取りと共通(app/vault_dir.py。GH#204)。
    """
    # 呼び出し時に import する(deepdive → delivery → poller → answer_worker → 本モジュール
    # の循環を避ける)
    from app.deepdive import DEEPDIVE_FOLDER

    if config.obsidian.vault_path is None:
        raise _NoteMissing("vault が未設定")
    try:
        return folder_dir(config, DEEPDIVE_FOLDER).open()
    except VaultPathError as e:
        raise _NoteMissing(f"DeepDive までのフォルダ: {e}") from e


def _take_body(row: sqlite3.Row) -> bytes:
    """ノートに足す take の節。見出しの記録番号(質問 id)で、書いたかどうかを見分ける。

    印に HTML コメントを使わない: 途中まで書いて止まると閉じていない `<!--` が残り、Obsidian で
    その後ろが全部隠れるため。見出しの断片が残っても、ただの文字列で済む。
    """
    heading = f"**take**({row['take_time']}・Telegram のリプライ・記録番号 {row['id']})"
    return f"{heading}\n\n{row['question']}\n".encode()


def _remaining(current: bytes, body: bytes) -> bytes:
    """ノートに足す分を返す。

    - 節がそのまま入っていれば何も足さない(書き終えている)
    - 末尾が行頭からの節の書きかけなら、続きだけを足す(途中まで書いて止まった)
    - それ以外(書きかけの後ろに本人のメモなどが続く場合を含む)は、空行を置いて節を丸ごと足す。
      書きかけの断片は残るが、take は欠けずに入る
    """
    if body in current:
        return b""
    for k in range(min(len(body) - 1, len(current)), 0, -1):
        # 行頭 = 改行の直後か、ファイルの先頭(空のノートに書きかけた場合)
        if current.endswith(b"\n" + body[:k]) or current == body[:k]:
            return body[k:]
    if not current or current.endswith(b"\n\n"):
        return body
    return (b"\n" if current.endswith(b"\n") else b"\n\n") + body


def _read_all(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while chunk := os.read(fd, 65536):
        chunks.append(chunk)
    return b"".join(chunks)


def _record_followup(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """followup_dig = yes(最新の記録が yes なら足さない)。signal_date は 🔖 の reaction と
    同じ配信日(設計 01 §7)。

    🔖 の無いアイテム(X のブックマーク・自動キュー)は take の日にする。追い掛けで
    「いいえ」に訂正した後の take は yes を足す(未消化一覧と KPI 06 は最新の記録を見る。GH#195)。
    """
    latest = conn.execute(
        """
        SELECT value FROM signals
        WHERE kind = 'followup_dig' AND item_id = ?
        ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (row["item_id"],),
    ).fetchone()
    if latest is not None and latest["value"] == "yes":
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
