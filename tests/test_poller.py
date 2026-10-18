import json
import logging
import sqlite3
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from app.config import Config, DigConfig, TelegramConfig
from app.db import apply_migrations, connect
from app.delivery import DeliveryError, TelegramApi, poll_once, run_poller
from app.delivery.poller import NO_URL_GUIDE, OFFSET_KEY, RECONNECT_BACKOFF_S
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
        self.sent: list[tuple[int, str]] = []

    def send_message(self, chat_id: int, text: str, *, reply_markup: dict | None = None) -> int:
        self.sent.append((chat_id, text))
        return 900 + len(self.sent)

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
        "z:1:up",  # 不明 prefix(q1/q2/f は test_surveys 側で検証)
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


@pytest.mark.parametrize(
    "bad_from",
    [None, "attacker", 123, {"id": "10"}, {"id": None}, {}],  # 非 dict / 非 int id
)
def test_malformed_from_treated_as_unauthorized(conn, tmp_path, bad_from) -> None:
    a = add_item(conn, "https://a/1")
    add_digest(conn, [a])
    update = make_callback_update(a, "up")
    update["callback_query"]["from"] = bad_from
    api = FakeApi([[update]])

    poll_once(conn, make_config(tmp_path), api, logger)

    assert api.answered == []  # 未認可扱いで無応答破棄
    assert conn.execute("SELECT count(*) FROM signals").fetchone()[0] == 0
    assert conn.execute(
        "SELECT value FROM telegram_state WHERE key = ?", (OFFSET_KEY,)
    ).fetchone()["value"] == "2"  # offset は進む(poison update 化しない)


@pytest.mark.parametrize(
    "bad_message",
    [
        "not-a-dict",
        {"message_id": 55, "chat": "not-a-dict"},
        {"message_id": 55, "chat": {}},  # chat.id 欠落
        {"message_id": 55, "chat": {"id": "77"}},  # chat.id 非 int
        {"chat": {"id": 77}},  # message_id 欠落
    ],
)
def test_malformed_message_skips_edit_but_records(conn, tmp_path, bad_message) -> None:
    a = add_item(conn, "https://a/1")
    add_digest(conn, [a])
    update = make_callback_update(a, "up")
    update["callback_query"]["message"] = bad_message
    api = FakeApi([[update]])

    poll_once(conn, make_config(tmp_path), api, logger)

    # 記帳と answer は完了し、✓ 反映だけ skip(offset も進む)
    assert conn.execute("SELECT count(*) FROM signals").fetchone()[0] == 1
    assert api.answered == [("cb1", "👍 記録しました")]
    assert api.edited == []
    assert conn.execute(
        "SELECT value FROM telegram_state WHERE key = ?", (OFFSET_KEY,)
    ).fetchone()["value"] == "2"


def test_unexpected_exception_advances_offset(conn, tmp_path) -> None:
    a = add_item(conn, "https://a/1")
    add_digest(conn, [a])

    class ExplodingApi(FakeApi):
        def answer_callback_query(self, callback_query_id, *, text=None):
            raise RuntimeError("想定外")

    api = ExplodingApi([[make_callback_update(a, "up")]])
    poll_once(conn, make_config(tmp_path), api, logger)  # 例外が漏れない

    assert conn.execute(
        "SELECT value FROM telegram_state WHERE key = ?", (OFFSET_KEY,)
    ).fetchone()["value"] == "2"  # poison update を無限再処理しない


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


def make_message_update(
    text: object, *, update_id: int = 1, from_id: int = OWNER_ID
) -> dict:
    return {
        "update_id": update_id,
        "message": {"text": text, "from": {"id": from_id}, "chat": {"id": CHAT_ID}},
    }


def manual_items(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT items.* FROM items JOIN sources ON sources.id = items.source_id "
        "WHERE sources.type = 'manual' ORDER BY items.id"
    ).fetchall()


def test_url_message_ingested_and_acked(conn, tmp_path) -> None:
    api = FakeApi([[make_message_update("これ読みたい https://example.com/article?utm_source=x")]])
    poll_once(conn, make_config(tmp_path), api, logger)

    rows = manual_items(conn)
    assert len(rows) == 1
    assert rows[0]["status"] == "new"
    assert rows[0]["canonical_url"] == "https://example.com/article"  # u1 正規化済み
    assert api.sent == [(CHAT_ID, "✅ 1件受け付けました。明朝のダイジェストで分析します")]


def test_two_different_urls_same_day_both_new(conn, tmp_path) -> None:
    # plan #18 完了条件: 空 content_hash による誤 duplicate 化がない(設計 01 §5〜6)
    api = FakeApi([[
        make_message_update("https://example.com/a", update_id=1),
        make_message_update("https://example.org/b", update_id=2),
    ]])
    poll_once(conn, make_config(tmp_path), api, logger)

    rows = manual_items(conn)
    assert [r["status"] for r in rows] == ["new", "new"]
    assert all(r["dup_of_item_id"] is None for r in rows)


def test_existing_url_replies_already_ingested(conn, tmp_path) -> None:
    api = FakeApi([
        [make_message_update("https://example.com/a", update_id=1)],
        [make_message_update("https://example.com/a?utm_source=tg", update_id=2)],  # 同一正規形
    ])
    config = make_config(tmp_path)
    poll_once(conn, config, api, logger)
    poll_once(conn, config, api, logger)

    assert len(manual_items(conn)) == 1  # 冪等
    assert api.sent[1] == (CHAT_ID, "投入済みです(1件)")


def test_multiple_urls_in_one_message(conn, tmp_path) -> None:
    api = FakeApi([[make_message_update(
        "https://example.com/a と https://example.org/b と再掲 https://example.com/a"
    )]])
    poll_once(conn, make_config(tmp_path), api, logger)

    assert len(manual_items(conn)) == 2  # メッセージ内重複は1件扱い
    assert api.sent == [(CHAT_ID, "✅ 2件受け付けました。明朝のダイジェストで分析します")]


def test_mixed_new_and_known_urls(conn, tmp_path) -> None:
    api = FakeApi([
        [make_message_update("https://example.com/a", update_id=1)],
        [make_message_update("https://example.com/a https://example.org/b", update_id=2)],
    ])
    config = make_config(tmp_path)
    poll_once(conn, config, api, logger)
    poll_once(conn, config, api, logger)

    assert len(manual_items(conn)) == 2
    assert api.sent[1] == (
        CHAT_ID, "✅ 1件受け付けました。明朝のダイジェストで分析します(ほか1件は投入済み)"
    )


@pytest.mark.parametrize("no_url_text", ["おはよう", None, 123])
def test_message_without_url_gets_guide(conn, tmp_path, no_url_text) -> None:
    api = FakeApi([[make_message_update(no_url_text)]])
    poll_once(conn, make_config(tmp_path), api, logger)
    assert manual_items(conn) == []
    assert api.sent == [(CHAT_ID, "URL を送ると翌朝のダイジェスト候補になります")]


@pytest.mark.parametrize(
    "invalid_url",
    [
        "https://alice:secret@example.com/a",  # userinfo(credential 漏えい防止で reject)
        "https://example.com:99999/a",  # 不正ポート
        "https://?x=1",  # host なし
    ],
)
def test_invalid_urls_rejected_with_reply(conn, tmp_path, invalid_url) -> None:
    api = FakeApi([[make_message_update(invalid_url)]])
    poll_once(conn, make_config(tmp_path), api, logger)
    assert manual_items(conn) == []
    assert api.sent == [(CHAT_ID, "有効な URL がありませんでした")]


def test_channel_post_is_ignored(conn, tmp_path) -> None:
    api = FakeApi([[{
        "update_id": 1,
        "channel_post": {"text": "https://example.com/a", "chat": {"id": CHAT_ID}},
    }]])
    poll_once(conn, make_config(tmp_path), api, logger)
    assert manual_items(conn) == []
    assert api.sent == []


def test_non_owner_message_discarded_without_reply(conn, tmp_path) -> None:
    api = FakeApi([[make_message_update("https://example.com/a", from_id=999)]])
    poll_once(conn, make_config(tmp_path), api, logger)
    assert manual_items(conn) == []
    assert api.sent == []  # 応答も返さない(設計 04 §6)
    assert conn.execute(
        "SELECT value FROM telegram_state WHERE key = ?", (OFFSET_KEY,)
    ).fetchone()["value"] == "2"


def test_message_with_malformed_chat_still_ingests(conn, tmp_path) -> None:
    update = make_message_update("https://example.com/a")
    update["message"]["chat"] = "broken"
    api = FakeApi([[update]])
    poll_once(conn, make_config(tmp_path), api, logger)
    assert len(manual_items(conn)) == 1  # 投入は成立、返信のみ skip
    assert api.sent == []


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


def test_repress_not_modified_is_tolerated_without_error(conn, tmp_path, caplog) -> None:
    # 同一ボタン再押下は markup 不変で Telegram が 400「message is not modified」を
    # 返す。冪等な再押下は成功扱いにし ERROR ログを出さない(GH#55)
    a = add_item(conn, "https://a/1")
    add_digest(conn, [a])

    class NotModifiedApi(FakeApi):
        def edit_message_reply_markup(self, chat_id, message_id, reply_markup):
            raise DeliveryError(
                "Telegram editMessageReplyMarkup が失敗: HTTP 400: Bad Request: "
                "message is not modified: specified new message content and reply "
                "markup are exactly the same as a current content and reply markup "
                "of the message"
            )

    api = NotModifiedApi([[make_callback_update(a, "up")]])
    with caplog.at_level(logging.INFO):
        poll_once(conn, make_config(tmp_path), api, logger)

    # 記帳と answer は完了扱いのまま、ERROR ログは出ない
    assert conn.execute("SELECT count(*) FROM signals").fetchone()[0] == 1
    assert api.answered == [("cb1", "👍 記録しました")]
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert conn.execute(
        "SELECT value FROM telegram_state WHERE key = ?", (OFFSET_KEY,)
    ).fetchone()["value"] == "2"  # offset も進む


def test_insert_failure_answers_warning(conn, tmp_path) -> None:
    # signals INSERT 自体の失敗(DB 故障相当)は「⚠️ 記録失敗、もう一度」で伝える
    # (設計 04 §3 の「DB 書込失敗時のみ ⚠️」の実 INSERT 失敗経路)
    a = add_item(conn, "https://a/1")
    add_digest(conn, [a])
    conn.execute("DROP TABLE signals")
    api = FakeApi([[make_callback_update(a, "up")]])

    poll_once(conn, make_config(tmp_path), api, logger)

    assert api.answered == [("cb1", "⚠️ 記録失敗、もう一度")]
    assert api.edited == []  # 記帳できていないので ✓ も付けない
    assert conn.execute(
        "SELECT value FROM telegram_state WHERE key = ?", (OFFSET_KEY,)
    ).fetchone()["value"] == "2"  # offset は進む


def test_survey_insert_failure_answers_warning(conn, tmp_path) -> None:
    from datetime import datetime

    from app.digest.builder import JST

    conn.execute("DROP TABLE signals")
    date_compact = datetime.now(JST).strftime("%Y%m%d")
    api = FakeApi([[make_callback_update(0, data=f"q1:{date_compact}:y")]])

    poll_once(conn, make_config(tmp_path), api, logger)

    assert api.answered == [("cb1", "⚠️ 記録失敗、もう一度")]


def test_corrupt_offset_resets_instead_of_crashing(conn, tmp_path) -> None:
    conn.execute(
        "INSERT INTO telegram_state (key, value) VALUES (?, 'garbage')", (OFFSET_KEY,)
    )
    api = FakeApi([[]])
    poll_once(conn, make_config(tmp_path), api, logger)  # 落ちない
    assert api.offsets == [None]  # offset なしで再開(at-least-once の安全側)


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


def test_run_poller_backs_off_on_db_lock_instead_of_crashing(conn, tmp_path) -> None:
    # VACUUM/backup 競合の "database is locked" は一過性 → プロセスを落とさず待って再開
    class LockedApi(FakeApi):
        def get_updates(self, offset):
            raise sqlite3.OperationalError("database is locked")

    sleeps: list[float] = []
    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    run_poller(
        conn, make_config(tmp_path), LockedApi(), logger,
        sleep=sleeps.append, should_stop=should_stop,
    )
    assert sleeps == [RECONNECT_BACKOFF_S, RECONNECT_BACKOFF_S]


def test_run_poller_fails_fast_on_permanent_db_error(conn, tmp_path) -> None:
    # スキーマ破損等の恒久エラーは 30s ループせず落とす(KeepAlive → doctor #3 で検知)
    conn.execute("DROP TABLE telegram_state")
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        run_poller(conn, make_config(tmp_path), FakeApi([[]]), logger)


# --- 感想キャプチャ(digest 記事メッセージへのリプライ。GH#92)---


def make_reply_update(
    text: object, reply_message_id: object, *, update_id: int = 1, from_id: int = OWNER_ID
) -> dict:
    update = make_message_update(text, update_id=update_id, from_id=from_id)
    update["message"]["reply_to_message"] = {"message_id": reply_message_id}
    return update


def add_delivered_item(conn, url: str, *, message_id: int) -> int:
    """配信済み(digest_messages に記事メッセージが台帳登録済み)の item を作る。"""
    item_id = add_item(conn, url)
    digest_id = add_digest(conn, [item_id])
    conn.execute(
        "INSERT INTO digest_messages (digest_id, item_id, message_id) VALUES (?, ?, ?)",
        (digest_id, item_id, message_id),
    )
    conn.commit()
    return item_id


def test_reply_to_item_message_records_note_not_urls(conn, tmp_path) -> None:
    item_id = add_delivered_item(conn, "https://a/article", message_id=500)
    api = FakeApi([[
        make_reply_update("これ試したい。https://tool.example と組み合わせると良さそう", 500)
    ]])
    poll_once(conn, make_config(tmp_path), api, logger)

    rows = conn.execute("SELECT * FROM item_notes").fetchall()
    assert len(rows) == 1
    assert rows[0]["item_id"] == item_id
    assert rows[0]["note"].startswith("これ試したい。")
    assert rows[0]["exported_at"] is None  # 朝バッチ export が反映(差分管理)
    assert manual_items(conn) == []  # 感想内の URL は投入しない(感想の一部)
    assert api.sent == [
        (CHAT_ID, "📝 感想を記録しました。明朝の export で Obsidian ノートに反映します")
    ]


def test_reply_to_dig_card_records_note(conn, tmp_path) -> None:
    """深掘り概要カード(dig_results)へのリプライも感想として拾う(GH#105。
    take / 続き要求への分類・ノート還流は GH#106)。"""
    item_id = add_item(conn, "https://a/article")
    conn.execute(
        "INSERT INTO dig_results (item_id, note_name, card_text, message_id) "
        "VALUES (?, 'note', 'カード', 700)",
        (item_id,),
    )
    conn.commit()
    api = FakeApi([[make_reply_update("判定軸が面白い。もっと掘りたい", 700)]])
    poll_once(conn, make_config(tmp_path), api, logger)

    rows = conn.execute("SELECT * FROM item_notes").fetchall()
    assert len(rows) == 1
    assert rows[0]["item_id"] == item_id
    assert rows[0]["note"] == "判定軸が面白い。もっと掘りたい"
    assert api.sent == [
        (CHAT_ID, "📝 感想を記録しました。明朝の export で Obsidian ノートに反映します")
    ]


class FakeWorker:
    """AnswerWorker の代わり(別プロセスを起動しない)。呼び出しを記録する。"""

    def __init__(self):
        self.kicks = 0
        self.sweeps = 0

    def kick(self, logger) -> None:
        self.kicks += 1

    def sweep(self, conn, logger) -> None:
        self.sweeps += 1


def add_dig_card(conn, *, message_id: int = 700) -> tuple[int, int]:
    item_id = add_item(conn, f"https://a/dig/{message_id}")
    dig_id = conn.execute(
        "INSERT INTO dig_results (item_id, note_name, card_text, message_id) "
        "VALUES (?, 'note', 'カード', ?)",
        (item_id, message_id),
    ).lastrowid
    conn.commit()
    return item_id, dig_id


def card_reply(text: str, reply_to: int, *, message_id: int = 81, update_id: int = 1) -> dict:
    update = make_reply_update(text, reply_to, update_id=update_id)
    update["message"]["message_id"] = message_id
    return update


def answer_config(tmp_path: Path, cap: int = 20) -> Config:
    return replace(make_config(tmp_path), dig=DigConfig(answer_daily_cap=cap))


def test_card_reply_is_recorded_and_queued_for_answer(conn, tmp_path) -> None:
    # GH#126: カードへのリプライは感想として記録し、質問として台帳に積んで返事を起動する
    item_id, dig_id = add_dig_card(conn)
    api, worker = FakeApi([[card_reply("ユースケースがいまいちわからん", 700)]]), FakeWorker()

    poll_once(conn, answer_config(tmp_path), api, logger, answer_worker=worker)

    note = conn.execute("SELECT item_id, note FROM item_notes").fetchone()
    assert (note["item_id"], note["note"]) == (item_id, "ユースケースがいまいちわからん")
    q = conn.execute("SELECT * FROM dig_questions").fetchone()
    assert (q["dig_result_id"], q["question"], q["question_message_id"], q["status"]) == (
        dig_id, "ユースケースがいまいちわからん", 81, "pending"
    )
    assert api.sent == [(CHAT_ID, "📝 記録しました。返事を作っています(数分かかることがあります)")]
    assert worker.kicks == 1


def test_reply_to_answer_continues_the_same_card(conn, tmp_path) -> None:
    item_id, dig_id = add_dig_card(conn)
    conn.execute(
        "INSERT INTO dig_questions (dig_result_id, question, question_message_id, status, "
        "answer, answer_message_id) VALUES (?, '最初', 81, 'answered', '返事', 950)",
        (dig_id,),
    )
    conn.commit()
    api, worker = FakeApi([[card_reply("続けての質問", 950, message_id=82)]]), FakeWorker()

    poll_once(conn, answer_config(tmp_path), api, logger, answer_worker=worker)

    rows = conn.execute("SELECT dig_result_id, question FROM dig_questions ORDER BY id").fetchall()
    assert [(r["dig_result_id"], r["question"]) for r in rows] == [
        (dig_id, "最初"), (dig_id, "続けての質問")
    ]
    assert conn.execute("SELECT item_id FROM item_notes").fetchone()[0] == item_id
    assert worker.kicks == 1


def test_card_reply_over_daily_cap_is_recorded_only(conn, tmp_path) -> None:
    _item_id, dig_id = add_dig_card(conn)
    conn.execute(
        "INSERT INTO dig_questions (dig_result_id, question, question_message_id) "
        "VALUES (?, '今日の 1 件目', 80)",
        (dig_id,),
    )
    conn.commit()
    api, worker = FakeApi([[card_reply("2 件目", 700)]]), FakeWorker()

    poll_once(conn, answer_config(tmp_path, cap=1), api, logger, answer_worker=worker)

    assert conn.execute("SELECT count(*) FROM dig_questions").fetchone()[0] == 1
    assert conn.execute("SELECT note FROM item_notes").fetchone()[0] == "2 件目"
    assert api.sent == [
        (CHAT_ID, "📝 記録しました。今日の返事の上限(1 件)に達したため、返事は作りません")
    ]
    assert worker.kicks == 0


def test_card_reply_with_answers_disabled_is_note_only(conn, tmp_path) -> None:
    item_id, _dig_id = add_dig_card(conn)
    api, worker = FakeApi([[card_reply("感想", 700)]]), FakeWorker()

    poll_once(conn, answer_config(tmp_path, cap=0), api, logger, answer_worker=worker)

    assert conn.execute("SELECT count(*) FROM dig_questions").fetchone()[0] == 0
    assert conn.execute("SELECT item_id FROM item_notes").fetchone()[0] == item_id
    assert api.sent == [
        (CHAT_ID, "📝 感想を記録しました。明朝の export で Obsidian ノートに反映します")
    ]
    assert worker.kicks == 0


def test_run_poller_sweeps_answers_each_cycle(conn, tmp_path) -> None:
    worker = FakeWorker()
    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 2  # 2周で停止

    run_poller(
        conn, make_config(tmp_path), FakeApi(), logger,
        should_stop=should_stop, answer_worker=worker,
    )
    assert worker.sweeps == 2


def test_reply_outside_item_ledger_falls_back_to_url_flow(conn, tmp_path) -> None:
    item_id = add_item(conn, "https://a/article")
    digest_id = add_digest(conn, [item_id])
    conn.execute(  # ヘッダメッセージ(item_id IS NULL)
        "INSERT INTO digest_messages (digest_id, item_id, message_id) VALUES (?, NULL, 400)",
        (digest_id,),
    )
    conn.commit()
    api = FakeApi([[
        make_reply_update("感想のつもり", 999, update_id=1),  # 台帳に無い message_id
        make_reply_update("   ", 400, update_id=2),  # 空白のみ → 従来フロー
        make_reply_update("https://example.com/a", 999, update_id=3),  # 台帳外 + URL
    ]])
    poll_once(conn, make_config(tmp_path), api, logger)

    assert conn.execute("SELECT count(*) FROM item_notes").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM digest_notes").fetchone()[0] == 0
    assert api.sent[0] == (CHAT_ID, NO_URL_GUIDE)
    assert api.sent[1] == (CHAT_ID, NO_URL_GUIDE)
    assert len(manual_items(conn)) == 1  # 台帳外へのリプライは URL 投入として扱う


def test_reply_with_malformed_reply_to_is_url_flow(conn, tmp_path) -> None:
    # 外部 API 由来のネスト値は形を仮定しない(型不正は「記事へのリプライでない」扱い)。
    # 台帳側に一致しうる行(500・1)を用意し、型不正が「たまたま不一致」でなく
    # 型検査で弾かれていることを確認する
    add_delivered_item(conn, "https://a/article", message_id=500)
    second = add_item(conn, "https://a/second")
    conn.execute(  # 同一 digest に2本目の記事メッセージ(message_id=1)を台帳登録
        "INSERT INTO digest_messages (digest_id, item_id, message_id) "
        "SELECT digest_id, ?, 1 FROM digest_messages LIMIT 1",
        (second,),
    )
    conn.commit()
    api = FakeApi([[
        make_reply_update("感想テキスト", "500", update_id=1),  # 文字列(SQLite 型親和で一致しうる)
        # bool は int の派生型で ID 1 に一致しうる(Codex should)。素通りさせない
        make_reply_update("感想テキスト", True, update_id=2),
    ]])
    poll_once(conn, make_config(tmp_path), api, logger)

    assert conn.execute("SELECT count(*) FROM item_notes").fetchone()[0] == 0
    assert api.sent == [(CHAT_ID, NO_URL_GUIDE), (CHAT_ID, NO_URL_GUIDE)]


def test_reply_in_other_chat_not_matched_to_dm_ledger(conn, tmp_path) -> None:
    # message_id は chat 単位(Codex must): owner が別チャットで同じ message_id に
    # 返信しても、owner DM の台帳と突合してはならない(誤記帳防止)→ 従来 URL フロー
    add_delivered_item(conn, "https://a/article", message_id=500)
    update = make_reply_update("グループでの感想", 500)
    update["message"]["chat"] = {"id": CHAT_ID + 1}  # owner DM 以外の chat
    with_url = make_reply_update("グループから https://example.com/g", 500, update_id=2)
    with_url["message"]["chat"] = {"id": CHAT_ID + 1}
    api = FakeApi([[update, with_url]])
    poll_once(conn, make_config(tmp_path), api, logger)

    assert conn.execute("SELECT count(*) FROM item_notes").fetchone()[0] == 0
    assert api.sent[0] == (CHAT_ID + 1, NO_URL_GUIDE)
    # 従来の URL 投入フローへ正しくフォールスルーする(感想扱いで握られない)
    assert len(manual_items(conn)) == 1
    assert api.sent[1] == (CHAT_ID + 1, "✅ 1件受け付けました。明朝のダイジェストで分析します")


def test_reply_to_header_records_digest_note(conn, tmp_path) -> None:
    # GH#96: ヘッダ(item_id IS NULL)へのリプライは日次感想として digest_notes へ
    item_id = add_item(conn, "https://a/article")
    digest_id = add_digest(conn, [item_id])
    conn.execute(
        "INSERT INTO digest_messages (digest_id, item_id, message_id) VALUES (?, NULL, 400)",
        (digest_id,),
    )
    conn.commit()
    api = FakeApi([[make_reply_update("今日は全体的にツール寄りで良かった", 400)]])
    poll_once(conn, make_config(tmp_path), api, logger)

    rows = conn.execute("SELECT * FROM digest_notes").fetchall()
    assert len(rows) == 1
    assert rows[0]["digest_id"] == digest_id
    assert rows[0]["note"] == "今日は全体的にツール寄りで良かった"
    assert rows[0]["exported_at"] is None
    assert conn.execute("SELECT count(*) FROM item_notes").fetchone()[0] == 0
    assert manual_items(conn) == []  # URL フローに落ちない
    assert api.sent == [(
        CHAT_ID,
        "📝 ダイジェストへの感想を記録しました。明朝の export で Daily ノートに反映します",
    )]


# --- 未消化一覧へのリプライ = 深掘り指定(GH#104) ---

BACKLOG_MSG_ID = 640


def add_dig_candidate(conn, url: str, *, backlog_message_id: int = BACKLOG_MSG_ID) -> int:
    """未消化候補(🔖 reaction 済み)と一覧メッセージの台帳行を作る。"""
    item_id = add_item(conn, url)
    conn.execute(
        "INSERT INTO signals (kind, item_id, value, signal_date) "
        "VALUES ('reaction', ?, 'dig', '2026-07-19')",
        (item_id,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO dig_backlog_messages (message_id, sent_date) "
        "VALUES (?, '2026-07-19')",
        (backlog_message_id,),
    )
    return item_id


def test_dig_reply_registers_request_with_angle(conn, tmp_path) -> None:
    item = add_dig_candidate(conn, "https://a/1")
    api = FakeApi([[make_reply_update(f"{item} を承認フローの観点で", BACKLOG_MSG_ID)]])

    poll_once(conn, make_config(tmp_path), api, logger)

    row = conn.execute("SELECT * FROM dig_queue WHERE item_id = ?", (item,)).fetchone()
    assert (row["status"], row["source"], row["angle"]) == (
        "queued", "request", "承認フローの観点で",
    )
    assert row["requested_at"] is not None
    assert api.sent == [(CHAT_ID, f"🔍 {item} を登録しました(観点: 承認フローの観点で)")]


def test_dig_reply_pass_and_request_combined(conn, tmp_path) -> None:
    first = add_dig_candidate(conn, "https://a/1")
    second = add_dig_candidate(conn, "https://a/2")
    api = FakeApi([[make_reply_update(f"{first} はパス、あと {second} も", BACKLOG_MSG_ID)]])

    poll_once(conn, make_config(tmp_path), api, logger)

    statuses = {
        row["item_id"]: row["status"]
        for row in conn.execute("SELECT item_id, status FROM dig_queue")
    }
    assert statuses == {first: "passed", second: "queued"}
    text = api.sent[0][1]
    assert f"⏭️ {first} を深掘り対象から外しました" in text
    assert f"🔍 {second} を登録しました(観点なし)" in text


def test_dig_reply_without_valid_numbers_gets_guide(conn, tmp_path) -> None:
    add_dig_candidate(conn, "https://a/1")
    api = FakeApi([[make_reply_update("9999 をパスで", BACKLOG_MSG_ID)]])

    poll_once(conn, make_config(tmp_path), api, logger)

    assert conn.execute("SELECT count(*) FROM dig_queue").fetchone()[0] == 0
    from app.delivery.poller import DIG_REPLY_GUIDE

    assert api.sent == [(CHAT_ID, DIG_REPLY_GUIDE)]


def test_dig_reply_does_not_hijack_url_ingestion(conn, tmp_path) -> None:
    # 台帳にないメッセージへのリプライは従来どおり URL 投入経路へ
    add_dig_candidate(conn, "https://a/1")
    api = FakeApi([[make_reply_update("https://example.com/article", 999)]])

    poll_once(conn, make_config(tmp_path), api, logger)

    assert conn.execute("SELECT count(*) FROM dig_queue").fetchone()[0] == 0
    assert manual_items(conn) != []


def test_dig_reply_accepts_queued_id_not_in_pending(conn, tmp_path) -> None:
    # 一覧から消えていてもキュー在籍なら指定できる(パス済みの復帰など)
    item = add_item(conn, "https://a/queued-only")
    conn.execute(
        "INSERT INTO dig_queue (item_id, status, source) VALUES (?, 'passed', 'request')",
        (item,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO dig_backlog_messages (message_id, sent_date) "
        "VALUES (?, '2026-07-19')",
        (BACKLOG_MSG_ID,),
    )
    api = FakeApi([[make_reply_update(f"{item} を再検討の観点で", BACKLOG_MSG_ID)]])

    poll_once(conn, make_config(tmp_path), api, logger)

    row = conn.execute("SELECT * FROM dig_queue WHERE item_id = ?", (item,)).fetchone()
    assert (row["status"], row["angle"]) == ("queued", "再検討の観点で")
