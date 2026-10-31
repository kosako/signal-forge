import json
import logging
import sqlite3
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from app.config import Config, DigConfig, ObsidianConfig, TelegramConfig
from app.db import apply_migrations, connect
from app.delivery import DeliveryError, TelegramApi, poll_once, run_poller
from app.delivery.poller import (
    NO_URL_GUIDE,
    OFFSET_KEY,
    RECONNECT_BACKOFF_S,
    TRANSIENT_RETRIES,
)
from app.delivery.telegram import LONG_POLL_S, dig_card_keyboard, reaction_keyboard
from app.export import export_pending_notes
from app.jst import JST
from tests.test_delivery import add_digest, add_item, telegram_text_error

logger = logging.getLogger("test-poller")

OWNER_ID = 10
CHAT_ID = 777


@pytest.fixture(autouse=True)
def _source_report_not_today(monkeypatch):
    """週 1 回のソースの健康診断(GH#150)を今日以外の曜日にする。ほかのタイマーのテストは
    実際の今日の日時で動くので、日曜に流すと通数が 1 通増えて落ちるため(+3 日 = 日付を
    跨いでも今日・明日にならない)。健康診断のテストは日付を固定して曜日を戻す。"""
    from app import source_health

    monkeypatch.setattr(source_health, "REPORT_WEEKDAY", (datetime.now(JST).weekday() + 3) % 7)


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
    """poller のロジックテスト用(HTTP なし)。呼び出しを記録する。

    本文が Telegram の長さの制約に反すれば、TelegramApi が 400 で送出するのと同じ
    DeliveryError を送出する(GH#343)。
    """

    def __init__(self, batches: list[list[dict]] | None = None):
        self._batches = list(batches or [])
        self.offsets: list[int | None] = []
        self.answered: list[tuple[str, str | None]] = []
        self.edited: list[tuple[int, int, dict]] = []
        self.sent: list[tuple[int, str]] = []

    def send_message(self, chat_id: int, text: str, *, reply_markup: dict | None = None) -> int:
        error = telegram_text_error(text)
        if error is not None:
            raise DeliveryError(f"Telegram sendMessage が失敗: HTTP 400: {error}")
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


def test_fake_api_rejects_text_over_telegram_limit() -> None:
    # GH#343: 模型は Telegram の 4096 字を超える本文を送れたことにしない
    api = FakeApi()
    assert api.send_message(CHAT_ID, "あ" * 4096) == 901
    with pytest.raises(DeliveryError, match="HTTP 400"):
        api.send_message(CHAT_ID, "あ" * 4097)
    assert len(api.sent) == 1


class RedeliveringApi(FakeApi):
    """getUpdates の offset を Telegram と同じに扱う FakeApi(GH#181)。offset より前の update は
    確認済みとして捨て、残りを毎回返す(offset を保存しなければ、同じ update が再配達される)。"""

    def __init__(self, updates: list[dict]):
        super().__init__()
        self.pending = list(updates)

    def get_updates(self, offset: int | None) -> list[dict]:
        self.offsets.append(offset)
        if offset is not None:
            self.pending = [u for u in self.pending if u["update_id"] >= offset]
        return list(self.pending)


class _Stopped(BaseException):
    """プロセスが止まったことの代わり(poll_once の except Exception を素通りする)。"""


def stop_before_saving_offset(monkeypatch) -> None:
    """次の 1 回の offset の保存の前でプロセスが止まった状態を作る(再起動の後、同じ update が
    再配達される)。2 回目からは本物の保存に戻す。"""
    from app.delivery import poller

    real_save_offset = poller._save_offset

    def stop(conn, offset):
        monkeypatch.setattr(poller, "_save_offset", real_save_offset)
        raise _Stopped

    monkeypatch.setattr(poller, "_save_offset", stop)


def saved_offset(conn) -> str | None:
    row = conn.execute(
        "SELECT value FROM telegram_state WHERE key = ?", (OFFSET_KEY,)
    ).fetchone()
    return row["value"] if row else None


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


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("想定外"),
        # 待っても直らない DB の失敗(制約違反・スキーマ破損)も、ロック競合と違って進める(GH#181)
        sqlite3.IntegrityError("constraint failed"),
        sqlite3.OperationalError("no such table: x"),
    ],
)
def test_unexpected_exception_advances_offset(conn, tmp_path, error) -> None:
    a = add_item(conn, "https://a/1")
    add_digest(conn, [a])

    class ExplodingApi(FakeApi):
        def answer_callback_query(self, callback_query_id, *, text=None):
            raise error

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
    text: object, *, update_id: int = 1, from_id: int = OWNER_ID, entities: list | None = None
) -> dict:
    message = {"text": text, "from": {"id": from_id}, "chat": {"id": CHAT_ID}}
    if entities is not None:
        message["entities"] = entities  # Telegram の message.entities(GH#227)
    return {"update_id": update_id, "message": message}


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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("（https://example.com/article）", "https://example.com/article"),  # 全角の括弧で囲んだ
        ("「https://example.com/article」を読む", "https://example.com/article"),  # 括弧の後に文
        ("あとで読む https://example.com/article。", "https://example.com/article"),  # 文末の句点
        ("https://example.com/article、あとで読む", "https://example.com/article"),  # 読点の後に文
        ("Read this (https://example.com/article).", "https://example.com/article"),
        ("Is it https://example.com/article?", "https://example.com/article"),
        # 平文では末尾の `!` を文の区切りとして外す(url entity の範囲なら残す)
        ("https://example.com/report!", "https://example.com/report"),
        # URL の中の対になった括弧は残し、対になっていない閉じ括弧と句点だけを外す
        ("https://en.wikipedia.org/wiki/Foo_(bar)", "https://en.wikipedia.org/wiki/Foo_(bar)"),
        ("(https://en.wikipedia.org/wiki/Foo_(bar)).", "https://en.wikipedia.org/wiki/Foo_(bar)"),
        # 全角の括弧も同じ
        ("https://example.com/記事（補足）/詳細", "https://example.com/記事（補足）/詳細"),
        ("（https://example.com/記事（補足））", "https://example.com/記事（補足）"),
    ],
)
def test_plain_text_url_excludes_trailing_punctuation_and_unpaired_brackets(
    conn, tmp_path, text, expected
) -> None:
    # entities の無いメッセージは平文を解析する。URL の直後の句読点・括弧を URL に含めない(GH#227)
    api = FakeApi([[make_message_update(text)]])
    poll_once(conn, make_config(tmp_path), api, logger)

    rows = manual_items(conn)
    assert [(r["url"], r["canonical_url"]) for r in rows] == [(expected, expected)]
    assert api.sent == [(CHAT_ID, "✅ 1件受け付けました。明朝のダイジェストで分析します")]


@pytest.mark.parametrize(
    ("text", "entities", "expected"),
    [
        # url entity の範囲を使う(平文の解析なら「あとで読む」まで URL に含める)。offset・length は
        # UTF-16 の code unit で数える(絵文字は 2)
        (
            "😀 https://example.com/articleあとで読む",
            [{"type": "url", "offset": 3, "length": 27}],
            ["https://example.com/article"],
        ),
        # Telegram の範囲が全角の閉じ括弧まで含んでも、平文と同じ規則で外す
        (
            "（https://example.com/article）",
            [{"type": "url", "offset": 1, "length": 28}],
            ["https://example.com/article"],
        ),
        # URL の中の対になった全角の括弧は残す
        (
            "https://example.com/記事（補足）/詳細",
            [{"type": "url", "offset": 0, "length": 29}],
            ["https://example.com/記事（補足）/詳細"],
        ),
        # Telegram が範囲に含めた末尾の ASCII の句読点は URL の一部として残す(平文の規則を当てない)
        (
            "https://example.com/report!",
            [{"type": "url", "offset": 0, "length": 27}],
            ["https://example.com/report!"],
        ),
        # 文字列に付けたリンク(text_link)は、その url を投入する
        (
            "この記事",
            [{"type": "text_link", "offset": 0, "length": 4, "url": "https://example.com/linked"}],
            ["https://example.com/linked"],
        ),
        # entities から http/https の URL を得られなければ平文を解析する
        (
            "太字 https://example.com/article。",
            [
                {"type": "bold", "offset": 0, "length": 2},
                {"type": "text_link", "offset": 0, "length": 2, "url": "tg://user?id=1"},
                {"type": "url", "offset": 99, "length": 5},  # 本文の外を指す
            ],
            ["https://example.com/article"],
        ),
        # 範囲が本文の外を指す text_link は使わない
        (
            "https://example.com/real",
            [{"type": "text_link", "offset": 999, "length": 4, "url": "https://example.com/other"}],
            ["https://example.com/real"],
        ),
        # offset・length が bool の text_link は使わない(bool は int の派生型だが不正な範囲)
        (
            "https://example.com/real",
            [{"type": "text_link", "offset": False, "length": 4, "url": "https://example.com/other"}],
            ["https://example.com/real"],
        ),
        (
            "https://example.com/real",
            [{"type": "text_link", "offset": 0, "length": True, "url": "https://example.com/other"}],
            ["https://example.com/real"],
        ),
    ],
)
def test_url_taken_from_message_entities(conn, tmp_path, text, entities, expected) -> None:
    api = FakeApi([[make_message_update(text, entities=entities)]])
    poll_once(conn, make_config(tmp_path), api, logger)

    assert [r["url"] for r in manual_items(conn)] == expected
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
        # userinfo 付きは括弧で切らない(切ると資格情報の一部が host・port として通る。GH#227)
        "https://alice:123)@example.com/a",
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

    from app.jst import JST

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


def test_locked_db_while_saving_note_redelivers_the_update(conn, tmp_path) -> None:
    # GH#181: 受信した後の記帳がロック競合で失敗したら offset を進めず、待った後に Telegram から
    # 再配達された同じ update で記帳する(上の LockedApi と違い、受信した後の DB 書き込みの失敗)
    add_delivered_item(conn, "https://a/article", message_id=500)
    update = make_reply_update("あとで読み返す", 500, update_id=5)
    update["message"]["message_id"] = 31
    api = RedeliveringApi([update])
    conn.execute("PRAGMA busy_timeout=0")  # busy_timeout の超過を待たずに起こす
    holder = connect(tmp_path / "intel.db")
    holder.execute("BEGIN IMMEDIATE")  # 朝バッチが書き込みのロックを握っている状態

    def release_lock() -> None:
        if holder.in_transaction:
            holder.execute("ROLLBACK")

    # ロックは、記帳に失敗した後に offset を保存しようとした時点で解ける(Issue の狭い窓。
    # 以前はここで未保存の update の offset を進めていた)。保存しようとしなければ、待つ間に解ける
    conn.set_trace_callback(
        lambda sql: release_lock() if sql.startswith("INSERT INTO telegram_state") else None
    )
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        release_lock()

    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    try:
        run_poller(
            conn, make_config(tmp_path), api, logger,
            sleep=sleep, should_stop=should_stop, answer_worker=FakeWorker(),
            # 内蔵タイマーが DB に書かない時刻(夜アンケート・watchdog の前)
            now_fn=lambda: datetime(2026, 10, 20, 6, 0, tzinfo=JST),
        )
    finally:
        conn.set_trace_callback(None)
        holder.close()

    assert sleeps == [RECONNECT_BACKOFF_S]
    assert api.offsets == [None, None]  # offset を進めずに取り直した
    assert conn.execute("SELECT note FROM item_notes").fetchall()[0]["note"] == "あとで読み返す"
    assert conn.execute("SELECT count(*) FROM item_notes").fetchone()[0] == 1
    assert api.sent == [
        (CHAT_ID, "📝 感想を記録しました。明朝の export で Obsidian ノートに反映します")
    ]
    assert saved_offset(conn) == "6"


def test_update_failing_transiently_too_often_is_quarantined(
    conn, tmp_path, monkeypatch, caplog
) -> None:
    # GH#181: 同じ update が続けて一時的に失敗するなら、やり直しの上限を超えたところで隔離して
    # offset を進める(無限に再処理しない)。ログは WARN で、本文は残さない。後ろの update は処理する
    from app.delivery import poller

    real_handle_update = poller.handle_update

    def handle_update(conn, config, api, update, logger, **kwargs):
        if update["update_id"] == 1:
            raise sqlite3.OperationalError("database is locked")
        real_handle_update(conn, config, api, update, logger, **kwargs)

    monkeypatch.setattr(poller, "handle_update", handle_update)
    api = RedeliveringApi([
        make_message_update("隔離される本文", update_id=1),
        make_message_update("https://example.com/next", update_id=2),
    ])
    sleeps: list[float] = []
    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] > TRANSIENT_RETRIES + 1

    with caplog.at_level(logging.WARNING, logger=logger.name):
        run_poller(
            conn, make_config(tmp_path), api, logger,
            sleep=sleeps.append, should_stop=should_stop, answer_worker=FakeWorker(),
            now_fn=lambda: datetime(2026, 10, 20, 6, 0, tzinfo=JST),
        )

    # 初回 + やり直し TRANSIENT_RETRIES 回。最後の失敗の後は待たずに隔離して進める
    assert api.offsets == [None] * (TRANSIENT_RETRIES + 1)
    assert sleeps == [RECONNECT_BACKOFF_S] * TRANSIENT_RETRIES
    assert saved_offset(conn) == "3"
    assert len(manual_items(conn)) == 1  # 後ろの update は隔離の後に処理した
    quarantined = [r for r in caplog.records if "隔離" in r.getMessage()]
    assert len(quarantined) == 1
    assert quarantined[0].levelno == logging.WARNING
    assert quarantined[0].extra == {"update_id": 1, "keys": ["message"]}
    assert "隔離される本文" not in caplog.text


def test_run_poller_fails_fast_on_permanent_db_error(conn, tmp_path) -> None:
    # スキーマ破損等の恒久エラーは 30s ループせず落とす(KeepAlive → doctor #3 で検知)
    conn.execute("DROP TABLE telegram_state")
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        run_poller(conn, make_config(tmp_path), FakeApi([[]]), logger)


@pytest.mark.parametrize(
    ("error", "receives"),
    [("database is locked", True), ("no such table: telegram_state", False)],
    ids=["transient", "permanent"],
)
def test_run_poller_timer_db_error_boundary(conn, tmp_path, monkeypatch, error, receives) -> None:
    # GH#224: 内蔵タイマーのロック競合は受信を止めず、間隔を空けて試し直す。恒久エラーは
    # 今どおり握らずに落とす(受信へ進まない)
    from app.delivery import poller

    timer_calls: list[datetime] = []

    def run_due_timers(conn, config, api, logger, now):
        timer_calls.append(now)
        raise sqlite3.OperationalError(error)

    monkeypatch.setattr(poller, "run_due_timers", run_due_timers)
    api = FakeApi([[make_message_update("https://example.com/a")]])
    sleeps: list[float] = []
    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    def run() -> None:
        run_poller(
            conn, make_config(tmp_path), api, logger,
            sleep=sleeps.append, should_stop=should_stop, answer_worker=FakeWorker(),
            clock=lambda: 0.0,  # 再試行の間隔は過ぎない
        )

    if not receives:
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            run()
        assert api.offsets == []
        return
    run()
    assert len(timer_calls) == 1  # 2 周目は間隔が過ぎていないので試さない
    assert len(api.offsets) == 2  # 受信は毎周期
    assert len(manual_items(conn)) == 1
    assert sleeps == []


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
    # 記事メッセージとして送っていない item は Items ノートへの投影の対象外(GH#229)
    assert api.sent == [(CHAT_ID, "📝 感想を記録しました")]


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


def test_reply_to_failure_notice_is_linked_to_the_failed_question(conn, tmp_path) -> None:
    # GH#236: 失敗の知らせへのリプライだけを、答えられなかった元の質問に結び付ける
    # (返事・カードへのリプライは結び付けない)
    _item_id, dig_id = add_dig_card(conn)
    conn.execute(
        "INSERT INTO dig_questions (dig_result_id, question, question_message_id, status, "
        "answer, answer_message_id) VALUES (?, '答えた質問', 80, 'answered', '返事', 950)",
        (dig_id,),
    )
    failed_id = conn.execute(
        "INSERT INTO dig_questions (dig_result_id, question, question_message_id, status, "
        "error, answer_message_id) VALUES (?, '元の質問', 81, 'failed', 'LLMError: x', 951)",
        (dig_id,),
    ).lastrowid
    conn.commit()
    api = FakeApi([[
        card_reply("もう一度", 951, message_id=82, update_id=1),
        card_reply("返事への質問", 950, message_id=83, update_id=2),
        card_reply("カードへの質問", 700, message_id=84, update_id=3),
    ]])

    poll_once(conn, answer_config(tmp_path), api, logger, answer_worker=FakeWorker())

    rows = conn.execute(
        "SELECT question, retry_of FROM dig_questions WHERE question_message_id >= 82 ORDER BY id"
    ).fetchall()
    assert [(r["question"], r["retry_of"]) for r in rows] == [
        ("もう一度", failed_id), ("返事への質問", None), ("カードへの質問", None)
    ]


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


@pytest.mark.parametrize(
    ("earlier_created_at", "accepted"),
    [
        ("2026-10-17T14:59:59Z", True),  # JST 10/17 23:59:59 = 前日分は数えない
        ("2026-10-17T15:00:00Z", False),  # JST 10/18 00:00:00 = 当日分
    ],
)
def test_daily_cap_counts_by_jst_day(conn, tmp_path, earlier_created_at, accepted) -> None:
    _item_id, dig_id = add_dig_card(conn)
    conn.execute(
        "INSERT INTO dig_questions (dig_result_id, question, question_message_id, created_at) "
        "VALUES (?, '先の質問', 80, ?)",
        (dig_id, earlier_created_at),
    )
    api, worker = FakeApi([[card_reply("次の質問", 700)]]), FakeWorker()
    now = datetime(2026, 10, 18, 0, 0, 30, tzinfo=JST)

    poll_once(
        conn, answer_config(tmp_path, cap=1), api, logger,
        now_fn=lambda: now, answer_worker=worker,
    )

    count = conn.execute("SELECT count(*) FROM dig_questions").fetchone()[0]
    assert count == (2 if accepted else 1)
    assert worker.kicks == (1 if accepted else 0)


def test_note_and_question_are_recorded_together(conn, tmp_path, monkeypatch) -> None:
    # 台帳に積めなかったら感想も残さない(再配達で感想だけ二重にならない)。積めなかったのが
    # ロック競合なら offset を進めず、Telegram が再配達した同じ update で両方を記帳する(GH#181)
    from app.delivery import poller

    _item_id, _dig_id = add_dig_card(conn)
    api, worker = RedeliveringApi([card_reply("質問", 700)]), FakeWorker()
    config = answer_config(tmp_path)
    real_enqueue_question = poller.enqueue_question

    def locked_once(*args):
        monkeypatch.setattr(poller, "enqueue_question", real_enqueue_question)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(poller, "enqueue_question", locked_once)

    with pytest.raises(sqlite3.OperationalError, match="locked"):  # run_poller が待って再開する
        poll_once(conn, config, api, logger, answer_worker=worker)
    assert conn.execute("SELECT count(*) FROM item_notes").fetchone()[0] == 0
    assert saved_offset(conn) is None  # offset を進めない
    assert api.sent == []

    poll_once(conn, config, api, logger, answer_worker=worker)
    assert api.offsets == [None, None]  # 同じ offset で取り直した = 再配達
    assert conn.execute("SELECT count(*) FROM item_notes").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM dig_questions").fetchone()[0] == 1
    assert api.sent == [(CHAT_ID, "📝 記録しました。返事を作っています(数分かかることがあります)")]
    assert worker.kicks == 1
    assert saved_offset(conn) == "2"


@pytest.mark.parametrize(
    ("delivered", "projected"),
    [
        ("article", True),  # 記事メッセージとして送った item = 追いパスが Items ノートへ投影する
        ("superseded", True),  # --force の置換で外れた記事も、前の世代の行で投影する(GH#190)
        ("bookmark_section", False),  # 🔖 保存節にだけ載った X ブックマーク = 投影の対象外
    ],
)
def test_card_reply_with_answers_disabled_is_note_only(
    conn, tmp_path, delivered, projected
) -> None:
    # GH#229: Obsidian への反映は、翌朝の export が実際に感想を投影する item のときだけ案内する
    item_id, _dig_id = add_dig_card(conn)
    if delivered == "bookmark_section":
        digest_id = add_digest(conn, [], bookmark_item_ids=[item_id])
        conn.execute(  # 保存節はヘッダメッセージに載る(記事の行は無い)
            "INSERT INTO digest_messages (digest_id, item_id, message_id) VALUES (?, NULL, 500)",
            (digest_id,),
        )
    else:
        # 置換で外れた記事は今の台帳(item_ids)に無く、印を付けた前の世代の行だけが残る
        digest_id = add_digest(conn, [item_id] if delivered == "article" else [])
        conn.execute(
            "INSERT INTO digest_messages (digest_id, item_id, message_id, superseded_at) "
            "VALUES (?, ?, 501, ?)",
            (digest_id, item_id, None if delivered == "article" else "2026-10-20T00:00:00Z"),
        )
    conn.commit()
    api, worker = FakeApi([[card_reply("カードへの感想", 700)]]), FakeWorker()
    config = replace(
        answer_config(tmp_path, cap=0),
        obsidian=ObsidianConfig(vault_path=str(tmp_path / "vault")),
    )

    poll_once(conn, config, api, logger, answer_worker=worker)

    assert conn.execute("SELECT count(*) FROM dig_questions").fetchone()[0] == 0
    assert conn.execute("SELECT item_id FROM item_notes").fetchone()[0] == item_id
    notice = (
        "📝 感想を記録しました。明朝の export で Obsidian ノートに反映します"
        if projected
        else "📝 感想を記録しました"
    )
    assert api.sent == [(CHAT_ID, notice)]
    assert worker.kicks == 0

    # 案内は翌朝の export の投影と一致する(対象外の感想は item_notes に残るだけ)
    export_pending_notes(conn, config, logger)
    items_dir = tmp_path / "vault" / config.obsidian.folder / "Items"
    notes = [p for p in items_dir.glob("*.md") if "カードへの感想" in p.read_text()]
    assert len(notes) == (1 if projected else 0)
    exported_at = conn.execute("SELECT exported_at FROM item_notes").fetchone()[0]
    assert (exported_at is not None) == projected


def test_redelivered_card_reply_is_not_queued_twice(conn, tmp_path, monkeypatch) -> None:
    # 台帳に積んだ後、offset を保存する前に止まると同じ update が再配達される
    _item_id, _dig_id = add_dig_card(conn)
    api, worker = RedeliveringApi([card_reply("質問", 700)]), FakeWorker()
    config = answer_config(tmp_path)
    stop_before_saving_offset(monkeypatch)

    with pytest.raises(_Stopped):
        poll_once(conn, config, api, logger, answer_worker=worker)
    poll_once(conn, config, api, logger, answer_worker=worker)  # 再起動の後

    assert api.offsets == [None, None]  # 同じ update が再配達された
    assert conn.execute("SELECT count(*) FROM dig_questions").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM item_notes").fetchone()[0] == 1
    assert len(api.sent) == 1
    assert worker.kicks == 1
    assert saved_offset(conn) == "2"


@pytest.mark.parametrize("cap", [0, 1])  # 返事をしない設定 / 今日の上限に達した
def test_redelivered_card_reply_recorded_only_is_not_noted_twice(
    conn, tmp_path, monkeypatch, cap
) -> None:
    # GH#193: 質問として受け付けなかった感想は台帳(dig_questions)に無い。感想の記帳そのもので
    # 再配達を見分け、感想を二重にしない
    _item_id, dig_id = add_dig_card(conn)
    if cap:
        conn.execute(
            "INSERT INTO dig_questions (dig_result_id, question, question_message_id) "
            "VALUES (?, '今日の 1 件目', 80)",
            (dig_id,),
        )
    api, worker = RedeliveringApi([card_reply("感想", 700)]), FakeWorker()
    config = answer_config(tmp_path, cap=cap)
    stop_before_saving_offset(monkeypatch)

    with pytest.raises(_Stopped):
        poll_once(conn, config, api, logger, answer_worker=worker)
    poll_once(conn, config, api, logger, answer_worker=worker)  # 再起動の後

    assert api.offsets == [None, None]
    assert conn.execute("SELECT count(*) FROM item_notes").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM dig_questions").fetchone()[0] == cap
    assert len(api.sent) == 1
    assert worker.kicks == 0
    assert saved_offset(conn) == "2"


@pytest.mark.parametrize("fail_updates", [False, True])
def test_run_poller_sweeps_answers_each_cycle(conn, tmp_path, fail_updates) -> None:
    # 受信が失敗する周期でも見回る(終わった子の回収・起動し直しを止めない)
    class FailingApi(FakeApi):
        def get_updates(self, offset):
            raise DeliveryError("接続失敗")

    worker = FakeWorker()
    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 2  # 2周で停止

    run_poller(
        conn, make_config(tmp_path), FailingApi() if fail_updates else FakeApi(), logger,
        sleep=lambda _s: None, should_stop=should_stop, answer_worker=worker,
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


def test_reply_to_message_replaced_by_force_records_note(conn, tmp_path) -> None:
    # GH#190: --force の置換と送り直しの後も、Telegram に残る前のメッセージへのリプライを
    # 感想として記録する。再選抜で外れた記事(dropped)も、その記事の感想になる
    from app.config import DigestConfig
    from app.delivery import deliver_digest
    from app.digest import generate_digest
    from tests.test_delivery import ApiRecorder
    from tests.test_ranking import add_analyzed

    kept = add_analyzed(conn, "https://a/kept", relevance=90)
    dropped = add_analyzed(conn, "https://a/dropped", relevance=80)
    config = replace(make_config(tmp_path), digest=DigestConfig(max_items=2))
    digest_id = generate_digest(conn, config, logger)
    recorder = ApiRecorder()  # message_id はヘッダ 101・kept 102・dropped 103 の順
    deliver_digest(conn, config, recorder.api(), digest_id, logger)

    narrowed = replace(config, digest=DigestConfig(max_items=1))
    assert generate_digest(conn, narrowed, logger, force=True) == digest_id
    row = conn.execute("SELECT item_ids FROM digests").fetchone()
    assert json.loads(row["item_ids"]) == [kept]  # dropped は再選抜で外れた
    deliver_digest(conn, narrowed, recorder.api(), digest_id, logger)  # 104・105 を送り直す

    api = FakeApi([[
        make_reply_update("外れた記事への感想", 103, update_id=1),
        make_reply_update("置換の前のヘッダへの感想", 101, update_id=2),
        make_reply_update("送り直した記事への感想", 105, update_id=3),
    ]])
    poll_once(conn, config, api, logger)

    notes = conn.execute("SELECT item_id, note FROM item_notes ORDER BY id").fetchall()
    assert [tuple(r) for r in notes] == [
        (dropped, "外れた記事への感想"), (kept, "送り直した記事への感想"),
    ]
    digest_notes = conn.execute("SELECT digest_id, note FROM digest_notes").fetchall()
    assert [tuple(r) for r in digest_notes] == [(digest_id, "置換の前のヘッダへの感想")]
    assert all(text != NO_URL_GUIDE for _, text in api.sent)  # URL 投入へ落ちない
    assert manual_items(conn) == []


@pytest.mark.parametrize("table", ["item_notes", "digest_notes"])  # 記事への感想 / 日次感想
def test_redelivered_note_is_recorded_once(conn, tmp_path, monkeypatch, table) -> None:
    # GH#193: 感想を記帳した後、offset を保存する前に止まると同じ update が再配達される。
    # 同じリプライ(chat_id と message_id)は 1 回だけ記帳し、知らせも二重にしない。
    # 別のリプライは同じ文面でも別の感想として記帳する
    item_id = add_item(conn, "https://a/article")
    digest_id = add_digest(conn, [item_id])
    conn.execute(
        "INSERT INTO digest_messages (digest_id, item_id, message_id) VALUES (?, ?, 500)",
        (digest_id, item_id if table == "item_notes" else None),
    )
    first = make_reply_update("同じ文面", 500, update_id=9)
    first["message"]["message_id"] = 31
    second = make_reply_update("同じ文面", 500, update_id=10)
    second["message"]["message_id"] = 32
    api = RedeliveringApi([first, second])
    stop_before_saving_offset(monkeypatch)

    with pytest.raises(_Stopped):
        poll_once(conn, make_config(tmp_path), api, logger)
    poll_once(conn, make_config(tmp_path), api, logger)  # 再起動の後

    assert api.offsets == [None, None]  # first が再配達された
    rows = conn.execute(f"SELECT note, chat_id, message_id FROM {table} ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("同じ文面", CHAT_ID, 31), ("同じ文面", CHAT_ID, 32)]
    assert len(api.sent) == 2  # first と second に 1 回ずつ
    assert saved_offset(conn) == "11"


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


def test_dig_reply_failed_midway_applies_nothing_until_redelivered(
    conn, tmp_path, monkeypatch
) -> None:
    # GH#181: 2 件目の反映がロック競合で失敗したら 1 件目も戻し、再配達で全部を 1 回で反映する
    # (反映済みの 1 件目をやり直して requested_at を動かすと、生成中の深掘りの
    # compare-and-set が外れて同じ依頼を翌朝も掘ってしまう)
    from app.delivery import poller

    first = add_dig_candidate(conn, "https://a/1")
    second = add_dig_candidate(conn, "https://a/2")
    api = RedeliveringApi([make_reply_update(f"{first} を観点Aで、{second} も", BACKLOG_MSG_ID)])
    real_apply_instructions = poller.apply_instructions

    def locked_at_second(conn, instructions):
        monkeypatch.setattr(poller, "apply_instructions", real_apply_instructions)
        real_apply_instructions(conn, instructions[:1])
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(poller, "apply_instructions", locked_at_second)

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        poll_once(conn, make_config(tmp_path), api, logger)
    assert conn.execute("SELECT count(*) FROM dig_queue").fetchone()[0] == 0  # 1 件目も戻した
    assert saved_offset(conn) is None
    assert api.sent == []

    poll_once(conn, make_config(tmp_path), api, logger)  # 再配達
    rows = conn.execute("SELECT item_id, status, angle FROM dig_queue ORDER BY item_id").fetchall()
    assert [tuple(r) for r in rows] == [(first, "queued", "観点Aで"), (second, "queued", None)]
    assert len(api.sent) == 1
    assert saved_offset(conn) == "2"


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


def test_dig_reply_ack_escapes_html_in_angle(conn, tmp_path) -> None:
    # GH#194: 返信は parse_mode=HTML。自由文の観点は escape して埋め込み、キューには原文
    item = add_dig_candidate(conn, "https://a/1")
    angle = "R&D と <b>A<B</b> の観点で"
    api = FakeApi([[make_reply_update(f"{item} を{angle}", BACKLOG_MSG_ID)]])

    poll_once(conn, make_config(tmp_path), api, logger)

    row = conn.execute("SELECT angle FROM dig_queue WHERE item_id = ?", (item,)).fetchone()
    assert row["angle"] == angle
    assert api.sent == [(
        CHAT_ID,
        f"🔍 {item} を登録しました"
        "(観点: R&amp;D と &lt;b&gt;A&lt;B&lt;/b&gt; の観点で)",
    )]


def test_dig_reply_ack_shows_retained_angle_on_bare_rerequest(conn, tmp_path) -> None:
    # GH#228: 観点を省いた再指定は前回の観点を引き継ぐ(apply_instructions の COALESCE)。
    # 確認の返信は「観点なし」ではなく、引き継いだ観点を(escape して)出す
    item = add_dig_candidate(conn, "https://a/1")
    fresh = add_dig_candidate(conn, "https://a/2")
    conn.execute(
        "INSERT INTO dig_queue (item_id, status, source, angle) "
        "VALUES (?, 'passed', 'request', '旧<観点>&')",
        (item,),
    )
    conn.commit()
    api = FakeApi([[make_reply_update(f"{item} も、{fresh} も", BACKLOG_MSG_ID)]])

    poll_once(conn, make_config(tmp_path), api, logger)

    row = conn.execute("SELECT * FROM dig_queue WHERE item_id = ?", (item,)).fetchone()
    assert (row["status"], row["angle"]) == ("queued", "旧<観点>&")
    assert api.sent == [(
        CHAT_ID,
        f"🔍 {item} を登録しました(前回の観点を引き継ぎ: 旧&lt;観点&gt;&amp;)\n"
        f"🔍 {fresh} を登録しました(観点なし)",
    )]


def test_dig_reply_ack_with_many_long_retained_angles_fits_message_limit(
    conn, tmp_path
) -> None:
    # GH#228 の Codex must: 長い観点を引き継ぐ item を何件もまとめて再指定しても、確認の
    # 返信が Telegram の 1 通の上限を超えない(観点は表示だけ切り詰め、行単位で複数通に分ける)
    from app.delivery.poller import DIG_ACK_ANGLE_MAX_CHARS, DIG_ACK_MAX_CHARS
    from app.delivery.telegram import escape_and_truncate

    long_angle = "あ&" * 1_050  # 2,100 字(escape 後はもっと長い)
    items = [add_dig_candidate(conn, f"https://a/{n}") for n in range(15)]
    for item in items:
        conn.execute(
            "INSERT INTO dig_queue (item_id, status, source, angle) "
            "VALUES (?, 'passed', 'request', ?)",
            (item, long_angle),
        )
    conn.commit()
    text = "、".join(f"{item} も" for item in items)
    api = FakeApi([[make_reply_update(text, BACKLOG_MSG_ID)]])

    poll_once(conn, make_config(tmp_path), api, logger)

    # キューの観点は原文のまま(切り詰めは表示だけ)
    angles = {r["angle"] for r in conn.execute("SELECT angle FROM dig_queue")}
    assert angles == {long_angle}
    assert len(api.sent) >= 2
    assert all(chat == CHAT_ID for chat, _ in api.sent)
    assert all(len(message) <= DIG_ACK_MAX_CHARS for _, message in api.sent)
    lines = [line for _, message in api.sent for line in message.split("\n")]
    shown = escape_and_truncate(long_angle, DIG_ACK_ANGLE_MAX_CHARS)
    assert shown.endswith("…")
    assert lines == [
        f"🔍 {item} を登録しました(前回の観点を引き継ぎ: {shown})" for item in items
    ]


def dig_button(dig_id: int, code: str, *, card_message_id: int = 700, update_id: int = 1,
               chat_id: int = CHAT_ID) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": OWNER_ID},
            "data": f"d:{dig_id}:{code}",
            "message": {"message_id": card_message_id, "chat": {"id": chat_id}},
        },
    }


def dig_requests(conn) -> list[tuple]:
    rows = conn.execute(
        "SELECT dig_result_id, request, question_message_id, status FROM dig_questions ORDER BY id"
    )
    return [tuple(r) for r in rows]


def test_card_buttons_queue_requests_and_kick_answer(conn, tmp_path) -> None:
    # GH#141: 📝 要約・📖 詳しく は質問と同じ台帳に積み、返事はカードへのリプライ
    _item_id, dig_id = add_dig_card(conn)
    api = FakeApi([[dig_button(dig_id, "s"), dig_button(dig_id, "m", update_id=2)]])
    worker = FakeWorker()

    poll_once(conn, answer_config(tmp_path), api, logger, answer_worker=worker)

    assert dig_requests(conn) == [
        (dig_id, "summary", None, "pending"),
        (dig_id, "more", None, "pending"),
    ]
    assert api.answered == [
        ("cb1", "📝 要約を作っています(数分かかることがあります)"),
        ("cb2", "📖 詳しい説明を作っています(数分かかることがあります)"),
    ]
    assert worker.kicks == 2
    assert conn.execute("SELECT count(*) FROM item_notes").fetchone()[0] == 0  # 感想ではない


def test_same_button_twice_is_accepted_once(conn, tmp_path) -> None:
    # 再配達・二重押しで 2 回作らない。上限に達していても「受け付け済み」と分かるように返す
    _item_id, dig_id = add_dig_card(conn)
    api = FakeApi([[dig_button(dig_id, "s"), dig_button(dig_id, "s", update_id=2)]])
    worker = FakeWorker()

    poll_once(conn, answer_config(tmp_path, cap=1), api, logger, answer_worker=worker)

    assert dig_requests(conn) == [(dig_id, "summary", None, "pending")]
    assert api.answered[1] == ("cb2", "受け付け済みです(返事は届いているか作成中)")
    assert worker.kicks == 1


def test_card_button_over_daily_cap_or_disabled_is_not_queued(conn, tmp_path) -> None:
    _item_id, dig_id = add_dig_card(conn)
    conn.execute(
        "INSERT INTO dig_questions (dig_result_id, question, question_message_id) "
        "VALUES (?, '今日の 1 件目', 80)",
        (dig_id,),
    )
    conn.commit()
    api, worker = FakeApi([[dig_button(dig_id, "s")]]), FakeWorker()
    poll_once(conn, answer_config(tmp_path, cap=1), api, logger, answer_worker=worker)
    assert api.answered == [("cb1", "今日の返事の上限(1 件)に達しました")]

    api = FakeApi([[dig_button(dig_id, "m", update_id=2)]])
    poll_once(conn, answer_config(tmp_path, cap=0), api, logger, answer_worker=worker)
    assert api.answered == [("cb2", "返事をしない設定です")]
    assert len(dig_requests(conn)) == 1  # 今日の 1 件目だけ
    assert worker.kicks == 0


@pytest.mark.parametrize(
    ("card_message_id", "chat_id"),
    [(701, CHAT_ID), (700, 999)],  # 別のメッセージ / owner の chat 以外
)
def test_card_button_not_bound_to_the_card_is_rejected(
    conn, tmp_path, card_message_id, chat_id
) -> None:
    # callback_data だけを信じない: 押されたのが、そのカードとして送ったメッセージか確かめる
    _item_id, dig_id = add_dig_card(conn)
    api = FakeApi([[dig_button(dig_id, "s", card_message_id=card_message_id, chat_id=chat_id)]])
    worker = FakeWorker()

    poll_once(conn, answer_config(tmp_path), api, logger, answer_worker=worker)

    assert dig_requests(conn) == []
    assert api.answered == [("cb1", "⚠️ このカードには対応できません")]
    assert worker.kicks == 0


@pytest.mark.parametrize(
    ("url", "has_url_button"),
    [
        ("https://a.example/1", True),
        ("ftp://a.example/1", False),
        ("https://例え.jp/記事", False),  # 送信が 400 で恒久失敗しないよう、ASCII の URL だけ
        (None, False),
        ("https://a.example/" + "x" * 3000, False),
        ("https://bad host.example/article", False),  # ホストに空白
        ("https://a.example/a b", False),  # 空白
        ("https://a.example/\x01", False),  # 制御文字
        ("https://localhost/x", False),  # ドットの無いホスト
        ("https://A.Example:8443/path?q=1#f", True),
    ],
)
def test_dig_card_keyboard_only_links_safe_urls(url, has_url_button) -> None:
    keyboard = dig_card_keyboard(5, url, with_requests=True)
    buttons = keyboard["inline_keyboard"][0]
    assert ("url" in buttons[0]) is has_url_button
    assert [b["callback_data"] for b in buttons if "callback_data" in b] == ["d:5:s", "d:5:m"]
    assert dig_card_keyboard(5, None, with_requests=False) is None

