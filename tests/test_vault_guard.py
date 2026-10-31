"""vault へのアクセスの時間の上限(GH#145)。"""

import threading

import pytest

from app.vault_guard import VaultGuard, VaultStalled, VaultWriteLock


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


def test_write_lock_waits_for_stalled_thread_then_releases(tmp_path) -> None:
    # 見切ったスレッドが動いている間は、呼び出し側が離れてもロックを握り、スレッドの終わりで
    # 放す(遅れた書き込みを別の実行の書き込みと重ねない。GH#341)
    path = tmp_path / "x.lock"
    release = threading.Event()
    lock = VaultWriteLock.acquire(path, wait_s=0)
    assert lock is not None
    try:
        with pytest.raises(VaultStalled):
            lock.call(VaultGuard(timeout_s=0.05), release.wait)
        lock.leave()
        assert VaultWriteLock.acquire(path, wait_s=0.05) is None  # まだ書いている
    finally:
        release.set()
    for thread in threading.enumerate():
        if thread.name == "vault-io":
            thread.join(5)
    other = VaultWriteLock.acquire(path, wait_s=0)
    assert other is not None  # スレッドの終わりで放した
    other.leave()


def test_write_lock_skips_write_started_after_leave(tmp_path) -> None:
    # 見切った時にまだ動き出していなかったスレッドは、ロックを放した後に動き出しても書かない
    path = tmp_path / "x.lock"
    lock = VaultWriteLock.acquire(path, wait_s=0)
    assert lock is not None
    lock.leave()
    calls: list[int] = []
    assert lock.call(VaultGuard(timeout_s=5), lambda: calls.append(1)) is None
    assert calls == []
    other = VaultWriteLock.acquire(path, wait_s=0)
    assert other is not None
    other.leave()


def test_write_lock_without_path_does_not_lock() -> None:
    # ファイルでない DB は別の実行と置き場を共有しないので、ロックファイルを作らない
    lock = VaultWriteLock.acquire(None, wait_s=0)
    assert lock is not None
    assert lock.call(VaultGuard(timeout_s=5), lambda: "値") == "値"
    lock.leave()
