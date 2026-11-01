import json
import logging
import re
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import Config, DigConfig, TelegramConfig, XConfig
from app.db import apply_migrations, connect
from app.delivery import notify_run_failure, poll_once, run_due_timers, run_poller
from app.delivery.poller import TIMER_RETRY_S
from app.delivery.state import get_state, set_state
from app.delivery.surveys import (
    STATE_RUN_FAILURE_NOTIFIED,
    STATE_SOURCE_REPORT_SENT,
    STATE_SURVEY_SENT,
    STATE_WATCHDOG_NOTIFIED,
    pending_dig_items,
)
from app.delivery.telegram import DeliveryError
from app.jst import JST, today_jst
from tests.test_delivery import add_digest, add_item
from tests.test_poller import (
    CHAT_ID,
    FakeApi,
    FakeWorker,
    make_callback_update,
    make_config,
    make_reply_update,
)

logger = logging.getLogger("test-surveys")


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


def jst_now(hhmm: str = "21:30") -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    return datetime.now(JST).replace(hour=hour, minute=minute, second=0, microsecond=0)


def add_dig_reaction(
    conn, item_id: int, signal_date: str, *, pressed_at: str | None = None
) -> None:
    """🔖 の reaction。signal_date は配信日。押した時刻 pressed_at(created_at の形)の既定は
    配信日の 12:00 JST(追い掛けの対象は押した日で選ぶ。GH#226)。"""
    conn.execute(
        "INSERT INTO signals (kind, item_id, value, signal_date, created_at) "
        "VALUES ('reaction', ?, 'dig', ?, ?)",
        (item_id, signal_date, pressed_at or jst_to_created_at(signal_date, "12:00:00")),
    )


def jst_to_created_at(day: str, hms: str) -> str:
    """JST の日時を signals.created_at の形(UTC の ISO)に直す。"""
    moment = datetime.strptime(f"{day} {hms}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def test_followup_targets_chosen_by_jst_date_dig_was_pressed(conn, tmp_path) -> None:
    # GH#226: 追い掛けの対象は 🔖 を押した日(created_at を JST に直した日付)で選ぶ。
    # 配信日(signal_date)では選ばない。回答の帰属は配信日のまま(KPI の意味は変えない)
    now = jst_now("21:30")
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    two_days_ago = (now - timedelta(days=2)).strftime("%Y-%m-%d")
    older = (now - timedelta(days=5)).strftime("%Y-%m-%d")
    silence_watchdog(conn)
    # 過去の digest に昨日の 0:00 JST に付けた 🔖(UTC では前々日)→ 対象
    late = add_item(conn, "https://a/late", title="過去の配信に昨日🔖")
    add_dig_reaction(conn, late, older, pressed_at=jst_to_created_at(yesterday, "00:00:00"))
    # 昨日の digest に昨日の 23:59:59 JST に付けた 🔖 → 対象
    last = add_item(conn, "https://a/last", title="昨日の終わりに🔖")
    add_dig_reaction(conn, last, yesterday, pressed_at=jst_to_created_at(yesterday, "23:59:59"))
    # 昨日の digest に今日の 0:00 JST に付けた 🔖(UTC では昨日)→ 明日の夜の対象
    today_mark = add_item(conn, "https://a/today", title="今日🔖")
    add_dig_reaction(conn, today_mark, yesterday, pressed_at=jst_to_created_at(today, "00:00:00"))
    # 前々日に付けた 🔖 → 昨日の夜の対象で、今夜は送らない
    earlier = add_item(conn, "https://a/earlier", title="前々日🔖")
    add_dig_reaction(conn, earlier, older, pressed_at=jst_to_created_at(two_days_ago, "23:59:59"))

    api = FakeApi()
    run_due_timers(conn, make_config(tmp_path), api, logger, now)

    followups = [t for _, t in api.sent if "深掘りした?" in t]
    assert len(followups) == 2
    assert "過去の配信に昨日🔖" in followups[0]
    assert "昨日の終わりに🔖" in followups[1]

    answer = FakeApi([[make_callback_update(0, data=f"f:{late}:y")]])
    poll_once(conn, make_config(tmp_path), answer, logger)
    row = conn.execute("SELECT * FROM signals WHERE kind = 'followup_dig'").fetchone()
    assert (row["item_id"], row["value"], row["signal_date"]) == (late, "yes", older)


def test_followup_judges_latest_reaction_per_item(conn, tmp_path) -> None:
    # GH#226: 最新 reaction は配信日ごとでなく item ごとに決める(回答の記帳と同じ判定)。
    # 2 つの配信日に 🔖 がある item も 1 通だけ送り、最新が 🔖 でない item は送らない
    now = jst_now("21:30")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    older = (now - timedelta(days=4)).strftime("%Y-%m-%d")
    silence_watchdog(conn)
    twice = add_item(conn, "https://a/twice", title="2つの配信で🔖")
    add_dig_reaction(conn, twice, older, pressed_at=jst_to_created_at(yesterday, "09:00:00"))
    add_dig_reaction(conn, twice, yesterday, pressed_at=jst_to_created_at(yesterday, "10:00:00"))
    undone = add_item(conn, "https://a/undone", title="後の配信で👎")
    add_dig_reaction(conn, undone, older, pressed_at=jst_to_created_at(yesterday, "09:00:00"))
    conn.execute(
        "INSERT INTO signals (kind, item_id, value, signal_date, created_at) "
        "VALUES ('reaction', ?, 'noise', ?, ?)",
        (undone, yesterday, jst_to_created_at(yesterday, "10:00:00")),
    )

    api = FakeApi()
    run_due_timers(conn, make_config(tmp_path), api, logger, now)

    followups = [t for _, t in api.sent if "深掘りした?" in t]
    assert len(followups) == 1
    assert "2つの配信で🔖" in followups[0]


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


def test_dig_backlog_follows_latest_followup_answer(conn, tmp_path) -> None:
    # GH#195: 一覧から外すのは最新の followup_dig が yes のときだけ。追い掛けのボタンで
    # 「はい」→「いいえ」と訂正すると一覧に戻り、「いいえ」→「はい」で消える
    now = jst_now("21:30")
    older = (now - timedelta(days=3)).strftime("%Y-%m-%d")
    silence_watchdog(conn)
    back = add_item(conn, "https://a/back", title="はいからいいえへ")
    add_dig_reaction(conn, back, older)
    gone = add_item(conn, "https://a/gone", title="いいえからはいへ")
    add_dig_reaction(conn, gone, older)
    api = FakeApi([[
        make_callback_update(0, update_id=1, data=f"f:{back}:y"),
        make_callback_update(0, update_id=2, data=f"f:{back}:n"),
        make_callback_update(0, update_id=3, data=f"f:{gone}:n"),
        make_callback_update(0, update_id=4, data=f"f:{gone}:y"),
    ]])
    poll_once(conn, make_config(tmp_path), api, logger)
    assert [a for a in api.answered if a[1] != "🔖 記録しました"] == []

    api = FakeApi()
    run_due_timers(conn, make_config(tmp_path), api, logger, now)

    backlog = next(t for _, t in api.sent if "未消化の深掘り候補" in t)
    assert "(1件)" in backlog
    assert "はいからいいえへ" in backlog
    assert "いいえからはいへ" not in backlog


def test_dig_backlog_latest_followup_by_created_at_then_id(conn) -> None:
    # GH#195: 最新の followup_dig は created_at の新しい行。同じ秒なら id の大きい行
    # (id の順だけでも、created_at の順だけでも決めない)
    def add_followups(item_id: int, rows: list[tuple[str, str]]) -> None:
        for value, created_at in rows:  # この順に INSERT する(後ほど id が大きい)
            conn.execute(
                "INSERT INTO signals (kind, item_id, value, signal_date, created_at) "
                "VALUES ('followup_dig', ?, ?, '2026-10-15', ?)",
                (item_id, value, created_at),
            )

    items = {}
    for name, rows in {
        "later_no": [("no", "2026-10-16T12:00:00Z"), ("yes", "2026-10-16T11:00:00Z")],
        "later_yes": [("yes", "2026-10-16T12:00:00Z"), ("no", "2026-10-16T11:00:00Z")],
        "same_sec_no": [("yes", "2026-10-16T12:00:00Z"), ("no", "2026-10-16T12:00:00Z")],
        "same_sec_yes": [("no", "2026-10-16T12:00:00Z"), ("yes", "2026-10-16T12:00:00Z")],
    }.items():
        items[name] = add_item(conn, f"https://a/{name}")
        add_dig_reaction(conn, items[name], "2026-10-15")
        add_followups(items[name], rows)

    pending = {row["item_id"] for row in pending_dig_items(conn)}

    assert pending == {items["later_no"], items["same_sec_no"]}


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


def test_watchdog_unsent_tells_resend_by_date(conn, tmp_path) -> None:
    # 翌日の配信は当日分しか送らないので、日をまたいだときの送り直し方をその日の日付で添える
    # (GH#247)
    add_undelivered_digest(conn, [], WATCHDOG_DUE)
    api = FakeApi()
    run_due_timers(conn, make_config(tmp_path), api, logger, WATCHDOG_DUE)
    ((_, text),) = api.sent
    assert "未配信" in text
    assert "`intel deliver --date 2026-10-20`" in text


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
    # 送れたかを返す(材料不足のリプライ指定は、送れたときだけ外す。GH#337)
    assert notify_run_failure(conn, config, api, ["collect", "digest"], "01RUN", logger)
    assert not notify_run_failure(conn, config, api, ["export"], "01RUN2", logger)

    assert len(api.sent) == 1
    assert "collect, digest" in api.sent[0][1]
    assert "intel run" in api.sent[0][1]
    assert get_state(conn, STATE_RUN_FAILURE_NOTIFIED) == today_jst()
    assert "ダイジェストを作れませんでした" not in api.sent[0][1]


def test_notify_run_failure_tells_resend_by_date_of_failed_delivery(
    conn, tmp_path, monkeypatch
) -> None:
    # 配信の段が失敗した日は、日をまたいだら日付を指定して送り直せることを添える(GH#247)。
    # 日付は送ろうとした digest のもの。通知までに 0 時を越えても、通知の時点の日付にしない
    monkeypatch.setattr("app.delivery.surveys.today_jst", lambda: "2026-10-24")
    api = FakeApi()
    notify_run_failure(
        conn, make_config(tmp_path), api, ["deliver"], "01RUN", logger,
        deliver_date="2026-10-23",
    )

    ((_, text),) = api.sent
    assert "`intel run` で再走してください" in text
    assert "`intel deliver --date 2026-10-23`" in text


@pytest.mark.parametrize(
    ("failures", "deliver_date"),
    [
        (["digest", "export", "deliver"], None),  # 送る digest を引けなかった
        (["dig"], "2026-10-23"),  # 配信は届いた
    ],
)
def test_notify_run_failure_omits_resend_by_date_unless_delivery_failed(
    conn, tmp_path, failures, deliver_date
) -> None:
    api = FakeApi()
    notify_run_failure(
        conn, make_config(tmp_path), api, failures, "01RUN", logger, deliver_date=deliver_date
    )

    ((_, text),) = api.sent
    assert "`intel run` で再走してください" in text
    assert "intel deliver" not in text


def test_notify_run_failure_tells_same_day_rerun_when_digest_held(conn, tmp_path) -> None:
    # 分析の障害で当日の digest を確定しなかった日は、同じ 1 通で同じ日の再走で届くことを知らせる
    # (GH#191)。別の通知は足さず、同一種別 1 日 1 通の枠も同じ
    api = FakeApi()
    config = make_config(tmp_path)
    notify_run_failure(conn, config, api, ["analyze"], "01RUN", logger, digest_held=True)
    notify_run_failure(conn, config, api, ["analyze"], "01RUN2", logger, digest_held=True)

    ((_, text),) = api.sent
    assert "段: analyze / run_id=01RUN" in text
    assert "分析の障害で今日のダイジェストを作れませんでした" in text
    assert "今日のうちに `intel run` で再走すれば届きます" in text


def test_notify_run_failure_swallows_send_error(conn, tmp_path) -> None:
    class FailingApi(FakeApi):
        def send_message(self, chat_id, text, *, reply_markup=None):
            raise DeliveryError("送信失敗")

    assert not notify_run_failure(
        conn, make_config(tmp_path), FailingApi(), ["deliver"], "01RUN", logger
    )  # 例外が漏れず、送れなかったと返す(GH#337)
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
    # 4096 字を超えると試し直すたびに送信が失敗し、watchdog が届かない
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


# --- 週 1 回のソースの健康診断(GH#150) ---

SUNDAY = datetime(2026, 10, 25, 21, 30, tzinfo=JST)


def only_source_report_due(conn, monkeypatch, now: datetime) -> None:
    """曜日を日曜に戻し、同じ時刻に動く夜アンケート・watchdog は送信済みにする。"""
    from app import source_health

    monkeypatch.setattr(source_health, "REPORT_WEEKDAY", 6)
    day = now.strftime("%Y-%m-%d")
    set_state(conn, STATE_SURVEY_SENT, day)
    set_state(conn, STATE_WATCHDOG_NOTIFIED, day)


def test_source_report_sent_once_on_sunday_evening(conn, tmp_path, monkeypatch) -> None:
    only_source_report_due(conn, monkeypatch, SUNDAY)
    api = FakeApi()
    config = make_config(tmp_path)

    run_due_timers(conn, config, api, logger, SUNDAY)
    run_due_timers(conn, config, api, logger, SUNDAY + timedelta(minutes=1))  # 同じ日は 1 通

    assert len(api.sent) == 1
    assert "🩺 ソースの健康診断" in api.sent[0][1]
    assert get_state(conn, STATE_SOURCE_REPORT_SENT) == "2026-10-25"


@pytest.mark.parametrize(
    "now",
    [SUNDAY - timedelta(minutes=1), SUNDAY - timedelta(days=1), SUNDAY + timedelta(days=1)],
    ids=["before-time", "saturday", "monday"],
)
def test_source_report_not_sent_other_times(conn, tmp_path, monkeypatch, now) -> None:
    only_source_report_due(conn, monkeypatch, now)
    api = FakeApi()
    run_due_timers(conn, make_config(tmp_path), api, logger, now)
    assert api.sent == []
    assert get_state(conn, STATE_SOURCE_REPORT_SENT) is None


def test_source_report_retried_after_send_failure(conn, tmp_path, monkeypatch) -> None:
    only_source_report_due(conn, monkeypatch, SUNDAY)

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
        run_due_timers(conn, config, api, logger, SUNDAY)
    assert get_state(conn, STATE_SOURCE_REPORT_SENT) is None  # 届くまで再試行する

    run_due_timers(conn, config, api, logger, SUNDAY + timedelta(minutes=1))
    assert len(api.sent) == 1
    assert get_state(conn, STATE_SOURCE_REPORT_SENT) == "2026-10-25"


def test_source_report_escapes_external_strings(conn, tmp_path, monkeypatch) -> None:
    # 外部由来の文字列(ソース名・エラー・移転先)で parse_mode=HTML の送信を壊さない
    only_source_report_due(conn, monkeypatch, SUNDAY)
    conn.execute(
        "INSERT INTO sources (type, name, moved_url) VALUES ('rss', 'feed<&>', ?)",
        ("https://n.example/feed?a=1&b=<2>",),
    )
    add_source(conn, "rss", "r<x>", 1, error="E: <b>&")
    api = FakeApi()

    run_due_timers(conn, make_config(tmp_path), api, logger, SUNDAY)

    text = api.sent[0][1]
    assert "<b>" not in text and "&lt;b&gt;&amp;" in text
    assert "「r&lt;x&gt;」" in text
    assert "「feed&lt;&amp;&gt;」" in text and "a=1&amp;b=&lt;2&gt;" in text


def test_source_report_stays_within_telegram_limit(conn, tmp_path, monkeypatch) -> None:
    # 4096 字を超えると送信が毎周期失敗するので 1 通に切り詰める(entity は分断しない)
    only_source_report_due(conn, monkeypatch, SUNDAY)
    for i in range(40):
        add_source(conn, "rss", f"r{i}<" + "n" * 100, 1, error="E: <b>&" + "e" * 300)
    api = FakeApi()

    run_due_timers(conn, make_config(tmp_path), api, logger, SUNDAY)

    assert len(api.sent) == 1
    text = api.sent[0][1]
    assert len(text) <= 4000 and text.endswith("…")
    assert not re.search(r"&[#a-z0-9]*…$", text)


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
    assert row["signal_date"] == dig_date  # 🔖 の reaction と同じ配信日に帰属(設計 01 §7)
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
    # 記帳しない(送信側と同じ「item の最新 reaction が dig」判定。followup_dig 率の汚染防止)
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


class SendFailingApi(FakeApi):
    """送信だけが失敗する FakeApi(通知先 chat の誤りなど。GH#224)。fail_first 回まで失敗し、
    以降は送れる(None なら失敗し続ける)。送信を試みた周期を cycle() で記録する。"""

    def __init__(self, batches=None, *, cycle, fail_first: int | None = None):
        super().__init__(batches)
        self._cycle = cycle
        self._fail_first = fail_first
        self.attempt_cycles: list[int] = []

    def send_message(self, chat_id, text, *, reply_markup=None):
        self.attempt_cycles.append(self._cycle())
        if self._fail_first is None or len(self.attempt_cycles) <= self._fail_first:
            raise DeliveryError("HTTP 400: chat not found")
        return super().send_message(chat_id, text, reply_markup=reply_markup)


# 日付を固定する(実際の日付を取り直すと、日付の変わり目で翌日分の watchdog が動いて揺れる)
WATCHDOG_DUE = datetime(2026, 10, 20, 7, 15, tzinfo=JST)


def add_undelivered_digest(conn, item_ids: list[int], day: datetime) -> None:
    """day の未配信の digest(その日の watchdog が通知を試みる)。"""
    digest_id = add_digest(conn, item_ids)
    conn.execute(
        "UPDATE digests SET digest_date = ? WHERE id = ?", (day.strftime("%Y-%m-%d"), digest_id)
    )


def test_run_poller_keeps_receiving_while_notification_keeps_failing(conn, tmp_path) -> None:
    # GH#224: 内蔵タイマーの送信が失敗し続けても、同じ周期の getUpdates を飛ばさない
    # (以前は同じ try の中で受信まで飛ばし、30s 待ってまた送信からやり直していた)
    item_id = add_item(conn, "https://a/1")
    add_undelivered_digest(conn, [item_id], WATCHDOG_DUE)
    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 3

    api = SendFailingApi(
        [[make_callback_update(item_id, "up")], []], cycle=lambda: calls["n"]
    )
    sleeps: list[float] = []
    run_poller(
        conn, make_config(tmp_path), api, logger,
        sleep=sleeps.append, should_stop=should_stop, answer_worker=FakeWorker(),
        now_fn=lambda: WATCHDOG_DUE,
        clock=lambda: calls["n"] * TIMER_RETRY_S,  # 毎周期、再試行の間隔が過ぎている
    )

    assert api.attempt_cycles == [1, 2, 3]  # 毎周期の通知は失敗した
    assert len(api.offsets) == 3  # それでも毎周期 getUpdates を呼んだ
    assert api.answered == [("cb1", "👍 記録しました")]  # ボタン操作を受け取って応答した
    assert conn.execute("SELECT count(*) FROM signals").fetchone()[0] == 1
    assert sleeps == []  # 受信は失敗していないので、再接続の待ちも無い
    assert get_state(conn, STATE_WATCHDOG_NOTIFIED) is None  # 届くまで枠を消費しない


def test_run_poller_retries_failed_notification_at_own_interval(conn, tmp_path) -> None:
    # GH#224: 失敗した通知は受信の周期ごとではなく、失敗から TIMER_RETRY_S 経ってから試し直す
    # (受信が続く間に、送れない通知で Telegram を毎周期叩かない)。届いたら枠を消費する
    add_undelivered_digest(conn, [], WATCHDOG_DUE)
    elapsed = [0, 60, TIMER_RETRY_S - 1, TIMER_RETRY_S, TIMER_RETRY_S + 60]  # 周期ごとの clock
    cycle = {"i": -1}

    def should_stop() -> bool:
        cycle["i"] += 1
        return cycle["i"] >= len(elapsed)

    api = SendFailingApi(cycle=lambda: cycle["i"], fail_first=1)
    sleeps: list[float] = []
    run_poller(
        conn, make_config(tmp_path), api, logger,
        sleep=sleeps.append, should_stop=should_stop, answer_worker=FakeWorker(),
        now_fn=lambda: WATCHDOG_DUE, clock=lambda: elapsed[cycle["i"]],
    )

    assert api.attempt_cycles == [0, 3]  # 失敗した周期と、間隔が過ぎた周期だけ試した
    assert len(api.offsets) == len(elapsed)  # 受信は毎周期
    assert sleeps == []
    assert len(api.sent) == 1 and "⚠️ watchdog" in api.sent[0][1]
    assert get_state(conn, STATE_WATCHDOG_NOTIFIED) == "2026-10-20"


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


# --- 未消化一覧の各行の状態と明朝の予定(GH#349。設計 06 §2) ---

BODY = "深掘りの材料になる本文。" * 30  # min_body_chars(既定 200 字)を満たす


def add_body(conn, item_id: int, text: str = BODY) -> None:
    conn.execute(
        "INSERT INTO documents (item_id, extracted_text, language, extraction_status,"
        " expires_at) VALUES (?, ?, 'ja', 'ok', '2099-01-01T00:00:00Z')",
        (item_id, text),
    )


def add_queue(
    conn,
    item_id: int,
    status: str = "queued",
    *,
    requested_at: str | None = None,
    enqueued_at: str = "2026-07-01T00:00:00Z",
) -> None:
    conn.execute(
        "INSERT INTO dig_queue (item_id, status, source, requested_at, enqueued_at) "
        "VALUES (?, ?, 'bookmark', ?, ?)",
        (item_id, status, requested_at, enqueued_at),
    )


def config_with_cap(tmp_path: Path, daily_cap: int) -> Config:
    return replace(make_config(tmp_path), dig=DigConfig(daily_cap=daily_cap))


def send_backlog(conn, config: Config, now: datetime | None = None) -> str | None:
    """夜アンケートを送り、未消化一覧の本文を返す(送らなければ None)。"""
    api = FakeApi()
    run_due_timers(conn, config, api, logger, now or jst_now("21:30"))
    return next((t for _, t in api.sent if "未消化の深掘り候補" in t), None)


def backlog_line(text: str, item_id: int) -> str:
    return next(line for line in text.splitlines() if line.startswith(f"{item_id}. "))


def test_dig_backlog_marks_each_state(conn, tmp_path) -> None:
    # 各行の番号の後に深掘りの状態の印を添える。上限 1 件なので 2 件目の queued は ⏳ 待ち
    now = jst_now("21:30")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    silence_watchdog(conn)
    items = {}
    for name in ("planned", "waiting", "done", "passed", "queued_no_body", "gone", "plain"):
        items[name] = add_item(conn, f"https://a/{name}", title=f"記事-{name}")
        add_dig_reaction(conn, items[name], yesterday)
    for name in ("planned", "waiting", "done", "passed", "plain"):
        add_body(conn, items[name])
    add_queue(conn, items["planned"], enqueued_at="2026-07-03T00:00:00Z")  # ② の新しい順で先
    add_queue(conn, items["waiting"], enqueued_at="2026-07-02T00:00:00Z")
    add_queue(conn, items["done"], "done")
    add_queue(conn, items["passed"], "passed")
    add_queue(conn, items["queued_no_body"])  # キューにあるが本文が消えた
    # gone = キューに無く本文も無い / plain = キューに無く本文あり(印なし)

    backlog = send_backlog(conn, config_with_cap(tmp_path, 1), now)

    assert backlog is not None
    expected = {
        "planned": "🗓 明朝 ",
        "waiting": "⏳ 待ち ",
        "done": "✅ 掘った ",
        "passed": "⏭️ パス ",
        "queued_no_body": "⚠️ 本文なし ",
        "gone": "⚠️ 本文なし ",
        "plain": "",
    }
    for name, mark in expected.items():
        assert backlog_line(backlog, items[name]).startswith(f"{items[name]}. {mark}<a "), name
    assert "(7件)" in backlog


def test_dig_backlog_done_and_passed_marks_ignore_missing_body(conn, tmp_path) -> None:
    # done・passed はキューの状態のまま示す(本文が消えていても ⚠️ にしない)
    now = jst_now("21:30")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    silence_watchdog(conn)
    done = add_item(conn, "https://a/done", title="掘った")
    passed = add_item(conn, "https://a/passed", title="パスした")
    for item, status in ((done, "done"), (passed, "passed")):
        add_dig_reaction(conn, item, yesterday)
        add_queue(conn, item, status)

    backlog = send_backlog(conn, config_with_cap(tmp_path, 2), now)

    assert "✅ 掘った <a " in backlog_line(backlog, done)
    assert "⏭️ パス <a " in backlog_line(backlog, passed)


def test_dig_backlog_plan_rows_on_top_and_not_counted_in_limit(conn, tmp_path) -> None:
    # 予定の行は先頭に置き、上限 15 件に数えずに必ず載せる。🔖 が一番古い行でも先頭へ
    now = jst_now("21:30")
    silence_watchdog(conn)
    planned = add_item(conn, "https://a/planned", title="予定の記事")
    add_dig_reaction(conn, planned, (now - timedelta(days=30)).strftime("%Y-%m-%d"))
    add_body(conn, planned)
    add_queue(conn, planned)
    for i in range(16):
        item = add_item(conn, f"https://a/many-{i}", title=f"記事{i}")
        add_dig_reaction(conn, item, (now - timedelta(days=1)).strftime("%Y-%m-%d"))

    backlog = send_backlog(conn, config_with_cap(tmp_path, 1), now)

    lines = backlog.splitlines()
    assert lines[0] == "🔖 未消化の深掘り候補(17件)"
    assert lines[1].startswith(f"{planned}. 🗓 明朝 ")
    assert backlog.count("<a href=") == 16  # 予定 1 + 残りの上限 15
    assert sum(line.startswith(f"{planned}. ") for line in lines) == 1  # 残りの行に重ねない
    assert "(他 1 件)" in backlog


def test_dig_backlog_adds_plan_rows_outside_bookmarks(conn, tmp_path) -> None:
    # 🔖 の一覧に無い予定(カードからの調べ直しの依頼など)も行として足す。① が ② より先
    now = jst_now("21:30")
    silence_watchdog(conn)
    bookmarked = add_item(conn, "https://a/bookmarked", title="🔖の記事")
    add_dig_reaction(conn, bookmarked, (now - timedelta(days=1)).strftime("%Y-%m-%d"))
    add_body(conn, bookmarked)
    add_queue(conn, bookmarked, enqueued_at="2026-07-05T00:00:00Z")
    redig = add_item(conn, "https://a/redig", title="調べ直しの記事", title_ja="調べ直し(和題)")
    add_body(conn, redig)
    add_queue(conn, redig, requested_at="2026-07-04T00:00:00Z")

    backlog = send_backlog(conn, config_with_cap(tmp_path, 2), now)

    lines = backlog.splitlines()
    assert lines[0] == "🔖 未消化の深掘り候補(2件)"
    assert lines[1] == f'{redig}. 🗓 明朝 <a href="https://a/redig">調べ直し(和題)</a>'
    assert lines[2].startswith(f"{bookmarked}. 🗓 明朝 ")
    assert "ダイジェストの上位" not in backlog  # 予定が上限に届いている


def test_dig_backlog_sent_with_plan_only_and_reply_accepts_plan_id(conn, tmp_path) -> None:
    # 🔖 の一覧が空でも予定があれば送り、予定の行の番号はリプライで指定できる
    now = jst_now("21:30")
    silence_watchdog(conn)
    redig = add_item(conn, "https://a/redig", title="調べ直しの記事")
    add_body(conn, redig)
    add_queue(conn, redig, requested_at="2026-07-04T00:00:00Z")
    config = config_with_cap(tmp_path, 2)
    assert pending_dig_items(conn) == []

    api = FakeApi()
    run_due_timers(conn, config, api, logger, now)

    texts = [t for _, t in api.sent]
    position = next(i for i, t in enumerate(texts) if "未消化の深掘り候補" in t)
    backlog = texts[position]
    assert "(1件)" in backlog
    assert backlog_line(backlog, redig).startswith(f"{redig}. 🗓 明朝 ")
    assert f"「{redig} を◯◯の観点で」" in backlog  # リプライ例は予定の行の id
    message_id = 900 + position + 1
    assert conn.execute(
        "SELECT 1 FROM dig_backlog_messages WHERE message_id = ?", (message_id,)
    ).fetchone()

    poll_once(conn, config, FakeApi([[make_reply_update(f"{redig} はパス", message_id)]]), logger)

    status = conn.execute(
        "SELECT status FROM dig_queue WHERE item_id = ?", (redig,)
    ).fetchone()["status"]
    assert status == "passed"


def test_dig_backlog_not_sent_without_bookmarks_or_plan(conn, tmp_path) -> None:
    # 🔖 の一覧が空で予定も無い日は今までどおり送らない(本文の無い queued は予定にならない)
    silence_watchdog(conn)
    gone = add_item(conn, "https://a/gone")
    add_queue(conn, gone, requested_at="2026-07-04T00:00:00Z")

    assert send_backlog(conn, config_with_cap(tmp_path, 2)) is None


def test_dig_backlog_caveat_lines(conn, tmp_path) -> None:
    # 予定があれば入れ替わりの 1 行、予定が翌日の上限に足りなければ digest で埋める 1 行
    now = jst_now("21:30")
    silence_watchdog(conn)
    planned = add_item(conn, "https://a/planned")
    add_dig_reaction(conn, planned, (now - timedelta(days=1)).strftime("%Y-%m-%d"))
    add_body(conn, planned)
    add_queue(conn, planned)
    swap_note = "🗓 明朝 = キューからの予定。朝に新しい 🔖 が入ると入れ替わることがあります"

    short = send_backlog(conn, config_with_cap(tmp_path, 3), now)
    assert swap_note in short.splitlines()
    assert "残り 2 件は明朝のダイジェストの上位から選びます" in short.splitlines()

    set_state(conn, STATE_SURVEY_SENT, "")  # 同じ日にもう一度送る
    full = send_backlog(conn, config_with_cap(tmp_path, 1), now)
    assert swap_note in full.splitlines()
    assert "ダイジェストの上位" not in full

    conn.execute("UPDATE dig_queue SET status = 'passed'")
    set_state(conn, STATE_SURVEY_SENT, "")
    none_planned = send_backlog(conn, config_with_cap(tmp_path, 2), now)
    assert swap_note not in none_planned
    lines = none_planned.splitlines()
    # 注記は行の後に空行で区切って置き、リプライの案内は最後
    assert lines[-4:-2] == ["", "残り 2 件は明朝のダイジェストの上位から選びます"]
    assert lines[-2] == ""
    assert lines[-1].startswith("↩️ ")


def test_dig_backlog_unchanged_when_daily_cap_zero(conn, tmp_path) -> None:
    # [dig].daily_cap = 0 なら印も予定も出さず、今の一覧のまま。🔖 に無い予定も足さない
    now = jst_now("21:30")
    silence_watchdog(conn)
    item = add_item(conn, "https://a/fresh", title="候補記事")
    add_dig_reaction(conn, item, (now - timedelta(days=1)).strftime("%Y-%m-%d"))
    add_body(conn, item)
    add_queue(conn, item)
    redig = add_item(conn, "https://a/redig", title="調べ直しの記事")
    add_body(conn, redig)
    add_queue(conn, redig, requested_at="2026-07-04T00:00:00Z")

    backlog = send_backlog(conn, config_with_cap(tmp_path, 0), now)

    assert backlog == (
        "🔖 未消化の深掘り候補(1件)\n"
        f'{item}. <a href="https://a/fresh">候補記事</a>\n'
        "\n"
        f"↩️ このメッセージへのリプライで指定: 「{item} を◯◯の観点で」= "
        f"朝の深掘りに登録 / 「{item} はパス」= 深掘り対象から外す"
    )


def test_dig_backlog_plan_only_not_sent_when_daily_cap_zero(conn, tmp_path) -> None:
    silence_watchdog(conn)
    redig = add_item(conn, "https://a/redig")
    add_body(conn, redig)
    add_queue(conn, redig, requested_at="2026-07-04T00:00:00Z")

    assert send_backlog(conn, config_with_cap(tmp_path, 0)) is None


def test_dig_backlog_plan_row_outside_bookmarks_is_escaped(conn, tmp_path) -> None:
    # 🔖 に無い予定の行も、タイトルは escape・URL は属性の escape(設計 04 §1)
    silence_watchdog(conn)
    url = 'https://a/x?q="1"&r=<2>'
    redig = add_item(conn, url, title="<b>危ない</b> & タイトル")
    add_body(conn, redig)
    add_queue(conn, redig, requested_at="2026-07-04T00:00:00Z")

    backlog = send_backlog(conn, config_with_cap(tmp_path, 1))

    line = backlog_line(backlog, redig)
    assert "&lt;b&gt;危ない&lt;/b&gt; &amp; タイトル" in line
    assert "<b>" not in line
    assert 'href="https://a/x?q=&quot;1&quot;&amp;r=&lt;2&gt;"' in line


def test_dig_backlog_does_not_write_queue(conn, tmp_path) -> None:
    # 夜の一覧は読むだけ(キューは変えない。設計 06 §7 の 3)
    now = jst_now("21:30")
    silence_watchdog(conn)
    for i, status in enumerate(("queued", "queued", "done", "passed")):
        item = add_item(conn, f"https://a/{i}")
        add_dig_reaction(conn, item, (now - timedelta(days=1)).strftime("%Y-%m-%d"))
        add_body(conn, item)
        add_queue(conn, item, status)
    conn.commit()
    before = [tuple(row) for row in conn.execute("SELECT * FROM dig_queue ORDER BY item_id")]

    assert send_backlog(conn, config_with_cap(tmp_path, 1), now) is not None

    after = [tuple(row) for row in conn.execute("SELECT * FROM dig_queue ORDER BY item_id")]
    assert after == before


def test_next_morning_plan_matches_morning_queue_selection(conn, tmp_path) -> None:
    # 夜の予定と朝の選定の ①② は同じ関数(GH#349)。③ の digest の上位は夜には含めない
    from app.deepdive import _select_candidates
    from app.dig import next_morning_plan

    config = config_with_cap(tmp_path, 3)
    requested = add_item(conn, "https://a/requested")
    add_body(conn, requested)
    add_queue(conn, requested, requested_at="2026-07-04T00:00:00Z")
    no_body = add_item(conn, "https://a/no-body")
    add_queue(conn, no_body, requested_at="2026-07-05T00:00:00Z")  # 材料不足の指定は予定にしない
    auto = add_item(conn, "https://a/auto")
    add_body(conn, auto)
    add_queue(conn, auto)
    top = add_item(conn, "https://a/digest-top")
    add_body(conn, top)
    day = "2026-07-23"
    digest_id = add_digest(conn, [top])
    conn.execute(
        "UPDATE digests SET digest_date = ?, delivery_status = 'sent' WHERE id = ?",
        (day, digest_id),
    )

    plan = next_morning_plan(conn, config, day)
    morning, skipped = _select_candidates(conn, config, 3, day)

    assert [c.item_id for c in plan] == [requested, auto]
    assert [c.item_id for c in morning] == [requested, auto, top]  # 朝は ③ で埋める
    assert [c.item_id for c in skipped] == [no_body]
    assert next_morning_plan(conn, config_with_cap(tmp_path, 0), day) == []
