from pathlib import Path

import pytest
from typer.testing import CliRunner

from app.cli import app

runner = CliRunner()

ALL_COMMANDS = (
    "collect", "extract", "analyze", "digest", "deliver", "dig", "answer", "poll", "run",
    "doctor", "x-auth", "x-signals", "x-insights",
)


def test_help_lists_all_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ALL_COMMANDS:
        assert command in result.output


def test_doctor_accepts_documented_options(tmp_path, monkeypatch) -> None:
    # --purge/--cost が受理され、隔離した config_dir + 空 DB で完走する
    from app import logs

    monkeypatch.setattr(logs, "DEFAULT_LOG_DIR", tmp_path / "logs")  # 本物のログに書かない(GH#207)
    (tmp_path / "config.toml").write_text(
        f'[storage]\ndb_path = "{tmp_path}/intel.db"\n', encoding="utf-8"
    )
    result = runner.invoke(
        app, ["doctor", "--config-dir", str(tmp_path), "--purge", "--cost"]
    )
    assert result.exit_code == 0
    assert "purge 完了" in result.output
    assert "text_excerpt 0 件 NULL 化" in result.output  # doctor #11 の WARN と同じ単位(GH#250)
    assert "着地見込み" in result.output
    assert (tmp_path / "logs" / "morning.log").exists()  # purge の記録は隔離先に書かれる


def _answer_config(tmp_path, *, telegram: bool) -> None:
    lines = [f'[storage]\ndb_path = "{tmp_path}/intel.db"\n']
    if telegram:
        lines.append("[telegram]\nowner_user_id = 1\nowner_chat_id = 10\n")
        env = tmp_path / ".env"
        env.write_text("TELEGRAM_BOT_TOKEN=test-token\n", encoding="utf-8")
        env.chmod(0o600)  # group/other に開いた .env は読まない(設計 00 §4)
    (tmp_path / "config.toml").write_text("".join(lines), encoding="utf-8")


def test_answer_requires_telegram(tmp_path) -> None:
    _answer_config(tmp_path, telegram=False)
    result = runner.invoke(app, ["answer", "--config-dir", str(tmp_path)])
    assert result.exit_code == 1


def test_answer_exits_quietly_while_another_runs(tmp_path) -> None:
    # 同時に 2 つ走ると同じ質問に 2 回答えうる(GH#126)。後から来た方は DB にも触れない
    import fcntl

    _answer_config(tmp_path, telegram=True)
    with (tmp_path / "answer.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = runner.invoke(app, ["answer", "--config-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "別の answer が実行中" in result.output
    assert not (tmp_path / "intel.db").exists()


def test_dig_exits_quietly_while_another_digs(tmp_path, monkeypatch) -> None:
    # 朝バッチの dig 段が進めている間の intel dig は、同じ候補を 2 度掘らずに理由を出して終わる
    # (GH#196)
    import fcntl

    from app import logs

    monkeypatch.setattr(logs, "DEFAULT_LOG_DIR", tmp_path / "logs")  # 本物のログに書かない
    _answer_config(tmp_path, telegram=True)
    with (tmp_path / "intel.db.dig.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = runner.invoke(app, ["dig", "--config-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "深掘りを進めているため、何もしなかった" in result.output


def _unsent_take(tmp_path, monkeypatch) -> Path:
    """返事が未送信の take が 1 件ある DB と vault を用意し、vault の置き場を返す。"""
    from app import logs
    from app.db import apply_migrations, connect

    monkeypatch.setattr(logs, "DEFAULT_LOG_DIR", tmp_path / "logs")  # 本物のログに書かない
    _answer_config(tmp_path, telegram=True)
    vault = tmp_path / "vault"
    (vault / "SignalForge" / "DeepDive").mkdir(parents=True)
    (vault / "SignalForge" / "DeepDive" / "note.md").write_text(
        "---\nstatus: take待ち\n---\n本文\n", encoding="utf-8"
    )
    with (tmp_path / "config.toml").open("a", encoding="utf-8") as f:
        f.write(f'[obsidian]\nvault_path = "{vault}"\n')
    conn = connect(tmp_path / "intel.db")
    apply_migrations(conn, tmp_path / "intel.db")
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed')")
    conn.execute(
        "INSERT INTO items (id, source_id, url, canonical_url, content_hash) "
        "VALUES (1, 2, 'https://a/1', 'https://a/1', 'sha256:x')"
    )
    conn.execute(
        "INSERT INTO dig_results (id, item_id, note_name, card_text, message_id) "
        "VALUES (1, 1, 'note', 'カード', 700)"
    )
    conn.execute(
        "INSERT INTO dig_questions (dig_result_id, question, question_message_id, status, kind, "
        "answer) VALUES (1, '結論はこう', 81, 'answered', 'take', '返事')"  # 返事は未送信
    )
    conn.close()
    return vault


def test_answer_records_takes_after_replies(tmp_path, monkeypatch) -> None:
    # 返事を送った後に take を記録する(GH#106)。送る時点では未記録で、同じ実行の終わりに記録済み
    import app.delivery as delivery
    from app.db import connect

    vault = _unsent_take(tmp_path, monkeypatch)
    recorded_at_send: list = []

    class Api:
        def send_message(self, chat_id, text, *, reply_to_message_id=None):
            check = connect(tmp_path / "intel.db")
            recorded_at_send.append(
                check.execute("SELECT take_recorded_at FROM dig_questions").fetchone()[0]
            )
            check.close()
            return 82

    monkeypatch.setattr(delivery, "create_api", lambda env: Api())

    result = runner.invoke(app, ["answer", "--config-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert recorded_at_send == [None]  # 送った時点ではまだ記録していない
    assert "送信 1 通・take の記録 1 件" in result.output
    text = (vault / "SignalForge" / "DeepDive" / "note.md").read_text(encoding="utf-8")
    assert "結論はこう" in text


def test_answer_keeps_lock_while_stalled_vault_thread_may_write(tmp_path, monkeypatch) -> None:
    # vault が止まったら、止まったスレッドが後から追記を終えうる間(プロセスの終了まで)
    # ロックを握り、次の answer に同じ take を書かせない(GH#145)
    import fcntl
    import threading

    import app.delivery as delivery
    from app import dig_take, vault_guard

    _unsent_take(tmp_path, monkeypatch)
    release = threading.Event()
    monkeypatch.setattr(dig_take, "_append_to_note", lambda config, row: release.wait())
    real_guard = vault_guard.VaultGuard
    monkeypatch.setattr(vault_guard, "VaultGuard", lambda: real_guard(timeout_s=0.05))

    class Api:
        def send_message(self, chat_id, text, *, reply_to_message_id=None):
            return 82

    monkeypatch.setattr(delivery, "create_api", lambda env: Api())
    try:
        result = runner.invoke(app, ["answer", "--config-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "送信 1 通・take の記録 0 件" in result.output
        # このテストのプロセスが終わるまでロックは外れない(複製した fd は閉じない)
        with (tmp_path / "answer.lock").open("w") as other, pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        release.set()


def test_answer_keeps_lock_when_interrupted_while_vault_thread_lives(tmp_path, monkeypatch) -> None:
    # 待っている途中で中断(KeyboardInterrupt など)されても、ロックは外さない(GH#145 round 2)
    import fcntl
    import threading

    import app.delivery as delivery
    from app import dig_take, vault_guard

    _unsent_take(tmp_path, monkeypatch)
    release = threading.Event()
    monkeypatch.setattr(dig_take, "_append_to_note", lambda config, row: release.wait())

    class InterruptedGuard(vault_guard.VaultGuard):
        def call(self, fn):
            threading.Thread(target=fn, daemon=True).start()  # 書き込み直前で待つスレッド
            raise KeyboardInterrupt

    monkeypatch.setattr(vault_guard, "VaultGuard", InterruptedGuard)

    class Api:
        def send_message(self, chat_id, text, *, reply_to_message_id=None):
            return 82

    monkeypatch.setattr(delivery, "create_api", lambda env: Api())
    try:
        result = runner.invoke(app, ["answer", "--config-dir", str(tmp_path)])
        assert result.exit_code == 130  # 中断で終わる(typer は SIGINT と同じ 130 で抜ける)
        with (tmp_path / "answer.lock").open("w") as other, pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        release.set()


class _StopAtFactory(Exception):
    pass


@pytest.mark.parametrize(
    ("command", "stage"),
    [("analyze", "analyze"), ("digest", "trend"), ("dig", "dig"), ("answer", "answer")],
)
def test_commands_build_engines_through_fallback(tmp_path, monkeypatch, command, stage) -> None:
    # 主のエンジンを使う各コマンドは、段の名前で予備への切り替え(GH#156)を通して作る
    from app import logs
    from app.analysis import fallback

    monkeypatch.setattr(logs, "DEFAULT_LOG_DIR", tmp_path / "logs")  # 本物のログに書かない
    _answer_config(tmp_path, telegram=True)
    (tmp_path / "interest-profile.md").write_text(
        "---\nupdated: 2026-10-20\n---\n# 関心\n", encoding="utf-8"
    )
    # spy の中の assert は digest の except に吸われるので、記録して後で確かめる
    seen: list[tuple[str, dict]] = []

    def spy(conn, config, env, logger, *, stage, run_id=None, primary=None):
        seen.append((stage, dict(env)))
        raise _StopAtFactory

    monkeypatch.setattr(fallback, "fallback_engine_factory", spy)
    result = runner.invoke(app, [command, "--config-dir", str(tmp_path)])
    assert [name for name, _ in seen] == [stage]
    assert seen[0][1].get("TELEGRAM_BOT_TOKEN") == "test-token"  # .env の dict を渡す
    if command != "digest":  # digest はトレンドの失敗を握って本体を続ける(付加機能)
        assert isinstance(result.exception, _StopAtFactory)


def test_run_notifies_fallback_after_pipeline(tmp_path, monkeypatch) -> None:
    # 朝バッチは、段が成功していても予備の利用(GH#156)を知らせる経路を通る
    from app import logs, run

    monkeypatch.setattr(logs, "DEFAULT_LOG_DIR", tmp_path / "logs")
    _answer_config(tmp_path, telegram=True)
    calls: list[str] = []
    monkeypatch.setattr(run, "run_pipeline", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        run, "notify_llm_fallback", lambda conn, config, env, run_id, logger: calls.append(run_id)
    )
    result = runner.invoke(app, ["run", "--config-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1 and calls[0]


@pytest.mark.parametrize("digest_held", [True, False])
def test_run_failure_notice_tells_whether_digest_was_held(
    tmp_path, monkeypatch, digest_held
) -> None:
    # digest 段が保留した事実(run_pipeline の戻り値)を失敗通知へ渡す(GH#191)
    from app import delivery, logs, run

    monkeypatch.setattr(logs, "DEFAULT_LOG_DIR", tmp_path / "logs")
    _answer_config(tmp_path, telegram=True)
    failures = run.RunFailures(["analyze"])
    failures.digest_held = digest_held
    passed: list[bool] = []
    monkeypatch.setattr(run, "run_pipeline", lambda *args, **kwargs: failures)
    monkeypatch.setattr(run, "notify_llm_fallback", lambda *args: None)
    monkeypatch.setattr(
        delivery, "notify_run_failure", lambda *args, digest_held: passed.append(digest_held)
    )
    result = runner.invoke(app, ["run", "--config-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert passed == [digest_held]
