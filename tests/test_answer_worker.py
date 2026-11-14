"""質問への返事(GH#126)と今すぐの深掘り(GH#388)の起動係。別プロセスは起動せず、Popen を差し替えて
確かめる。"""

import logging
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.db import apply_migrations, connect
from app.delivery.answer_worker import RETRY_UNSENT_S, AnswerWorker, DigNowWorker

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


# --- 今すぐの深掘り(GH#388) ---


def add_now_request(conn, *, started: bool = False, status: str = "queued") -> None:
    conn.execute(
        "INSERT INTO dig_queue (item_id, status, source, now_requested_at, now_started_at) "
        "VALUES (1, ?, 'request', '2026-07-22T12:00:00Z', ?)",
        (status, "2026-07-22T12:00:01Z" if started else None),
    )
    conn.commit()


def test_dig_now_worker_starts_dig_now_in_its_own_session(conn, tmp_path) -> None:
    popen = FakePopen()
    worker = DigNowWorker(tmp_path, popen=popen)
    add_now_request(conn)

    worker.sweep(conn, logger)

    [(command, kwargs)] = popen.calls
    assert command == [
        sys.executable, "-m", "app.cli", "dig-now", "--config-dir", str(tmp_path)
    ]
    assert kwargs == {"stdin": subprocess.DEVNULL, "start_new_session": True}


@pytest.mark.parametrize(
    ("started", "status", "starts"),
    [
        (False, "queued", True),  # 作っていない依頼
        (True, "queued", True),  # 前の実行が途中で止まった(子が明朝へ回して知らせる)
        (False, "passed", False),  # パスした(依頼は消えている前提だが、状態でも見ない)
        (False, "done", False),
    ],
)
def test_dig_now_worker_sweeps_pending_requests(conn, tmp_path, started, status, starts) -> None:
    popen = FakePopen()
    worker = DigNowWorker(tmp_path, popen=popen)
    add_now_request(conn, started=started, status=status)

    worker.sweep(conn, logger)

    assert len(popen.calls) == int(starts)


def test_dig_now_worker_restarts_after_child_exits_while_requests_remain(conn, tmp_path) -> None:
    # 朝バッチの dig 段の間に起動した子は何もせず終わり、依頼が残る。次の周期に起動し直す
    popen = FakePopen()
    worker = DigNowWorker(tmp_path, popen=popen)
    add_now_request(conn)

    worker.sweep(conn, logger)
    worker.sweep(conn, logger)  # 子が動いている間は起動しない
    popen.procs[0].code = 0
    worker.sweep(conn, logger)

    assert len(popen.calls) == 2


def test_dig_now_worker_retries_unsent_cards_at_interval(conn, tmp_path) -> None:
    popen = FakePopen()
    clock = Clock()
    worker = DigNowWorker(tmp_path, popen=popen, clock=clock)
    conn.execute(
        "INSERT INTO dig_results (item_id, note_name, card_text, on_demand) "
        "VALUES (1, 'now-note', 'カード', 1)"
    )
    conn.commit()

    worker.sweep(conn, logger)
    popen.procs[0].code = 0
    clock.now += RETRY_UNSENT_S - 1
    worker.sweep(conn, logger)
    clock.now += 1
    worker.sweep(conn, logger)

    assert len(popen.calls) == 2


def test_dig_now_worker_also_retries_unsent_morning_cards(conn, tmp_path) -> None:
    # 朝の段が今すぐの依頼を引き受けて作り、送れなかった分も、朝を待たずに送り直す
    popen = FakePopen()
    worker = DigNowWorker(tmp_path, popen=popen)
    conn.execute(
        "INSERT INTO dig_results (item_id, note_name, card_text) VALUES (1, 'n', 'カード')"
    )
    conn.commit()

    worker.sweep(conn, logger)

    assert len(popen.calls) == 1


def test_dig_now_worker_logs_abnormal_exit(conn, tmp_path, caplog) -> None:
    popen = FakePopen()
    worker = DigNowWorker(tmp_path, popen=popen)
    worker.kick(logger)
    popen.procs[0].code = 1

    with caplog.at_level(logging.ERROR):
        worker.sweep(conn, logger)

    assert "intel dig-now が異常終了(exit=1)" in caplog.text


# --- 出来事の調査(GH#415 の 4 本目) ---


def add_investigation(conn, *, status: str = "requested", message_id: int | None = None) -> None:
    digest = conn.execute(
        "INSERT INTO digests (digest_date, markdown, item_ids, item_count) "
        "VALUES ('2026-11-14', '# md', '[]', 0)"
    ).lastrowid
    conn.execute(
        "INSERT INTO news_investigations (digest_id, event_index, label, summary, item_ids, "
        "status, note_name, card_text, message_id) VALUES (?, 1, 'l', 's', '[1]', ?, ?, ?, ?)",
        (
            digest,
            status,
            "n" if status == "done" else None,
            "c" if status == "done" else None,
            message_id,
        ),
    )
    conn.commit()


@pytest.mark.parametrize(
    ("status", "message_id", "starts"),
    [
        ("requested", None, True),  # 作っていない依頼
        ("running", None, True),  # 作っている途中か、前の実行が途中で止まった(子が閉じて知らせる)
        ("done", None, True),  # 作り終えて送れていない(送り直す)
        ("done", 42, False),  # 送った
        ("failed", None, False),
    ],
)
def test_dig_now_worker_sweeps_investigations(conn, tmp_path, status, message_id, starts) -> None:
    popen = FakePopen()
    worker = DigNowWorker(tmp_path, popen=popen)
    add_investigation(conn, status=status, message_id=message_id)

    worker.sweep(conn, logger)

    assert len(popen.calls) == int(starts)
