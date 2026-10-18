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
