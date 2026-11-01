"""intel CLI(Typer)。設計 00 §3。"""

from __future__ import annotations

from pathlib import Path

import typer

from app.config import DEFAULT_CONFIG_DIR

app = typer.Typer(
    name="intel",
    help="個人用インテリジェンスサービス。毎朝のダイジェスト生成パイプライン。",
    no_args_is_help=True,
)


def _build_connectors(config, env: dict[str, str], http, conn) -> dict:
    """有効なソース種別のコネクタを組み立てる(合成点)。X は config で有効なときだけ配線。

    import は実行時(他コマンドの --help を httpx 等の重い依存に触れさせない)。
    X の日次の読取上限は同じ日の実行で共有するので、台帳(conn の collect_runs)から当日の
    読取数を出してコネクタに渡す(GH#178)。collect の直前に組み立てること。
    """
    from app.connectors.rss import RssConnector

    connectors = {"rss": RssConnector(http)}
    if config.x.enabled:
        from app.connectors.x import XRouter, build_x_connector, has_x_credentials, x_reads_today

        # キー欠落は doctor #5 が WARN。ここでは X を載せず RSS の collect を止めない
        if has_x_credentials(env):
            used = x_reads_today(conn)  # 同じ日(JST)の実行が読んだぶん(GH#178)
            search = None
            if config.x_search.slots:  # slots 空 = 検索無効(設計 02 §2)
                from app.connectors.x_search import build_x_search_connector

                search = build_x_search_connector(
                    config.x_search.slots,
                    config.x_search.daily_read_cap,
                    config.x.unit_price_usd,
                    env,
                    reads_used_today=used["search"],
                )
            home = build_x_connector(
                config.x.daily_read_cap,
                config.x.unit_price_usd,
                env,
                reads_used_today=used["home"],
            )
            # type='x' の sources 行は home / search の2種(config.mode で識別)。
            # collect の「type → Connector 1:1」契約は XRouter が保つ
            connectors["x"] = XRouter(home=home, search=search)
    return connectors


@app.command()
def collect(
    config_dir: Path = typer.Option(DEFAULT_CONFIG_DIR, help="config.toml / .env の置き場"),
) -> None:
    """全ソース(X・RSS)から items を取り込む。"""
    from app.collect import collect_all
    from app.config import load_config, load_env
    from app.db import apply_migrations, connect
    from app.http import HttpClient
    from app.logs import DEFAULT_LOG_DIR, setup_logging

    config = load_config(config_dir)
    env = load_env(config_dir)
    conn = connect(config.db_path)
    apply_migrations(conn, config.db_path)  # CLI 起動時に自動適用(設計 01 §10)
    logger = setup_logging(DEFAULT_LOG_DIR, "morning.log")
    http = HttpClient()
    collect_all(
        conn,
        _build_connectors(config, env, http, conn),
        logger,
        resolve_short=http.resolve_short_url,
        limits=config.collect,
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
    from app.config import load_config, load_env
    from app.db import apply_migrations, connect
    from app.logs import DEFAULT_LOG_DIR, setup_logging
    from app.run import analyze_stage

    config = load_config(config_dir)
    env = load_env(config_dir)
    conn = connect(config.db_path)
    apply_migrations(conn, config.db_path)
    logger = setup_logging(DEFAULT_LOG_DIR, "morning.log")
    counts = analyze_stage(conn, config, env, logger)  # 朝バッチと同じ段の組み立て(GH#206)
    typer.echo(f"analyzed={counts['analyzed']} degraded={counts['degraded']}")


@app.command()
def digest(
    config_dir: Path = typer.Option(DEFAULT_CONFIG_DIR, help="config.toml / .env の置き場"),
    force: bool = typer.Option(False, "--force", help="生成済みでも当日分を置換する"),
) -> None:
    """ランキングして日本語ダイジェスト(digests)を生成する。"""
    from app.config import load_config, load_env
    from app.db import apply_migrations, connect
    from app.logs import DEFAULT_LOG_DIR, setup_logging
    from app.run import digest_stage

    config = load_config(config_dir)
    env = load_env(config_dir)
    conn = connect(config.db_path)
    apply_migrations(conn, config.db_path)
    logger = setup_logging(DEFAULT_LOG_DIR, "morning.log")
    # 朝バッチと同じ段の組み立て(トレンドの省略・生成済みの skip を含む。GH#206)
    digest_id = digest_stage(conn, config, env, logger, force=force)
    typer.echo("skip(生成済み)" if digest_id is None else f"digest id={digest_id}")


@app.command()
def deliver(
    config_dir: Path = typer.Option(DEFAULT_CONFIG_DIR, help="config.toml / .env の置き場"),
    digest_date: str | None = typer.Option(
        None,
        "--date",
        help="送る digest の日付(YYYY-MM-DD・JST。既定 = 当日)。日をまたいで届かなかった"
        "過去の分を送り直す。台帳にある分は送らない(GH#247)",
    ),
) -> None:
    """当日ダイジェストを Telegram に配信する(--date で過去の未配信の分を送り直す)。"""
    from datetime import datetime

    from app.config import load_config, load_env
    from app.db import apply_migrations, connect
    from app.delivery import create_api, deliver_digest
    from app.jst import today_jst
    from app.logs import DEFAULT_LOG_DIR, setup_logging

    if digest_date is not None:
        # 過去の分は自動で繰り越さず、本人が日付で指定して送る(日次配信の契約と通知の量を
        # 変えない。GH#247)。DB の digest_date と同じ 0 埋めの形に揃えてから引く
        try:
            digest_date = datetime.strptime(digest_date, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            typer.echo(f"--date が不正: {digest_date}(YYYY-MM-DD で指定)", err=True)
            raise typer.Exit(code=1) from None
    config = load_config(config_dir)
    env = load_env(config_dir)
    conn = connect(config.db_path)
    apply_migrations(conn, config.db_path)
    logger = setup_logging(DEFAULT_LOG_DIR, "morning.log")
    row = conn.execute(
        "SELECT id FROM digests WHERE digest_date = ?", (digest_date or today_jst(),)
    ).fetchone()
    if row is None:
        if digest_date is None:
            typer.echo("当日の digest が存在しない(先に intel digest を実行)", err=True)
        else:
            typer.echo(f"{digest_date} の digest が存在しない", err=True)
        raise typer.Exit(code=1)
    sent = deliver_digest(conn, config, create_api(env), row["id"], logger)
    typer.echo("配信済み(skip)" if sent == 0 else f"配信完了({sent}通)")


@app.command()
def dig(
    config_dir: Path = typer.Option(DEFAULT_CONFIG_DIR, help="config.toml / .env の置き場"),
) -> None:
    """深掘りキューから本日分を調査し、DeepDive ノート生成+概要カード送信(GH#105)。"""
    from app.config import load_config, load_env
    from app.db import apply_migrations, connect
    from app.logs import DEFAULT_LOG_DIR, setup_logging
    from app.run import dig_stage

    config = load_config(config_dir)
    env = load_env(config_dir)
    conn = connect(config.db_path)
    apply_migrations(conn, config.db_path)
    logger = setup_logging(DEFAULT_LOG_DIR, "morning.log")
    if config.dig.daily_cap <= 0:
        typer.echo("[dig].daily_cap = 0(調査段は無効)")
        raise typer.Exit(code=0)
    ran = dig_stage(conn, config, env, logger)  # 朝バッチと同じ段の組み立て(GH#206)
    if not ran:  # 同じ候補を 2 度掘らない(GH#196)。先の実行が終われば届く
        typer.echo("別の実行(朝バッチか intel dig)が深掘りを進めているため、何もしなかった")
        return
    typer.echo("dig 完了(生成・送信の内訳はログ参照)")


@app.command()
def answer(
    config_dir: Path = typer.Option(DEFAULT_CONFIG_DIR, help="config.toml / .env の置き場"),
) -> None:
    """深掘りカードへの質問に答え、Telegram で返事を送る(GH#126)。take は記録する(GH#106)。"""
    import fcntl
    import os

    from app.analysis.fallback import fallback_engine_factory
    from app.config import load_config, load_env
    from app.db import apply_migrations, connect
    from app.delivery import create_api, is_telegram_configured
    from app.delivery.telegram import ANSWER_TEXT_MAX_CHARS, escape_and_truncate
    from app.dig_answer import answer_pending
    from app.dig_take import record_takes
    from app.logs import DEFAULT_LOG_DIR, setup_logging
    from app.vault_guard import VaultGuard

    config = load_config(config_dir)
    env = load_env(config_dir)
    chat_id = config.telegram.owner_chat_id
    if chat_id is None or not is_telegram_configured(config, env):
        typer.echo("Telegram が未設定のため返事を送れない", err=True)
        raise typer.Exit(code=1)
    lock_path = config.db_path.with_name("answer.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        # 同時に 2 つ走ると同じ質問に 2 回答えうる。後から来た方は何もせず終わる
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            typer.echo("別の answer が実行中")
            return
        # ロックはプロセスの終了まで握る(fd を複製し、with の close では外れないようにする)。
        # vault が止まると、中断できないスレッドが残って後から追記を終えうる。正常の終わりでも
        # 中断(例外)でも、その間に次の answer が同じ take を書かないため(GH#145)
        os.dup(lock.fileno())
        conn = connect(config.db_path)
        apply_migrations(conn, config.db_path)
        logger = setup_logging(DEFAULT_LOG_DIR, "answer.log")  # poller.log とはプロセスを分ける
        api = create_api(env)
        # vault へのアクセスは時間の上限つき。止まったら、この実行では vault に触れない(GH#145)
        vault = VaultGuard()
        sent = answer_pending(
            conn,
            config,
            fallback_engine_factory(conn, config, env, logger, stage="answer"),  # GH#156
            lambda reply_to, text: api.send_message(
                chat_id, escape_and_truncate(text, ANSWER_TEXT_MAX_CHARS),
                reply_to_message_id=reply_to,
            ),
            logger,
            vault,
        )
        # 返事を送った後に記録する(本人を待たせない)。前回の実行で記録できなかった分も含む
        recorded = record_takes(conn, config, logger, vault)
        if vault.stalled:
            logger.error(
                "vault が応答しなかった(macOS のプライバシーの確認待ちの可能性。runbook §11)",
                extra={"stage": "answer"},
            )
    typer.echo(f"answer 完了(送信 {sent} 通・take の記録 {recorded} 件)")


@app.command(name="x-auth")
def x_auth(
    config_dir: Path = typer.Option(DEFAULT_CONFIG_DIR, help="config.toml / .env の置き場"),
) -> None:
    """X OAuth 2.0(PKCE)の認可フローを実行し token を保存する(S1 = GH#58)。"""
    import httpx

    from app.config import load_config, load_env
    from app.connectors.x_oauth2 import (
        ENV_CLIENT_SECRET,
        XOAuth2Session,
        fetch_me,
        run_authorization_flow,
        token_path,
    )

    config = load_config(config_dir)
    env = load_env(config_dir)
    if config.x_oauth2.client_id is None:
        typer.echo(
            "config.toml の [x_oauth2].client_id が未設定"
            "(X developer portal の OAuth 2.0 Client ID。runbook §9)",
            err=True,
        )
        raise typer.Exit(code=1)
    with httpx.Client() as client:
        token = run_authorization_flow(
            client,
            config_dir=config_dir,
            client_id=config.x_oauth2.client_id,
            client_secret=env.get(ENV_CLIENT_SECRET),
            callback_port=config.x_oauth2.callback_port,
            echo=typer.echo,
        )
        # 保存した token で実際に認証できることまで確認して終わる(users/me は読取課金なし)
        session = XOAuth2Session(
            client=client,
            config_dir=config_dir,
            client_id=config.x_oauth2.client_id,
            client_secret=env.get(ENV_CLIENT_SECRET),
            token=token,
        )
        username = fetch_me(session)
    typer.echo(f"認可完了: @{username} / token を保存した({token_path(config_dir)})")


@app.command(name="x-signals")
def x_signals(
    config_dir: Path = typer.Option(DEFAULT_CONFIG_DIR, help="config.toml / .env の置き場"),
    backfill: bool = typer.Option(
        False, "--backfill", help="cold start: kind ごと直近 --limit 件を種に取り込む"
    ),
    limit: int = typer.Option(
        100,
        "--limit",
        min=5,
        help="--backfill 時の kind ごと読取上限(最悪コスト = 3 × limit × 単価。GH#84)",
    ),
    kind: list[str] = typer.Option(
        [],
        "--kind",
        help="対象 kind を限定(bookmark / like / self_rt。複数可・既定 = 全種)。"
        "過去ブックマークの item 化 backfill 等、読取を 1 kind に絞る用途(GH#99)",
    ),
) -> None:
    """X 興味シグナル(🔖ブックマーク・❤️いいね・🔁自RT)を取り込む(S1b = GH#58)。"""
    import httpx

    from app.config import load_config, load_env
    from app.connectors.x_oauth2 import ENV_CLIENT_SECRET, XOAuth2Session
    from app.connectors.x_signals import KIND_ENDPOINTS, collect_x_signals

    unknown_kinds = sorted(set(kind) - set(KIND_ENDPOINTS))
    if unknown_kinds:
        typer.echo(
            f"--kind が不正: {', '.join(unknown_kinds)}"
            f"(有効値: {', '.join(KIND_ENDPOINTS)})",
            err=True,
        )
        raise typer.Exit(code=1)
    from app.db import apply_migrations, connect
    from app.logs import DEFAULT_LOG_DIR, setup_logging

    config = load_config(config_dir)
    env = load_env(config_dir)
    if config.x_oauth2.client_id is None:
        typer.echo("[x_oauth2].client_id が未設定(runbook §9 → intel x-auth)", err=True)
        raise typer.Exit(code=1)
    if config.x_signals.daily_read_cap == 0 and not backfill:
        typer.echo(
            "[x_signals].daily_read_cap が 0(取込無効)。有効化は合算予算の再配分"
            "(ADR 0001)、cold start は --backfill(kind ごと直近 --limit 件)",
            err=True,
        )
        raise typer.Exit(code=1)
    conn = connect(config.db_path)
    apply_migrations(conn, config.db_path)
    logger = setup_logging(DEFAULT_LOG_DIR, "morning.log")
    with httpx.Client() as client:
        session = XOAuth2Session.load(
            client=client,
            config_dir=config_dir,
            client_id=config.x_oauth2.client_id,
            client_secret=env.get(ENV_CLIENT_SECRET),
        )
        totals = collect_x_signals(
            conn,
            config,
            session,
            logger,
            backfill_limit=limit if backfill else None,
            kinds=tuple(kind) or None,
        )
    typer.echo(f"reads={totals['api_reads']} new_signals={totals['new_signals']}")


@app.command(name="x-insights")
def x_insights(
    config_dir: Path = typer.Option(DEFAULT_CONFIG_DIR, help="config.toml / .env の置き場"),
) -> None:
    """X 興味シグナルの週次インサイト(常連著者・頻出テーマ。読取専用 = GH#58 S1c)。"""
    import sqlite3

    from app.config import load_config
    from app.insights import render_insights

    config = load_config(config_dir)
    # 読取専用契約: connect()/apply_migrations は DB 作成・WAL 化・migration 適用の
    # 書込を行うため使わない。未初期化・未 migration は変更せず fail fast する
    try:
        conn = sqlite3.connect(f"file:{config.db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        typer.echo(
            f"DB がまだ無い({config.db_path})。先に intel run / collect で初期化する",
            err=True,
        )
        raise typer.Exit(code=1) from None
    conn.row_factory = sqlite3.Row
    try:
        typer.echo(render_insights(conn, config))
    except sqlite3.OperationalError as e:
        typer.echo(f"DB が読めない({e})。migration 未適用なら intel run で適用", err=True)
        raise typer.Exit(code=1) from None
    finally:
        conn.close()


@app.command()
def poll(
    config_dir: Path = typer.Option(DEFAULT_CONFIG_DIR, help="config.toml / .env の置き場"),
) -> None:
    """受信デーモン(long polling 常駐+内蔵タイマー)。"""
    from app.config import load_config, load_env
    from app.db import apply_migrations, connect
    from app.delivery import create_api, run_poller
    from app.logs import DEFAULT_LOG_DIR, setup_logging

    config = load_config(config_dir)
    env = load_env(config_dir)
    conn = connect(config.db_path)
    apply_migrations(conn, config.db_path)
    logger = setup_logging(DEFAULT_LOG_DIR, "poller.log")
    run_poller(conn, config, create_api(env), logger)


@app.command()
def run(
    config_dir: Path = typer.Option(DEFAULT_CONFIG_DIR, help="config.toml / .env の置き場"),
    force_digest: bool = typer.Option(False, "--force", help="当日 digest を置換再生成する"),
) -> None:
    """朝バッチ: collect→x_signals→extract→analyze→digest→export→deliver→dig→purge→backup。

    各段を直列実行する。x_signals は config の x_signals.daily_read_cap > 0、dig は
    dig.daily_cap > 0 のときのみ(段の順序の正本は設計 00 §2)。
    """
    from ulid import ULID

    from app.analysis.llm import create_engine
    from app.config import load_config, load_env
    from app.db import apply_migrations, connect
    from app.http import HttpClient
    from app.logs import DEFAULT_LOG_DIR, setup_logging
    from app.run import notify_llm_fallback, run_pipeline

    config = load_config(config_dir)
    env = load_env(config_dir)
    conn = connect(config.db_path)
    apply_migrations(conn, config.db_path)
    logger = setup_logging(DEFAULT_LOG_DIR, "morning.log")
    http = HttpClient()
    run_id = str(ULID())  # ログ・digests・analyses を貫通(設計 05 §3)
    failures = run_pipeline(
        conn,
        config,
        env,
        _build_connectors(config, env, http, conn),
        http,
        lambda: create_engine(config.llm.engine),
        logger,
        run_id,
        force_digest=force_digest,
    )
    # 予備(GH#156)を使ったら知らせる。段が成功していても Codex は止まっているため
    notify_llm_fallback(conn, config, env, run_id, logger)
    if failures:
        # 朝バッチ失敗通知(設計 05 §4)。通知の失敗は notify 側でログのみに握る
        from app.delivery import create_api, is_telegram_configured, notify_run_failure

        notified = False
        if is_telegram_configured(config, env):
            try:
                notified = notify_run_failure(
                    conn, config, create_api(env), failures, run_id, logger,
                    digest_held=failures.digest_held,  # GH#191
                    deliver_date=failures.deliver_date,  # GH#247
                )
            except Exception as e:  # create_api の設定不備等でも終了コードを汚さない
                logger.error(f"失敗通知を送れない: {e}", extra={"run_id": run_id, "stage": "run"})
        if notified:
            # 材料不足のリプライ指定は、この通知を送れた後に外す(GH#337)。送れなかった日は
            # 指定のまま残し、次の朝の実行がもう一度知らせてから外す
            from app.deepdive import release_material_request

            for candidate in failures.material_requests:
                release_material_request(conn, candidate, logger, run_id)
        typer.echo(f"失敗した段: {', '.join(failures)}(run_id={run_id})", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"run 完了(run_id={run_id})")


@app.command()
def doctor(
    config_dir: Path = typer.Option(DEFAULT_CONFIG_DIR, help="config.toml / .env の置き場"),
    purge: bool = typer.Option(False, "--purge", help="期限切れ削除の単独実行"),
    cost: bool = typer.Option(False, "--cost", help="コスト集計の単発表示"),
) -> None:
    """構成不備・故障・コスト超過の検査(14項目)。終了コード: FAIL あり=1。"""
    from app.config import load_config
    from app.doctor import has_failure, llm_cost_text, month_cost_summary, run_doctor
    from app.logs import DEFAULT_LOG_DIR, setup_logging

    if purge or cost:
        from app.db import apply_migrations, connect
        from app.purge import purge_expired

        config = load_config(config_dir)
        conn = connect(config.db_path)
        apply_migrations(conn, config.db_path)
        if purge:
            logger = setup_logging(DEFAULT_LOG_DIR, "morning.log")
            counts = purge_expired(conn, config, logger)
            typer.echo(
                f"purge 完了: documents {counts['documents_deleted']} 件削除 / "
                f"raw_payload {counts['raw_payload_nullified']} 件 / "
                f"text_excerpt {counts['x_signal_excerpts_nullified']} 件 NULL 化"
            )
        if cost:
            summary = month_cost_summary(conn, config)
            typer.echo(
                f"当月実績 ${summary['spent_usd']:.2f}"
                f"({llm_cost_text(summary)} / X ${summary['x_usd']:.2f})\n"
                f"着地見込み ${summary['projected_usd']:.2f} / 上限 ${summary['cap_usd']:.0f}"
            )
        return

    results = run_doctor(config_dir)
    icons = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌", "SKIP": "⏭ "}
    for r in results:
        typer.echo(f"#{r.number:>2} {icons[r.status]} [{r.status}] {r.name}: {r.detail}")
    if has_failure(results):
        raise typer.Exit(code=1)


def main() -> None:
    from app.config import ConfigError

    try:
        app()
    except ConfigError as e:
        # 設定の不備(config.toml の欠落を含む。GH#338)は traceback でなく、どのファイルの
        # 何が悪いかを 1 行で出して止める。各コマンドは DB に接続する前に設定を読む
        typer.echo(f"設定エラー: {e}", err=True)
        raise SystemExit(1) from None


if __name__ == "__main__":  # poller が `python -m app.cli answer` で起動する(GH#126)
    main()
