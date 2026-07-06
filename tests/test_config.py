from pathlib import Path

import pytest

from app.config import Config, ConfigError, load_config, load_env


def write_config(tmp_path: Path, body: str) -> Path:
    (tmp_path / "config.toml").write_text(body, encoding="utf-8")
    return tmp_path


def test_missing_file_returns_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    assert isinstance(config, Config)
    assert config.digest.max_items == 10
    assert config.retention.cache_ttl_days == 30
    assert config.retention.x_cache_ttl_days == 7
    assert config.llm.engine == "codex-cli"
    assert config.x.enabled is False
    assert config.db_path == tmp_path / "intel.db"


def test_valid_config_overrides_defaults(tmp_path: Path) -> None:
    write_config(
        tmp_path,
        """
        [digest]
        max_items = 5

        [x]
        enabled = true
        daily_read_cap = 100
        unit_price_usd = 0.001

        [llm]
        engine = "claude-cli"

        [telegram]
        owner_user_id = 12345
        """,
    )
    config = load_config(tmp_path)
    assert config.digest.max_items == 5
    assert config.x.enabled is True
    assert config.x.unit_price_usd == 0.001
    assert config.llm.engine == "claude-cli"
    assert config.telegram.owner_user_id == 12345
    assert config.telegram.owner_chat_id is None


def test_unknown_section_fails(tmp_path: Path) -> None:
    write_config(tmp_path, "[degist]\nmax_items = 5\n")
    with pytest.raises(ConfigError, match="不明なセクション"):
        load_config(tmp_path)


def test_unknown_key_fails(tmp_path: Path) -> None:
    write_config(tmp_path, "[digest]\nmax_item = 5\n")
    with pytest.raises(ConfigError, match="不明なキー"):
        load_config(tmp_path)


def test_wrong_type_fails(tmp_path: Path) -> None:
    write_config(tmp_path, '[digest]\nmax_items = "ten"\n')
    with pytest.raises(ConfigError, match="型が不正"):
        load_config(tmp_path)


def test_bool_is_not_int(tmp_path: Path) -> None:
    write_config(tmp_path, "[digest]\nmax_items = true\n")
    with pytest.raises(ConfigError, match="型が不正"):
        load_config(tmp_path)


def test_invalid_engine_fails(tmp_path: Path) -> None:
    write_config(tmp_path, '[llm]\nengine = "gpt-web"\n')
    with pytest.raises(ConfigError, match="engine"):
        load_config(tmp_path)


def test_nonpositive_value_fails(tmp_path: Path) -> None:
    write_config(tmp_path, "[digest]\nmax_items = 0\n")
    with pytest.raises(ConfigError, match="正の整数"):
        load_config(tmp_path)


def test_broken_toml_fails(tmp_path: Path) -> None:
    write_config(tmp_path, "[digest\n")
    with pytest.raises(ConfigError, match="構文エラー"):
        load_config(tmp_path)


def test_load_env_parses_and_strips_quotes(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        '# comment\nX_API_KEY=abc123\nTELEGRAM_BOT_TOKEN="tok:en"\n\n', encoding="utf-8"
    )
    env = load_env(tmp_path)
    assert env == {"X_API_KEY": "abc123", "TELEGRAM_BOT_TOKEN": "tok:en"}


def test_load_env_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_env(tmp_path) == {}


def test_load_env_rejects_malformed_line(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("JUST_A_KEY\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="KEY=VALUE"):
        load_env(tmp_path)
