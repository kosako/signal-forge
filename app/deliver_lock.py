"""配信のロック(`<DB>.deliver.lock`)。配信と digest の置換が共有する共通の部品(設計 00 §3)。

- 配信(delivery/telegram.py の deliver_digest)は台帳の確認から送信・記帳・完了の更新まで持つ(GH#192)
- digest の --force の置換(digest/builder.py)は置換のトランザクションの前に取り、先の配信が
  終わるまで待つ(GH#333。配信の途中で内容と台帳の世代を置き換えると、新しい世代が届かないまま
  送信済みになる。設計 04 §2)
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

# DB の隣の `<DB>.deliver.lock`(migrate.lock と同じ名付け)
DELIVER_LOCK_SUFFIX = ".deliver.lock"


@contextlib.contextmanager
def delivery_lock(
    conn: sqlite3.Connection,
    logger: logging.Logger,
    *,
    run_id: str | None,
    stage: str,
    wait_message: str,
) -> Iterator[None]:
    """同じ DB の配信と digest の置換をプロセス間で直列化する(GH#192・GH#333)。

    ロックファイルは接続が開いている DB の隣に置く(config の db_path ではなく接続から引き、
    同じ DB を開いた処理どうしを確実に揃える)。先の側が終わるまで待つ(待つときは wait_message を
    INFO に残す)。メモリ上の DB は他のプロセスと共有されないので直列化しない。
    SQLite の書き込みのトランザクションを開いたまま呼ばない(待つ間に相手の書き込みを止める)。
    """
    db_file = next(
        (row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"), ""
    )
    if not db_file:
        yield
        return
    db_path = Path(db_file)
    lock_path = db_path.with_name(db_path.name + DELIVER_LOCK_SUFFIX)
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info(wait_message, extra={"run_id": run_id, "stage": stage})
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # close で flock は解放される
