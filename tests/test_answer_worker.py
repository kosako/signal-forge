"""質問への返事の起動係(GH#126)。別プロセスは起動せず、Popen を差し替えて確かめる。"""

import logging
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.db import apply_migrations, connect
from app.delivery.answer_worker import RETRY_UNSENT_S, AnswerWorker

logger = logging.getLogger("test-answer-worker")


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed')")  # id=2
    item_id = conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, content_hash) "
        "VALUES (2, 'https://a/1', 'https://a/1', 'sha256:x')"
    ).lastrowid
    conn.execute(
        "INSERT INTO dig_results (id, item_id, note_name, card_text, message_id) "
        "VALUES (1, ?, 'note', 'カード', 700)",
        (item_id,),
    )
    conn.commit()
    yield conn
    conn.close()


class FakeProc:
    def __init__(self):
        self.code: int | None = None

    def poll(self) -> int | None:
        return self.code


class FakePopen:
    def __init__(self, fail: bool = False):
        self.calls: list[tuple[list[str], dict]] = []
        self.procs: list[FakeProc] = []
        self._fail = fail

    def __call__(self, command, **kwargs) -> FakeProc:
        if self._fail:
            raise OSError("起動できない")
        self.calls.append((command, kwargs))
        self.procs.append(FakeProc())
        return self.procs[-1]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def add_question(
    conn, *, status: str = "pending", answer_message_id: int | None = None,
    kind: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO dig_questions (dig_result_id, question, question_message_id, status, "
        "kind, answer, answer_message_id) VALUES (1, '質問', 81, ?, ?, ?, ?)",
        (status, kind, None if status == "pending" else "返事", answer_message_id),
    )
    conn.commit()


def test_kick_starts_answer_in_its_own_session(tmp_path) -> None:
    popen = FakePopen()
    worker = AnswerWorker(tmp_path, popen=popen)

    worker.kick(logger)

    command, kwargs = popen.calls[0]
    assert command == [
        sys.executable,
        "-m",
        "app.cli",
        "answer",
        "--config-dir",
        str(tmp_path),
    ]
    # poller の再起動で作りかけの返事を道連れにしない・標準入力は渡さない
    assert kwargs == {"stdin": subprocess.DEVNULL, "start_new_session": True}


def test_kick_does_not_start_twice_while_running(tmp_path) -> None:
    popen = FakePopen()
    worker = AnswerWorker(tmp_path, popen=popen)

    worker.kick(logger)
    worker.kick(logger)
    assert len(popen.calls) == 1

    popen.procs[0].code = 0  # 終わったら次は起動できる
    worker.kick(logger)
    assert len(popen.calls) == 2


def test_sweep_starts_only_when_questions_are_pending(conn, tmp_path) -> None:
    popen = FakePopen()
    worker = AnswerWorker(tmp_path, popen=popen)

    worker.sweep(conn, logger)
    assert popen.calls == []

    add_question(conn)
    worker.sweep(conn, logger)
    worker.sweep(conn, logger)  # 動いている間は起動しない
    assert len(popen.calls) == 1


def test_sweep_retries_unsent_answers_with_backoff(conn, tmp_path) -> None:
    add_question(conn, status="answered")  # 作ったが送れていない
    popen, clock = FakePopen(), Clock()
    worker = AnswerWorker(tmp_path, popen=popen, clock=clock)

    worker.sweep(conn, logger)
    assert len(popen.calls) == 1
    popen.procs[0].code = 1  # 送信先の障害で終わった

    clock.now += RETRY_UNSENT_S - 1
    worker.sweep(conn, logger)
    assert len(popen.calls) == 1  # 毎周期は起動しない

    clock.now += 1
    worker.sweep(conn, logger)
    assert len(popen.calls) == 2


def test_sweep_retries_unrecorded_takes_with_backoff(conn, tmp_path) -> None:
    # 送れたが take を記録できていない(vault の障害など。GH#106)
    add_question(conn, status="answered", answer_message_id=950, kind="take")
    popen, clock = FakePopen(), Clock()
    worker = AnswerWorker(tmp_path, popen=popen, clock=clock)

    worker.sweep(conn, logger)
    assert len(popen.calls) == 1
    popen.procs[0].code = 0

    clock.now += RETRY_UNSENT_S - 1
    worker.sweep(conn, logger)
    assert len(popen.calls) == 1  # 毎周期は起動しない

    clock.now += 1
    worker.sweep(conn, logger)
    assert len(popen.calls) == 2  # 10 分たっても記録できていなければ起動し直す
    popen.procs[1].code = 0

    conn.execute("UPDATE dig_questions SET take_recorded_at = '2026-10-18T13:11:10Z'")
    clock.now += RETRY_UNSENT_S
    worker.sweep(conn, logger)
    assert len(popen.calls) == 2  # 記録できたら起動しない


def test_sent_answers_do_not_start(conn, tmp_path) -> None:
    add_question(conn, status="answered", answer_message_id=950)
    popen = FakePopen()

    AnswerWorker(tmp_path, popen=popen).sweep(conn, logger)

    assert popen.calls == []


def test_abnormal_exit_and_launch_failure_are_logged(tmp_path, caplog) -> None:
    popen = FakePopen()
    worker = AnswerWorker(tmp_path, popen=popen)
    worker.kick(logger)
    popen.procs[0].code = 2

    with caplog.at_level(logging.ERROR, logger="test-answer-worker"):
        worker.kick(logger)  # 回収時に異常終了を記録し、起動し直す
        AnswerWorker(tmp_path, popen=FakePopen(fail=True)).kick(logger)

    messages = [r.getMessage() for r in caplog.records]
    assert "intel answer が異常終了(exit=2)" in messages
    assert any("intel answer を起動できない" in m for m in messages)
    assert len(popen.calls) == 2


def test_cli_runs_as_a_module() -> None:
    # poller は `python -m app.cli answer` で起動する。モジュールとして実行できること
    result = subprocess.run(
        [sys.executable, "-m", "app.cli", "--help"], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0
    assert "answer" in result.stdout
