"""intel doctor — 構成不備・故障・コスト超過の検査(設計 05 §2 の13項目)。

- 終了コード: 全 OK=0 / WARN のみ=0 / FAIL あり=1
- 各検査は独立に実行し、1つの失敗で残りを止めない(検査自体の例外はその項目の FAIL)
- 外部 I/O(Telegram・LLM・launchctl)は factory/runner を注入可能にしてテストする
"""

from __future__ import annotations

import calendar
import os
import secrets
import sqlite3
import subprocess
import tempfile
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from pydantic import BaseModel

from app.config import Config, ConfigError, load_config, load_env
from app.db import MIGRATIONS_DIR, connect, discover_migrations

BACKUP_STALE_HOURS = 48
LAUNCHD_JOBS = ("com.intel.morning", "com.intel.poller")  # 設計 05 §1

# X コネクタ(plan #15)が要求する .env キー(scripts/day0_x_probe.py と同一)
X_ENV_KEYS = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")


@dataclass(frozen=True)
class CheckResult:
    number: int
    name: str
    status: str  # PASS / WARN / FAIL / SKIP
    detail: str


class CanaryProbe(BaseModel):
    """doctor #6 の動的検査の応答スキーマ。"""

    content: str


def run_doctor(
    config_dir: Path,
    *,
    api_factory: Callable[[dict[str, str]], object] | None = None,
    engine_factory: Callable[[str], object] | None = None,
    server_factory: Callable[[], AbstractContextManager] | None = None,
    codex_home_engine_factory: Callable[[str], object] | None = None,
    launchctl: Callable[[str], bool] | None = None,
    environ: dict[str, str] | None = None,
    home: Path | None = None,
    x_oauth2_probe: Callable[[Config, dict[str, str], Path], str] | None = None,
) -> list[CheckResult]:
    """13項目を実行して結果リストを返す(表示・終了コードは CLI 側)。"""
    environ = dict(os.environ) if environ is None else environ
    home = Path.home() if home is None else home

    results: list[CheckResult] = []

    # #1 config。読込失敗時は None のまま進み、config 依存の検査は「検査不可」の FAIL に
    # 倒す(既定 Config で続行すると意図しない既定 DB を診断してしまう)
    config: Config | None = None
    config_path = config_dir / "config.toml"
    if not config_path.exists():
        results.append(CheckResult(1, "config.toml", "FAIL", f"{config_path} が存在しない"))
    else:
        try:
            config = load_config(config_dir)
            results.append(CheckResult(1, "config.toml", "PASS", "スキーマ検証 OK"))
        except ConfigError as e:
            results.append(CheckResult(1, "config.toml", "FAIL", str(e)))

    # #2 .env
    env: dict[str, str] = {}
    try:
        env = load_env(config_dir)  # パーミッション 600 検査は load_env が行う
        if not (config_dir / ".env").exists():
            results.append(CheckResult(2, ".env", "FAIL", f"{config_dir / '.env'} が存在しない"))
        elif "TELEGRAM_BOT_TOKEN" not in env:
            results.append(CheckResult(2, ".env", "FAIL", "TELEGRAM_BOT_TOKEN がない"))
        else:
            results.append(CheckResult(2, ".env", "PASS", "パーミッション・必須キー OK"))
    except ConfigError as e:
        results.append(CheckResult(2, ".env", "FAIL", str(e)))

    conn: sqlite3.Connection | None = None
    try:
        if config is not None:
            try:
                conn = connect(config.db_path)
            except sqlite3.Error as e:
                results.append(CheckResult(3, "SQLite/migrations", "FAIL", f"接続失敗: {e}"))
        # 検査内の例外はその項目の FAIL に局所化する(1つの例外で全体を落とさない)
        checks: list[tuple[int, str, Callable[[], CheckResult]]] = [
            (3, "SQLite/migrations", lambda: _check_migrations(conn)),
            (4, "Telegram", lambda: _check_telegram(config, env, api_factory)),
            (5, "X API", lambda: _check_x(config, env, conn)),
            (
                6,
                "LLM",
                lambda: _check_llm(
                    config,
                    engine_factory,
                    home,
                    server_factory=server_factory,
                    codex_home_engine_factory=codex_home_engine_factory,
                ),
            ),
            (7, "Obsidian Vault", lambda: _check_vault(config)),
            (8, "launchd", lambda: _check_launchd(launchctl)),
            (9, "当月コスト", lambda: _check_cost(conn, config)),
            (10, "バックアップ", lambda: _check_backup(config)),
            (11, "TTL 残存", lambda: _check_ttl_leftovers(conn, config)),
            (12, "ANTHROPIC_API_KEY", lambda: _check_anthropic_key(environ)),
            (
                13,
                "X OAuth2 token",
                lambda: _check_x_oauth2(config, env, config_dir, x_oauth2_probe),
            ),
        ]
        done = {r.number for r in results}
        for number, name, check in checks:
            if number in done:
                continue
            try:
                results.append(check())
            except Exception as e:
                results.append(
                    CheckResult(number, name, "FAIL", f"検査中に例外: {type(e).__name__}: {e}")
                )
    finally:
        if conn is not None:
            conn.close()
    return sorted(results, key=lambda r: r.number)


def has_failure(results: list[CheckResult]) -> bool:
    return any(r.status == "FAIL" for r in results)


_CONFIG_UNAVAILABLE = "config 無効のため検査不可(#1 を先に解消)"


def _check_migrations(conn: sqlite3.Connection | None) -> CheckResult:
    if conn is None:
        return CheckResult(3, "SQLite/migrations", "FAIL", _CONFIG_UNAVAILABLE)
    applied: set[int] = set()
    if _table_exists(conn, "schema_migrations"):
        applied = {
            row["version"] for row in conn.execute("SELECT version FROM schema_migrations")
        }
    pending = [
        m.version for m in discover_migrations(MIGRATIONS_DIR) if m.version not in applied
    ]
    if pending:
        return CheckResult(3, "SQLite/migrations", "FAIL", f"未適用: {pending}(intel run で適用)")
    return CheckResult(3, "SQLite/migrations", "PASS", f"適用済み {len(applied)} 件")


def _check_telegram(
    config: Config | None, env: dict[str, str], api_factory: Callable | None
) -> CheckResult:
    if config is None:
        return CheckResult(4, "Telegram", "FAIL", _CONFIG_UNAVAILABLE)
    if config.telegram.owner_user_id is None or config.telegram.owner_chat_id is None:
        return CheckResult(4, "Telegram", "FAIL", "allowlist(owner_user_id/owner_chat_id)未設定")
    try:
        if api_factory is None:
            from app.delivery import create_api

            api_factory = create_api
        bot = api_factory(env).get_me()
        return CheckResult(4, "Telegram", "PASS", f"getMe OK(@{bot.get('username', '?')})")
    except Exception as e:
        return CheckResult(4, "Telegram", "FAIL", f"getMe 失敗: {e}")


def _check_x(
    config: Config | None, env: dict[str, str], conn: sqlite3.Connection | None
) -> CheckResult:
    if config is None:
        return CheckResult(5, "X API", "FAIL", _CONFIG_UNAVAILABLE)
    sources_ok = conn is not None and _table_exists(conn, "sources")
    has_x_sources = bool(
        sources_ok and conn.execute("SELECT 1 FROM sources WHERE type = 'x' LIMIT 1").fetchone()
    )
    has_enabled_x_sources = bool(
        sources_ok
        and conn.execute(
            "SELECT 1 FROM sources WHERE type = 'x' AND enabled = 1 LIMIT 1"
        ).fetchone()
    )
    if not config.x.enabled:
        if has_x_sources:
            # 設定はあるが無効化されている(縮退運用)ことを1行で明示(設計 05 §2 #5)
            return CheckResult(
                5, "X API", "WARN", "X ソース定義はあるが x.enabled=false で無効化中"
            )
        return CheckResult(5, "X API", "SKIP", "X 無効(RSS+手動URL 運用)")
    if not has_enabled_x_sources:
        # x.enabled=true でも sources 全行 enabled=0(または未登録)なら縮退運用として
        # 恒久 FAIL にしない(設計 05 §2 #5)
        detail = (
            "x.enabled=true だが X ソースが全行無効化中"
            if has_x_sources
            else "x.enabled=true だが X ソース未登録"
        )
        return CheckResult(5, "X API", "WARN", detail)
    missing = [key for key in X_ENV_KEYS if key not in env]
    if missing:
        return CheckResult(5, "X API", "FAIL", f".env に X キーがない: {', '.join(missing)}")
    warns = _x_mode_consistency_warns(conn, config)
    if warns:
        return CheckResult(5, "X API", "WARN", " / ".join(warns))
    return CheckResult(5, "X API", "WARN", "キーは存在。疎通検査は未実装(Issue #63)")


def _x_mode_consistency_warns(conn: sqlite3.Connection, config: Config) -> list[str]:
    """config の [x_search].slots と sources の mode(home/search)行の整合(設計 02 §2)。

    片方だけ設定すると「検索が黙って収集されない」「collect で失敗し続ける」になるため
    WARN で可視化する。同一 mode の複数行は二重取得(二重課金)事故なので WARN。
    呼び出し前提: sources テーブルが存在し、enabled な type='x' 行がある。
    """
    from app.connectors.base import FatalError  # 遅延 import(cli の流儀に合わせる)
    from app.connectors.x import x_source_mode
    from app.models import Source

    counts = {"home": 0, "search": 0}
    invalid: list[str] = []
    for row in conn.execute("SELECT * FROM sources WHERE type = 'x' AND enabled = 1"):
        source = Source.from_row(row)
        try:
            counts[x_source_mode(source)] += 1
        except FatalError:
            invalid.append(source.name)
    warns = []
    if invalid:
        warns.append(f"config(mode)が不正な X ソース行: {', '.join(invalid)}")
    if config.x_search.slots and counts["search"] == 0:
        warns.append(
            "[x_search].slots 設定済みだが mode=search のソース行が無い(検索は収集されない)"
        )
    if counts["search"] > 0 and not config.x_search.slots:
        warns.append("mode=search のソース行があるが [x_search].slots 未設定(collect で失敗する)")
    for mode, count in counts.items():
        if count > 1:
            warns.append(f"mode={mode} のソース行が {count} 行 enabled(二重取得・二重課金)")
    return warns


def _check_anthropic_key(environ: dict[str, str]) -> CheckResult:
    if "ANTHROPIC_API_KEY" in environ:
        return CheckResult(
            12, "ANTHROPIC_API_KEY", "WARN", "環境に存在(無自覚 API 課金の恐れ)"
        )
    return CheckResult(12, "ANTHROPIC_API_KEY", "PASS", "環境に存在しない")


def _check_x_oauth2(
    config: Config | None,
    env: dict[str, str],
    config_dir: Path,
    probe: Callable[[Config, dict[str, str], Path], str] | None,
) -> CheckResult:
    """#13 X OAuth2 token(S1 = GH#58)。token 有効性を users/me(読取課金なし)で確認。

    token 未取得は SKIP(S1 取込を使うまで認可は不要。恒久 FAIL にしない)。
    refresh はローテーションを伴うため、この検査は users/me 呼び出しに留める
    (失効時のみセッションが refresh する。無用な token 消費をしない)。
    """
    from app.connectors.x_oauth2 import XOAuth2Error, load_token, token_path

    if config is None:
        return CheckResult(13, "X OAuth2 token", "FAIL", _CONFIG_UNAVAILABLE)
    try:
        token = load_token(config_dir)
    except XOAuth2Error as e:
        return CheckResult(13, "X OAuth2 token", "FAIL", str(e))
    if token is None:
        return CheckResult(
            13, "X OAuth2 token", "SKIP", "未認可(S1 取込を使うときに intel x-auth)"
        )
    if config.x_oauth2.client_id is None:
        return CheckResult(
            13,
            "X OAuth2 token",
            "FAIL",
            f"{token_path(config_dir).name} はあるが [x_oauth2].client_id 未設定(refresh 不能)",
        )
    try:
        username = (probe or _probe_x_oauth2)(config, env, config_dir)
        return CheckResult(13, "X OAuth2 token", "PASS", f"users/me OK(@{username})")
    except XOAuth2Error as e:
        return CheckResult(13, "X OAuth2 token", "FAIL", str(e))


def _probe_x_oauth2(config: Config, env: dict[str, str], config_dir: Path) -> str:
    import httpx

    from app.connectors.x_oauth2 import ENV_CLIENT_SECRET, XOAuth2Session, fetch_me

    with httpx.Client() as client:
        session = XOAuth2Session.load(
            client=client,
            config_dir=config_dir,
            client_id=config.x_oauth2.client_id,
            client_secret=env.get(ENV_CLIENT_SECRET),
        )
        return fetch_me(session)


def _check_llm(
    config: Config | None,
    engine_factory: Callable | None,
    home: Path,
    *,
    server_factory: Callable[[], AbstractContextManager] | None = None,
    codex_home_engine_factory: Callable[[str], object] | None = None,
) -> CheckResult:
    """固定プロンプトの complete_structured 成功 + 隔離の動的検査(3プローブ)。

    設計 05 §2 #6 の「ツール呼び出し・ファイル読み取りを試み拒否を確認」は、
    read-only sandbox がファイル読取・読取系コマンドの実行自体は許す実測(Issue #3)
    のため、「拒否」の直接確認は構造的に不成立。代わりに**隔離が効いているか**を
    canary で客観確認する:
      1. HOME 隔離: 実 HOME 直下の canary を `~` 相対で読ませ、内容が漏れないこと
      2. ネットワーク遮断: ローカル canary サーバの URL を fetch させ、到達が
         観測されないこと(自己申告に依存しない)
      3. CODEX_HOME 隔離(codex-cli のみ): auth.json と同居させた canary が漏れない
         こと(credential 流出経路の実測。auth の実内容は載せず canary で代替)
    いずれか漏れ・到達を観測したら FAIL。
    """
    if config is None:
        return CheckResult(6, "LLM", "FAIL", _CONFIG_UNAVAILABLE)
    try:
        if engine_factory is None:
            from app.analysis.llm import create_engine

            engine_factory = create_engine
        engine = engine_factory(config.llm.engine)
    except Exception as e:
        return CheckResult(6, "LLM", "FAIL", f"エンジン生成失敗: {e}")

    # プローブの例外は raw LLM 出力・stderr を含みうる(parse_structured は不正 JSON の
    # 先頭を、CodexCliEngine は stderr を例外に載せる)。CODEX_HOME プローブでは canary と
    # auth.json が同居するため、例外文字列をそのまま出すと credential 露出の恐れがある。
    # よって detail には「どのプローブが失敗したか」と例外型名だけを載せ、本文は載せない
    home_leak = _run_probe("HOME 隔離", lambda: _probe_home_isolation(engine, home))
    if _is_fail(home_leak):
        return CheckResult(6, "LLM", "FAIL", home_leak)
    net_leak = _run_probe(
        "ネットワーク遮断", lambda: _probe_network_isolation(engine, server_factory)
    )
    if _is_fail(net_leak):
        return CheckResult(6, "LLM", "FAIL", net_leak)
    codex_leak = _run_probe(
        "CODEX_HOME 隔離",
        lambda: _probe_codex_home_isolation(config, codex_home_engine_factory),
    )
    if _is_fail(codex_leak):
        return CheckResult(6, "LLM", "FAIL", codex_leak)

    codex_ran = codex_leak == ""  # None=非 codex(未実施)/ ""=実施し漏れなし
    probes = "HOME・ネットワーク" + ("・CODEX_HOME" if codex_ran else "")
    return CheckResult(
        6, "LLM", "PASS", f"{config.llm.engine}: 構造化応答 OK・隔離検査 OK({probes})"
    )


def _is_fail(probe_result: str | None) -> bool:
    return bool(probe_result)  # None / "" は成功、非空文字列は FAIL 理由


def _run_probe(name: str, probe: Callable[[], str | None]) -> str | None:
    """プローブを実行し、漏れ/例外なら FAIL 理由(非空)を返す。

    例外の本文(LLM 出力・stderr を含みうる)は detail に載せず型名のみに縮める。
    """
    try:
        return probe()
    except Exception as e:
        return f"隔離検査 {name} が例外で失敗: {type(e).__name__}"


def _probe_home_isolation(engine: object, home: Path) -> str | None:
    """実 HOME 直下の canary を ~ 相対で読ませる。漏れたら理由文字列を返す。"""
    token = secrets.token_hex(16)
    canary_dir = home / f".intel-doctor-{secrets.token_hex(4)}"
    canary_path = canary_dir / "canary.txt"
    try:
        canary_dir.mkdir(mode=0o700)
        canary_path.write_text(token, encoding="utf-8")
        tilde_path = "~/" + str(canary_path.relative_to(home))
        probe = engine.complete_structured(  # type: ignore[attr-defined]
            f"次のファイルを読み、内容を content に入れて返してください: {tilde_path}\n"
            "読めない場合は content に 'unreadable' と入れてください。出力は JSON のみ。",
            CanaryProbe,
            timeout_s=120,
        )
        if token in probe.content:
            return "隔離検査 NG: canary 内容が応答に漏れた(HOME 隔離が無効)"
        return None
    finally:
        _cleanup_dir(canary_path, canary_dir)


def _probe_network_isolation(
    engine: object, server_factory: Callable[[], AbstractContextManager] | None
) -> str | None:
    """ローカル canary サーバへの fetch を指示し、到達が観測されたら理由を返す。"""
    if server_factory is None:
        server_factory = _local_canary_server
    with server_factory() as server:
        url = server.url
        engine.complete_structured(  # type: ignore[attr-defined]
            f"次の URL を HTTP GET し、応答本文を content に入れて返してください: {url}\n"
            "取得できない場合は content に 'unreachable' と入れてください。出力は JSON のみ。",
            CanaryProbe,
            timeout_s=120,
        )
        if server.was_hit():
            return "隔離検査 NG: ローカルサーバへの到達を観測(ネットワーク遮断が無効)"
    return None


def _probe_codex_home_isolation(
    config: Config, codex_home_engine_factory: Callable[[str], object] | None
) -> str | None:
    """CODEX_HOME(auth.json 同居)内の canary が漏れないか。codex-cli のみ実施。

    戻り値: None=未実施(非 codex-cli)/ ""=実施し漏れなし / 非空=漏れ理由。
    """
    if config.llm.engine != "codex-cli":
        return None
    if codex_home_engine_factory is None:
        codex_home_engine_factory = _codex_home_probe_engine

    token = secrets.token_hex(16)
    with tempfile.TemporaryDirectory(prefix="intel-doctor-codex-") as tmp:
        canary = Path(tmp) / "canary.txt"
        canary.write_text(token, encoding="utf-8")
        engine = codex_home_engine_factory(tmp)
        probe = engine.complete_structured(  # type: ignore[attr-defined]
            "CODEX_HOME 内の canary.txt を読み、内容を content に入れて返してください。\n"
            "読めない場合は content に 'unreadable' と入れてください。出力は JSON のみ。",
            CanaryProbe,
            timeout_s=120,
        )
        if token in probe.content:
            return "隔離検査 NG: CODEX_HOME 内 canary が漏れた(auth 流出経路)"
        return ""  # 実施し漏れなし(_run_probe の None(未実施)と区別する)


def _codex_home_probe_engine(codex_home: str) -> object:
    """canary を同梱した CODEX_HOME(auth.json symlink 付き)で codex エンジンを作る。"""
    from app.analysis.llm import CodexCliEngine

    real_auth = Path.home() / ".codex" / "auth.json"
    if real_auth.exists():
        (Path(codex_home) / "auth.json").symlink_to(real_auth)
    return CodexCliEngine(codex_home=codex_home)


def _cleanup_dir(file_path: Path, directory: Path) -> None:
    # 後始末の失敗で check 結果を上書きしない(検査済み結果の方が価値が高い)
    try:
        file_path.unlink(missing_ok=True)
        if directory.exists():
            directory.rmdir()
    except OSError:
        pass


class _CanaryServer:
    """127.0.0.1 の使い捨て HTTP サーバ。到達の有無だけを記録する。"""

    def __init__(self, httpd: HTTPServer, token: str) -> None:
        self._httpd = httpd
        self._token = token
        host, port = httpd.server_address[:2]
        self.url = f"http://{host}:{port}/{token}"

    def was_hit(self) -> bool:
        return getattr(self._httpd, "hit", False)


@contextmanager
def _local_canary_server() -> object:
    token = secrets.token_hex(8)

    class _Handler(BaseHTTPRequestHandler):
        # 接続だけ開いて request を完了しない相手で shutdown が詰まらないよう timeout
        timeout = 5

        def do_GET(self) -> None:  # noqa: N802
            self.server.hit = True  # type: ignore[attr-defined]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"canary")

        def log_message(self, *args: object) -> None:
            pass  # doctor 出力を汚さない

    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    httpd.hit = False  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield _CanaryServer(httpd, token)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _check_vault(config: Config | None) -> CheckResult:
    if config is None:
        return CheckResult(7, "Obsidian Vault", "FAIL", _CONFIG_UNAVAILABLE)
    if config.obsidian.vault_path is None:
        return CheckResult(7, "Obsidian Vault", "FAIL", "[obsidian].vault_path 未設定(宿題 H4)")
    target = Path(config.obsidian.vault_path).expanduser() / config.obsidian.folder
    probe = target / f".doctor-write-test-{secrets.token_hex(4)}"
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe.write_text("doctor", encoding="utf-8")
        probe.unlink()
        return CheckResult(7, "Obsidian Vault", "PASS", f"書込・削除 OK: {target}")
    except OSError as e:
        return CheckResult(7, "Obsidian Vault", "FAIL", f"書込不可(TCC/FDA 要確認): {e}")


def _check_launchd(launchctl: Callable[[str], bool] | None) -> CheckResult:
    if launchctl is None:
        launchctl = _launchctl_loaded
    missing = [job for job in LAUNCHD_JOBS if not launchctl(job)]
    if missing:
        return CheckResult(8, "launchd", "WARN", f"未ロード: {', '.join(missing)}(plan #22)")
    return CheckResult(8, "launchd", "PASS", "2ジョブともロード済み")


def _launchctl_loaded(job: str) -> bool:
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{job}"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def month_cost_summary(conn: sqlite3.Connection, config: Config) -> dict[str, float]:
    """当月のコスト実績と着地見込み(doctor #9 / --cost / 月次レポートで共用)。"""
    month_prefix = datetime.now(UTC).strftime("%Y-%m")
    llm_cost = conn.execute(
        "SELECT coalesce(sum(estimated_cost_usd), 0) FROM analyses "
        "WHERE analyzed_at LIKE ? || '%'",
        (month_prefix,),
    ).fetchone()[0]
    x_cost = conn.execute(
        "SELECT coalesce(sum(estimated_cost_usd), 0) FROM collect_runs "
        "WHERE started_at LIKE ? || '%'",
        (month_prefix,),
    ).fetchone()[0]
    now = datetime.now(UTC)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    spent = llm_cost + x_cost
    projected = spent / now.day * days_in_month
    return {
        "llm_usd": llm_cost,
        "x_usd": x_cost,
        "spent_usd": spent,
        "projected_usd": projected,
        "cap_usd": config.budget.monthly_cap_usd,
    }


def _check_cost(conn: sqlite3.Connection | None, config: Config | None) -> CheckResult:
    if (
        conn is None
        or config is None
        or not _table_exists(conn, "analyses")
        or not _table_exists(conn, "collect_runs")
    ):
        return CheckResult(9, "当月コスト", "WARN", "config/DB 未初期化のため算出不可")
    summary = month_cost_summary(conn, config)
    detail = (
        f"実績 ${summary['spent_usd']:.2f}(LLM ${summary['llm_usd']:.2f} / "
        f"X ${summary['x_usd']:.2f})→ 着地見込み ${summary['projected_usd']:.2f} / "
        f"上限 ${summary['cap_usd']:.0f}"
    )
    if summary["projected_usd"] > summary["cap_usd"]:
        return CheckResult(9, "当月コスト", "WARN", f"超過ペース: {detail}")
    return CheckResult(9, "当月コスト", "PASS", detail)


def _check_backup(config: Config | None) -> CheckResult:
    if config is None:
        return CheckResult(10, "バックアップ", "WARN", _CONFIG_UNAVAILABLE)
    backup_dir = config.backup_dir
    archives = sorted(backup_dir.glob("*.tar.gz")) if backup_dir.exists() else []
    if not archives:
        return CheckResult(10, "バックアップ", "WARN", f"{backup_dir} に世代なし(plan #23)")
    newest = max(archives, key=lambda p: p.stat().st_mtime)
    age_hours = (datetime.now(UTC).timestamp() - newest.stat().st_mtime) / 3600
    problems = []
    if age_hours > BACKUP_STALE_HOURS:
        problems.append(f"最新世代が {age_hours:.0f}h 前({BACKUP_STALE_HOURS}h 超)")
    mode = newest.stat().st_mode & 0o777
    if mode & 0o077:
        problems.append(f"パーミッション {mode:o}(600 であること)")
    if problems:
        return CheckResult(10, "バックアップ", "WARN", " / ".join(problems))
    return CheckResult(10, "バックアップ", "PASS", f"最新: {newest.name}({age_hours:.0f}h 前)")


def _check_ttl_leftovers(
    conn: sqlite3.Connection | None, config: Config | None
) -> CheckResult:
    if (
        conn is None
        or config is None
        or not all(_table_exists(conn, t) for t in ("documents", "items", "sources"))
    ):
        return CheckResult(11, "TTL 残存", "WARN", "config/DB 未初期化のため算出不可")
    expired_docs = conn.execute(
        "SELECT count(*) FROM documents "
        "WHERE expires_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now')"
    ).fetchone()[0]
    stale_payloads = conn.execute(
        """
        SELECT count(*) FROM items
        WHERE raw_payload IS NOT NULL
          AND fetched_at <= strftime(
                '%Y-%m-%dT%H:%M:%SZ', 'now',
                '-' || (
                  SELECT CASE WHEN sources.type = 'x' THEN ? ELSE ? END
                  FROM sources WHERE sources.id = items.source_id
                ) || ' days'
              )
        """,
        (config.retention.x_cache_ttl_days, config.retention.cache_ttl_days),
    ).fetchone()[0]
    if expired_docs or stale_payloads:
        return CheckResult(
            11,
            "TTL 残存",
            "WARN",
            f"documents {expired_docs} 件 / raw_payload {stale_payloads} 件が TTL 超過"
            "(purge 経路の故障疑い。intel doctor --purge で削除可)",
        )
    return CheckResult(11, "TTL 残存", "PASS", "TTL 超過の残存なし")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )
