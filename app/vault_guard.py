"""vault へのアクセスに時間の上限を付ける(GH#145)。

macOS のプライバシーの確認(書類フォルダへのアクセス)が出ると、vault の open() は本人が応答
するまで止まる。カードへの返事(`intel answer`)がその間ずっと止まらないよう、vault に触れる処理を
別スレッドで走らせ、上限の時間を超えたら待つのをやめて先へ進む。

止まったスレッドは外から中断できないので、1 度上限を超えたら、その実行の残りでは vault に
触れない(止まったスレッドを増やさない)。止まったスレッドはプロセスの終了とともに消える。
"""

from __future__ import annotations

import threading
from collections.abc import Callable

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
