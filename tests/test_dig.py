"""深掘りキューとリプライパーサ(GH#104 / 設計 06 §2〜3)。"""

import sqlite3
from pathlib import Path

import pytest

from app.db import apply_migrations, connect
from app.dig import (
    DigInstruction,
    apply_instructions,
    enqueue_bookmark,
    parse_dig_reply,
    queue_item_ids,
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
