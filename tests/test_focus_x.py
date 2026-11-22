"""今の重点で X の検索の枠を 1 本入れ替える(GH#433 の 6 本目): 検索の文・案・承認・収集・戻し。"""

import json
import logging
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import FocusConfig, XConfig, XSearchConfig
from app.db import apply_migrations, connect
from app.delivery import poll_once, run_due_timers
from app.delivery.surveys import focus_x_keyboard
from app.delivery.telegram import DeliveryError
from app.focus_x import (
    build_query,
    effective_slots,
    next_proposal,
    record_sent,
    render_state,
    revert_stale,
)
from app.jst import JST, today_jst
from app.models import FocusEntry
from tests.test_poller import CHAT_ID, FakeApi, make_callback_update, make_config

logger = logging.getLogger("test-focus-x")

SLOTS = ('"claude code" OR "codex cli"', '"mcp server"', '"ai agent" (導入 OR 事例)')
ACTIVATED = "2026-11-21T21:30:00Z"  # JST 11/22 06:30
NOW = datetime(2026, 11, 22, 7, 0, tzinfo=JST)
QUERY = '"外部 sandbox" OR "coding agent"'


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def active_by_status(monkeypatch):
    """今の重点は台帳の status で決める(重みの計算は test_focus が確かめる)。"""
    import app.focus

    def fake(conn, config, *, now=None):
        return [
            FocusEntry(topic_id=r["id"], name=r["name"], subjects=1)
            for r in conn.execute("SELECT id, name FROM focus_topics WHERE status = 'active' "
                                  "ORDER BY id")
        ]

    monkeypatch.setattr(app.focus, "active_focus", fake)


def x_config(tmp_path: Path, *, focus: bool = True, slots: tuple[str, ...] = SLOTS):
    return replace(
        make_config(tmp_path),
        x=XConfig(enabled=True),
        x_search=XSearchConfig(slots=slots),
        focus=FocusConfig(enabled=focus),
    )


def topic(
    conn, name: str = "話題A", terms=("外部 sandbox", "coding agent"), *,
    status: str = "active", activated: str = ACTIVATED,
) -> int:
    topic_id = conn.execute(
        "INSERT INTO focus_topics (name, terms, status, activated_at, dismissed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, json.dumps(list(terms), ensure_ascii=False), status, activated,
         activated if status == "dismissed" else None),
    ).lastrowid
    conn.commit()
    return topic_id


def approved(conn, topic_id: int, *, slot: int = 2, replaced: str = SLOTS[1]) -> int:
    proposal_id = conn.execute(
        "INSERT INTO focus_x_slots (topic_id, activated_at, query, status, slot, replaced_query, "
        "message_id, decided_at) VALUES (?, ?, ?, 'approved', ?, ?, 801, ?)",
        (topic_id, ACTIVATED, QUERY, slot, replaced, ACTIVATED),
    ).lastrowid
    conn.commit()
    return proposal_id


def statuses(conn) -> list[tuple]:
    return [
        tuple(r) for r in conn.execute(
            "SELECT topic_id, status, slot FROM focus_x_slots ORDER BY id"
        )
    ]


# --- 検索の文 ---


def test_build_query_quotes_clean_terms_and_joins_with_or() -> None:
    assert build_query(["外部 sandbox", "coding agent"]) == QUERY
    # 演算子・引用・括弧を外し直す(台帳の値をそのまま外へ出さない)
    assert build_query(['"a" OR b', "(c)", "NOT"]) == '"a b" OR "c"'
    assert build_query([]) is None
    assert build_query(["OR", "()"]) is None


# --- 収集が使う枠 ---


def test_effective_slots_replace_one_slot_only_while_the_topic_is_focus(conn, tmp_path) -> None:
    config = x_config(tmp_path)
    assert effective_slots(conn, config) == SLOTS  # 入れ替えなし

    topic_id = topic(conn)
    approved(conn, topic_id)
    assert effective_slots(conn, config) == (SLOTS[0], QUERY, SLOTS[2])
    assert effective_slots(conn, x_config(tmp_path, focus=False)) == SLOTS  # [focus] が off

    # config の 2 本目の文が承認したときと違う(人が書き換えた)
    changed = x_config(tmp_path, slots=(SLOTS[0], '"mcp" OR "a2a"', SLOTS[2]))
    assert effective_slots(conn, changed) == changed.x_search.slots

    # 重点から外れた・外した・入り直した
    for status, activated in (("candidate", ACTIVATED), ("dismissed", ACTIVATED),
                              ("active", "2026-11-25T21:30:00Z")):
        conn.execute("UPDATE focus_topics SET status = ?, activated_at = ?, dismissed_at = ? "
                     "WHERE id = ?",
                     (status, activated, activated if status == "dismissed" else None, topic_id))
        assert effective_slots(conn, config) == SLOTS


def test_revert_stale_closes_rows_of_topics_out_of_focus(conn) -> None:
    left = topic(conn, "外れた話題")
    approved(conn, left)
    reentered = topic(conn, "入り直した話題", ("x",))
    conn.execute(
        "INSERT INTO focus_x_slots (topic_id, activated_at, query) VALUES (?, ?, '\"x\"')",
        (reentered, ACTIVATED),
    )
    conn.execute("UPDATE focus_topics SET status = 'candidate' WHERE id = ?", (left,))
    conn.execute("UPDATE focus_topics SET activated_at = '2026-11-25T21:30:00Z' WHERE id = ?",
                 (reentered,))
    conn.commit()

    reverted, expired = revert_stale(conn, datetime(2026, 11, 26, tzinfo=UTC))

    assert (len(reverted), len(expired)) == (1, 1)
    assert statuses(conn) == [(left, "reverted", 2), (reentered, "expired", None)]
    assert revert_stale(conn, datetime(2026, 11, 27, tzinfo=UTC)) == ([], [])


def test_run_focus_reverts_the_slot_when_the_topic_leaves(conn, tmp_path) -> None:
    from app.focus import run_focus

    topic_id = topic(conn)  # 証拠が無いので、重みの更新で重点から外れる
    approved(conn, topic_id)

    result = run_focus(conn, x_config(tmp_path), None, logger, now=datetime(2026, 11, 23,
                                                                             tzinfo=UTC))

    assert result.x_reverted == 1
    assert statuses(conn) == [(topic_id, "reverted", 2)]


# --- 案 ---


def test_next_proposal_asks_once_per_focus_entry_and_one_at_a_time(conn, tmp_path) -> None:
    config = x_config(tmp_path)
    a = topic(conn, "話題A")
    b = topic(conn, "話題B", ("software factory",))

    first = next_proposal(conn, config, NOW)
    assert (first["topic_id"], first["query"], first["message_id"]) == (a, QUERY, None)
    assert next_proposal(conn, config, NOW)["id"] == first["id"]  # 送れていなければ送り直す
    record_sent(conn, first["id"], 901)
    assert next_proposal(conn, config, NOW) is None  # 答えを待つ間は次を出さない

    conn.execute("UPDATE focus_x_slots SET status = 'declined', decided_at = ? WHERE id = ?",
                 (ACTIVATED, first["id"]))
    conn.commit()
    second = next_proposal(conn, config, NOW)
    assert (second["topic_id"], second["query"]) == (b, '"software factory"')  # A は聞き直さない

    conn.execute("UPDATE focus_x_slots SET status = 'approved', slot = 1, replaced_query = ?, "
                 "decided_at = ? WHERE id = ?", (SLOTS[0], ACTIVATED, second["id"]))
    conn.execute("UPDATE focus_topics SET activated_at = '2026-11-25T21:30:00Z' WHERE id = ?",
                 (a,))
    conn.commit()
    assert next_proposal(conn, config, NOW) is None  # 入れ替え中は 1 本だけ


def test_next_proposal_does_not_write_while_a_slot_is_replaced(conn, tmp_path) -> None:
    # 受信デーモンが毎周期呼ぶので、入れ替え中は書き込みの lock を取りに行かない
    approved(conn, topic(conn, "話題A"))
    topic(conn, "話題B", ("software factory",))
    other = connect(tmp_path / "intel.db")
    other.execute("PRAGMA busy_timeout=0")
    conn.execute("PRAGMA busy_timeout=0")
    other.execute("BEGIN IMMEDIATE")
    try:
        assert next_proposal(conn, x_config(tmp_path), NOW) is None
    finally:
        other.execute("ROLLBACK")
        other.close()


def test_next_proposal_skips_topics_without_a_usable_query(conn, tmp_path) -> None:
    topic(conn, "語の無い話題", ())
    topic(conn, "枠と同じ語の話題", ("mcp server",))

    assert next_proposal(conn, x_config(tmp_path), NOW) is None
    assert statuses(conn) == []


def timer_now() -> datetime:
    # ほかのタイマー(watchdog 07:15・夜 21:30)が動かない時刻
    return datetime.now(JST).replace(hour=7, minute=0, second=0, microsecond=0)


def deliver_digest(conn) -> None:
    conn.execute(
        "INSERT INTO digests (digest_date, markdown, item_ids, item_count, status, "
        "delivery_status) VALUES (?, '# md', '[]', 0, 'ok', 'sent')",
        (today_jst(),),
    )
    conn.commit()


def test_proposal_is_sent_once_after_the_morning_digest(conn, tmp_path) -> None:
    config = x_config(tmp_path)
    topic(conn, "話題 <A>")
    api = FakeApi()

    run_due_timers(conn, config, api, logger, timer_now())
    assert api.sent == []  # 朝のダイジェストを送る前は送らない

    deliver_digest(conn)
    run_due_timers(conn, config, api, logger, timer_now())
    run_due_timers(conn, config, api, logger, timer_now())  # 2 回目は送らない

    [(chat_id, text)] = api.sent
    assert chat_id == CHAT_ID
    assert text.splitlines() == [
        "🔍 X の検索の枠を、今の重点で 1 本入れ替えますか?",
        "「話題 &lt;A&gt;」が 11/22 から今の重点です。",
        f"入れる検索: {QUERY}",
        "今の枠:",
        f"1. {SLOTS[0]}",
        f"2. {SLOTS[1]}",
        f"3. {SLOTS[2]}",
        "入れ替える枠の番号を押してください。重点から外れたら、次の朝から元の枠に戻します。"
        "1 日に読む件数は変わりません。",
    ]
    proposal_id = conn.execute("SELECT id FROM focus_x_slots").fetchone()[0]
    assert api.markups == [focus_x_keyboard(proposal_id, 3)]
    assert api.markups[0]["inline_keyboard"] == [
        [{"text": f"{n} と入れ替える", "callback_data": f"x:{proposal_id}:{n}"} for n in (1, 2, 3)],
        [{"text": "入れない", "callback_data": f"x:{proposal_id}:n"}],
    ]
    assert conn.execute("SELECT message_id FROM focus_x_slots").fetchone()[0] == 901


def test_proposal_not_sent_is_sent_on_the_next_cycle(conn, tmp_path) -> None:
    config = x_config(tmp_path)
    topic(conn)
    deliver_digest(conn)

    class Failing(FakeApi):
        def send_message(self, chat_id, text, *, reply_markup=None):
            raise DeliveryError("送れない")

    with pytest.raises(DeliveryError):
        run_due_timers(conn, config, Failing(), logger, timer_now())
    api = FakeApi()
    run_due_timers(conn, config, api, logger, timer_now())

    assert len(api.sent) == 1
    assert [r[1] for r in statuses(conn)] == ["proposed"]


@pytest.mark.parametrize(
    ("focus", "x_enabled", "slots"),
    [(False, True, SLOTS), (True, False, SLOTS), (True, True, ())],
)
def test_no_proposal_when_focus_or_search_is_off(conn, tmp_path, focus, x_enabled, slots) -> None:
    config = replace(x_config(tmp_path, focus=focus, slots=slots), x=XConfig(enabled=x_enabled))
    topic(conn)
    deliver_digest(conn)
    api = FakeApi()

    run_due_timers(conn, config, api, logger, timer_now())

    assert api.sent == [] and statuses(conn) == []


# --- ボタン ---


def press(code: str, proposal_id: int, *, message_id: int = 901, chat_id: int = CHAT_ID,
          update_id: int = 1) -> dict:
    update = make_callback_update(0, data=f"x:{proposal_id}:{code}", update_id=update_id)
    update["callback_query"]["message"] = {"message_id": message_id, "chat": {"id": chat_id}}
    return update


def sent_proposal(conn, config) -> int:
    topic(conn)
    row = next_proposal(conn, config, NOW)
    record_sent(conn, row["id"], 901)
    return row["id"]


def poll(conn, config, *updates) -> FakeApi:
    api = FakeApi([list(updates)])
    poll_once(conn, config, api, logger, now_fn=lambda: NOW)
    return api


def test_choosing_a_slot_replaces_it_and_revert_puts_it_back(conn, tmp_path) -> None:
    config = x_config(tmp_path)
    proposal_id = sent_proposal(conn, config)

    api = poll(conn, config, press("2", proposal_id), press("2", proposal_id, update_id=2))
    assert api.answered == [
        ("cb1", "🔍 2 本目を入れ替えました。次の朝の検索から使います"),
        ("cb2", "🔍 2 本目と入れ替え中です。戻すときは「↩️ 元に戻す」を押してください"),
    ]
    assert api.edited[0] == (CHAT_ID, 901, focus_x_keyboard(proposal_id, 3, status="approved",
                                                            slot=2))
    assert api.edited[0][2]["inline_keyboard"][0][1] == {
        "text": "↩️ 元に戻す", "callback_data": f"x:{proposal_id}:r"
    }
    assert effective_slots(conn, config) == (SLOTS[0], QUERY, SLOTS[2])
    assert "2 本目を " + QUERY + " に入れ替え中" in render_state(conn, config)[0]

    api = poll(conn, config, press("r", proposal_id, update_id=3),
               press("1", proposal_id, update_id=4))
    assert api.answered == [
        ("cb3", "↩️ 元の枠に戻しました。次の朝の検索から元に戻ります"),
        ("cb4", "↩️ 元に戻してあります。この重点の間は入れ替えません"),
    ]
    assert effective_slots(conn, config) == SLOTS
    assert statuses(conn)[0][1] == "reverted"
    assert next_proposal(conn, config, NOW) is None  # 同じ重点の入りには聞き直さない


def test_declining_keeps_the_slots_and_is_not_asked_again(conn, tmp_path) -> None:
    config = x_config(tmp_path)
    proposal_id = sent_proposal(conn, config)

    api = poll(conn, config, press("n", proposal_id), press("n", proposal_id, update_id=2),
               press("3", proposal_id, update_id=3), press("r", proposal_id, update_id=4))

    assert api.answered == [
        ("cb1", "入れないことにしました。この重点の間はもう聞きません"),
        ("cb2", "入れないことにしてあります"),
        ("cb3", "入れないことにしてあります"),
        ("cb4", "↩️ 入れ替えていません"),
    ]
    assert effective_slots(conn, config) == SLOTS
    assert next_proposal(conn, config, NOW) is None


def test_choosing_after_the_topic_left_focus_does_not_replace(conn, tmp_path) -> None:
    config = x_config(tmp_path)
    proposal_id = sent_proposal(conn, config)
    conn.execute("UPDATE focus_topics SET status = 'candidate'")
    conn.commit()

    api = poll(conn, config, press("1", proposal_id))

    assert api.answered == [("cb1", "この話題はもう今の重点から外れています。入れ替えません")]
    assert statuses(conn)[0][1] == "expired"
    assert effective_slots(conn, config) == SLOTS


def test_choosing_a_missing_slot_changes_nothing(conn, tmp_path) -> None:
    config = x_config(tmp_path, slots=SLOTS[:2])
    proposal_id = sent_proposal(conn, config)

    api = poll(conn, config, press("3", proposal_id))

    assert api.answered == [("cb1", "⚠️ その番号の枠がありません")]
    assert statuses(conn)[0][1] == "proposed"
    assert api.edited == []


@pytest.mark.parametrize(
    ("message_id", "chat_id", "other_id"),
    [(902, CHAT_ID, False), (901, CHAT_ID + 1, False), (901, CHAT_ID, True)],
)
def test_button_not_tied_to_the_proposal_is_refused(
    conn, tmp_path, message_id, chat_id, other_id
) -> None:
    config = x_config(tmp_path)
    proposal_id = sent_proposal(conn, config)

    api = poll(conn, config, press("1", 999 if other_id else proposal_id,
                                   message_id=message_id, chat_id=chat_id))

    assert api.answered == [("cb1", "⚠️ この案には対応できません")]
    assert statuses(conn)[0][1] == "proposed"
    assert api.edited == []


# --- 収集の合成点 ---


def test_collect_builds_the_search_connector_with_the_effective_slots(
    conn, tmp_path, monkeypatch
) -> None:
    import app.connectors.x_search as x_search
    from app.cli import _build_connectors

    seen = {}

    def fake_build(slots, *args, **kwargs):
        seen["slots"] = slots
        return object()

    monkeypatch.setattr(x_search, "build_x_search_connector", fake_build)
    config = x_config(tmp_path)
    approved(conn, topic(conn))
    env = {k: "v" for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN",
                            "X_ACCESS_TOKEN_SECRET")}

    _build_connectors(config, env, None, conn, logger)

    assert seen["slots"] == (SLOTS[0], QUERY, SLOTS[2])
    assert len(seen["slots"]) == len(SLOTS)  # 枠の数(読む上限の分け方)は変わらない
