from typer.testing import CliRunner

from app.cli import app

runner = CliRunner()

ALL_COMMANDS = (
    "collect", "extract", "analyze", "digest", "deliver", "poll", "run", "doctor",
    "x-auth", "x-signals",
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
