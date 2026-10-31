"""vault へのアクセスの時間の上限(GH#145)。"""

import threading
from types import SimpleNamespace

import pytest

from app import vault_guard as vault_guard_module
from app.vault_guard import VaultGuard, VaultStalled, VaultWriteLock

# 止まったときに suite を止めないための上限(順序はこの長さに頼らない)
HANG_GUARD_S = 30


class GatedThread(threading.Thread):
    """VaultGuard の vault-io のスレッドの代役。上限の時間の代わりに、fn が終わるか、止める
    位置(mark_blocked)に着くまで待つ(見切るかを時間でなく、着いた位置で決める。GH#341)。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.wake = threading.Event()
        self.blocked = False

    def run(self) -> None:
        try:
            super().run()
        finally:
            self.wake.set()

    def join(self, timeout: float | None = None) -> None:
        self.wake.wait(HANG_GUARD_S)
        if not self.blocked:  # 終わる途中なら終わりきるまで待つ(生きていると見て見切らない)
            super().join(HANG_GUARD_S)


def gate_vault_threads(monkeypatch) -> None:
    """VaultGuard のスレッドを GatedThread に差し替える(ロックの mutex は本物のまま)。"""
    monkeypatch.setattr(
        vault_guard_module,
        "threading",
        SimpleNamespace(Thread=GatedThread, Lock=threading.Lock),
    )


def mark_blocked() -> None:
    """vault-io のスレッドが止める位置に着いた(呼び出し側はここで見切る)。"""
    thread = threading.current_thread()
    if isinstance(thread, GatedThread):
        thread.blocked = True
        thread.wake.set()


def join_vault_threads() -> list[threading.Thread]:
    """vault-io のスレッドの終わりを待ち、終わらなかったものを返す。

    GatedThread の join は止める位置に着けば戻るので、本来の join で終わりまで待つ。
    """
    threads = [t for t in threading.enumerate() if t.name == "vault-io"]
    for thread in threads:
        threading.Thread.join(thread, HANG_GUARD_S)
    return [thread for thread in threads if thread.is_alive()]


def test_returns_value_and_reraises_errors() -> None:
    guard = VaultGuard(timeout_s=5)
    assert guard.call(lambda: "本文") == "本文"

    def broken() -> None:
        raise PermissionError("vault に書けない")

    with pytest.raises(PermissionError):
        guard.call(broken)
    assert not guard.stalled  # 失敗は止まったのと違う(次のアクセスは行う)


def test_stall_gives_up_and_later_calls_do_not_touch_vault() -> None:
    release = threading.Event()
    calls: list[int] = []
    guard = VaultGuard(timeout_s=0.05)
    try:
        with pytest.raises(VaultStalled):
            guard.call(release.wait)
        assert guard.stalled
        with pytest.raises(VaultStalled):
            guard.call(lambda: calls.append(1))
        assert calls == []  # 止まったスレッドを増やさない
    finally:
        release.set()


def test_write_lock_waits_for_stalled_thread_then_releases(tmp_path, monkeypatch) -> None:
    # 見切ったスレッドが動いている間は、呼び出し側が離れてもロックを握り、スレッドの終わりで
    # 放す(遅れた書き込みを別の実行の書き込みと重ねない。GH#341)。見切るのは、スレッドが
    # 書き込みの中(begin_write の後)で止まったのを確かめてから
    gate_vault_threads(monkeypatch)
    path = tmp_path / "x.lock"
    release = threading.Event()

    def stuck() -> None:
        mark_blocked()
        release.wait(HANG_GUARD_S)

    lock = VaultWriteLock.acquire(path, wait_s=0)
    assert lock is not None
    try:
        with pytest.raises(VaultStalled):
            lock.call(VaultGuard(), stuck)
        lock.leave()
        assert VaultWriteLock.acquire(path, wait_s=0.05) is None  # まだ書いている
    finally:
        release.set()
    assert join_vault_threads() == []
    other = VaultWriteLock.acquire(path, wait_s=0)
    assert other is not None  # スレッドの終わりで放した
    other.leave()


def test_write_lock_skips_write_started_after_leave(tmp_path, monkeypatch) -> None:
    # 見切った時にまだ動き出していなかったスレッドは、ロックを放した後に動き出しても書かない
    gate_vault_threads(monkeypatch)
    path = tmp_path / "x.lock"
    lock = VaultWriteLock.acquire(path, wait_s=0)
    assert lock is not None
    lock.leave()
    calls: list[int] = []
    assert lock.call(VaultGuard(), lambda: calls.append(1)) is None
    assert calls == []
    other = VaultWriteLock.acquire(path, wait_s=0)
    assert other is not None
    other.leave()


def test_write_lock_without_path_does_not_lock(monkeypatch) -> None:
    # ファイルでない DB は別の実行と置き場を共有しないので、ロックファイルを作らない
    gate_vault_threads(monkeypatch)
    lock = VaultWriteLock.acquire(None, wait_s=0)
    assert lock is not None
    assert lock.call(VaultGuard(), lambda: "値") == "値"
    lock.leave()
