"""intel doctor — 構成不備・故障・コスト超過の検査(設計 05 §2 の12項目)。

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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from app.config import Config, ConfigError, load_config, load_env
from app.db import MIGRATIONS_DIR, connect, discover_migrations

BACKUP_DIR = Path.home() / "Backups" / "intel"  # 設計 05 §5
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
    launchctl: Callable[[str], bool] | None = None,
    environ: dict[str, str] | None = None,
    backup_dir: Path = BACKUP_DIR,
    home: Path | None = None,
) -> list[CheckResult]:
    """12項目を実行して結果リストを返す(表示・終了コードは CLI 側)。"""
    environ = dict(os.environ) if environ is None else environ
    home = Path.home() if home is None else home

    results: list[CheckResult] = []

    # #1 config
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
    if config is None:
        # 後続の大半が config 依存。既定値で続行し、少なくとも独立検査は回す
        config = Config(config_dir=config_dir)

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

    # #3 SQLite + migrations
    conn: sqlite3.Connection | None = None
    try:
        conn = connect(config.db_path)
        applied: set[int] = set()
        if _table_exists(conn, "schema_migrations"):
            applied = {
                row["version"]
                for row in conn.execute("SELECT version FROM schema_migrations")
            }
        pending = [
            m.version for m in discover_migrations(MIGRATIONS_DIR) if m.version not in applied
        ]
        if pending:
            results.append(
                CheckResult(3, "SQLite/migrations", "FAIL", f"未適用: {pending}(intel run で適用)")
            )
        else:
            results.append(
                CheckResult(3, "SQLite/migrations", "PASS", f"適用済み {len(applied)} 件")
            )
    except sqlite3.Error as e:
        results.append(CheckResult(3, "SQLite/migrations", "FAIL", f"接続失敗: {e}"))

    # #4 Telegram getMe + allowlist
    results.append(_check_telegram(config, env, api_factory))

    # #5 X 疎通(無効時 SKIP/WARN。設計 05 §2 #5)
    results.append(_check_x(config, env, conn))

    # #6 LLM 動的検査
    results.append(_check_llm(config, engine_factory, home))

    # #7 Obsidian Vault 書込
    results.append(_check_vault(config))

    # #8 launchd
    results.append(_check_launchd(launchctl))

    # #9 当月コスト見込み
    results.append(_check_cost(conn, config))

    # #10 バックアップ
    results.append(_check_backup(backup_dir))

    # #11 TTL 超過残存
    results.append(_check_ttl_leftovers(conn, config))

    # #12 ANTHROPIC_API_KEY
    if "ANTHROPIC_API_KEY" in environ:
        results.append(
            CheckResult(12, "ANTHROPIC_API_KEY", "WARN", "環境に存在(無自覚 API 課金の恐れ)")
        )
    else:
        results.append(CheckResult(12, "ANTHROPIC_API_KEY", "PASS", "環境に存在しない"))

    if conn is not None:
        conn.close()
    return sorted(results, key=lambda r: r.number)


def has_failure(results: list[CheckResult]) -> bool:
    return any(r.status == "FAIL" for r in results)


def _check_telegram(
    config: Config, env: dict[str, str], api_factory: Callable | None
) -> CheckResult:
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


def _check_x(config: Config, env: dict[str, str], conn: sqlite3.Connection | None) -> CheckResult:
    if not config.x.enabled:
        has_x_sources = bool(
            conn is not None
            and _table_exists(conn, "sources")
            and conn.execute("SELECT 1 FROM sources WHERE type = 'x' LIMIT 1").fetchone()
        )
        if has_x_sources:
            # 設定はあるが無効化されている(縮退運用)ことを1行で明示(設計 05 §2 #5)
            return CheckResult(
                5, "X API", "WARN", "X ソース定義はあるが x.enabled=false で無効化中"
            )
        return CheckResult(5, "X API", "SKIP", "X 無効(RSS+手動URL 運用)")
    missing = [key for key in X_ENV_KEYS if key not in env]
    if missing:
        return CheckResult(5, "X API", "FAIL", f".env に X キーがない: {', '.join(missing)}")
    # 疎通(無料エンドポイント)は X コネクタ実装(plan #15)後に追加する
    return CheckResult(5, "X API", "WARN", "キーは存在。疎通検査は plan #15 で実装")


def _check_llm(config: Config, engine_factory: Callable | None, home: Path) -> CheckResult:
    """固定プロンプトの complete_structured 成功 + 隔離の動的検査(canary)。

    設計 05 §2 #6 の「ファイル読取の拒否確認」は、read-only sandbox がファイル読取
    自体は防がない実測(Issue #3)を踏まえ、**HOME 隔離の canary 検査**として実装する:
    実 HOME 直下のランダム名ファイルを `~` 相対パスで読ませ、内容(ランダムトークン)が
    応答に漏れないことを確認する(隔離が効いていれば `~` は空の作業 dir を指す)。
    """
    try:
        if engine_factory is None:
            from app.analysis.llm import create_engine

            engine_factory = create_engine
        engine = engine_factory(config.llm.engine)
    except Exception as e:
        return CheckResult(6, "LLM", "FAIL", f"エンジン生成失敗: {e}")

    token = secrets.token_hex(16)
    canary_dir = home / f".intel-doctor-{secrets.token_hex(4)}"
    canary_path = canary_dir / "canary.txt"
    try:
        canary_dir.mkdir(mode=0o700)
        canary_path.write_text(token, encoding="utf-8")
        tilde_path = "~/" + str(canary_path.relative_to(home))
        probe = engine.complete_structured(
            "次のファイルをその正確なパスのまま読み、内容を content フィールドに入れて"
            f"返してください: {tilde_path}\n"
            "そのパスで読めない場合は、他の場所を探さず content に 'unreadable' と入れて"
            "ください。出力は JSON のみ。",
            CanaryProbe,
            timeout_s=120,
        )
        if token in probe.content:
            return CheckResult(
                6, "LLM", "FAIL", "隔離検査 NG: canary 内容が応答に漏れた(HOME 隔離が無効)"
            )
        return CheckResult(6, "LLM", "PASS", f"{config.llm.engine}: 構造化応答 OK・隔離検査 OK")
    except Exception as e:
        return CheckResult(6, "LLM", "FAIL", f"complete_structured 失敗: {e}")
    finally:
        canary_path.unlink(missing_ok=True)
        if canary_dir.exists():
            canary_dir.rmdir()


def _check_vault(config: Config) -> CheckResult:
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


def _check_cost(conn: sqlite3.Connection | None, config: Config) -> CheckResult:
    if conn is None or not _table_exists(conn, "analyses"):
        return CheckResult(9, "当月コスト", "WARN", "DB 未初期化のため算出不可")
    summary = month_cost_summary(conn, config)
    detail = (
        f"実績 ${summary['spent_usd']:.2f}(LLM ${summary['llm_usd']:.2f} / "
        f"X ${summary['x_usd']:.2f})→ 着地見込み ${summary['projected_usd']:.2f} / "
        f"上限 ${summary['cap_usd']:.0f}"
    )
    if summary["projected_usd"] > summary["cap_usd"]:
        return CheckResult(9, "当月コスト", "WARN", f"超過ペース: {detail}")
    return CheckResult(9, "当月コスト", "PASS", detail)


def _check_backup(backup_dir: Path) -> CheckResult:
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


def _check_ttl_leftovers(conn: sqlite3.Connection | None, config: Config) -> CheckResult:
    if conn is None or not _table_exists(conn, "documents"):
        return CheckResult(11, "TTL 残存", "WARN", "DB 未初期化のため算出不可")
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
