import os
import sqlite3
import tarfile
from pathlib import Path

import pytest

from app.db import apply_migrations, connect
from app.doctor import CanaryProbe, CheckResult, has_failure, month_cost_summary, run_doctor


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """全項目 PASS/SKIP になるベースライン構成。"""
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "config.toml").write_text(
        "\n".join([
            "[telegram]",
            "owner_user_id = 10",
            "owner_chat_id = 77",
            "[storage]",
            f'db_path = "{tmp_path}/intel.db"',
            "[obsidian]",
            f'vault_path = "{tmp_path}/vault"',
        ]),
        encoding="utf-8",
    )
    env_path = conf / ".env"
    env_path.write_text("TELEGRAM_BOT_TOKEN=tok\n", encoding="utf-8")
    env_path.chmod(0o600)
    conn = connect(tmp_path / "intel.db")
    apply_migrations(conn, tmp_path / "intel.db")
    conn.close()
    return conf


class FakeBotApi:
    def __init__(self, env: dict[str, str]):
        assert env["TELEGRAM_BOT_TOKEN"] == "tok"

    def get_me(self) -> dict:
        return {"username": "intel_bot"}


class SafeEngine:
    """隔離が効いている(canary を読めない)エンジン。"""

    def complete_structured(self, prompt, schema, timeout_s=120):
        return CanaryProbe(content="unreadable")


class LeakyEngine:
    """隔離が破れている(~ が実 HOME に解決される)エンジンの再現。"""

    def __init__(self, home: Path):
        self._home = home

    def complete_structured(self, prompt, schema, timeout_s=120):
        for word in prompt.split():
            if word.startswith("~/"):
                return CanaryProbe(
                    content=(self._home / word[2:]).read_text(encoding="utf-8")
                )
        return CanaryProbe(content="unreadable")


def doctor(config_dir: Path, tmp_path: Path, **overrides) -> list[CheckResult]:
    kwargs = {
        "api_factory": FakeBotApi,
        "engine_factory": lambda name: SafeEngine(),
        "launchctl": lambda job: True,
        "environ": {},
        "backup_dir": tmp_path / "backups",
        "home": tmp_path / "home",
    }
    (tmp_path / "home").mkdir(exist_ok=True)
    kwargs.update(overrides)
    return run_doctor(config_dir, **kwargs)


def by_number(results: list[CheckResult], number: int) -> CheckResult:
    return next(r for r in results if r.number == number)


def make_fresh_backup(tmp_path: Path) -> Path:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(exist_ok=True)
    archive = backup_dir / "intel-20260707.tar.gz"
    with tarfile.open(archive, "w:gz"):
        pass
    archive.chmod(0o600)
    return backup_dir


def test_baseline_has_no_failure(config_dir, tmp_path) -> None:
    make_fresh_backup(tmp_path)
    results = doctor(config_dir, tmp_path)
    assert len(results) == 12
    assert not has_failure(results)
    statuses = {r.number: r.status for r in results}
    assert statuses[1] == "PASS"
    assert statuses[4] == "PASS"
    assert statuses[5] == "SKIP"  # X 無効(縮退運用が恒久 FAIL にならない)
    assert statuses[6] == "PASS"
    assert statuses[12] == "PASS"


def test_missing_config_is_fail_but_all_checks_run(tmp_path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    results = doctor(empty, tmp_path)
    assert by_number(results, 1).status == "FAIL"
    assert len(results) == 12  # 1つの失敗で残りを止めない
    assert has_failure(results)


def test_env_permission_and_missing_token_fail(config_dir, tmp_path) -> None:
    (config_dir / ".env").chmod(0o644)
    assert by_number(doctor(config_dir, tmp_path), 2).status == "FAIL"
    (config_dir / ".env").write_text("OTHER=1\n", encoding="utf-8")
    (config_dir / ".env").chmod(0o600)
    result = by_number(doctor(config_dir, tmp_path), 2)
    assert result.status == "FAIL"
    assert "TELEGRAM_BOT_TOKEN" in result.detail


def test_pending_migration_fails(config_dir, tmp_path) -> None:
    # 台帳を1件消して「未適用」状態を作る
    conn = sqlite3.connect(tmp_path / "intel.db")
    conn.execute("DELETE FROM schema_migrations")
    conn.commit()
    conn.close()
    result = by_number(doctor(config_dir, tmp_path), 3)
    assert result.status == "FAIL"
    assert "未適用" in result.detail


def test_telegram_allowlist_missing_fails_without_api_call(config_dir, tmp_path) -> None:
    (config_dir / "config.toml").write_text(
        f'[storage]\ndb_path = "{tmp_path}/intel.db"\n', encoding="utf-8"
    )

    def exploding_factory(env):
        raise AssertionError("allowlist 未設定なら getMe を呼ばない")

    result = by_number(doctor(config_dir, tmp_path, api_factory=exploding_factory), 4)
    assert result.status == "FAIL"
    assert "allowlist" in result.detail


def test_x_disabled_with_sources_is_warn(config_dir, tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "intel.db")
    conn.execute("INSERT INTO sources (type, name, enabled) VALUES ('x', 'home-tl', 0)")
    conn.commit()
    conn.close()
    result = by_number(doctor(config_dir, tmp_path), 5)
    assert result.status == "WARN"
    assert "無効化中" in result.detail


def test_x_enabled_requires_keys(config_dir, tmp_path) -> None:
    config_path = config_dir / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "\n[x]\nenabled = true\n", encoding="utf-8"
    )
    result = by_number(doctor(config_dir, tmp_path), 5)
    assert result.status == "FAIL"
    assert "X_API_KEY" in result.detail

    env_path = config_dir / ".env"
    env_path.write_text(
        "TELEGRAM_BOT_TOKEN=tok\nX_API_KEY=a\nX_API_SECRET=b\n"
        "X_ACCESS_TOKEN=c\nX_ACCESS_TOKEN_SECRET=d\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)
    result = by_number(doctor(config_dir, tmp_path), 5)
    assert result.status == "WARN"  # 疎通は plan #15


def test_llm_canary_leak_fails_and_cleans_up(config_dir, tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    results = doctor(
        config_dir, tmp_path, engine_factory=lambda name: LeakyEngine(home), home=home
    )
    result = by_number(results, 6)
    assert result.status == "FAIL"
    assert "隔離" in result.detail
    assert list(home.glob(".intel-doctor-*")) == []  # canary は後始末される


def test_llm_engine_error_fails(config_dir, tmp_path) -> None:
    class BrokenEngine:
        def complete_structured(self, prompt, schema, timeout_s=120):
            raise RuntimeError("codex 起動失敗")

    result = by_number(
        doctor(config_dir, tmp_path, engine_factory=lambda name: BrokenEngine()), 6
    )
    assert result.status == "FAIL"


def test_vault_unset_fails(config_dir, tmp_path) -> None:
    (config_dir / "config.toml").write_text(
        "[telegram]\nowner_user_id = 10\nowner_chat_id = 77\n"
        f'[storage]\ndb_path = "{tmp_path}/intel.db"\n',
        encoding="utf-8",
    )
    result = by_number(doctor(config_dir, tmp_path), 7)
    assert result.status == "FAIL"
    assert "H4" in result.detail


def test_launchd_not_loaded_is_warn(config_dir, tmp_path) -> None:
    result = by_number(doctor(config_dir, tmp_path, launchctl=lambda job: False), 8)
    assert result.status == "WARN"
    assert "com.intel.morning" in result.detail


def test_cost_over_pace_is_warn(config_dir, tmp_path) -> None:
    conn = connect(tmp_path / "intel.db")
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed')")
    item = conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, content_hash) "
        "VALUES (2, 'https://a', 'https://a', 'h')"
    ).lastrowid
    conn.execute(
        "INSERT INTO analyses (item_id, summary, category, relevance_score, novelty, "
        "recommendation_reason, implication, engine, model, prompt_version, input_hash, "
        "estimated_cost_usd) "
        "VALUES (?, 's', 'c', 50, 'new', 'r', 'i', 'codex-cli', 'm', 'v1', 'h', 100.0)",
        (item,),
    )
    conn.close()
    result = by_number(doctor(config_dir, tmp_path), 9)
    assert result.status == "WARN"
    assert "超過ペース" in result.detail


def test_backup_missing_and_stale_are_warn(config_dir, tmp_path) -> None:
    assert by_number(doctor(config_dir, tmp_path), 10).status == "WARN"  # 世代なし

    backup_dir = make_fresh_backup(tmp_path)
    assert by_number(doctor(config_dir, tmp_path), 10).status == "PASS"

    old = backup_dir / "intel-20260707.tar.gz"
    stale = 60 * 60 * 72  # 72h 前
    os.utime(old, (old.stat().st_atime - stale, old.stat().st_mtime - stale))
    result = by_number(doctor(config_dir, tmp_path), 10)
    assert result.status == "WARN"
    assert "48h 超" in result.detail


def test_backup_permission_warn(config_dir, tmp_path) -> None:
    backup_dir = make_fresh_backup(tmp_path)
    (backup_dir / "intel-20260707.tar.gz").chmod(0o644)
    result = by_number(doctor(config_dir, tmp_path), 10)
    assert result.status == "WARN"
    assert "パーミッション" in result.detail


def test_ttl_leftover_warn(config_dir, tmp_path) -> None:
    conn = connect(tmp_path / "intel.db")
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed')")
    item = conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, content_hash) "
        "VALUES (2, 'https://a', 'https://a', 'h')"
    ).lastrowid
    conn.execute(
        "INSERT INTO documents (item_id, extraction_status, expires_at) "
        "VALUES (?, 'ok', '2020-01-01T00:00:00Z')",
        (item,),
    )
    conn.close()
    result = by_number(doctor(config_dir, tmp_path), 11)
    assert result.status == "WARN"
    assert "documents 1 件" in result.detail


def test_anthropic_key_in_env_is_warn(config_dir, tmp_path) -> None:
    result = by_number(
        doctor(config_dir, tmp_path, environ={"ANTHROPIC_API_KEY": "sk"}), 12
    )
    assert result.status == "WARN"


def test_month_cost_summary_breakdown(config_dir, tmp_path) -> None:
    conn = connect(tmp_path / "intel.db")
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed')")
    conn.execute(
        "INSERT INTO collect_runs (source_id, status, estimated_cost_usd) "
        "VALUES (2, 'ok', 1.5)"
    )
    from app.config import load_config

    summary = month_cost_summary(conn, load_config(config_dir))
    conn.close()
    assert summary["x_usd"] == 1.5
    assert summary["spent_usd"] == 1.5
    assert summary["projected_usd"] >= 1.5
    assert summary["cap_usd"] == 30.0
