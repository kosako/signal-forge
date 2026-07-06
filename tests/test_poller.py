import json
import logging
import sqlite3
from pathlib import Path

import httpx
import pytest

from app.config import Config, TelegramConfig
from app.db import apply_migrations, connect
from app.delivery import DeliveryError, TelegramApi, poll_once, run_poller
from app.delivery.poller import OFFSET_KEY, RECONNECT_BACKOFF_S
from app.delivery.telegram import LONG_POLL_S, reaction_keyboard
from tests.test_delivery import add_digest, add_item

logger = logging.getLogger("test-poller")

OWNER_ID = 10
CHAT_ID = 777


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'TechCrunch')")  # id=2
    yield conn
    conn.close()


def make_config(tmp_path: Path, *, owner_user_id: int | None = OWNER_ID) -> Config:
    return Config(
        config_dir=tmp_path,
        telegram=TelegramConfig(owner_user_id=owner_user_id, owner_chat_id=CHAT_ID),
    )


class FakeApi:
    """poller のロジックテスト用(HTTP なし)。呼び出しを記録する。"""

    def __init__(self, batches: list[list[dict]] | None = None):
        self._batches = list(batches or [])
        self.offsets: list[int | None] = []
        self.answered: list[tuple[str, str | None]] = []
        self.edited: list[tuple[int, int, dict]] = []

    def get_updates(self, offset: int | None) -> list[dict]:
        self.offsets.append(offset)
        return self._batches.pop(0) if self._batches else []

    def answer_callback_query(self, callback_query_id: str, *, text: str | None = None) -> None:
        self.answered.append((callback_query_id, text))

    def edit_message_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: dict
    ) -> None:
        self.edited.append((chat_id, message_id, reply_markup))


def make_callback_update(
    item_id: int,
    code: str = "up",
    *,
    update_id: int = 1,
    from_id: int = OWNER_ID,
    data: str | None = None,
    with_message: bool = True,
) -> dict:
    callback: dict = {
        "id": f"cb{update_id}",
        "from": {"id": from_id},
        "data": data if data is not None else f"s:{item_id}:{code}",
    }
    if with_message:
        callback["message"] = {"message_id": 55, "chat": {"id": CHAT_ID}}
    return {"update_id": update_id, "callback_query": callback}


def test_reaction_recorded_answered_and_checkmarked(conn, tmp_path) -> None:
    a = add_item(conn, "https://a/1")
    add_digest(conn, [a])
    api = FakeApi([[make_callback_update(a, "up")]])

    assert poll_once(conn, make_config(tmp_path), api, logger) == 1

    row = conn.execute("SELECT * FROM signals").fetchone()
    assert (row["kind"], row["item_id"], row["value"]) == ("reaction", a, "useful")
    digest_date = conn.execute("SELECT digest_date FROM digests").fetchone()[0]
    assert row["signal_date"] == digest_date  # 配信 digest_date に帰属(設計 01 §7)
    assert api.answered == [("cb1", "👍 記録しました")]
    chat_id, message_id, markup = api.edited[0]
    assert (chat_id, message_id) == (CHAT_ID, 55)
    labels = [b["text"] for b in markup["inline_keyboard"][0]]
    assert "👍 価値あり ✓" in labels  # 選択ボタンに ✓


def test_offset_persisted_and_used_on_next_poll(conn, tmp_path) -> None:
    a = add_item(conn, "https://a/1")
    add_digest(conn, [a])
    api = FakeApi([
        [make_callback_update(a, "up", update_id=7)],
        [],
    ])
    config = make_config(tmp_path)

    poll_once(conn, config, api, logger)
    saved = conn.execute(
        "SELECT value FROM telegram_state WHERE key = ?", (OFFSET_KEY,)
    ).fetchone()
    assert saved["value"] == "8"  # update_id + 1

    poll_once(conn, config, api, logger)
    assert api.offsets == [None, 8]  # 再起動相当でも次回は永続化 offset から


def test_non_owner_callback_discarded_without_response(conn, tmp_path) -> None:
    a = add_item(conn, "https://a/1")
    add_digest(conn, [a])
    api = FakeApi([[make_callback_update(a, "up", from_id=999)]])

    poll_once(conn, make_config(tmp_path), api, logger)

    assert api.answered == []  # 応答も返さない(設計 04 §6)
    assert conn.execute("SELECT count(*) FROM signals").fetchone()[0] == 0
    # offset は進む(同じ update を再処理し続けない)
    assert conn.execute(
        "SELECT value FROM telegram_state WHERE key = ?", (OFFSET_KEY,)
    ).fetchone()["value"] == "2"


@pytest.mark.parametrize(
    "bad_data",
    [
        "x:1:up",  # 不明 prefix
        "s:abc:up",  # 非数値 item_id
        "s:1:hmm",  # 不明 code
        "s:1",  # 要素不足
        "q1:20260707:y",  # 夜アンケート(plan #19 で対応)
        "s:" + "9" * 70 + ":up",  # 64バイト超
    ],
)
def test_malformed_or_unsupported_callback_data(conn, tmp_path, bad_data) -> None:
    a = add_item(conn, "https://a/1")
    add_digest(conn, [a])
    api = FakeApi([[make_callback_update(a, data=bad_data)]])

    poll_once(conn, make_config(tmp_path), api, logger)

    assert api.answered == [("cb1", None)]  # スピナーは止めるが記帳しない
    assert conn.execute("SELECT count(*) FROM signals").fetchone()[0] == 0


def test_item_without_digest_membership_not_recorded(conn, tmp_path) -> None:
    a = add_item(conn, "https://a/1")  # digest に載っていない
    api = FakeApi([[make_callback_update(a, "dig")]])

    poll_once(conn, make_config(tmp_path), api, logger)

    assert conn.execute("SELECT count(*) FROM signals").fetchone()[0] == 0
    assert api.answered == [("cb1", "⚠️ 記録失敗、もう一度")]
    assert api.edited == []


def test_retap_appends_new_row(conn, tmp_path) -> None:
    a = add_item(conn, "https://a/1")
    add_digest(conn, [a])
    api = FakeApi([[
        make_callback_update(a, "up", update_id=1),
        make_callback_update(a, "no", update_id=2),
    ]])

    poll_once(conn, make_config(tmp_path), api, logger)

    values = [r["value"] for r in conn.execute("SELECT value FROM signals ORDER BY id")]
    assert values == ["useful", "noise"]  # 追記型(上書きしない。設計 01 §7)


def test_message_update_ignored_but_offset_advances(conn, tmp_path) -> None:
    api = FakeApi([[{"update_id": 3, "message": {"text": "https://x", "from": {"id": OWNER_ID}}}]])
    poll_once(conn, make_config(tmp_path), api, logger)
    assert conn.execute(
        "SELECT value FROM telegram_state WHERE key = ?", (OFFSET_KEY,)
    ).fetchone()["value"] == "4"


def test_update_failure_does_not_kill_batch(conn, tmp_path) -> None:
    a = add_item(conn, "https://a/1")
    b = add_item(conn, "https://a/2")
    add_digest(conn, [a, b])

    class EditFailsOnce(FakeApi):
        def edit_message_reply_markup(self, chat_id, message_id, reply_markup):
            if not self.edited:
                self.edited.append((chat_id, message_id, reply_markup))
                raise DeliveryError("edit 失敗")
            super().edit_message_reply_markup(chat_id, message_id, reply_markup)

    api = EditFailsOnce([[
        make_callback_update(a, "up", update_id=1),
        make_callback_update(b, "dig", update_id=2),
    ]])
    poll_once(conn, make_config(tmp_path), api, logger)

    # 1件目の edit 失敗でも 2件目は処理され、offset も進む
    assert conn.execute("SELECT count(*) FROM signals").fetchone()[0] == 2
    assert conn.execute(
        "SELECT value FROM telegram_state WHERE key = ?", (OFFSET_KEY,)
    ).fetchone()["value"] == "3"


def test_run_poller_requires_owner_user_id(conn, tmp_path) -> None:
    with pytest.raises(DeliveryError, match="owner_user_id"):
        run_poller(conn, make_config(tmp_path, owner_user_id=None), FakeApi(), logger)


def test_run_poller_backs_off_on_failure_and_stops(conn, tmp_path) -> None:
    class FailingApi(FakeApi):
        def get_updates(self, offset):
            raise DeliveryError("接続失敗")

    sleeps: list[float] = []
    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 2  # 2周で停止

    run_poller(
        conn, make_config(tmp_path), FailingApi(), logger,
        sleep=sleeps.append, should_stop=should_stop,
    )
    assert sleeps == [RECONNECT_BACKOFF_S, RECONNECT_BACKOFF_S]


def test_get_updates_api_payload_and_response() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(
            200, json={"ok": True, "result": [{"update_id": 1}, "garbage", {"update_id": 2}]}
        )

    api = TelegramApi("tok", transport=httpx.MockTransport(handler), sleep=lambda _s: None)
    updates = api.get_updates(42)
    assert seen[0]["timeout"] == str(LONG_POLL_S)
    assert seen[0]["offset"] == "42"
    assert updates == [{"update_id": 1}, {"update_id": 2}]  # dict 以外は落とす


def test_answer_and_edit_accept_bool_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": True})

    api = TelegramApi("tok", transport=httpx.MockTransport(handler), sleep=lambda _s: None)
    api.answer_callback_query("cb1", text="ok")  # 例外にならない
    api.edit_message_reply_markup(1, 2, reaction_keyboard(3))


def test_reaction_keyboard_selected_marks_only_one() -> None:
    markup = reaction_keyboard(9, selected="dig")
    buttons = markup["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == [
        "👍 価値あり", "🔖 深掘り ✓", "✅ 既知", "👎 不要"
    ]
    assert json.dumps(markup)  # シリアライズ可能
