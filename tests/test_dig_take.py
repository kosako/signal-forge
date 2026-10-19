"""take の記録(GH#106 / 設計 06 §6)。"""

import logging
import os
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from app import dig_take
from app.config import Config, DigConfig, ObsidianConfig, TelegramConfig
from app.db import apply_migrations, connect
from app.dig_take import has_unrecorded_take, record_takes
from app.vault_guard import VaultGuard
from tests.test_dig_answer import add_dig, ask

logger = logging.getLogger("test-dig-take")


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed')")  # id=2
    yield conn
    conn.close()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    vault = tmp_path / "vault"
    (vault / "SignalForge" / "DeepDive").mkdir(parents=True)
    return Config(
        config_dir=tmp_path,
        obsidian=ObsidianConfig(vault_path=str(vault)),
        telegram=TelegramConfig(owner_user_id=1, owner_chat_id=10),
        dig=DigConfig(),
    )

NOTE = (
    "---\nid: 1\nkind: deepdive\nstatus: take待ち\n---\n## 概要\n\n本文。status: take待ち\n"
    "\n---\n\n(この下は手書きメモ・take 領域)\n"
)


def note_path(config: Config, conn: sqlite3.Connection, dig_id: int) -> Path:
    name = conn.execute("SELECT note_name FROM dig_results WHERE id = ?", (dig_id,)).fetchone()[0]
    return Path(config.obsidian.vault_path) / "SignalForge" / "DeepDive" / f"{name}.md"


def answered(
    conn, dig_id, text, message_id, kind, *, created="2026-10-18T13:00:55Z", sent=True
) -> int:
    """返事を作った質問。sent=True なら返事も送れている(answer_message_id がある)。"""
    question_id = ask(conn, dig_id, text, message_id)
    conn.execute(
        "UPDATE dig_questions SET status = 'answered', kind = ?, answer = '返事', "
        "created_at = ?, answer_message_id = ? WHERE id = ?",
        (kind, created, message_id + 10_000 if sent else None, question_id),
    )
    return question_id


def item_of(conn, dig_id) -> int:
    return conn.execute("SELECT item_id FROM dig_results WHERE id = ?", (dig_id,)).fetchone()[0]


def followups(conn, item_id) -> list[tuple]:
    rows = conn.execute(
        "SELECT value, signal_date FROM signals WHERE kind = 'followup_dig' AND item_id = ?",
        (item_id,),
    )
    return [tuple(r) for r in rows]


def test_take_appended_and_followup_recorded(conn, config) -> None:
    dig_id = add_dig(conn, config, note=NOTE)
    question_id = answered(conn, dig_id, "判定軸が面白い。\n自分の運用にも使う", 900, "take")
    assert has_unrecorded_take(conn)

    assert record_takes(conn, config, logger) == 1

    text = note_path(config, conn, dig_id).read_text(encoding="utf-8")
    assert text.startswith(NOTE)  # 今ある中身(frontmatter の status も)は書き換えない
    assert text.endswith(
        "(この下は手書きメモ・take 領域)\n"
        f"\n**take**(2026-10-18 22:00・Telegram のリプライ・記録番号 {question_id})\n\n"
        "判定軸が面白い。\n自分の運用にも使う\n"
    )
    # 🔖 の無いアイテムは take の日(JST)
    assert followups(conn, item_of(conn, dig_id)) == [("yes", "2026-10-18")]
    assert not has_unrecorded_take(conn)


def test_question_and_failed_are_not_takes_but_both_is(conn, config) -> None:
    dig_id = add_dig(conn, config, note=NOTE)
    answered(conn, dig_id, "これはどういう意味?", 901, "question")
    failed = ask(conn, dig_id, "答えを作れなかった", 902)
    conn.execute("UPDATE dig_questions SET status = 'failed' WHERE id = ?", (failed,))
    both = answered(conn, dig_id, "面白い。でもこれは何?", 903, "both")

    assert record_takes(conn, config, logger) == 1
    text = note_path(config, conn, dig_id).read_text(encoding="utf-8")
    assert "これはどういう意味?" not in text
    assert "答えを作れなかった" not in text
    assert f"記録番号 {both})\n\n面白い。でもこれは何?\n" in text


def test_rerun_does_not_append_twice(conn, config) -> None:
    dig_id = add_dig(conn, config, note=NOTE)
    answered(conn, dig_id, "結論はこう", 904, "take")
    assert record_takes(conn, config, logger) == 1
    assert record_takes(conn, config, logger) == 0
    assert note_path(config, conn, dig_id).read_text(encoding="utf-8").count("結論はこう") == 1


def test_written_note_but_unrecorded_ledger_is_not_appended_again(conn, config) -> None:
    # ノートに書いた後、台帳に記帳する前に止まった: 見出しの記録番号を見て追記をやり直さない
    dig_id = add_dig(conn, config, note=NOTE)
    answered(conn, dig_id, "結論はこう", 905, "take")
    assert record_takes(conn, config, logger) == 1
    conn.execute("UPDATE dig_questions SET take_recorded_at = NULL")  # 記帳の前に止まった
    conn.execute("DELETE FROM signals WHERE kind = 'followup_dig'")
    path = note_path(config, conn, dig_id)
    path.write_text(path.read_text(encoding="utf-8") + "\n本人が後ろに書き足したメモ\n")

    assert record_takes(conn, config, logger) == 1
    text = path.read_text(encoding="utf-8")
    assert text.count("結論はこう") == 1
    assert text.endswith("本人が後ろに書き足したメモ\n")
    assert followups(conn, item_of(conn, dig_id)) == [("yes", "2026-10-18")]


def test_followup_uses_bookmark_date_and_is_not_duplicated(conn, config) -> None:
    dig_id = add_dig(conn, config, note=NOTE)
    item_id = item_of(conn, dig_id)
    conn.execute(
        "INSERT INTO signals (kind, item_id, value, signal_date) "
        "VALUES ('reaction', ?, 'dig', '2026-10-15')",
        (item_id,),
    )
    answered(conn, dig_id, "一つ目の take", 906, "take")
    answered(conn, dig_id, "二つ目の take", 907, "take")

    assert record_takes(conn, config, logger) == 2
    assert followups(conn, item_id) == [("yes", "2026-10-15")]  # 🔖 を付けた日・1 回だけ


def test_missing_note_still_records_followup(conn, config, caplog) -> None:
    dig_id = add_dig(conn, config, note=NOTE)
    note_path(config, conn, dig_id).unlink()  # 本人が動かした・消した
    answered(conn, dig_id, "結論", 909, "take")

    with caplog.at_level(logging.WARNING):
        assert record_takes(conn, config, logger) == 1
    assert "ノートが見つからない" in caplog.text
    assert followups(conn, item_of(conn, dig_id)) == [("yes", "2026-10-18")]
    assert not has_unrecorded_take(conn)


def test_note_outside_deepdive_is_not_written(conn, config, tmp_path) -> None:
    dig_id = add_dig(conn, config, note=NOTE)
    path = note_path(config, conn, dig_id)
    outside = tmp_path / "outside.md"
    outside.write_text(NOTE, encoding="utf-8")
    path.unlink()
    os.symlink(outside, path)
    answered(conn, dig_id, "結論", 910, "take")

    assert record_takes(conn, config, logger) == 1
    assert outside.read_text(encoding="utf-8") == NOTE  # DeepDive の外は書き換えない


def test_write_failure_is_retried_next_run(conn, config, monkeypatch, caplog) -> None:
    dig_id = add_dig(conn, config, note=NOTE)
    other_id = add_dig(conn, config, note=NOTE, url="https://a.example/2")
    answered(conn, dig_id, "書けない take", 911, "take")
    answered(conn, other_id, "書ける take", 912, "take")
    broken = note_path(config, conn, dig_id)
    real_open = dig_take.os.open

    def flaky(target, flags, *args, **kwargs):
        if str(target) == broken.name:  # DeepDive の置き場から開くノート
            raise PermissionError("vault に書けない")
        return real_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(dig_take.os, "open", flaky)
    with caplog.at_level(logging.ERROR):
        assert record_takes(conn, config, logger) == 1  # 1 件の失敗は他を止めない
    assert "次の実行で再試行" in caplog.text
    assert followups(conn, item_of(conn, dig_id)) == []
    assert has_unrecorded_take(conn)

    monkeypatch.setattr(dig_take.os, "open", real_open)
    assert record_takes(conn, config, logger) == 1
    assert "書けない take" in note_path(config, conn, dig_id).read_text(encoding="utf-8")
    assert followups(conn, item_of(conn, dig_id)) == [("yes", "2026-10-18")]


def test_vault_stall_stops_recording_and_is_retried(conn, config, monkeypatch, caplog) -> None:
    # 追記が止まったら(macOS の確認待ちなど)この実行では残りを試さず、次の実行で記録する(GH#145)
    first = add_dig(conn, config, note=NOTE)
    second = add_dig(conn, config, note=NOTE, url="https://a.example/2")
    answered(conn, first, "take 1", 921, "take")
    answered(conn, second, "take 2", 922, "take")
    release = threading.Event()
    attempts: list[int] = []
    real_append = dig_take._append_to_note

    def stuck(config, row):
        attempts.append(row["id"])
        release.wait()

    monkeypatch.setattr(dig_take, "_append_to_note", stuck)
    try:
        with caplog.at_level(logging.ERROR):
            assert record_takes(conn, config, logger, VaultGuard(timeout_s=0.05)) == 0
    finally:
        release.set()
    assert len(attempts) == 1
    assert "vault が応答しない" in caplog.text
    assert followups(conn, item_of(conn, first)) == []
    assert has_unrecorded_take(conn)

    monkeypatch.setattr(dig_take, "_append_to_note", real_append)
    assert record_takes(conn, config, logger) == 2
    assert "take 1" in note_path(config, conn, first).read_text(encoding="utf-8")
    assert "take 2" in note_path(config, conn, second).read_text(encoding="utf-8")


def test_ledger_failure_rolls_back_and_is_retried(conn, config, monkeypatch, caplog) -> None:
    dig_id = add_dig(conn, config, note=NOTE)
    answered(conn, dig_id, "結論", 913, "take")

    def broken(conn_, row):
        conn_.execute(
            "INSERT INTO signals (kind, item_id, value, signal_date) "
            "VALUES ('followup_dig', ?, 'yes', '2026-10-18')",
            (row["item_id"],),
        )
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(dig_take, "_record_followup", broken)
    with caplog.at_level(logging.ERROR):
        assert record_takes(conn, config, logger) == 0
    assert "台帳に記録できなかった" in caplog.text
    assert followups(conn, item_of(conn, dig_id)) == []  # 途中まで書いた分は戻る
    assert has_unrecorded_take(conn)
    assert not conn.in_transaction


def test_no_vault_records_followup_only(conn, config) -> None:
    dig_id = add_dig(conn, config, note=NOTE)
    answered(conn, dig_id, "結論", 914, "take")
    config = replace(config, obsidian=replace(config.obsidian, vault_path=None))
    assert record_takes(conn, config, logger) == 1
    assert followups(conn, item_of(conn, dig_id)) == [("yes", "2026-10-18")]


def test_unsent_reply_is_not_recorded_until_sent(conn, config) -> None:
    # 返事が届く前に未消化一覧から消さない。送れたら記録する
    dig_id = add_dig(conn, config, note=NOTE)
    question_id = answered(conn, dig_id, "結論", 915, "take", sent=False)
    assert not has_unrecorded_take(conn)
    assert record_takes(conn, config, logger) == 0
    assert followups(conn, item_of(conn, dig_id)) == []
    assert "結論" not in note_path(config, conn, dig_id).read_text(encoding="utf-8")

    conn.execute("UPDATE dig_questions SET answer_message_id = 9915 WHERE id = ?", (question_id,))
    assert has_unrecorded_take(conn)
    assert record_takes(conn, config, logger) == 1
    assert followups(conn, item_of(conn, dig_id)) == [("yes", "2026-10-18")]


def test_take_on_bookmarked_item_counts_in_kpi_06(conn, config) -> None:
    # followup_dig=yes は「深掘りした」の記録。🔖 の付いたアイテムでは KPI 06 の分子に入る
    dig_id = add_dig(conn, config, note=NOTE)
    conn.execute(
        "INSERT INTO signals (kind, item_id, value, signal_date) "
        "VALUES ('reaction', ?, 'dig', '2026-10-15')",
        (item_of(conn, dig_id),),
    )
    answered(conn, dig_id, "結論", 920, "take")
    kpi = Path(__file__).resolve().parent.parent / "scripts" / "kpi" / "06-phase2-boundary.sql"
    before = conn.execute(kpi.read_text(encoding="utf-8")).fetchone()
    assert (before["dig_total"], before["followup_yes"]) == (1, 0)

    assert record_takes(conn, config, logger) == 1
    after = conn.execute(kpi.read_text(encoding="utf-8")).fetchone()
    assert (after["dig_total"], after["followup_yes"], after["dig_execution_pct"]) == (1, 1, 100.0)


def test_missing_vault_folder_is_retried_not_given_up(conn, config, caplog) -> None:
    # vault が一時的に使えない(DeepDive の置き場ごと無い)のは、ノートが消えたのとは分けて再試行する
    dig_id = add_dig(conn, config, note=NOTE)
    answered(conn, dig_id, "結論", 924, "take")
    deepdive = note_path(config, conn, dig_id).parent
    moved = deepdive.with_name("DeepDive-unmounted")
    deepdive.rename(moved)

    with caplog.at_level(logging.ERROR):
        assert record_takes(conn, config, logger) == 0
    assert "次の実行で再試行" in caplog.text
    assert followups(conn, item_of(conn, dig_id)) == []
    assert has_unrecorded_take(conn)

    moved.rename(deepdive)
    assert record_takes(conn, config, logger) == 1
    assert "結論" in note_path(config, conn, dig_id).read_text(encoding="utf-8")


def test_append_only_never_replaces_or_rewrites_the_note(conn, config) -> None:
    # 本人のノートは書き換えない: ファイルの実体(inode)も今ある中身もそのまま、末尾に足すだけ
    note = "---\n---\n本文\n\n---\nstatus: take待ち\n---\n本人のメモ(末尾に改行なし)"
    dig_id = add_dig(conn, config, note=note)
    answered(conn, dig_id, "結論", 925, "take")
    path = note_path(config, conn, dig_id)
    inode = path.stat().st_ino

    assert record_takes(conn, config, logger) == 1
    assert path.stat().st_ino == inode
    text = path.read_text(encoding="utf-8")
    assert text.startswith(note + "\n")  # 末尾に改行が無ければ足してから書く
    assert text.count("status: take待ち") == 1


def test_owner_save_just_before_append_is_kept(conn, config, monkeypatch) -> None:
    # 読んだ後・追記の直前に本人が保存しても、その中身は残り、take はその後ろに付く
    dig_id = add_dig(conn, config, note=NOTE)
    answered(conn, dig_id, "結論はこう", 926, "take")
    path = note_path(config, conn, dig_id)
    edited = NOTE + "\n本人がいま書いたメモ\n"
    real_write = dig_take.os.write
    saved = []

    def save_then_write(fd, data):
        if not saved:  # ノートを読み終えた後、最初の書き込みの直前
            with path.open("a", encoding="utf-8") as f:  # エディタの保存(同じファイルへの書き足し)
                f.write("\n本人がいま書いたメモ\n")
            saved.append(True)
        return real_write(fd, data)

    monkeypatch.setattr(dig_take.os, "write", save_then_write)
    assert record_takes(conn, config, logger) == 1
    text = path.read_text(encoding="utf-8")
    assert text.startswith(edited)
    assert text.endswith("結論はこう\n")


def test_non_utf8_note_is_appended_without_touching_its_bytes(conn, config) -> None:
    dig_id = add_dig(conn, config, note=NOTE)
    path = note_path(config, conn, dig_id)
    path.write_bytes(b"---\nstatus: take\x8a\x82\n---\n")
    answered(conn, dig_id, "結論", 927, "take")

    assert record_takes(conn, config, logger) == 1
    data = path.read_bytes()
    assert data.startswith(b"---\nstatus: take\x8a\x82\n---\n")
    assert "結論".encode() in data


@pytest.mark.parametrize("cut", [1, 2, 3, 20, 100])  # 見出しの先頭〜途中 / 本文の途中で止まる
@pytest.mark.parametrize("ends_with_newline", [True, False])
def test_partial_write_then_error_is_completed_without_duplication(
    conn, config, monkeypatch, cut, ends_with_newline
) -> None:
    note = NOTE if ends_with_newline else NOTE.rstrip("\n")
    dig_id = add_dig(conn, config, note=note)
    answered(conn, dig_id, "途中まで書いて止まった結論。続きの文がここにある", 928, "take")
    path = note_path(config, conn, dig_id)
    real_write = dig_take.os.write
    calls = []

    def short_then_fail(fd, data):
        calls.append(len(data))
        if len(calls) == 1:
            return real_write(fd, data[:cut])  # 短い書き込み
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(dig_take.os, "write", short_then_fail)
    assert record_takes(conn, config, logger) == 0
    assert has_unrecorded_take(conn)

    monkeypatch.setattr(dig_take.os, "write", real_write)
    assert record_takes(conn, config, logger) == 1
    text = path.read_text(encoding="utf-8")
    assert text.startswith(note)
    tail = text[len(note):]
    assert tail.lstrip("\n").startswith("**take**(")  # 断片を残さず、見出しから始まる
    assert tail.count("*") == 4 and tail.count("記録番号") == 1
    assert text.endswith("続きの文がここにある\n")


def test_partial_write_followed_by_other_text_is_not_taken_as_done(
    conn, config, monkeypatch
) -> None:
    # 書きかけの後ろに本人がメモを足した: 書き終えたとはみなさず、節を丸ごと足す
    dig_id = add_dig(conn, config, note=NOTE)
    answered(conn, dig_id, "途中まで書いて止まった結論。続きの文がここにある", 931, "take")
    path = note_path(config, conn, dig_id)
    real_write = dig_take.os.write
    calls = []

    def short_then_fail(fd, data):
        calls.append(len(data))
        if len(calls) == 1:
            return real_write(fd, data[:100])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(dig_take.os, "write", short_then_fail)
    assert record_takes(conn, config, logger) == 0
    monkeypatch.setattr(dig_take.os, "write", real_write)
    path.write_text(path.read_text(encoding="utf-8") + "\n本人のメモ\n", encoding="utf-8")

    assert record_takes(conn, config, logger) == 1
    text = path.read_text(encoding="utf-8")
    assert "本人のメモ" in text
    assert text.endswith("途中まで書いて止まった結論。続きの文がここにある\n")  # 欠けずに入る


@pytest.mark.parametrize("swapped", ["SignalForge", "DeepDive"])
def test_folder_swapped_for_symlink_is_never_written(conn, config, tmp_path, swapped) -> None:
    # 途中のフォルダや置き場が外への symlink にすり替えられたままでも、やり直しても外には書かない
    dig_id = add_dig(conn, config, note=NOTE)
    answered(conn, dig_id, "結論", 929, "take")
    note = note_path(config, conn, dig_id)
    folder = note.parent.parent if swapped == "SignalForge" else note.parent
    outside = tmp_path / "outside"
    target = outside / "DeepDive" if swapped == "SignalForge" else outside
    target.mkdir(parents=True)
    victim = target / note.name
    victim.write_text("外のファイル\n", encoding="utf-8")
    folder.rename(folder.with_name(folder.name + "-real"))
    os.symlink(outside, folder)

    for _ in range(2):
        conn.execute("UPDATE dig_questions SET take_recorded_at = NULL")
        record_takes(conn, config, logger)
        assert victim.read_text(encoding="utf-8") == "外のファイル\n"


@pytest.mark.parametrize("cut", [1, 2, 20])
def test_partial_write_into_empty_note_is_completed(conn, config, monkeypatch, cut) -> None:
    # 空のノートに書きかけた(ファイルの先頭から始まる書きかけ)も、続きだけを足す
    dig_id = add_dig(conn, config, note=NOTE)
    path = note_path(config, conn, dig_id)
    path.write_text("", encoding="utf-8")
    answered(conn, dig_id, "空のノートへの結論", 932, "take")
    real_write = dig_take.os.write
    calls = []

    def short_then_fail(fd, data):
        calls.append(len(data))
        if len(calls) == 1:
            return real_write(fd, data[:cut])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(dig_take.os, "write", short_then_fail)
    assert record_takes(conn, config, logger) == 0
    monkeypatch.setattr(dig_take.os, "write", real_write)
    assert record_takes(conn, config, logger) == 1
    text = path.read_text(encoding="utf-8")
    assert text.startswith("**take**(") and text.count("記録番号") == 1
    assert text.endswith("空のノートへの結論\n")

