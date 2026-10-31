"""vault へのアクセスに時間の上限を付ける(GH#145)。

macOS のプライバシーの確認(書類フォルダへのアクセス)が出ると、vault の open() は本人が応答
するまで止まる。カードへの返事(`intel answer`)や深掘りのノートの公開(GH#234)がその間ずっと
止まらないよう、vault に触れる処理を別スレッドで走らせ、上限の時間を超えたら待つのをやめて先へ進む。

止まったスレッドは外から中断できないので、1 度上限を超えたら、その実行の残りでは vault に
触れない(止まったスレッドを増やさない)。止まったスレッドはプロセスの終了とともに消える。

止まったスレッドは後から書き終えうる。別の実行が同じノートを書いたあとで、遅れた書き込みが
それを置き換えないよう、書く側は VaultWriteLock を使う(Daily / Items の export。GH#341)。
"""

from __future__ import annotations

import fcntl
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

VAULT_TIMEOUT_S = 10.0  # ふだんの読み書きは数ミリ秒。確認待ちと見分けられれば十分


class VaultStalled(Exception):
    """vault へのアクセスが上限の時間内に終わらなかった(この実行では以後 vault に触れない)。"""


class VaultGuard:
    def __init__(self, timeout_s: float = VAULT_TIMEOUT_S) -> None:
        self._timeout_s = timeout_s
        self.stalled = False

    def call[T](self, fn: Callable[[], T]) -> T:
        """fn を上限の時間つきで実行し、その戻り値を返す(fn の例外はそのまま上げる)。"""
        if self.stalled:
            raise VaultStalled("この実行の前のアクセスが止まったまま")
        outcome: dict[str, object] = {}

        def run() -> None:
            try:
                outcome["value"] = fn()
            except BaseException as e:  # 呼び出し側のスレッドで上げ直す
                outcome["error"] = e

        thread = threading.Thread(target=run, name="vault-io", daemon=True)
        thread.start()
        thread.join(self._timeout_s)
        if thread.is_alive():
            self.stalled = True
            raise VaultStalled(
                f"{self._timeout_s:g} 秒で終わらなかった(macOS のプライバシーの確認待ちなど)"
            )
        if "error" in outcome:
            raise outcome["error"]  # type: ignore[misc]
        return outcome["value"]  # type: ignore[return-value]


class VaultWriteLock:
    """vault への書き込みを、同じ置き場を書く実行どうしで 1 つに絞る flock(GH#341)。

    見切ったスレッドは中断できず、後から書き終えうる。その間に別の実行が同じノートを書き、
    遅れた書き込みがそれを古い内容で置き換えないよう、見切ったときはそのスレッドが終わるまで
    握る。flock なのでプロセスが終われば外れる(止まったスレッドもプロセスと一緒に消える)。
    深掘りのノートの公開のロック(deepdive の _PublishLock。GH#234)と同じ考え方。

    放す役は 1 つの mutex の下で決める: vault-io のスレッドの終わり(end_write)と呼び出し側の
    離脱(leave)のうち、後になった側が放す。見切りの判定の直後にスレッドが書き終えても、
    どちらも放さずに残ることはない。見切った時にまだ動き出していなかったスレッドは、後から
    動き出しても書かない(放したロックの外で書かせない)。
    """

    def __init__(self, fd: int | None) -> None:
        self._fd = fd
        self._mutex = threading.Lock()
        self._writing = False  # vault-io のスレッドが動いている
        self._left = False  # 呼び出し側が書き込みを終えた(見切った場合を含む)

    @classmethod
    def acquire(cls, path: Path | None, wait_s: float) -> VaultWriteLock | None:
        """path に flock を取る。wait_s を過ぎても別の実行が握っていれば None。

        path が None(ファイルでない DB。別の実行と置き場を共有しない)なら取らずに返す。
        """
        if path is None:
            return cls(None)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            deadline = time.monotonic() + wait_s
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return cls(fd)
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.1)
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)
        return None

    def call[T](self, vault: VaultGuard, fn: Callable[[], T]) -> T:
        """fn を vault の上限の時間つきで、このロックの中で実行する(VaultGuard.call と同じ)。"""

        def guarded() -> T | None:
            if not self.begin_write():  # 見切られた後に動き出した。ロックは放されたので書かない
                return None
            try:
                return fn()
            finally:
                self.end_write()  # 見切られた後に終わったなら、ここで放す

        # 書かずに戻るのは呼び出し側が見切って離れた後だけで、その戻り値は誰も受け取らない
        return vault.call(guarded)  # type: ignore[return-value]

    def begin_write(self) -> bool:
        """vault-io のスレッドの始まり。呼び出し側が既に離れていれば False(書かない)。"""
        with self._mutex:
            if self._left:
                return False
            self._writing = True
            return True

    def end_write(self) -> None:
        """vault-io のスレッドの終わり。呼び出し側が先に離れていれば、ここで放す。"""
        with self._mutex:
            self._writing = False
            if self._left:
                self._close()

    def leave(self) -> None:
        """呼び出し側の離脱。動いているスレッドがあれば、放すのはその終わり(end_write)に任せる。"""
        with self._mutex:
            self._left = True
            if not self._writing:
                self._close()

    def _close(self) -> None:
        """放す(fd を閉じると flock も外れる。2 回目は何もしない)。mutex の中で呼ぶ。"""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
