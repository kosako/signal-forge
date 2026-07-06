"""intel CLI(Typer)。設計 00 §3。

M1 完了(plan #14)までは各サブコマンドはスタブ。実装は 1 Issue = 1 コマンドで
段階的に差し替える(スタブは「未実装」を表示して終了コード 1)。
"""

from __future__ import annotations

from pathlib import Path

import typer

from app.config import DEFAULT_CONFIG_DIR

app = typer.Typer(
    name="intel",
    help="個人用インテリジェンスサービス。毎朝のダイジェスト生成パイプライン。",
    no_args_is_help=True,
)

_STUB_MSG = "未実装: plan #{plan} で実装予定"


def _stub(plan: int) -> None:
    typer.echo(_STUB_MSG.format(plan=plan), err=True)
    raise typer.Exit(code=1)


@app.command()
def collect(
    config_dir: Path = typer.Option(DEFAULT_CONFIG_DIR, help="config.toml / .env の置き場"),
) -> None:
    """全ソース(X・RSS)から items を取り込む。"""
    # import は実行時(スタブ含む他コマンドの --help を httpx 等に依存させない)
    from app.collect import collect_all
    from app.config import load_config
    from app.connectors.rss import RssConnector
    from app.db import apply_migrations, connect
    from app.http import HttpClient
    from app.logs import DEFAULT_LOG_DIR, setup_logging

    config = load_config(config_dir)
    conn = connect(config.db_path)
    apply_migrations(conn, config.db_path)  # CLI 起動時に自動適用(設計 01 §10)
    logger = setup_logging(DEFAULT_LOG_DIR, "morning.log")
    http = HttpClient()
    collect_all(
        conn,
        {"rss": RssConnector(http)},  # X コネクタは plan #15(Day 0 ゲート通過後)
        logger,
        resolve_short=http.resolve_short_url,
    )


@app.command()
def extract(
    config_dir: Path = typer.Option(DEFAULT_CONFIG_DIR, help="config.toml / .env の置き場"),
) -> None:
    """items から本文を抽出して documents を生成する。"""
    from app.config import load_config
    from app.db import apply_migrations, connect
    from app.extraction import extract_pending
    from app.http import HttpClient
    from app.logs import DEFAULT_LOG_DIR, setup_logging

    config = load_config(config_dir)
    conn = connect(config.db_path)
    apply_migrations(conn, config.db_path)
    logger = setup_logging(DEFAULT_LOG_DIR, "morning.log")
    extract_pending(conn, config, HttpClient(), logger)


@app.command()
def analyze(
    config_dir: Path = typer.Option(DEFAULT_CONFIG_DIR, help="config.toml / .env の置き場"),
) -> None:
    """事前選別+LLM 本分析で analyses を生成する。"""
    from app.analysis.analyzer import analyze_selected
    from app.analysis.llm import create_engine
    from app.analysis.profile import load_interest_profile
    from app.analysis.stage1 import preselect
    from app.config import load_config
    from app.db import apply_migrations, connect
    from app.logs import DEFAULT_LOG_DIR, setup_logging

    config = load_config(config_dir)
    conn = connect(config.db_path)
    apply_migrations(conn, config.db_path)
    logger = setup_logging(DEFAULT_LOG_DIR, "morning.log")
    profile = load_interest_profile(config.profile_path)  # 欠落・不正は本分析前に fail fast
    engine = create_engine(config.llm.engine)
    result = preselect(conn, config, logger)
    counts = analyze_selected(conn, config, engine, profile.body, result.selected_ids, logger)
    typer.echo(f"analyzed={counts['analyzed']} degraded={counts['degraded']}")


@app.command()
def digest() -> None:
    """ランキングして日本語ダイジェスト(digests)を生成する。"""
    _stub(12)


@app.command()
def deliver() -> None:
    """当日ダイジェストを Telegram に配信する。"""
    _stub(16)


@app.command()
def poll() -> None:
    """受信デーモン(long polling 常駐+内蔵タイマー)。"""
    _stub(17)


@app.command()
def run() -> None:
    """朝バッチ: collect→extract→analyze→digest→export→deliver→purge を直列実行。"""
    _stub(14)


@app.command()
def doctor(
    purge: bool = typer.Option(False, "--purge", help="期限切れ削除の単独実行"),
    cost: bool = typer.Option(False, "--cost", help="コスト集計の単発表示"),
) -> None:
    """構成不備・故障・コスト超過の検査(12項目)。"""
    _stub(21)


def main() -> None:
    app()
