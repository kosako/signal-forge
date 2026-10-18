"""take の記録(GH#106 / 設計 06 §6)。"""

import logging
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from app import dig_take
from app.config import Config, DigConfig, ObsidianConfig, TelegramConfig
from app.db import apply_migrations, connect
from app.dig_take import has_unrecorded_take, record_takes
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


def answered(conn, dig_id, text, message_id, kind, *, created="2026-10-18T13:00:55Z") -> int:
    question_id = ask(conn, dig_id, text, message_id)
    conn.execute(
        "UPDATE dig_questions SET status = 'answered', kind = ?, answer = '返事', "
        "created_at = ? WHERE id = ?",
        (kind, created, question_id),
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


def test_take_appended_status_marked_and_followup(conn, config) -> None:
    dig_id = add_dig(conn, config, note=NOTE)
    question_id = answered(conn, dig_id, "判定軸が面白い。\n自分の運用にも使う", 900, "take")
    assert has_unrecorded_take(conn)

    assert record_takes(conn, config, logger) == 1

    text = note_path(config, conn, dig_id).read_text(encoding="utf-8")
    assert text.startswith("---\nid: 1\nkind: deepdive\nstatus: take済\n---\n")
    assert "本文。status: take待ち" in text  # frontmatter の外は書き換えない
    assert text.endswith(
        "(この下は手書きメモ・take 領域)\n"
        "\n**take**(2026-10-18 22:00・Telegram のリプライ)\n\n"
        "判定軸が面白い。\n自分の運用にも使う\n\n"
        f"<!-- signal-forge:take q={question_id} -->\n"
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
    assert f"面白い。でもこれは何?\n\n<!-- signal-forge:take q={both} -->" in text


def test_rerun_does_not_append_twice(conn, config) -> None:
    dig_id = add_dig(conn, config, note=NOTE)
    answered(conn, dig_id, "結論はこう", 904, "take")
    assert record_takes(conn, config, logger) == 1
    assert record_takes(conn, config, logger) == 0
    assert note_path(config, conn, dig_id).read_text(encoding="utf-8").count("結論はこう") == 1


def test_written_note_but_unrecorded_ledger_is_not_appended_again(conn, config) -> None:
    # ノートに書いた後、台帳に記帳する前に止まった: 印を見て追記をやり直さない
    dig_id = add_dig(conn, config, note=NOTE)
    question_id = answered(conn, dig_id, "結論はこう", 905, "take")
    path = note_path(config, conn, dig_id)
    path.write_text(NOTE + f"\n結論はこう\n\n<!-- signal-forge:take q={question_id} -->\n")

    assert record_takes(conn, config, logger) == 1
    assert path.read_text(encoding="utf-8").count("結論はこう") == 1
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


def test_status_changed_by_owner_is_kept(conn, config) -> None:
    dig_id = add_dig(conn, config, note=NOTE.replace("status: take待ち\n---", "status: 保留\n---"))
    answered(conn, dig_id, "結論", 908, "take")
    assert record_takes(conn, config, logger) == 1
    assert "status: 保留" in note_path(config, conn, dig_id).read_text(encoding="utf-8")


def test_missing_note_still_records_followup(conn, config, caplog) -> None:
    dig_id = add_dig(conn, config, note=NOTE)
    note_path(config, conn, dig_id).unlink()  # 本人が動かした・消した
    answered(conn, dig_id, "結論", 909, "take")

    with caplog.at_level(logging.WARNING):
        assert record_takes(conn, config, logger) == 1
    assert "解説ノートが見つからず" in caplog.text
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
    real_write = dig_take.atomic_write

    def flaky(path, content):
        if "書けない take" in content:
            raise PermissionError("vault に書けない")
        real_write(path, content)

    monkeypatch.setattr(dig_take, "atomic_write", flaky)
    with caplog.at_level(logging.ERROR):
        assert record_takes(conn, config, logger) == 1  # 1 件の失敗は他を止めない
    assert "次の実行で再試行" in caplog.text
    assert followups(conn, item_of(conn, dig_id)) == []
    assert has_unrecorded_take(conn)

    monkeypatch.setattr(dig_take, "atomic_write", real_write)
    assert record_takes(conn, config, logger) == 1
    assert "書けない take" in note_path(config, conn, dig_id).read_text(encoding="utf-8")
    assert followups(conn, item_of(conn, dig_id)) == [("yes", "2026-10-18")]


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
