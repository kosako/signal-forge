"""深掘りキューとリプライパーサ(GH#104 / 設計 06 §2〜3)。"""

import itertools
import sqlite3
from pathlib import Path

import pytest

from app.config import Config, DigConfig
from app.db import apply_migrations, connect
from app.dig import (
    DigInstruction,
    apply_instructions,
    claim_now_request,
    close_now_request,
    dig_now_marked,
    dug_on,
    enqueue_bookmark,
    has_now_requests,
    has_unsent_cards,
    interrupted_now_requests,
    parse_dig_reply,
    queue_item_ids,
    request_dig_now,
    select_from_queue,
)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'TechCrunch')")  # id=2
    yield conn
    conn.close()


def add_plain_item(conn: sqlite3.Connection, url: str) -> int:
    return conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, content_hash) "
        "VALUES (2, ?, ?, 'sha256:x')",
        (url, url),
    ).lastrowid


def queue_row(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM dig_queue WHERE item_id = ?", (item_id,)
    ).fetchone()


# --- parse_dig_reply ---


def test_parse_request_with_angle() -> None:
    result = parse_dig_reply("3664 を app-server の観点で", {3664})
    assert result == [
        DigInstruction(3664, "request", angle="app-server の観点で")
    ]


def test_parse_pass_variants() -> None:
    for text in ("3773 はパス", "3773 パスで", "3773 pass", "3773 スキップ"):
        assert parse_dig_reply(text, {3773}) == [DigInstruction(3773, "pass")], text


def test_parse_multiple_instructions_in_spoken_style() -> None:
    text = "えっと、3664 を承認フローの観点で。3773 はパス、あと 3660 も"
    result = parse_dig_reply(text, {3664, 3773, 3660})
    assert result == [
        DigInstruction(3664, "request", angle="承認フローの観点で"),
        DigInstruction(3773, "pass"),
        DigInstruction(3660, "request", angle=None),  # 「も」だけ = 観点なし登録
    ]


def test_parse_ignores_numbers_not_in_candidates() -> None:
    # 観点本文中の数字(GPT-5 の 5)を命令に誤検出しない(設計 06 §2)
    result = parse_dig_reply("3669 を GPT-5.6 移行の観点で", {3669})
    assert result == [
        DigInstruction(3669, "request", angle="GPT-5.6 移行の観点で")
    ]


def test_parse_unknown_id_yields_nothing() -> None:
    assert parse_dig_reply("9999 はパス", {3664}) == []


def test_parse_same_id_last_instruction_wins() -> None:
    text = "3664 はパス。やっぱり 3664 を検証の観点で"
    assert parse_dig_reply(text, {3664}) == [
        DigInstruction(3664, "request", angle="検証の観点で")
    ]


def test_parse_pass_word_inside_angle_is_not_pass() -> None:
    result = parse_dig_reply("3664 クリティカルパスの観点で", {3664})
    assert result == [
        DigInstruction(3664, "request", angle="クリティカルパスの観点で")
    ]


def test_parse_keeps_particle_like_word_heads() -> None:
    # 「はてな」の「は」を助詞と誤認して剥がさない(助詞剥がしは区切りが続くときだけ)
    result = parse_dig_reply("3664 はてなブックマーク動向の観点で", {3664})
    assert result == [
        DigInstruction(3664, "request", angle="はてなブックマーク動向の観点で")
    ]


def test_parse_filler_before_next_number_not_mixed_into_previous() -> None:
    # 「、あと」は次の命令の前置きであり、前の命令のパス判定・観点を汚さない
    result = parse_dig_reply("3773 はパス、あと 3660 を検証の観点で", {3773, 3660})
    assert result == [
        DigInstruction(3773, "pass"),
        DigInstruction(3660, "request", angle="検証の観点で"),
    ]


def test_parse_comma_inside_angle_kept_when_next_number_follows() -> None:
    # 観点の中の読点より後ろを次の番号の前置きと誤認して捨てない(GH#238)。
    # 接続語で始まっても後ろに自由文が続くなら観点の一部
    for text, angle in (
        ("3664 をコスト、運用の観点で 3773 はパス", "コスト、運用の観点で"),
        ("3664 をコスト、それと運用の観点で 3773 はパス", "コスト、それと運用の観点で"),
        ("3664 をコスト、運用の観点で\n3773 はパス", "コスト、運用の観点で"),
        ("3664 をコスト、運用の観点で。あと 3773 はパス", "コスト、運用の観点で"),
    ):
        assert parse_dig_reply(text, {3664, 3773}) == [
            DigInstruction(3664, "request", angle=angle),
            DigInstruction(3773, "pass"),
        ], text


def test_parse_connector_after_comma_still_dropped() -> None:
    # 読点の後ろが接続語だけなら次の番号の前置きとして切る(GH#238 で保つ挙動)
    for preface in ("、あと", "、それと", "、それから", "、で", "、えっと、あと", "、"):
        text = f"3664 をコスト、運用の観点で{preface} 3773 はパス"
        assert parse_dig_reply(text, {3664, 3773}) == [
            DigInstruction(3664, "request", angle="コスト、運用の観点で"),
            DigInstruction(3773, "pass"),
        ], text


def test_parse_pass_followed_by_comma_and_free_text() -> None:
    # パスは観点をとらないので、パス語の直後の読点で命令が終わる(GH#238)
    text = "3773 はパス、気になるのは 3660 を検証の観点で"
    assert parse_dig_reply(text, {3773, 3660}) == [
        DigInstruction(3773, "pass"),
        DigInstruction(3660, "request", angle="検証の観点で"),
    ]


# --- queue 操作 ---


def test_enqueue_bookmark_is_idempotent(conn) -> None:
    item = add_plain_item(conn, "https://a/1")
    assert enqueue_bookmark(conn, item) is True
    assert enqueue_bookmark(conn, item) is False  # 再投入しない
    row = queue_row(conn, item)
    assert (row["status"], row["source"]) == ("queued", "bookmark")


def test_enqueue_does_not_revive_passed_item(conn) -> None:
    """パス済み item への再ブックマークで status を戻さない(明示指示を自動が覆さない)。"""
    item = add_plain_item(conn, "https://a/1")
    apply_instructions(conn, [DigInstruction(item, "pass")])
    assert enqueue_bookmark(conn, item) is False
    assert queue_row(conn, item)["status"] == "passed"


def test_request_creates_row_with_angle_and_requested_at(conn) -> None:
    item = add_plain_item(conn, "https://a/1")
    apply_instructions(conn, [DigInstruction(item, "request", angle="検証の観点")])
    row = queue_row(conn, item)
    assert (row["status"], row["source"], row["angle"]) == (
        "queued", "request", "検証の観点",
    )
    assert row["requested_at"] is not None


def test_request_revives_passed_row_and_keeps_old_angle(conn) -> None:
    item = add_plain_item(conn, "https://a/1")
    apply_instructions(conn, [DigInstruction(item, "request", angle="旧観点")])
    apply_instructions(conn, [DigInstruction(item, "pass")])
    assert queue_row(conn, item)["status"] == "passed"
    # 観点なしの再指定 = 優先度だけ上げ、前回の観点は生かす
    apply_instructions(conn, [DigInstruction(item, "request", angle=None)])
    row = queue_row(conn, item)
    assert (row["status"], row["angle"]) == ("queued", "旧観点")


def test_pass_on_unqueued_item_creates_passed_row(conn) -> None:
    """キュー外の候補(Telegram 🔖 等)へのパスも記録される(調査段のスキップ対象)。"""
    item = add_plain_item(conn, "https://a/1")
    apply_instructions(conn, [DigInstruction(item, "pass")])
    row = queue_row(conn, item)
    assert (row["status"], row["source"]) == ("passed", "request")


def test_queue_item_ids(conn) -> None:
    a = add_plain_item(conn, "https://a/1")
    b = add_plain_item(conn, "https://a/2")
    enqueue_bookmark(conn, a)
    apply_instructions(conn, [DigInstruction(b, "pass")])
    assert queue_item_ids(conn) == {a, b}


def test_dig_queue_cascades_on_item_delete(conn) -> None:
    item = add_plain_item(conn, "https://a/1")
    enqueue_bookmark(conn, item)
    conn.execute("DELETE FROM items WHERE id = ?", (item,))
    assert queue_row(conn, item) is None


def test_reply_on_bookmark_row_keeps_origin_source(conn) -> None:
    """source は初回投入の経路で不変(migration 009 / Codex レビュー反映)。

    明示指定の事実は requested_at / status が担い、起源(自動で入ったか)を
    上書きで失わない。
    """
    item = add_plain_item(conn, "https://a/1")
    enqueue_bookmark(conn, item)
    apply_instructions(conn, [DigInstruction(item, "request", angle="検証の観点")])
    row = queue_row(conn, item)
    assert (row["source"], row["status"]) == ("bookmark", "queued")
    assert row["requested_at"] is not None  # 明示指定は requested_at で判別
    apply_instructions(conn, [DigInstruction(item, "pass")])
    assert queue_row(conn, item)["source"] == "bookmark"


# --- 今すぐの依頼(GH#388。設計 06 §3) ---

BODY = "本文の段落です。" * 50  # min_body_chars(既定 200)を超える
TODAY = "2026-07-22"


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(config_dir=tmp_path, dig=DigConfig(daily_cap=2))


def add_item_with_body(conn: sqlite3.Connection, url: str, body: str | None = BODY) -> int:
    item_id = add_plain_item(conn, url)
    if body is not None:
        conn.execute(
            "INSERT INTO documents (item_id, extracted_text, language, extraction_status,"
            " expires_at) VALUES (?, ?, 'ja', 'ok', '2099-01-01T00:00:00Z')",
            (item_id, body),
        )
    return item_id


_callbacks = itertools.count(1)


def press_now(
    conn: sqlite3.Connection,
    config: Config,
    item_id: int,
    *,
    from_backlog: bool = True,
    callback_id: str | None = None,
) -> str:
    """今すぐのボタンを押す(callback の id は押すたびに新しい。再配達は callback_id で渡す)。"""
    callback = callback_id if callback_id is not None else f"cb-{next(_callbacks)}"
    return request_dig_now(conn, config, item_id, TODAY, callback, from_backlog=from_backlog)


def add_question_row(conn: sqlite3.Connection) -> int:
    """受け付け済みの質問(受付の順序の印の比較用。GH#235)。"""
    card_item = add_plain_item(conn, "https://a/card")
    dig_id = conn.execute(
        "INSERT INTO dig_results (item_id, note_name, card_text, message_id) "
        "VALUES (?, 'note', 'カード', 700)",
        (card_item,),
    ).lastrowid
    return conn.execute(
        "INSERT INTO dig_questions (dig_result_id, question, question_message_id) "
        "VALUES (?, '質問', 81)",
        (dig_id,),
    ).lastrowid


def test_now_request_on_queued_row_only_marks_request(conn, config) -> None:
    # 状態・観点・requested_at・updated_at を変えない(朝の生成中の done 化の照合を外さない)
    item = add_item_with_body(conn, "https://a/1")
    enqueue_bookmark(conn, item)
    conn.execute("UPDATE dig_queue SET angle = '観点', updated_at = '2026-07-20T00:00:00Z'")
    before = queue_row(conn, item)

    assert press_now(conn, config, item) == "accepted"

    row = queue_row(conn, item)
    assert row["now_requested_at"] is not None and row["now_started_at"] is None
    for column in ("status", "angle", "requested_at", "updated_at", "backlog_op_question_id"):
        assert row[column] == before[column], column
    assert has_now_requests(conn)


def test_now_request_creates_row_for_item_out_of_queue(conn, config) -> None:
    item = add_item_with_body(conn, "https://a/1")
    question_id = add_question_row(conn)

    assert press_now(conn, config, item) == "accepted"

    row = queue_row(conn, item)
    assert (row["status"], row["source"], row["requested_at"]) == ("queued", "request", None)
    assert row["now_requested_at"] is not None
    assert row["backlog_op_question_id"] == question_id  # 未消化一覧への本人の操作の印


def test_now_request_revives_passed_row(conn, config) -> None:
    item = add_item_with_body(conn, "https://a/1")
    apply_instructions(conn, [DigInstruction(item, "request", angle="前の観点")])
    apply_instructions(conn, [DigInstruction(item, "pass")])
    conn.execute("UPDATE dig_queue SET updated_at = '2026-07-20T00:00:00Z'")

    assert press_now(conn, config, item, from_backlog=False) == "accepted"

    row = queue_row(conn, item)
    assert (row["status"], row["angle"]) == ("queued", "前の観点")
    assert row["updated_at"] != "2026-07-20T00:00:00Z"  # 本人の明示の操作
    assert row["now_requested_at"] is not None


@pytest.mark.parametrize(
    ("setup", "outcome"),
    [
        ("done", "done"),
        ("pending", "pending"),
        ("running", "running"),
        ("dug_today", "dug_today"),
        ("no_body", "no_body"),
        ("short_body", "no_body"),
    ],
)
def test_now_request_refusals_write_nothing(conn, config, setup, outcome) -> None:
    body = {"no_body": None, "short_body": "短い"}.get(setup, BODY)
    item = add_item_with_body(conn, "https://a/1", body)
    enqueue_bookmark(conn, item)
    if setup == "done":
        conn.execute("UPDATE dig_queue SET status = 'done'")
    elif setup in ("pending", "running"):
        conn.execute(
            "UPDATE dig_queue SET now_requested_at = '2026-07-22T00:00:00Z', now_started_at = ?",
            ("2026-07-22T00:00:01Z" if setup == "running" else None,),
        )
    elif setup == "dug_today":
        conn.execute(  # JST 2026-07-22 の朝に掘った(created_at は UTC)
            "INSERT INTO dig_results (item_id, note_name, card_text, created_at) "
            "VALUES (?, 'n', 'カード', '2026-07-21T21:40:00Z')",
            (item,),
        )
    columns = "item_id, status, source, angle, requested_at, updated_at, now_requested_at, " \
        "now_started_at, backlog_op_question_id"
    before = [tuple(row) for row in conn.execute(f"SELECT {columns} FROM dig_queue")]

    assert press_now(conn, config, item, callback_id="cb-x") == outcome

    assert [tuple(row) for row in conn.execute(f"SELECT {columns} FROM dig_queue")] == before
    # 受け付け済み・作成中と答えた callback は id を残し、再配達を新しい依頼にしない
    kept = queue_row(conn, item)["now_callback_id"]
    assert kept == ("cb-x" if outcome in ("pending", "running") else None)


def test_now_request_for_missing_item(conn, config) -> None:
    assert press_now(conn, config, 9999) == "no_item"
    assert conn.execute("SELECT count(*) FROM dig_queue").fetchone()[0] == 0


def test_dug_on_uses_jst_day(conn) -> None:
    item = add_plain_item(conn, "https://a/1")
    conn.execute(
        "INSERT INTO dig_results (item_id, note_name, card_text, created_at) "
        "VALUES (?, 'n', 'カード', '2026-07-21T15:00:00Z')",  # JST 2026-07-22 00:00
        (item,),
    )
    assert dug_on(conn, item, "2026-07-22")
    assert not dug_on(conn, item, "2026-07-21")


def test_pass_cancels_now_request_but_keeps_started_mark(conn, config) -> None:
    # パスは今すぐの依頼を取り消す。作っている途中の印は残し、作り終えるまで朝の選定に選ばせない
    # (パスの後に指定し直しても、同じ item を 2 回作らない)
    item = add_item_with_body(conn, "https://a/1")
    press_now(conn, config, item)
    candidate = claim_now_request(conn)
    assert candidate is not None

    apply_instructions(conn, [DigInstruction(item, "pass")])
    apply_instructions(conn, [DigInstruction(item, "request", angle="観点")])

    row = queue_row(conn, item)
    assert (row["status"], row["now_requested_at"], row["now_started_at"]) == (
        "queued", None, candidate.now_started_at,
    )
    assert claim_now_request(conn) is None  # 依頼は取り消し済み
    assert has_now_requests(conn)  # 印の残った行は dig-now を起こす(止まっていれば片付ける)
    assert select_from_queue(conn, config, 2, "2099-01-01") == ([], [])


def test_pass_before_claim_cancels_request(conn, config) -> None:
    item = add_item_with_body(conn, "https://a/1")
    press_now(conn, config, item)

    apply_instructions(conn, [DigInstruction(item, "pass")])

    row = queue_row(conn, item)
    assert (row["status"], row["now_requested_at"], row["now_started_at"]) == (
        "passed", None, None,
    )
    assert not has_now_requests(conn)


def test_redelivered_callback_is_not_a_new_request(conn, config) -> None:
    # 受け付けた後、offset を保存する前に poller が止まると同じ callback が再配達される。依頼を
    # 閉じた後(作れずに明朝へ回した)に届いても、新しい依頼にしない。別の押下は受け付ける
    item = add_item_with_body(conn, "https://a/1")
    assert press_now(conn, config, item, callback_id="cb-a") == "accepted"
    candidate = claim_now_request(conn)
    assert candidate is not None
    close_now_request(conn, item, candidate.now_started_at, to_morning=True)

    assert press_now(conn, config, item, callback_id="cb-a") == "duplicate"
    assert queue_row(conn, item)["now_requested_at"] is None
    assert press_now(conn, config, item, callback_id="cb-b") == "accepted"


def test_reply_request_keeps_now_request(conn, config) -> None:
    item = add_item_with_body(conn, "https://a/1")
    press_now(conn, config, item)

    apply_instructions(conn, [DigInstruction(item, "request", angle="観点")])

    assert queue_row(conn, item)["now_requested_at"] is not None


def test_claim_takes_oldest_request_and_marks_started(conn, config) -> None:
    first = add_item_with_body(conn, "https://a/1")
    second = add_item_with_body(conn, "https://a/2")
    for item in (second, first):
        press_now(conn, config, item)
    conn.execute(
        "UPDATE dig_queue SET now_requested_at = '2026-07-22T00:00:00Z' WHERE item_id = ?",
        (first,),
    )
    snapshot = queue_row(conn, first)

    candidate = claim_now_request(conn)

    assert candidate is not None and candidate.item_id == first
    assert candidate.on_demand and candidate.now_started_at is not None
    assert candidate.queue_updated_at == snapshot["updated_at"]  # 印は updated_at を進めない
    assert queue_row(conn, first)["now_started_at"] == candidate.now_started_at
    assert interrupted_now_requests(conn) == [(first, candidate.now_started_at)]
    next_candidate = claim_now_request(conn)
    assert next_candidate is not None and next_candidate.item_id == second
    assert claim_now_request(conn) is None


def test_close_now_request_only_closes_same_claim(conn, config) -> None:
    item = add_item_with_body(conn, "https://a/1")
    press_now(conn, config, item)
    candidate = claim_now_request(conn)
    assert candidate is not None

    assert not close_now_request(conn, item, "2000-01-01T00:00:00Z", to_morning=False)
    assert close_now_request(conn, item, candidate.now_started_at, to_morning=True)

    row = queue_row(conn, item)
    assert (row["status"], row["now_requested_at"], row["now_started_at"]) == (
        "queued", None, None,
    )
    assert row["requested_at"] is not None  # 明朝の深掘りの ①(リプライ指定)へ回した


def test_dig_now_marked_and_unsent_cards(conn, config) -> None:
    pending = add_item_with_body(conn, "https://a/1")
    done = add_item_with_body(conn, "https://a/2")
    plain = add_item_with_body(conn, "https://a/3")
    press_now(conn, config, pending)
    enqueue_bookmark(conn, done)
    conn.execute("UPDATE dig_queue SET status = 'done' WHERE item_id = ?", (done,))
    enqueue_bookmark(conn, plain)

    assert dig_now_marked(conn, [pending, done, plain]) == {pending, done}
    assert not has_unsent_cards(conn)
    conn.execute(  # 朝の段が今すぐの依頼を引き受けて作った分(on_demand = 0)も送り直す対象
        "INSERT INTO dig_results (item_id, note_name, card_text) VALUES (?, 'n', 'カード')",
        (pending,),
    )
    assert has_unsent_cards(conn)


def test_redelivered_running_press_after_first_request_failed(conn, config) -> None:
    # A を作っている間に押した B(作成中と答えた)の再配達が、A が作れずに閉じた後に届いても、
    # 新しい依頼にしない(1 回押して 1 回だけ作る)
    item = add_item_with_body(conn, "https://a/1")
    assert press_now(conn, config, item, callback_id="cb-a") == "accepted"
    candidate = claim_now_request(conn)
    assert candidate is not None
    assert press_now(conn, config, item, callback_id="cb-b") == "running"
    close_now_request(conn, item, candidate.now_started_at, to_morning=True)

    assert press_now(conn, config, item, callback_id="cb-b") == "duplicate"
    assert queue_row(conn, item)["now_requested_at"] is None
