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


def test_doctor_accepts_documented_options(tmp_path) -> None:
    # --purge/--cost が受理され、隔離した config_dir + 空 DB で完走する
    (tmp_path / "config.toml").write_text(
        f'[storage]\ndb_path = "{tmp_path}/intel.db"\n', encoding="utf-8"
    )
    result = runner.invoke(
        app, ["doctor", "--config-dir", str(tmp_path), "--purge", "--cost"]
    )
    assert result.exit_code == 0
    assert "purge 完了" in result.output
    assert "着地見込み" in result.output


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


def test_answer_records_takes_after_replies(tmp_path, monkeypatch) -> None:
    # 返事を送った後に take を記録する(GH#106)。送る時点では未記録で、同じ実行の終わりに記録済み
    import app.delivery as delivery
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
