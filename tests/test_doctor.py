import os
import sqlite3
import tarfile
from pathlib import Path

import pytest

from app.connectors.base import FatalError, RetryableError
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
        # 実サーバ経路は test_llm_network_probe_uses_real_local_server が明示的に検証
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
    assert len(results) == 13
    assert not has_failure(results)
    statuses = {r.number: r.status for r in results}
    assert statuses[1] == "PASS"
    assert statuses[4] == "PASS"
    assert statuses[5] == "SKIP"  # X 無効(縮退運用が恒久 FAIL にならない)
    assert statuses[6] == "PASS"
    assert statuses[12] == "PASS"
    assert statuses[13] == "SKIP"  # OAuth2 未認可(S1 前は恒久 FAIL にしない)


def test_missing_config_is_fail_but_all_checks_run(tmp_path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    results = doctor(empty, tmp_path)
    assert by_number(results, 1).status == "FAIL"
    assert len(results) == 13  # 1つの失敗で残りを止めない
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
    assert len(results) == 13  # 他の検査は巻き添えにならない
    assert by_number(results, 4).status == "PASS"


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
