"""vault へのアクセスの時間の上限(GH#145)。"""

import threading

import pytest

from app.vault_guard import VaultGuard, VaultStalled


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
