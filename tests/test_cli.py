from typer.testing import CliRunner

from app.cli import app

runner = CliRunner()

ALL_COMMANDS = ("collect", "extract", "analyze", "digest", "deliver", "poll", "run", "doctor")


def test_help_lists_all_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ALL_COMMANDS:
        assert command in result.output


def test_stub_exits_nonzero() -> None:
    result = runner.invoke(app, ["digest"])
    assert result.exit_code == 1
    assert "未実装" in result.output


def test_doctor_accepts_documented_options() -> None:
    result = runner.invoke(app, ["doctor", "--purge", "--cost"])
    assert result.exit_code == 1  # スタブ。オプションが受理されることだけ確認
