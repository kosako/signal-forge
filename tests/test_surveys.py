import json
import logging
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.config import Config, TelegramConfig, XConfig
from app.db import apply_migrations, connect
from app.delivery import notify_run_failure, poll_once, run_due_timers, run_poller
from app.delivery.state import get_state
from app.delivery.surveys import (
    STATE_RUN_FAILURE_NOTIFIED,
    STATE_SURVEY_SENT,
    STATE_WATCHDOG_NOTIFIED,
)
from app.delivery.telegram import DeliveryError
from app.digest.builder import JST, today_jst
from tests.test_delivery import add_digest, add_item
from tests.test_poller import CHAT_ID, FakeApi, make_callback_update, make_config

logger = logging.getLogger("test-surveys")


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'TechCrunch')")  # id=2
    yield conn
    conn.close()


def jst_now(hhmm: str = "21:30") -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    return datetime.now(JST).replace(hour=hour, minute=minute, second=0, microsecond=0)


def add_dig_reaction(conn, item_id: int, signal_date: str) -> None:
    conn.execute(
        "INSERT INTO signals (kind, item_id, value, signal_date) "
        "VALUES ('reaction', ?, 'dig', ?)",
        (item_id, signal_date),
    )


def silence_watchdog(conn) -> None:
    """当日 digest を配信済みにして watchdog を発火させない(夜アンケート単体の検証用)。"""
    digest_id = add_digest(conn, [])
    conn.execute("UPDATE digests SET delivery_status = 'sent' WHERE id = ?", (digest_id,))


# --- 内蔵タイマー: 夜アンケート ---


def test_survey_not_sent_before_time(conn, tmp_path) -> None:
    silence_watchdog(conn)
    api = FakeApi()
    run_due_timers(conn, make_config(tmp_path), api, logger, jst_now("21:29"))
    assert api.sent == []
    assert get_state(conn, STATE_SURVEY_SENT) is None


def test_survey_sent_once_at_time(conn, tmp_path) -> None:
    silence_watchdog(conn)
    api = FakeApi()
    now = jst_now("21:30")
    config = make_config(tmp_path)

    run_due_timers(conn, config, api, logger, now)
    run_due_timers(conn, config, api, logger, now)  # 同日2回目は送らない

    texts = [t for _, t in api.sent]
    assert len(texts) == 2  # 質問1 + 質問2(追い掛け対象なし)
    assert "巡回した?" in texts[0]
    assert "ダイジェストを読むのにかかった時間" in texts[1]
    assert get_state(conn, STATE_SURVEY_SENT) == now.strftime("%Y-%m-%d")


def test_survey_callback_data_carries_date(conn, tmp_path) -> None:
    api = FakeApi()

    class RecordingApi(FakeApi):
        def __init__(self):
            super().__init__()
            self.markups: list[dict] = []

        def send_message(self, chat_id, text, *, reply_markup=None):
            self.markups.append(reply_markup)
            return super().send_message(chat_id, text, reply_markup=reply_markup)

    api = RecordingApi()
    now = jst_now("21:30")
    run_due_timers(conn, make_config(tmp_path), api, logger, now)

    date_compact = now.strftime("%Y%m%d")
    q1_buttons = api.markups[0]["inline_keyboard"][0]
    assert [b["callback_data"] for b in q1_buttons] == [
        f"q1:{date_compact}:y", f"q1:{date_compact}:n"
    ]
    q2_buttons = api.markups[1]["inline_keyboard"][0]
    assert [b["callback_data"] for b in q2_buttons] == [
        f"q2:{date_compact}:5", f"q2:{date_compact}:10", f"q2:{date_compact}:15",
        f"q2:{date_compact}:20", f"q2:{date_compact}:30",
    ]
    assert all(len(b["callback_data"].encode()) <= 64 for b in q1_buttons + q2_buttons)


def test_followup_sent_for_yesterdays_dig(conn, tmp_path) -> None:
    now = jst_now("21:30")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    dug = add_item(conn, "https://a/dug", title="深掘り対象 <X>")
    add_dig_reaction(conn, dug, yesterday)
    retapped = add_item(conn, "https://a/retap")  # dig 後に noise へ変更 → 対象外
    add_dig_reaction(conn, retapped, yesterday)
    conn.execute(
        "INSERT INTO signals (kind, item_id, value, signal_date) "
        "VALUES ('reaction', ?, 'noise', ?)",
        (retapped, yesterday),
    )
    answered = add_item(conn, "https://a/answered")  # followup_dig 記録済み → 対象外
    add_dig_reaction(conn, answered, yesterday)
    conn.execute(
        "INSERT INTO signals (kind, item_id, value, signal_date) "
        "VALUES ('followup_dig', ?, 'yes', ?)",
        (answered, yesterday),
    )

    api = FakeApi()
    run_due_timers(conn, make_config(tmp_path), api, logger, now)

    followup_texts = [t for _, t in api.sent if "深掘りした?" in t]
    assert len(followup_texts) == 1
    assert "深掘り対象 &lt;X&gt;" in followup_texts[0]  # HTML エスケープ済み


def test_followup_prefers_title_ja(conn, tmp_path) -> None:
    now = jst_now("21:30")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    silence_watchdog(conn)
    dug = add_item(conn, "https://a/en", title="English Title", title_ja="日本語の見出し")
    add_dig_reaction(conn, dug, yesterday)

    api = FakeApi()
    run_due_timers(conn, make_config(tmp_path), api, logger, now)

    followup = next(t for _, t in api.sent if "深掘りした?" in t)
    assert "日本語の見出し" in followup
    assert "English Title" not in followup


def test_followup_links_to_canonical_url(conn, tmp_path) -> None:
    # GH#88: その場から対象を読みに行ける(タイトルが canonical_url へのリンク)
    now = jst_now("21:30")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    silence_watchdog(conn)
    dug = add_item(conn, "https://a/dug?x=1&y=2", title="対象記事")
    add_dig_reaction(conn, dug, yesterday)

    api = FakeApi()
    run_due_timers(conn, make_config(tmp_path), api, logger, now)

    followup = next(t for _, t in api.sent if "深掘りした?" in t)
    assert '<a href="https://a/dug?x=1&amp;y=2">対象記事</a>' in followup  # 属性エスケープ済み


def test_dig_backlog_list_sent_before_followups(conn, tmp_path) -> None:
    # GH#88: 未消化 🔖 の累計一覧を追い掛けの前に1通。「いいえ」回答済みは残し、
    # 「はい」(深掘り済み)と reaction 訂正済みは載せない。新しい 🔖 が上
    now = jst_now("21:30")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    older = (now - timedelta(days=3)).strftime("%Y-%m-%d")
    silence_watchdog(conn)

    not_dug_yet = add_item(conn, "https://a/not-dug", title="いいえ回答済み")
    add_dig_reaction(conn, not_dug_yet, older)
    conn.execute(
        "INSERT INTO signals (kind, item_id, value, signal_date) "
        "VALUES ('followup_dig', ?, 'no', ?)",
        (not_dug_yet, older),
    )
    done = add_item(conn, "https://a/done", title="深掘り済み")  # yes → 一覧に載せない
    add_dig_reaction(conn, done, older)
    conn.execute(
        "INSERT INTO signals (kind, item_id, value, signal_date) "
        "VALUES ('followup_dig', ?, 'yes', ?)",
        (done, older),
    )
    corrected = add_item(conn, "https://a/corrected", title="訂正済み")  # 最新が noise
    add_dig_reaction(conn, corrected, older)
    conn.execute(
        "INSERT INTO signals (kind, item_id, value, signal_date) "
        "VALUES ('reaction', ?, 'noise', ?)",
        (corrected, older),
    )
    fresh = add_item(conn, "https://a/fresh", title="昨日の🔖")
    add_dig_reaction(conn, fresh, yesterday)

    api = FakeApi()
    run_due_timers(conn, make_config(tmp_path), api, logger, now)

    texts = [t for _, t in api.sent]
    backlog = next(t for t in texts if "未消化の深掘り候補" in t)
    assert "(2件)" in backlog
    assert backlog.index("昨日の🔖") < backlog.index("いいえ回答済み")  # 新しい 🔖 が上
    assert "深掘り済み" not in backlog
    assert "訂正済み" not in backlog
    followup_index = next(i for i, t in enumerate(texts) if "深掘りした?" in t)
    assert texts.index(backlog) < followup_index  # 一覧は追い掛けより前


def test_dig_backlog_not_sent_when_empty(conn, tmp_path) -> None:
    # GH#88: 0件の日は一覧を送らない(followup_dig=yes 済みのみ → 未消化ゼロ)
    now = jst_now("21:30")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    silence_watchdog(conn)
    done = add_item(conn, "https://a/done")
    add_dig_reaction(conn, done, yesterday)
    conn.execute(
        "INSERT INTO signals (kind, item_id, value, signal_date) "
        "VALUES ('followup_dig', ?, 'yes', ?)",
        (done, yesterday),
    )

    api = FakeApi()
    run_due_timers(conn, make_config(tmp_path), api, logger, now)

    assert not any("未消化の深掘り候補" in t for _, t in api.sent)


def test_dig_backlog_caps_at_max_items(conn, tmp_path) -> None:
    # GH#88: 上限 15 件 + 「他 N 件」。可視テキストを 4096 字制限内に保つ
    now = jst_now("21:30")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    silence_watchdog(conn)
    for i in range(17):
        item = add_item(conn, f"https://a/many-{i}", title=f"記事{i}")
        add_dig_reaction(conn, item, yesterday)

    api = FakeApi()
    run_due_timers(conn, make_config(tmp_path), api, logger, now)

    backlog = next(t for _, t in api.sent if "未消化の深掘り候補" in t)
    assert "(17件)" in backlog
    assert backlog.count("<a href=") == 15
    assert "(他 2 件)" in backlog


# --- 内蔵タイマー: watchdog ---


def test_watchdog_notifies_when_digest_missing(conn, tmp_path) -> None:
    api = FakeApi()
    config = make_config(tmp_path)
    now = jst_now("07:15")

    run_due_timers(conn, config, api, logger, now)
    run_due_timers(conn, config, api, logger, now)  # 同一種別1日1通

    warnings = [t for _, t in api.sent if "watchdog" in t]
    assert len(warnings) == 1
    assert "未生成" in warnings[0]
    assert get_state(conn, STATE_WATCHDOG_NOTIFIED) == now.strftime("%Y-%m-%d")


def test_watchdog_notifies_when_digest_unsent(conn, tmp_path) -> None:
    add_digest(conn, [])  # delivery_status='pending'
    api = FakeApi()
    run_due_timers(conn, make_config(tmp_path), api, logger, jst_now("07:15"))
    assert any("未配信" in t for _, t in api.sent)


def test_watchdog_silent_when_digest_sent(conn, tmp_path) -> None:
    digest_id = add_digest(conn, [])
    conn.execute("UPDATE digests SET delivery_status = 'sent' WHERE id = ?", (digest_id,))
    api = FakeApi()
    run_due_timers(conn, make_config(tmp_path), api, logger, jst_now("07:15"))
    assert api.sent == []
    assert get_state(conn, STATE_WATCHDOG_NOTIFIED) is None  # 問題なし = 通知枠を消費しない


def test_watchdog_not_due_before_time(conn, tmp_path) -> None:
    api = FakeApi()
    run_due_timers(conn, make_config(tmp_path), api, logger, jst_now("07:14"))
    assert api.sent == []


# --- 朝バッチ失敗通知 ---


def test_notify_run_failure_once_per_day(conn, tmp_path) -> None:
    api = FakeApi()
    config = make_config(tmp_path)
    notify_run_failure(conn, config, api, ["collect", "digest"], "01RUN", logger)
    notify_run_failure(conn, config, api, ["export"], "01RUN2", logger)

    assert len(api.sent) == 1
    assert "collect, digest" in api.sent[0][1]
    assert "intel run" in api.sent[0][1]
    assert get_state(conn, STATE_RUN_FAILURE_NOTIFIED) == today_jst()


def test_notify_run_failure_swallows_send_error(conn, tmp_path) -> None:
    class FailingApi(FakeApi):
        def send_message(self, chat_id, text, *, reply_markup=None):
            raise DeliveryError("送信失敗")

    notify_run_failure(
        conn, make_config(tmp_path), FailingApi(), ["deliver"], "01RUN", logger
    )  # 例外が漏れない
    # 送信できなかった日は通知枠を消費しない(次の再走で再通知できる)
    assert get_state(conn, STATE_RUN_FAILURE_NOTIFIED) is None


def test_watchdog_retries_after_send_failure(conn, tmp_path) -> None:
    class FailOnceApi(FakeApi):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def send_message(self, chat_id, text, *, reply_markup=None):
            self.calls += 1
            if self.calls == 1:
                raise DeliveryError("送信失敗")
            return super().send_message(chat_id, text, reply_markup=reply_markup)

    api = FailOnceApi()
    config = make_config(tmp_path)
    with pytest.raises(DeliveryError):
        run_due_timers(conn, config, api, logger, jst_now("07:15"))
    assert get_state(conn, STATE_WATCHDOG_NOTIFIED) is None  # 枠を消費しない

    run_due_timers(conn, config, api, logger, jst_now("07:16"))  # 次周期で成功
    assert len(api.sent) == 1
    assert get_state(conn, STATE_WATCHDOG_NOTIFIED) is not None


# --- watchdog: X ソースの連続失敗(GH#124 / 設計 02 §2) ---

X_400 = "FatalError: X が HTTP 400: https://api.twitter.com/2/tweets/search/recent"


def add_source(
    conn, type_: str, name: str, failures: int, *, enabled: int = 1, error: str = X_400
) -> None:
    conn.execute(
        "INSERT INTO sources (type, name, enabled, consecutive_failures, last_error) "
        "VALUES (?, ?, ?, ?, ?)",
        (type_, name, enabled, failures, error),
    )


def x_config(tmp_path: Path, *, enabled: bool = True) -> Config:
    return replace(make_config(tmp_path), x=XConfig(enabled=enabled))


def test_watchdog_notifies_x_source_failing_three_times(conn, tmp_path) -> None:
    silence_watchdog(conn)
    add_source(conn, "x", "x:search", 3)
    api = FakeApi()
    config = x_config(tmp_path)
    now = jst_now("07:15")

    run_due_timers(conn, config, api, logger, now)
    run_due_timers(conn, config, api, logger, now)  # 同一種別1日1通

    assert len(api.sent) == 1
    text = api.sent[0][1]
    assert "「x:search」が 3 回連続で失敗" in text
    assert "HTTP 400" in text
    assert "sources.enabled=0" in text
    assert "intel run" not in text  # 再走では直らない問題に再走を勧めない
    assert get_state(conn, STATE_WATCHDOG_NOTIFIED) == now.strftime("%Y-%m-%d")


@pytest.mark.parametrize(
    ("type_", "failures", "enabled", "x_enabled"),
    [
        ("x", 2, 1, True),  # しきい値未満
        ("x", 9, 0, True),  # 人が止めたソース
        ("x", 9, 1, False),  # X ごと止めている間は回数が進まない(古い回数で鳴らさない)
        ("rss", 30, 1, True),  # RSS は doctor #14 のみ(設計 02 §3)
    ],
)
def test_watchdog_silent_for_sources_not_to_alert(
    conn, tmp_path, type_, failures, enabled, x_enabled
) -> None:
    silence_watchdog(conn)
    add_source(conn, type_, "src", failures, enabled=enabled)
    api = FakeApi()
    run_due_timers(conn, x_config(tmp_path, enabled=x_enabled), api, logger, jst_now("07:15"))
    assert api.sent == []
    assert get_state(conn, STATE_WATCHDOG_NOTIFIED) is None  # 問題なし = 通知枠を消費しない


def test_watchdog_combines_digest_and_source_problems(conn, tmp_path) -> None:
    add_source(conn, "x", "x:home", 4)
    add_source(conn, "x", "x:search", 5)
    api = FakeApi()  # digest 未生成
    run_due_timers(conn, x_config(tmp_path), api, logger, jst_now("07:15"))

    assert len(api.sent) == 1  # 同一種別1日1通 = 1通にまとめる
    lines = api.sent[0][1].split("\n")
    assert len(lines) == 3
    assert "未生成" in lines[0] and "intel run" in lines[0]
    assert "「x:home」が 4 回" in lines[1]
    assert "「x:search」が 5 回" in lines[2]


def test_watchdog_escapes_and_truncates_source_error(conn, tmp_path) -> None:
    silence_watchdog(conn)
    add_source(conn, "x", "x:<home>", 3, error="E: <b>" + "x" * 500)
    api = FakeApi()
    run_due_timers(conn, x_config(tmp_path), api, logger, jst_now("07:15"))

    text = api.sent[0][1]
    assert "<b>" not in text and "&lt;b&gt;" in text  # parse_mode=HTML で壊れない
    assert "「x:&lt;home&gt;」" in text
    assert "x" * 200 not in text and "…" in text


def test_watchdog_keeps_one_line_per_source(conn, tmp_path) -> None:
    silence_watchdog(conn)
    add_source(conn, "x", "x:\nsearch", 3, error="FatalError: first\nsecond")
    api = FakeApi()
    run_due_timers(conn, x_config(tmp_path), api, logger, jst_now("07:15"))

    assert api.sent[0][1].count("\n") == 0  # 例外の文言が複数行でも 1 件 1 行
    assert "「x: search」" in api.sent[0][1] and "first second" in api.sent[0][1]


def test_watchdog_message_stays_within_telegram_limit(conn, tmp_path) -> None:
    # 4096 字を超えると送信が毎周期失敗し、同じ周期の getUpdates まで止まる
    silence_watchdog(conn)
    for i in range(40):
        add_source(conn, "x", f"x:{i}-" + "n" * 100, 3, error="e" * 300)
    api = FakeApi()
    run_due_timers(conn, x_config(tmp_path), api, logger, jst_now("07:15"))

    assert len(api.sent[0][1]) <= 4000
    assert api.sent[0][1].endswith("…")


def test_watchdog_repeats_source_alert_next_morning(conn, tmp_path) -> None:
    # 「直すか止めるまで毎朝通知」: 翌朝は再送し、同じ朝の再実行では増やさない
    add_source(conn, "x", "x:search", 3)
    api = FakeApi()
    config = x_config(tmp_path)
    today = jst_now("07:15")

    run_due_timers(conn, config, api, logger, today - timedelta(days=1))
    run_due_timers(conn, config, api, logger, today)
    run_due_timers(conn, config, api, logger, today)

    assert len(api.sent) == 2
    assert all("「x:search」が 3 回" in text for _, text in api.sent)
    assert get_state(conn, STATE_WATCHDOG_NOTIFIED) == today.strftime("%Y-%m-%d")


# --- callback: 夜アンケート回答(q1/q2) ---


def test_q1_answer_recorded_with_date_attribution(conn, tmp_path) -> None:
    now = datetime.now(JST)  # 日付は1回だけ取る(深夜跨ぎで date_compact とずれない)
    date_compact = now.strftime("%Y%m%d")
    update = make_callback_update(0, data=f"q1:{date_compact}:y")
    api = FakeApi([[update]])

    poll_once(conn, make_config(tmp_path), api, logger)

    row = conn.execute("SELECT * FROM signals").fetchone()
    assert (row["kind"], row["value"], row["item_id"]) == ("survey_patrol", "yes", None)
    assert row["signal_date"] == now.strftime("%Y-%m-%d")
    assert api.answered == [("cb1", "📝 記録しました")]
    _, _, markup = api.edited[0]
    assert "はい ✓" in json.dumps(markup, ensure_ascii=False)


def test_q2_answer_recorded(conn, tmp_path) -> None:
    date_compact = datetime.now(JST).strftime("%Y%m%d")
    update = make_callback_update(0, data=f"q2:{date_compact}:15")
    api = FakeApi([[update]])

    poll_once(conn, make_config(tmp_path), api, logger)

    row = conn.execute("SELECT * FROM signals").fetchone()
    assert (row["kind"], row["value"]) == ("survey_read_min", "15")


def test_expired_survey_answer_discarded(conn, tmp_path) -> None:
    old = (datetime.now(JST) - timedelta(days=10)).strftime("%Y%m%d")
    update = make_callback_update(0, data=f"q1:{old}:y")
    api = FakeApi([[update]])

    poll_once(conn, make_config(tmp_path), api, logger)

    assert conn.execute("SELECT count(*) FROM signals").fetchone()[0] == 0
    assert api.answered == [("cb1", "⚠️ 回答期限(翌日12:00)を過ぎています")]


@pytest.mark.parametrize(
    "bad_data",
    [
        "q1:20261399:y",  # 実在しない日付
        "q1:2026070:y",  # 桁不足
        "q2:20260707:7",  # 5択にない分数
        "q1:20260707:x",  # 不明 code
    ],
)
def test_malformed_survey_callback_discarded(conn, tmp_path, bad_data) -> None:
    api = FakeApi([[make_callback_update(0, data=bad_data)]])
    poll_once(conn, make_config(tmp_path), api, logger)
    assert conn.execute("SELECT count(*) FROM signals").fetchone()[0] == 0
    assert api.answered == [("cb1", None)]


# --- callback: 🔖追い掛け回答(f) ---


def test_followup_answer_attributed_to_dig_date(conn, tmp_path) -> None:
    item = add_item(conn, "https://a/1")
    dig_date = (datetime.now(JST) - timedelta(days=1)).strftime("%Y-%m-%d")
    add_dig_reaction(conn, item, dig_date)
    api = FakeApi([[make_callback_update(0, data=f"f:{item}:y")]])

    poll_once(conn, make_config(tmp_path), api, logger)

    row = conn.execute("SELECT * FROM signals WHERE kind = 'followup_dig'").fetchone()
    assert (row["item_id"], row["value"]) == (item, "yes")
    assert row["signal_date"] == dig_date  # 🔖 を付けた日に帰属(設計 01 §7)
    assert api.answered == [("cb1", "🔖 記録しました")]
    _, _, markup = api.edited[0]
    assert "はい ✓" in json.dumps(markup, ensure_ascii=False)


def test_followup_without_dig_rejected(conn, tmp_path) -> None:
    item = add_item(conn, "https://a/1")  # 🔖 なし
    api = FakeApi([[make_callback_update(0, data=f"f:{item}:n")]])

    poll_once(conn, make_config(tmp_path), api, logger)

    assert conn.execute("SELECT count(*) FROM signals").fetchone()[0] == 0
    assert api.answered == [("cb1", "⚠️ 記録失敗、もう一度")]


def test_followup_after_dig_corrected_to_noise_rejected(conn, tmp_path) -> None:
    # 追い掛け送信後に digest ボタンで dig → noise に訂正 → 古い追い掛けボタンの回答は
    # 記帳しない(送信側と同じ「その日の最新 reaction が dig」判定。followup_dig 率の汚染防止)
    item = add_item(conn, "https://a/1")
    dig_date = (datetime.now(JST) - timedelta(days=1)).strftime("%Y-%m-%d")
    add_dig_reaction(conn, item, dig_date)
    conn.execute(
        "INSERT INTO signals (kind, item_id, value, signal_date) "
        "VALUES ('reaction', ?, 'noise', ?)",
        (item, dig_date),
    )
    api = FakeApi([[make_callback_update(0, data=f"f:{item}:y")]])

    poll_once(conn, make_config(tmp_path), api, logger)

    assert (
        conn.execute("SELECT count(*) FROM signals WHERE kind='followup_dig'").fetchone()[0]
        == 0
    )
    assert api.answered == [("cb1", "⚠️ 記録失敗、もう一度")]


def test_followup_multiday_dig_with_latest_correction_rejected(conn, tmp_path) -> None:
    # 複数日に dig があり、最新の reaction が noise に訂正されている → 過去日の dig に
    # フォールバックして記帳しない(Codex SF36-01 再指摘の回帰テスト)
    item = add_item(conn, "https://a/1")
    add_dig_reaction(conn, item, "2026-07-05")
    add_dig_reaction(conn, item, "2026-07-06")
    conn.execute(
        "INSERT INTO signals (kind, item_id, value, signal_date) "
        "VALUES ('reaction', ?, 'noise', '2026-07-06')",
        (item,),
    )
    api = FakeApi([[make_callback_update(0, data=f"f:{item}:y")]])

    poll_once(conn, make_config(tmp_path), api, logger)

    assert (
        conn.execute("SELECT count(*) FROM signals WHERE kind='followup_dig'").fetchone()[0]
        == 0
    )
    assert api.answered == [("cb1", "⚠️ 記録失敗、もう一度")]


def test_survey_deadline_boundary(conn, tmp_path) -> None:
    # 対象日の翌日 12:00 JST ちょうどは受理、12:00 を過ぎたら破棄(設計 04 §4)
    target = datetime(2026, 7, 6, tzinfo=JST)
    date_compact = target.strftime("%Y%m%d")
    at_noon = datetime(2026, 7, 7, 12, 0, 0, tzinfo=JST)

    api = FakeApi([[make_callback_update(0, data=f"q1:{date_compact}:y")]])
    poll_once(conn, make_config(tmp_path), api, logger, now_fn=lambda: at_noon)
    assert conn.execute("SELECT count(*) FROM signals").fetchone()[0] == 1

    api = FakeApi([[make_callback_update(0, data=f"q2:{date_compact}:10", update_id=2)]])
    poll_once(
        conn, make_config(tmp_path), api, logger,
        now_fn=lambda: at_noon + timedelta(seconds=1),
    )
    assert conn.execute("SELECT count(*) FROM signals").fetchone()[0] == 1  # 増えない
    assert api.answered == [("cb2", "⚠️ 回答期限(翌日12:00)を過ぎています")]


# --- run_poller 統合 ---


def test_run_poller_requires_owner_chat_id(conn, tmp_path) -> None:
    config = Config(
        config_dir=tmp_path,
        telegram=TelegramConfig(owner_user_id=10, owner_chat_id=None),
    )
    with pytest.raises(DeliveryError, match="owner_chat_id"):
        run_poller(conn, config, FakeApi(), logger)


def test_run_poller_fires_timers_each_cycle(conn, tmp_path) -> None:
    api = FakeApi([[], []])
    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    run_poller(
        conn, make_config(tmp_path), api, logger,
        should_stop=should_stop, now_fn=lambda: jst_now("21:30"),
    )
    assert [t for _, t in api.sent if "巡回した?" in t]  # タイマー経由で送信された
    assert api.sent[0][0] == CHAT_ID


def test_dig_backlog_includes_x_bookmarks(conn, tmp_path) -> None:
    # GH#99: X ブックマークの保存アイテムも未消化一覧に合流する。followup_dig=yes で
    # 消え、dig 反応と両方ある item は 1 行に dedupe
    silence_watchdog(conn)
    older = "2026-07-10"

    bm_only = add_item(conn, "https://b/only", title="ブクマのみ")
    conn.execute(
        "INSERT INTO x_signals (kind, tweet_id, item_id, observed_at) "
        "VALUES ('bookmark', 't1', ?, '2026-07-18T00:00:00Z')",
        (bm_only,),
    )
    bm_done = add_item(conn, "https://b/done", title="ブクマ深掘り済み")
    conn.execute(
        "INSERT INTO x_signals (kind, tweet_id, item_id, observed_at) "
        "VALUES ('bookmark', 't2', ?, '2026-07-18T00:00:00Z')",
        (bm_done,),
    )
    conn.execute(
        "INSERT INTO signals (kind, item_id, value, signal_date) "
        "VALUES ('followup_dig', ?, 'yes', ?)",
        (bm_done, older),
    )
    both = add_item(conn, "https://b/both", title="ブクマと🔖両方")
    conn.execute(
        "INSERT INTO x_signals (kind, tweet_id, item_id, observed_at) "
        "VALUES ('bookmark', 't3', ?, '2026-07-01T00:00:00Z')",
        (both,),
    )
    add_dig_reaction(conn, both, older)

    api = FakeApi()
    run_due_timers(conn, make_config(tmp_path), api, logger, jst_now("21:30"))

    backlog = next(t for _, t in api.sent if "未消化の深掘り候補" in t)
    assert "(2件)" in backlog  # dedupe 済み(bm_only + both)
    assert "ブクマのみ" in backlog
    assert "ブクマと🔖両方" in backlog
    assert "ブクマ深掘り済み" not in backlog  # followup_dig=yes で消える


# --- 未消化一覧の深掘り指定動線(GH#104) ---


def test_dig_backlog_uses_item_ids_and_reply_hint(conn, tmp_path) -> None:
    # 番号は表示位置の連番でなく item id(リプライ指定の安定性。設計 06 §2)
    now = jst_now("21:30")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    silence_watchdog(conn)
    item = add_item(conn, "https://a/fresh", title="候補記事")
    add_dig_reaction(conn, item, yesterday)

    api = FakeApi()
    run_due_timers(conn, make_config(tmp_path), api, logger, now)

    backlog = next(t for _, t in api.sent if "未消化の深掘り候補" in t)
    assert f"{item}. " in backlog  # item id が行頭の番号
    assert f"「{item} を◯◯の観点で」" in backlog  # 実在 id でリプライ例を示す
    assert f"「{item} はパス」" in backlog


def test_dig_backlog_message_id_recorded_in_ledger(conn, tmp_path) -> None:
    # リプライ先の認識用に message_id を台帳へ(GH#104)
    now = jst_now("21:30")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    silence_watchdog(conn)
    item = add_item(conn, "https://a/fresh")
    add_dig_reaction(conn, item, yesterday)

    api = FakeApi()
    run_due_timers(conn, make_config(tmp_path), api, logger, now)

    texts = [t for _, t in api.sent]
    backlog_position = next(i for i, t in enumerate(texts) if "未消化の深掘り候補" in t)
    rows = conn.execute("SELECT * FROM dig_backlog_messages").fetchall()
    assert len(rows) == 1
    # FakeApi の message_id は 900 + 送信順(1始まり)
    assert rows[0]["message_id"] == 900 + backlog_position + 1
    assert rows[0]["sent_date"] == now.strftime("%Y-%m-%d")
