"""intel run — 朝バッチの直列実行(設計 00 §2・§6)。

順序: collect → x_signals(cap > 0 のときのみ。GH#58 S1b)→ extract → analyze →
digest → export → deliver → dig(cap > 0 のときのみ。GH#105)→ purge → backup
(backup は設計 05 §5 の「朝バッチ末尾」)。dig を deliver の後に置くのは、
ダイジェスト配信を深掘り生成(分オーダー)で遅らせないため。purge より前なのは
X 由来 documents の TTL 失効前に本文を材料として使うため。
原則「段ごとに局所化し、進めるものは進め、止まったことは必ず通知する」:
段の失敗はその段で捕捉して記録し、後続の段は実行する。失敗が1つでもあれば
呼び出し側(CLI)は非0で終了する(朝バッチ失敗通知 = 設計 05 §4 の入口)。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable

from app.analysis.llm import LLMEngine
from app.backup import backup_daily
from app.collect import collect_all
from app.config import Config
from app.connectors.base import Connector
from app.delivery import create_api, deliver_digest, is_telegram_configured
from app.digest.builder import generate_digest
from app.export import export_digest, export_pending_notes
from app.extraction import extract_pending
from app.http import HttpClient
from app.jst import today_jst
from app.purge import purge_expired


def run_pipeline(
    conn: sqlite3.Connection,
    config: Config,
    env: dict[str, str],
    connectors: dict[str, Connector],
    http: HttpClient,
    engine_factory: Callable[[], LLMEngine],
    logger: logging.Logger,
    run_id: str,
    *,
    force_digest: bool = False,
) -> list[str]:
    """全段を直列実行し、失敗した段の名前リストを返す(空 = 全段成功)。"""
    failures: list[str] = []

    def stage(name: str, action: Callable[[], object]) -> None:
        try:
            action()
        except Exception as e:
            failures.append(name)
            logger.error(
                f"{name} 段が失敗(後続の段は継続): {type(e).__name__}: {e}",
                extra={"run_id": run_id, "stage": name},
                exc_info=True,  # 障害調査用に traceback を JSON ログへ残す
            )

    stage("collect", lambda: collect_all(
        conn, connectors, logger, run_id=run_id, resolve_short=http.resolve_short_url
    ))
    if config.x_signals.daily_read_cap > 0:  # 0 = S1b 取込無効(段ごと省く)
        stage("x_signals", lambda: _collect_x_signals(conn, config, env, logger, run_id))
    stage("extract", lambda: extract_pending(conn, config, http, logger, run_id=run_id))
    stage("analyze", lambda: _analyze(conn, config, engine_factory, logger, run_id))
    stage("digest", lambda: _digest(conn, config, engine_factory, logger, run_id, force_digest))
    stage("export", lambda: _export_today(conn, config, logger, run_id))
    if is_telegram_configured(config, env):
        stage("deliver", lambda: _deliver_today(conn, config, env, logger, run_id))
    else:
        # 完全未設定 = M2a 前の未セットアップ。欠落検知は doctor #4 の責務(設計 05 §2)
        logger.warning(
            "Telegram 未設定のため deliver 段を skip",
            extra={"run_id": run_id, "stage": "deliver"},
        )
    if config.dig.daily_cap > 0:  # 0 = 調査段無効(段ごと省く。x_signals と同じ流儀)
        stage("dig", lambda: _dig(conn, config, env, logger, run_id))
    stage("purge", lambda: purge_expired(conn, config, logger, run_id=run_id))
    stage("backup", lambda: backup_daily(conn, config, logger, run_id=run_id))

    logger.info(
        "run 完了",
        extra={"run_id": run_id, "stage": "run", "extra": {"failures": failures}},
    )
    return failures


def _digest(
    conn: sqlite3.Connection,
    config: Config,
    engine_factory: Callable[[], LLMEngine],
    logger: logging.Logger,
    run_id: str,
    force_digest: bool,
) -> int | None:
    """digest 段: 先に「今日のトレンド」を synthesis(付加機能・失敗は節省略)し digest に渡す。"""
    from app.analysis.trend import synthesize_trend

    trend = None
    # digest が skip される場合(当日生成済み & not force)は synthesis しない(無駄な LLM 呼び)
    if force_digest or not _digest_exists_today(conn):
        try:
            trend = synthesize_trend(conn, engine_factory(), logger, run_id=run_id)
        except Exception as e:  # トレンドは付加機能。失敗しても digest 本体を止めない
            logger.warning(
                f"トレンド synthesis が例外(節を省略): {type(e).__name__}",
                extra={"run_id": run_id, "stage": "digest"},
            )
    return generate_digest(
        conn, config, logger, run_id=run_id, force=force_digest, trend_markdown=trend
    )


def _digest_exists_today(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM digests WHERE digest_date = ?", (today_jst(),)
        ).fetchone()
        is not None
    )


def _collect_x_signals(
    conn: sqlite3.Connection,
    config: Config,
    env: dict[str, str],
    logger: logging.Logger,
    run_id: str,
) -> None:
    """x_signals 段(S1b = GH#58)。cap > 0(取込を意図)なのに未認可・client_id 欠落は
    設定不備として段の失敗に倒す(黙って skip しない)。"""
    import httpx

    from app.connectors.x_oauth2 import ENV_CLIENT_SECRET, XOAuth2Session
    from app.connectors.x_signals import collect_x_signals

    if config.x_oauth2.client_id is None:
        raise RuntimeError("[x_signals].daily_read_cap > 0 だが [x_oauth2].client_id 未設定")
    with httpx.Client() as client:
        session = XOAuth2Session.load(
            client=client,
            config_dir=config.config_dir,
            client_id=config.x_oauth2.client_id,
            client_secret=env.get(ENV_CLIENT_SECRET),
        )
        collect_x_signals(conn, config, session, logger, run_id=run_id)


def _analyze(
    conn: sqlite3.Connection,
    config: Config,
    engine_factory: Callable[[], LLMEngine],
    logger: logging.Logger,
    run_id: str,
) -> None:
    # プロファイル欠落・エンジン未実装は analyze 段の失敗として局所化する
    # (import をここに置くのは循環回避でなく段の失敗系に含めるため)
    from app.analysis.analyzer import analyze_selected
    from app.analysis.profile import load_interest_profile
    from app.analysis.stage1 import preselect

    profile = load_interest_profile(config.profile_path)
    engine = engine_factory()
    result = preselect(conn, config, logger, run_id=run_id)
    analyze_selected(
        conn, config, engine, profile.body, result.selected_ids, logger, run_id=run_id
    )


def _dig(
    conn: sqlite3.Connection,
    config: Config,
    env: dict[str, str],
    logger: logging.Logger,
    run_id: str,
) -> None:
    """dig 段(GH#105)。エンジンは effort 可変で生成する(解説 = config / 対訳 = low)。

    Telegram 未設定でもノート生成は行い、カードは台帳(dig_results)に未送信で残す
    (deliver 段の skip と同じく、欠落検知は doctor #4 の責務)。
    """
    from app.analysis.llm import create_anthropic_engine, create_engine
    from app.deepdive import run_dig_stage

    api = create_api(env) if is_telegram_configured(config, env) else None
    run_dig_stage(
        conn,
        config,
        lambda effort: create_engine(config.llm.engine, model_reasoning_effort=effort),
        api,
        logger,
        run_id=run_id,
        # 別系統の AI のレビュー(GH#153)。キーが無ければレビューだけが失敗し、深掘りは届く
        review_engine_factory=(
            (lambda: create_anthropic_engine(config.anthropic_api, env))
            if config.dig.review
            else None
        ),
    )


def _export_today(
    conn: sqlite3.Connection, config: Config, logger: logging.Logger, run_id: str
) -> None:
    # digest 段が skip(生成済み)でも当日分を export する(再実行の冪等性)
    export_digest(conn, config, _today_digest_id(conn), logger, run_id=run_id)
    # 既配信 item に付いた感想(Telegram リプライ)を該当ノートへ投影(GH#92)
    export_pending_notes(conn, config, logger, run_id=run_id)


def _deliver_today(
    conn: sqlite3.Connection,
    config: Config,
    env: dict[str, str],
    logger: logging.Logger,
    run_id: str,
) -> None:
    # 配信済みは deliver_digest 側で skip(再実行の冪等性)
    deliver_digest(conn, config, create_api(env), _today_digest_id(conn), logger, run_id=run_id)


def _today_digest_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT id FROM digests WHERE digest_date = ?", (today_jst(),)
    ).fetchone()
    if row is None:
        raise RuntimeError("当日の digest が存在しない(digest 段の失敗を参照)")
    return row["id"]
