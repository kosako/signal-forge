import json
import os
import re
import sqlite3
import tarfile
import threading
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.connectors.base import FatalError, RetryableError
from app.connectors.x import XApiSession, XCredentials
from app.db import MigrationError, apply_migrations, connect
from app.doctor import (
    CanaryProbe,
    CheckResult,
    _users_me_username,
    has_failure,
    month_cost_summary,
    run_doctor,
)
from app.vault_guard import VaultGuard


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
            f'backup_dir = "{tmp_path}/backups"',
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
    """隔離が効いている(canary を読めず・ネットワークに出ない)エンジン。"""

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


class FakeServer:
    """ローカル canary サーバの偽物。prompt の URL を fetch されたら hit を立てる。"""

    def __init__(self):
        self.url = "http://127.0.0.1:0/canary"
        self.hit = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def was_hit(self):
        return self.hit


def network_leaky_factory(server_holder: dict):
    """FakeServer を hit させるエンジン factory(server は server_holder 経由で共有)。"""

    class _Engine:
        def complete_structured(self, prompt, schema, timeout_s=120):
            if server_holder.get("server") is not None and "http://" in prompt:
                server_holder["server"].hit = True
            return CanaryProbe(content="unreadable")

    return lambda name: _Engine()


def doctor(config_dir: Path, tmp_path: Path, **overrides) -> list[CheckResult]:
    kwargs = {
        "api_factory": FakeBotApi,
        "engine_factory": lambda name: SafeEngine(),
        # 既定は偽物(実 codex 起動・実 HTTP サーバを避けて高速化)。
        # 実サーバ経路は test_llm_network_probe_uses_real_local_server(到達なし → PASS)と
        # test_llm_network_probe_detects_real_local_hit(到達あり → FAIL)が明示的に検証
        "codex_home_engine_factory": lambda codex_home: SafeEngine(),
        "server_factory": FakeServer,
        "launchctl": lambda job: True,
        "environ": {},
        "home": tmp_path / "home",
        "x_oauth1_probe": lambda env: "intel_probe",  # 実 users/me を呼ばない(GH#63)
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
    assert len(results) == 14
    assert not has_failure(results)
    statuses = {r.number: r.status for r in results}
    assert statuses[1] == "PASS"
    assert statuses[4] == "PASS"
    assert statuses[5] == "SKIP"  # X 無効(縮退運用が恒久 FAIL にならない)
    assert statuses[6] == "PASS"
    assert statuses[12] == "PASS"
    assert statuses[13] == "SKIP"  # OAuth2 未認可(S1 前は恒久 FAIL にしない)
    assert statuses[14] == "PASS"


def test_missing_config_is_fail_but_all_checks_run(tmp_path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    results = doctor(empty, tmp_path)
    assert by_number(results, 1).status == "FAIL"
    assert len(results) == 14  # 1つの失敗で残りを止めない
    assert has_failure(results)
    # config 依存項目は既定 Config で別 DB を診断せず「検査不可」に倒す
    assert "検査不可" in by_number(results, 3).detail
    assert "検査不可" in by_number(results, 4).detail
    assert by_number(results, 9).status == "WARN"
    # config 非依存の検査は通常どおり動く
    assert by_number(results, 8).status == "PASS"
    assert by_number(results, 12).status == "PASS"


def test_broken_config_toml_does_not_probe_default_db(tmp_path) -> None:
    conf = tmp_path / "broken"
    conf.mkdir()
    (conf / "config.toml").write_text("[storage\n", encoding="utf-8")  # 構文エラー
    results = doctor(conf, tmp_path)
    assert by_number(results, 1).status == "FAIL"
    assert "検査不可" in by_number(results, 3).detail


def test_check_exception_is_isolated_to_its_item(config_dir, tmp_path) -> None:
    def boom(job: str) -> bool:
        raise RuntimeError("launchctl 爆発")

    results = doctor(config_dir, tmp_path, launchctl=boom)
    result = by_number(results, 8)
    assert result.status == "FAIL"
    assert "例外" in result.detail
    assert len(results) == 14  # 他の検査は巻き添えにならない
    assert by_number(results, 4).status == "PASS"


def test_undecodable_config_is_fail_but_all_checks_run(config_dir, tmp_path) -> None:
    # 検証の前の読込で起きた例外(文字コード)も #1 の FAIL に留め、全体を止めない(GH#201)
    (config_dir / "config.toml").write_bytes(b"[storage]\n# \xff\n")
    results = doctor(config_dir, tmp_path)
    assert len(results) == 14
    result = by_number(results, 1)
    assert result.status == "FAIL"
    assert "UnicodeDecodeError" in result.detail
    assert "検査不可" in by_number(results, 3).detail
    assert by_number(results, 8).status == "PASS"


@pytest.mark.skipif(os.geteuid() == 0, reason="root は権限の検査を素通りする")
def test_unreadable_config_dir_is_fail_but_all_checks_run(config_dir, tmp_path) -> None:
    # 置き場を読めないと config.toml・.env の exists() 自体が PermissionError を上げる(GH#201)
    config_dir.chmod(0o000)
    try:
        results = doctor(config_dir, tmp_path)
    finally:
        config_dir.chmod(0o700)
    assert len(results) == 14
    assert "PermissionError" in by_number(results, 1).detail
    assert "PermissionError" in by_number(results, 2).detail
    assert by_number(results, 8).status == "PASS"


def test_undecodable_env_is_fail_without_its_content(config_dir, tmp_path) -> None:
    # .env の読込の例外も #2 の FAIL に留める。例外の文(読めなかったバイト)は出さない(GH#201)
    env_path = config_dir / ".env"
    env_path.write_bytes(b"TELEGRAM_BOT_TOKEN=tok\xe9secret\n")
    env_path.chmod(0o600)
    results = doctor(config_dir, tmp_path)
    assert len(results) == 14
    result = by_number(results, 2)
    assert result.status == "FAIL"
    assert result.detail == "検査中に例外: UnicodeDecodeError"
    assert by_number(results, 1).status == "PASS"
    assert by_number(results, 3).status == "PASS"


def test_db_dir_prepare_failure_is_fail_but_all_checks_run(config_dir, tmp_path) -> None:
    # DB の置き場を作れない(親がファイル)mkdir の OSError も #3 の FAIL に留める(GH#201)
    (tmp_path / "blocker").write_text("", encoding="utf-8")
    config_path = config_dir / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            f"{tmp_path}/intel.db", f"{tmp_path}/blocker/intel.db"
        ),
        encoding="utf-8",
    )
    results = doctor(config_dir, tmp_path)
    assert len(results) == 14
    result = by_number(results, 3)
    assert result.status == "FAIL"
    assert "接続失敗" in result.detail
    assert by_number(results, 1).status == "PASS"
    assert by_number(results, 4).status == "PASS"


def test_env_permission_and_missing_token_fail(config_dir, tmp_path) -> None:
    (config_dir / ".env").chmod(0o644)
    assert by_number(doctor(config_dir, tmp_path), 2).status == "FAIL"
    (config_dir / ".env").write_text("OTHER=1\n", encoding="utf-8")
    (config_dir / ".env").chmod(0o600)
    result = by_number(doctor(config_dir, tmp_path), 2)
    assert result.status == "FAIL"
    assert "TELEGRAM_BOT_TOKEN" in result.detail


def test_review_on_requires_anthropic_key(config_dir, tmp_path) -> None:
    # 深掘りのレビュー(GH#153)を使う設定ならキーが要る。無いとレビューだけが毎朝失敗する
    config_path = config_dir / "config.toml"
    base = config_path.read_text(encoding="utf-8")
    config_path.write_text(base + "\n[dig]\nreview = true\n", encoding="utf-8")
    result = by_number(doctor(config_dir, tmp_path), 2)
    assert result.status == "FAIL"
    assert "INTEL_ANTHROPIC_API_KEY" in result.detail
    (config_dir / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=tok\nINTEL_ANTHROPIC_API_KEY=k\n", encoding="utf-8"
    )
    assert by_number(doctor(config_dir, tmp_path), 2).status == "PASS"
    # 深掘り自体が無効ならキーは要らない
    (config_dir / ".env").write_text("TELEGRAM_BOT_TOKEN=tok\n", encoding="utf-8")
    config_path.write_text(
        base + "\n[dig]\nreview = true\ndaily_cap = 0\n", encoding="utf-8"
    )
    assert by_number(doctor(config_dir, tmp_path), 2).status == "PASS"


def test_llm_fallback_requires_anthropic_key(config_dir, tmp_path) -> None:
    # 予備(GH#156)を使う設定ならキーが要る。無いと主が失敗したときに予備が動かない
    config_path = config_dir / "config.toml"
    base = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        base + '\n[llm_fallback]\nengine = "anthropic-api"\n', encoding="utf-8"
    )
    result = by_number(doctor(config_dir, tmp_path), 2)
    assert result.status == "FAIL"
    assert "INTEL_ANTHROPIC_API_KEY" in result.detail and "予備" in result.detail
    (config_dir / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=tok\nINTEL_ANTHROPIC_API_KEY=k\n", encoding="utf-8"
    )
    assert by_number(doctor(config_dir, tmp_path), 2).status == "PASS"


def test_pending_migration_fails(config_dir, tmp_path) -> None:
    # 台帳を1件消して「未適用」状態を作る
    conn = sqlite3.connect(tmp_path / "intel.db")
    conn.execute("DELETE FROM schema_migrations")
    conn.commit()
    conn.close()
    result = by_number(doctor(config_dir, tmp_path), 3)
    assert result.status == "FAIL"
    assert "未適用" in result.detail


@pytest.mark.parametrize(
    "tamper",
    [
        # DB より古い checkout(ファイルに無い適用済み番号)
        "INSERT INTO schema_migrations (version, filename) VALUES (999, '999_future.sql')",
        # 適用済みファイルの改名
        "UPDATE schema_migrations SET filename = '001_renamed.sql' WHERE version = 1",
    ],
)
def test_migration_ledger_mismatch_fails(config_dir, tmp_path, tamper) -> None:
    # run・poll が起動時に拒否する台帳を、doctor も PASS にしない(GH#252)
    conn = sqlite3.connect(tmp_path / "intel.db")
    conn.execute(tamper)
    conn.commit()
    conn.close()
    result = by_number(doctor(config_dir, tmp_path), 3)
    assert result.status == "FAIL"
    assert "台帳の不整合" in result.detail
    conn = connect(tmp_path / "intel.db")
    try:
        with pytest.raises(MigrationError):
            apply_migrations(conn, tmp_path / "intel.db")
    finally:
        conn.close()


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


def test_x_enabled_without_enabled_sources_is_warn_not_fail(config_dir, tmp_path) -> None:
    # 設計 05 §2 #5: sources 全行 enabled=0(または未登録)の縮退運用を恒久 FAIL にしない
    config_path = config_dir / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "\n[x]\nenabled = true\n", encoding="utf-8"
    )
    result = by_number(doctor(config_dir, tmp_path), 5)
    assert result.status == "WARN"
    assert "未登録" in result.detail

    conn = sqlite3.connect(tmp_path / "intel.db")
    conn.execute("INSERT INTO sources (type, name, enabled) VALUES ('x', 'home-tl', 0)")
    conn.commit()
    conn.close()
    result = by_number(doctor(config_dir, tmp_path), 5)
    assert result.status == "WARN"
    assert "全行無効化中" in result.detail


def test_x_enabled_requires_keys(config_dir, tmp_path) -> None:
    config_path = config_dir / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "\n[x]\nenabled = true\n", encoding="utf-8"
    )
    conn = sqlite3.connect(tmp_path / "intel.db")
    conn.execute("INSERT INTO sources (type, name, enabled) VALUES ('x', 'home-tl', 1)")
    conn.commit()
    conn.close()
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
    assert result.status == "PASS"
    assert "@intel_probe" in result.detail


def _write_x_env(config_dir: Path) -> None:
    env_path = config_dir / ".env"
    env_path.write_text(
        "TELEGRAM_BOT_TOKEN=tok\nX_API_KEY=a\nX_API_SECRET=b\n"
        "X_ACCESS_TOKEN=c\nX_ACCESS_TOKEN_SECRET=d\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)


def _enable_x(config_dir: Path, tmp_path: Path) -> None:
    """[x] enabled + キー + enabled なホームTL 行(疎通検査まで到達する構成)。"""
    config_path = config_dir / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "\n[x]\nenabled = true\n", encoding="utf-8"
    )
    _write_x_env(config_dir)
    conn = sqlite3.connect(tmp_path / "intel.db")
    conn.execute("INSERT INTO sources (type, name, enabled) VALUES ('x', 'home-tl', 1)")
    conn.commit()
    conn.close()


def test_x_probe_auth_failure_is_fail(config_dir, tmp_path) -> None:
    # 設計 05 §2 #5 / GH#63: 401/403(FatalError)はキー・権限の恒久異常 → FAIL
    _enable_x(config_dir, tmp_path)

    def probe(env):
        assert env["X_API_KEY"] == "a"  # .env のキーが probe に渡ること
        raise FatalError("X 認証失敗(users/me): HTTP 401(キー・権限を確認)")

    result = by_number(doctor(config_dir, tmp_path, x_oauth1_probe=probe), 5)
    assert result.status == "FAIL"
    assert "認証失敗" in result.detail


def test_x_probe_transient_error_is_warn(config_dir, tmp_path) -> None:
    # GH#63: 429・5xx・transport(RetryableError)は一時異常 → WARN(恒久 FAIL にしない)
    _enable_x(config_dir, tmp_path)

    def probe(env):
        raise RetryableError("users/me が HTTP 429")

    result = by_number(doctor(config_dir, tmp_path, x_oauth1_probe=probe), 5)
    assert result.status == "WARN"
    assert "一時エラー" in result.detail


def test_x_probe_success_is_pass(config_dir, tmp_path) -> None:
    _enable_x(config_dir, tmp_path)
    result = by_number(doctor(config_dir, tmp_path, x_oauth1_probe=lambda env: "aki"), 5)
    assert result.status == "PASS"
    assert "users/me OK(@aki)" in result.detail


def test_x_checks_work_before_migration_016(config_dir, tmp_path) -> None:
    # doctor は migration を当てない。sources.moved_url(016)の無い DB でも X の検査を続ける
    _enable_x(config_dir, tmp_path)
    conn = sqlite3.connect(tmp_path / "intel.db")
    conn.execute("ALTER TABLE sources DROP COLUMN moved_url")
    conn.execute("DELETE FROM schema_migrations WHERE version = 16")
    conn.commit()
    conn.close()
    results = doctor(config_dir, tmp_path, x_oauth1_probe=lambda env: "aki")
    assert by_number(results, 3).status == "FAIL"  # 未適用は未適用として知らせる
    assert by_number(results, 5).status == "PASS"
    assert "users/me OK(@aki)" in by_number(results, 5).detail


def _probe_session(status: int, body: dict) -> XApiSession:
    """既定 probe の判定写像(_users_me_username)を実 HTTP なしで検証するセッション。"""
    return XApiSession(
        client=httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body))
        ),
        credentials=XCredentials(
            api_key="k", api_secret="s", access_token="t", access_token_secret="ts"
        ),
        sleep=lambda _s: None,
    )


def test_users_me_mapping_success_and_auth_failure() -> None:
    username = _users_me_username(_probe_session(200, {"data": {"id": "1", "username": "aki"}}))
    assert username == "aki"
    with pytest.raises(FatalError, match="認証失敗"):
        _users_me_username(_probe_session(401, {}))


def test_users_me_mapping_permanent_anomaly_is_fatal_not_warn() -> None:
    # Codex must(PR #86): HTTP 400 等の恒久異常・username 欠損を WARN に埋めない
    with pytest.raises(FatalError, match="400"):
        _users_me_username(_probe_session(400, {}))
    with pytest.raises(FatalError, match="username がない"):
        _users_me_username(_probe_session(200, {"data": {"id": "1"}}))


def test_users_me_mapping_transient_is_retryable() -> None:
    with pytest.raises(RetryableError, match="429"):
        _users_me_username(_probe_session(429, {}))
    with pytest.raises(RetryableError, match="503"):
        _users_me_username(_probe_session(503, {}))


def test_x_probe_not_called_when_keys_missing_or_disabled(config_dir, tmp_path) -> None:
    def exploding(env):
        raise AssertionError("キー欠落・X 無効時は users/me を呼ばない")

    # X 無効(既定 config)→ SKIP で probe 不発
    result = by_number(doctor(config_dir, tmp_path, x_oauth1_probe=exploding), 5)
    assert result.status == "SKIP"

    # X 有効だがキー欠落 → FAIL(キー名)で probe 不発
    config_path = config_dir / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "\n[x]\nenabled = true\n", encoding="utf-8"
    )
    conn = sqlite3.connect(tmp_path / "intel.db")
    conn.execute("INSERT INTO sources (type, name, enabled) VALUES ('x', 'home-tl', 1)")
    conn.commit()
    conn.close()
    result = by_number(doctor(config_dir, tmp_path, x_oauth1_probe=exploding), 5)
    assert result.status == "FAIL"
    assert "X_API_KEY" in result.detail


def test_x_search_slots_without_search_row_warns(config_dir, tmp_path) -> None:
    config_path = config_dir / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '\n[x]\nenabled = true\n[x_search]\nslots = ["ai"]\n',
        encoding="utf-8",
    )
    _write_x_env(config_dir)
    conn = sqlite3.connect(tmp_path / "intel.db")
    conn.execute("INSERT INTO sources (type, name, enabled) VALUES ('x', 'home-tl', 1)")
    conn.commit()

    # slots 設定済みだが mode=search 行が無い → 検索が黙って収集されないことを可視化
    result = by_number(doctor(config_dir, tmp_path), 5)
    assert result.status == "WARN"
    assert "mode=search のソース行が無い" in result.detail

    # search 行を登録すると整合し、疎通 OK なら PASS
    conn.execute(
        "INSERT INTO sources (type, name, config, enabled) "
        """VALUES ('x', 'x:search', '{"mode": "search"}', 1)"""
    )
    conn.commit()
    result = by_number(doctor(config_dir, tmp_path), 5)
    assert result.status == "PASS"
    assert "users/me OK" in result.detail

    # 同一 mode の複数行は二重取得(二重課金)として WARN
    conn.execute("INSERT INTO sources (type, name, enabled) VALUES ('x', 'home-2', 1)")
    conn.commit()
    result = by_number(doctor(config_dir, tmp_path), 5)
    assert "二重取得" in result.detail
    conn.close()


def test_x_search_row_without_slots_warns(config_dir, tmp_path) -> None:
    config_path = config_dir / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "\n[x]\nenabled = true\n", encoding="utf-8"
    )
    _write_x_env(config_dir)
    conn = sqlite3.connect(tmp_path / "intel.db")
    conn.execute(
        "INSERT INTO sources (type, name, config, enabled) "
        """VALUES ('x', 'x:search', '{"mode": "search"}', 1)"""
    )
    conn.execute(
        "INSERT INTO sources (type, name, config, enabled) "
        """VALUES ('x', 'x:bad', '{"mode": "serach"}', 1)"""
    )
    conn.commit()
    conn.close()
    result = by_number(doctor(config_dir, tmp_path), 5)
    assert result.status == "WARN"
    assert "[x_search].slots 未設定" in result.detail  # collect で失敗し続ける構成
    assert "config(mode)が不正" in result.detail  # タイポ行も名指しで可視化


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


def test_llm_all_probes_pass_reports_probe_names(config_dir, tmp_path) -> None:
    result = by_number(doctor(config_dir, tmp_path), 6)
    assert result.status == "PASS"
    # 3プローブ(HOME・ネットワーク・CODEX_HOME)を実施したことが detail に出る
    assert "HOME" in result.detail
    assert "ネットワーク" in result.detail
    assert "CODEX_HOME" in result.detail


def test_llm_network_leak_fails(config_dir, tmp_path) -> None:
    # ローカル canary サーバへ到達するエンジン → ネットワーク遮断が無効と判定
    holder: dict = {}

    def server_factory():
        holder["server"] = FakeServer()
        return holder["server"]

    result = by_number(
        doctor(
            config_dir, tmp_path,
            server_factory=server_factory,
            engine_factory=network_leaky_factory(holder),
        ),
        6,
    )
    assert result.status == "FAIL"
    assert "ネットワーク" in result.detail


def test_llm_network_probe_uses_real_local_server(config_dir, tmp_path) -> None:
    # 既定の server_factory(実ローカルサーバ)経由でも SafeEngine は到達せず PASS
    result = by_number(
        doctor(config_dir, tmp_path, server_factory=None), 6  # None → 実サーバ
    )
    assert result.status == "PASS"


def test_llm_network_probe_detects_real_local_hit(config_dir, tmp_path) -> None:
    # 陽性対照(GH#209): 既定の server_factory(127.0.0.1 の空きポートの実ローカルサーバ)へ
    # 実際に HTTP GET するエンジン → ハンドラの到達記録から was_hit() までの観測経路で FAIL。
    # 上の「到達しなければ PASS」と組で、到達の判定が実サーバで働くことを示す
    fetched: list[int] = []

    class FetchingEngine:
        def complete_structured(self, prompt, schema, timeout_s=120):
            # 接続先はプロンプト中のローカル canary サーバだけに限る(外部へは出ない)
            match = re.search(r"http://127\.0\.0\.1:\d+/\S+", prompt)
            if match is None:
                return CanaryProbe(content="unreadable")  # HOME プローブ
            # 環境の proxy 設定を使わず、ローカルサーバへ直接つなぐ
            response = httpx.get(match.group(0), timeout=5, trust_env=False)
            fetched.append(response.status_code)
            return CanaryProbe(content=response.text)

    result = by_number(
        doctor(
            config_dir, tmp_path,
            server_factory=None,  # None → 実サーバ
            engine_factory=lambda name: FetchingEngine(),
        ),
        6,
    )
    assert fetched == [200]  # 実サーバへ届き、ハンドラが応答した
    assert result.status == "FAIL"
    # 接続の例外による FAIL ではなく、到達の観測による FAIL であること
    assert "到達を観測" in result.detail


def test_llm_probe_exception_does_not_leak_output(config_dir, tmp_path) -> None:
    # プローブの例外(parse_structured は raw 出力を、codex は stderr を載せうる)は
    # detail に本文を出さず型名のみ(auth 内容の露出防止。Codex must)
    class LeakyExceptionEngine:
        def complete_structured(self, prompt, schema, timeout_s=120):
            raise RuntimeError("SECRET-AUTH-abc123 が含まれる生出力")

    result = by_number(
        doctor(config_dir, tmp_path, engine_factory=lambda name: LeakyExceptionEngine()), 6
    )
    assert result.status == "FAIL"
    assert "SECRET-AUTH" not in result.detail  # 本文は載せない
    assert "RuntimeError" in result.detail  # 型名のみ
    assert "HOME 隔離" in result.detail  # どのプローブか


def test_llm_server_closed_on_probe_exception(config_dir, tmp_path) -> None:
    # complete_structured が例外を投げても canary サーバの __exit__ が呼ばれる
    closed = {"value": False}

    class TrackingServer(FakeServer):
        def __exit__(self, *exc):
            closed["value"] = True
            return False

    class RaisingEngine:
        def complete_structured(self, prompt, schema, timeout_s=120):
            if "http://" in prompt:  # ネットワークプローブで例外
                raise RuntimeError("boom")
            return CanaryProbe(content="unreadable")

    result = by_number(
        doctor(
            config_dir, tmp_path,
            server_factory=TrackingServer,
            engine_factory=lambda name: RaisingEngine(),
        ),
        6,
    )
    assert result.status == "FAIL"
    assert closed["value"] is True  # server lifecycle は例外時も守られる


def test_llm_codex_home_leak_fails(config_dir, tmp_path) -> None:
    # CODEX_HOME 内 canary を読めるエンジン → auth 流出経路として FAIL
    class CodexHomeLeakyEngine:
        def __init__(self, codex_home):
            self._codex_home = codex_home

        def complete_structured(self, prompt, schema, timeout_s=120):
            canary = Path(self._codex_home) / "canary.txt"
            return CanaryProbe(content=canary.read_text(encoding="utf-8"))

    result = by_number(
        doctor(
            config_dir, tmp_path,
            codex_home_engine_factory=lambda ch: CodexHomeLeakyEngine(ch),
        ),
        6,
    )
    assert result.status == "FAIL"
    assert "CODEX_HOME" in result.detail


def test_llm_codex_home_probe_skipped_for_non_codex_engine(config_dir, tmp_path) -> None:
    # 非 codex-cli エンジンでは CODEX_HOME プローブを実施しない(detail に出ない)
    config_path = config_dir / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + '\n[llm]\nengine = "anthropic-api"\n',
        encoding="utf-8",
    )

    def exploding_codex_factory(codex_home):
        raise AssertionError("非 codex では CODEX_HOME プローブを呼ばない")

    result = by_number(
        doctor(config_dir, tmp_path, codex_home_engine_factory=exploding_codex_factory), 6
    )
    assert result.status == "PASS"
    assert "CODEX_HOME" not in result.detail


def test_vault_unset_fails(config_dir, tmp_path) -> None:
    (config_dir / "config.toml").write_text(
        "[telegram]\nowner_user_id = 10\nowner_chat_id = 77\n"
        f'[storage]\ndb_path = "{tmp_path}/intel.db"\n',
        encoding="utf-8",
    )
    result = by_number(doctor(config_dir, tmp_path), 7)
    assert result.status == "FAIL"
    assert "H4" in result.detail


def test_vault_folder_symlink_outside_fails_without_writing(config_dir, tmp_path) -> None:
    # 出力先のフォルダが vault の外への symlink なら、export と同じく辿って書かずに FAIL(GH#204)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "vault").mkdir()
    os.symlink(outside, tmp_path / "vault" / "SignalForge")

    result = by_number(doctor(config_dir, tmp_path), 7)

    assert result.status == "FAIL"
    assert "symlink" in result.detail
    assert list(outside.iterdir()) == []


def test_vault_stall_is_fail_and_later_checks_run(config_dir, tmp_path, monkeypatch) -> None:
    # 書込テストが応答待ちで止まっても、上限で #7 を FAIL にして残りの検査へ進む(GH#248)
    import app.doctor as doctor_module

    release, finished = threading.Event(), threading.Event()
    real_probe = doctor_module._write_and_remove_probe

    def stuck(target, probe):
        release.wait(5)  # プライバシーの確認に応答するまで止まる open() の再現
        real_probe(target, probe)
        finished.set()

    monkeypatch.setattr(doctor_module, "_write_and_remove_probe", stuck)
    try:
        results = doctor(config_dir, tmp_path, vault=VaultGuard(timeout_s=0.05))
    finally:
        release.set()
    assert len(results) == 14
    result = by_number(results, 7)
    assert result.status == "FAIL"
    assert "応答しない" in result.detail
    assert by_number(results, 8).status == "PASS"
    # 打ち切った後に書き込みが通っても、検査用のファイルは vault に残らない
    assert finished.wait(5)
    assert list((tmp_path / "vault").rglob(".doctor-write-test-*")) == []


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


def test_backup_ignores_unrelated_archives(config_dir, tmp_path) -> None:
    # 置き場にある別用途の新しい tar.gz で、世代の欠落・古さを隠さない(GH#253)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    unrelated = backup_dir / "unrelated.tar.gz"
    with tarfile.open(unrelated, "w:gz"):
        pass
    unrelated.chmod(0o600)
    result = by_number(doctor(config_dir, tmp_path), 10)
    assert result.status == "WARN"
    assert "世代" in result.detail and "なし" in result.detail

    make_fresh_backup(tmp_path)
    old = backup_dir / "intel-20260707.tar.gz"
    stale = 60 * 60 * 72  # 72h 前
    os.utime(old, (old.stat().st_atime - stale, old.stat().st_mtime - stale))
    result = by_number(doctor(config_dir, tmp_path), 10)
    assert result.status == "WARN"
    assert "48h 超" in result.detail


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


def test_ttl_leftover_counts_raw_payload_from_cached_at(config_dir, tmp_path) -> None:
    # purge と同じ起点: 書き戻した payload は cached_at から数える(GH#332)。
    # 異常な cached_at は fetched_at が新しくても TTL 超過に数える(purge が消す値と揃える)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = connect(tmp_path / "intel.db")
    conn.execute("INSERT INTO sources (type, name) VALUES ('x', 'home')")
    rows = (
        ("https://x/r", {"raw_content": "x", "cached_at": now}, "2020-01-01T00:00:00Z"),
        ("https://x/o", {"raw_content": "x"}, "2020-01-01T00:00:00Z"),
        ("https://x/b", {"raw_content": "x", "cached_at": "not-a-date"}, now),
        ("https://x/f", {"raw_content": "x", "cached_at": "2999-01-01T00:00:00Z"}, now),
        # 月末の 3 日を除けば、修飾子の無い strftime は 24 時をそのまま返す(purge のテスト)
        ("https://x/h", {"raw_content": "x", "cached_at": "2026-10-28T24:30:00Z"}, now),
    )
    for url, payload, fetched_at in rows:
        conn.execute(
            "INSERT INTO items (source_id, url, canonical_url, content_hash, raw_payload, "
            "fetched_at) VALUES (2, ?, ?, 'h', ?, ?)",
            (url, url, json.dumps(payload), fetched_at),
        )
    conn.close()
    result = by_number(doctor(config_dir, tmp_path), 11)
    assert result.status == "WARN"
    assert "raw_payload 4 件" in result.detail


def test_ttl_leftover_counts_expired_signal_excerpts(config_dir, tmp_path) -> None:
    # 記事行を持たないシグナルの抜粋だけが残っても見逃さない(purge と同じ条件。GH#250)
    conn = connect(tmp_path / "intel.db")
    conn.execute(
        "INSERT INTO x_signals (kind, tweet_id, text_excerpt, observed_at) "
        "VALUES ('like', 'old', '古い本文', '2020-01-01T00:00:00Z')"
    )
    # 期限内の抜粋と、NULL 化済みの古い行は数えない
    conn.execute(
        "INSERT INTO x_signals (kind, tweet_id, text_excerpt) VALUES ('like', 'new', '新しい本文')"
    )
    conn.execute(
        "INSERT INTO x_signals (kind, tweet_id, text_excerpt, observed_at) "
        "VALUES ('bookmark', 'purged', NULL, '2020-01-01T00:00:00Z')"
    )
    conn.close()
    result = by_number(doctor(config_dir, tmp_path), 11)
    assert result.status == "WARN"
    assert "text_excerpt 1 件" in result.detail


def _add_failing_source(
    tmp_path: Path, type_: str, name: str, failures: int, *, enabled: int = 1
) -> None:
    conn = sqlite3.connect(tmp_path / "intel.db")
    conn.execute(
        "INSERT INTO sources (type, name, enabled, consecutive_failures, last_error) "
        "VALUES (?, ?, ?, ?, 'FatalError: HTTP 400')",
        (type_, name, enabled, failures),
    )
    conn.commit()
    conn.close()


def test_source_health_warns_at_design_thresholds(config_dir, tmp_path) -> None:
    # 設計 02: X は 3 回・RSS は 7 回(死亡疑い)で警告。しきい値未満と止めたソースは出さない
    _add_failing_source(tmp_path, "x", "x:search", 3)
    _add_failing_source(tmp_path, "x", "x:home", 2)
    _add_failing_source(tmp_path, "rss", "dead-feed", 7)
    _add_failing_source(tmp_path, "rss", "flaky-feed", 6)
    _add_failing_source(tmp_path, "rss", "stopped-feed", 30, enabled=0)
    config_path = config_dir / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "\n[x]\nenabled = true\n", encoding="utf-8"
    )

    result = by_number(doctor(config_dir, tmp_path), 14)

    assert result.status == "WARN"  # 自動 disable はせず人に知らせるだけ(FAIL にしない)
    assert "X「x:search」が 3 回連続で失敗(直近: FatalError: HTTP 400)" in result.detail
    assert "RSS「dead-feed」が 7 回連続で失敗" in result.detail
    assert "死亡疑い" in result.detail
    for quiet in ("x:home", "flaky-feed", "stopped-feed"):
        assert quiet not in result.detail


def test_source_health_ignores_x_failures_while_x_disabled(config_dir, tmp_path) -> None:
    # x.enabled=false の間は collect が X を回さず回数が進まない(止める前の回数で鳴らさない)
    _add_failing_source(tmp_path, "x", "x:search", 9)
    result = by_number(doctor(config_dir, tmp_path), 14)
    assert result.status == "PASS"


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


def test_month_cost_summary_includes_review_cost(config_dir, tmp_path) -> None:
    # 深掘りのレビューの API 費用(GH#153)は LLM に入れる。前の月の分は入れない
    conn = connect(tmp_path / "intel.db")
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed')")
    item = conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, content_hash) "
        "VALUES (2, 'https://e.example/a', 'https://e.example/a', 'sha256:x')"
    ).lastrowid
    for created_at, cost in ((None, 0.25), ("2000-01-15T00:00:00Z", 9.0)):
        conn.execute(
            "INSERT INTO dig_results (item_id, note_name, card_text, review_cost_usd,"
            " created_at) VALUES (?, 'n', 'c', ?,"
            " coalesce(?, strftime('%Y-%m-%dT%H:%M:%SZ','now')))",
            (item, cost, created_at),
        )
    from app.config import load_config

    summary = month_cost_summary(conn, load_config(config_dir))
    conn.close()
    assert summary["llm_usd"] == 0.25
    assert summary["spent_usd"] == 0.25


def test_month_cost_summary_review_cost_month_boundary(config_dir, tmp_path) -> None:
    # 月は深掘りの上限の判定と同じ UTC の暦月。月初ちょうどは数え、前の月の最後の 1 秒は数えない
    from datetime import UTC, datetime, timedelta

    start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    conn = connect(tmp_path / "intel.db")
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed')")
    item = conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, content_hash) "
        "VALUES (2, 'https://e.example/a', 'https://e.example/a', 'sha256:x')"
    ).lastrowid
    for moment, cost in ((start, 0.5), (start - timedelta(seconds=1), 7.0)):
        conn.execute(
            "INSERT INTO dig_results (item_id, note_name, card_text, review_cost_usd,"
            " created_at) VALUES (?, 'n', 'c', ?, ?)",
            (item, cost, moment.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
    from app.config import load_config

    summary = month_cost_summary(conn, load_config(config_dir))
    conn.close()
    assert summary["llm_usd"] == 0.5


def test_month_cost_summary_includes_fallback_cost(config_dir, tmp_path) -> None:
    # 予備で使った額(GH#156)も LLM に入れる。月は UTC の暦月
    from datetime import UTC, datetime, timedelta

    start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    conn = connect(tmp_path / "intel.db")
    for moment, cost in ((start, 0.75), (start - timedelta(seconds=1), 5.0)):
        conn.execute(
            "INSERT INTO llm_fallback_calls (stage, model, primary_error, ok, cost_usd,"
            " created_at) VALUES ('analyze', 'm', 'LLMError', 1, ?, ?)",
            (cost, moment.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
    from app.config import load_config

    summary = month_cost_summary(conn, load_config(config_dir))
    conn.close()
    assert summary["llm_usd"] == 0.75


def test_cost_shows_fallback_estimate(config_dir, tmp_path) -> None:
    # 予備の費用不明分(トークン数が空の行。額は最大の額の見積もり)を含むことを #9 で分かるように
    # する(GH#241)。実額だけの月は今の表示のまま
    from app.config import load_config

    conn = connect(tmp_path / "intel.db")
    conn.execute(
        "INSERT INTO llm_fallback_calls (stage, model, primary_error, ok, input_tokens,"
        " output_tokens, cost_usd) VALUES ('analyze', 'm', 'LLMError', 1, 100, 10, 0.5)"
    )
    assert month_cost_summary(conn, load_config(config_dir))["fallback_estimated_usd"] == 0
    assert "見積もり" not in by_number(doctor(config_dir, tmp_path), 9).detail
    conn.execute(
        "INSERT INTO llm_fallback_calls (stage, model, primary_error, ok, cost_usd)"
        " VALUES ('dig', 'm', 'LLMError', 0, 0.25)"
    )
    summary = month_cost_summary(conn, load_config(config_dir))
    conn.close()
    assert (summary["llm_usd"], summary["fallback_estimated_usd"]) == (0.75, 0.25)
    detail = by_number(doctor(config_dir, tmp_path), 9).detail
    assert "LLM $0.75(うち予備の費用不明分の見積もり $0.25)" in detail


def test_month_cost_summary_without_fallback_table(config_dir, tmp_path) -> None:
    # migration を当てない doctor が 018 より前の DB で落ちない
    conn = connect(tmp_path / "intel.db")
    conn.execute("DROP TABLE llm_fallback_calls")
    from app.config import load_config

    summary = month_cost_summary(conn, load_config(config_dir))
    conn.close()
    assert summary["llm_usd"] == 0


def test_month_cost_summary_without_review_column(config_dir, tmp_path) -> None:
    # doctor は migration を当てない。017 より前の DB でも当月コストを出せる
    conn = connect(tmp_path / "intel.db")
    conn.execute("ALTER TABLE dig_results DROP COLUMN review_cost_usd")
    from app.config import load_config

    summary = month_cost_summary(conn, load_config(config_dir))
    conn.close()
    assert summary["llm_usd"] == 0


# --- #13 X OAuth2 token(S1 = GH#58) ---


def write_oauth2_token(config_dir: Path) -> None:
    from app.connectors.x_oauth2 import XOAuth2Token, save_token

    save_token(
        config_dir,
        XOAuth2Token(
            access_token="at",
            refresh_token="rt",
            expires_at="2026-12-31T00:00:00Z",
            scope="tweet.read users.read bookmark.read like.read offline.access",
        ),
    )


def append_client_id(config_dir: Path) -> None:
    with open(config_dir / "config.toml", "a", encoding="utf-8") as f:
        f.write('\n[x_oauth2]\nclient_id = "cid"\n')


def test_x_oauth2_token_present_and_valid_passes(config_dir, tmp_path) -> None:
    append_client_id(config_dir)
    write_oauth2_token(config_dir)
    results = doctor(
        config_dir, tmp_path, x_oauth2_probe=lambda config, env, config_dir: "kosako"
    )
    result = by_number(results, 13)
    assert result.status == "PASS"
    assert "@kosako" in result.detail


def test_x_oauth2_token_without_client_id_fails(config_dir, tmp_path) -> None:
    write_oauth2_token(config_dir)  # token はあるが [x_oauth2] 未設定 = refresh 不能
    result = by_number(doctor(config_dir, tmp_path), 13)
    assert result.status == "FAIL"
    assert "client_id" in result.detail


def test_x_oauth2_probe_failure_fails(config_dir, tmp_path) -> None:
    from app.connectors.x_oauth2 import XOAuth2Error

    append_client_id(config_dir)
    write_oauth2_token(config_dir)

    def probe(config, env, config_dir):
        raise XOAuth2Error("refresh 失敗(intel x-auth で再認可が必要)")

    result = by_number(doctor(config_dir, tmp_path, x_oauth2_probe=probe), 13)
    assert result.status == "FAIL"
    assert "再認可" in result.detail


def test_x_oauth2_corrupted_token_file_fails(config_dir, tmp_path) -> None:
    token_file = config_dir / "x-oauth2-token.json"
    token_file.write_text("not json", encoding="utf-8")
    token_file.chmod(0o600)  # 権限は正しく中身だけ壊れているケース
    result = by_number(doctor(config_dir, tmp_path), 13)
    assert result.status == "FAIL"
    assert "壊れている" in result.detail


def test_x_oauth2_loose_permission_token_fails(config_dir, tmp_path) -> None:
    write_oauth2_token(config_dir)
    (config_dir / "x-oauth2-token.json").chmod(0o644)
    result = by_number(doctor(config_dir, tmp_path), 13)
    assert result.status == "FAIL"
    assert "パーミッション" in result.detail


def enable_x_signals(config_dir: Path) -> None:
    with open(config_dir / "config.toml", "a", encoding="utf-8") as f:
        f.write("\n[x_signals]\ndaily_read_cap = 15\n")


def test_x_oauth2_missing_token_fails_when_signals_enabled(config_dir, tmp_path) -> None:
    # 取込が有効なら未認可は SKIP でなく FAIL(朝の x_signals 段が失敗する。GH#249)
    append_client_id(config_dir)
    enable_x_signals(config_dir)
    result = by_number(doctor(config_dir, tmp_path), 13)
    assert result.status == "FAIL"
    assert "未認可" in result.detail
    assert "client_id" not in result.detail


def test_x_oauth2_missing_token_and_client_id_fail_when_signals_enabled(
    config_dir, tmp_path
) -> None:
    enable_x_signals(config_dir)
    result = by_number(doctor(config_dir, tmp_path), 13)
    assert result.status == "FAIL"
    assert "client_id" in result.detail
    assert "未認可" in result.detail


def test_month_cost_summary_includes_x_signal_runs(config_dir, tmp_path) -> None:
    conn = connect(tmp_path / "intel.db")
    conn.execute(
        "INSERT INTO x_signal_runs (status, api_reads, estimated_cost_usd) "
        "VALUES ('ok', 15, 0.06)"
    )
    from app.config import load_config

    summary = month_cost_summary(conn, load_config(config_dir))
    conn.close()
    assert summary["x_usd"] == pytest.approx(0.06)  # S1b 取込コストも月次見込みに載る


def test_x_mode_consistency_flags_enabled_bookmark_row(tmp_path) -> None:
    # bookmark 行(GH#99)は enabled=0 が正。enabled 化は collect 失敗源として WARN
    from app.config import Config
    from app.doctor import _x_mode_consistency_warns

    conn = connect(tmp_path / "intel.db")
    apply_migrations(conn, tmp_path / "intel.db")
    conn.execute(
        "INSERT INTO sources (type, name, config, enabled) "
        """VALUES ('x', 'x:bookmark', '{"mode": "bookmark"}', 0)"""
    )
    config = Config(config_dir=tmp_path)
    assert _x_mode_consistency_warns(conn, config) == []  # enabled=0 は正常(WARN なし)

    conn.execute("UPDATE sources SET enabled = 1 WHERE name = 'x:bookmark'")
    warns = _x_mode_consistency_warns(conn, config)
    assert any("mode=bookmark 行が enabled" in w for w in warns)
    conn.close()
