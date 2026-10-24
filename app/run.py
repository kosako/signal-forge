"""intel run — 朝バッチの直列実行(設計 00 §2・§6)。

順序: collect → x_signals(cap > 0 のときのみ。GH#58 S1b)→ extract → analyze →
digest → export → deliver → dig(cap > 0 のときのみ。GH#105)→ purge → backup
(backup は設計 05 §5 の「朝バッチ末尾」)。dig を deliver の後に置くのは、
ダイジェスト配信を深掘り生成(分オーダー)で遅らせないため。purge より前なのは
X 由来 documents の TTL 失効前に本文を材料として使うため。
原則「段ごとに局所化し、進めるものは進め、止まったことは必ず通知する」:
段の失敗はその段で捕捉して記録し、後続の段は実行する。失敗が1つでもあれば
呼び出し側(CLI)は非0で終了する(朝バッチ失敗通知 = 設計 05 §4 の入口)。
analyze 段が失敗して載せる記事が無い日は、空の digest を確定しない(障害の空。GH#191)。
export・deliver は当日分を扱わず、同じ日の再走で作って配信する。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable

from app.analysis.fallback import fallback_engine_factory, run_fallback_notice
from app.analysis.llm import LLMEngine
from app.backup import backup_daily
from app.collect import collect_all
from app.config import Config
from app.connectors.base import Connector
from app.delivery import create_api, deliver_digest, is_telegram_configured
from app.digest.builder import DigestHeldError, generate_digest
from app.export import export_digest, export_pending_notes
from app.extraction import extract_pending
from app.http import HttpClient
from app.jst import today_jst
from app.purge import purge_expired


class RunFailures(list[str]):
    """run_pipeline の結果。失敗した段の名前の一覧(空 = 全段成功)。

    digest_held: analyze 段の失敗で載せる記事が無く、当日の digest を確定しなかった(GH#191)。
    digest 段が DigestHeldError を受けたときだけ立てる。当日の行の有無からは推し量らない
    (実行の途中で JST の日付が変わると、作った digest を保留と取り違えるため)。
    """

    digest_held: bool = False


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
) -> RunFailures:
    """全段を直列実行し、失敗した段の名前リストを返す(空 = 全段成功。保留は digest_held)。"""
    failures = RunFailures()

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
    stage("analyze", lambda: _analyze(conn, config, env, engine_factory, logger, run_id))

    def digest() -> None:
        try:
            _digest(
                conn, config, env, engine_factory, logger, run_id, force_digest,
                hold_if_empty="analyze" in failures,
            )
        except DigestHeldError as e:
            # 障害の空。段の失敗にはしない(原因の analyze 段の失敗として通知する。GH#191)
            failures.digest_held = True
            logger.warning(
                f"{e}(復旧後に同じ日の intel run で作る)",
                extra={"run_id": run_id, "stage": "digest"},
            )

    stage("digest", digest)
    # 障害で当日の digest を確定しなかった日は、export・deliver が当日分を扱わない(GH#191)
    stage(
        "export",
        lambda: _export_today(conn, config, logger, run_id, held=failures.digest_held),
    )
    if is_telegram_configured(config, env):
        stage(
            "deliver",
            lambda: _deliver_today(conn, config, env, logger, run_id, held=failures.digest_held),
        )
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
    env: dict[str, str],
    engine_factory: Callable[[], LLMEngine],
    logger: logging.Logger,
    run_id: str,
    force_digest: bool,
    *,
    hold_if_empty: bool = False,
) -> int | None:
    """digest 段: 先に「今日のトレンド」を synthesis(付加機能・失敗は節省略)し digest に渡す。

    hold_if_empty: analyze 段が失敗した日。載せる記事が無ければ DigestHeldError(GH#191)。
    """
    from app.analysis.trend import synthesize_trend

    trend = None
    # digest が skip される場合(当日生成済み & not force)は synthesis しない(無駄な LLM 呼び)
    if force_digest or not _digest_exists_today(conn):
        try:
            # 主が失敗したら予備でやり直す(GH#156。予備が無効なら主のまま)
            engine = fallback_engine_factory(
                conn, config, env, logger, stage="trend", run_id=run_id,
                primary=lambda _effort: engine_factory(),
            )("low")
            trend = synthesize_trend(conn, engine, logger, run_id=run_id)
        except Exception as e:  # トレンドは付加機能。失敗しても digest 本体を止めない
            logger.warning(
                f"トレンド synthesis が例外(節を省略): {type(e).__name__}",
                extra={"run_id": run_id, "stage": "digest"},
            )
    return generate_digest(
        conn, config, logger, run_id=run_id, force=force_digest, trend_markdown=trend,
        hold_if_empty=hold_if_empty,
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
    env: dict[str, str],
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
    # 主が失敗したら予備でやり直す(GH#156。予備が無効なら主のまま)
    engine = fallback_engine_factory(
        conn, config, env, logger, stage="analyze", run_id=run_id,
        primary=lambda _effort: engine_factory(),
    )("low")
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
    from app.analysis.llm import create_anthropic_engine
    from app.deepdive import run_dig_stage

    api = create_api(env) if is_telegram_configured(config, env) else None
    run_dig_stage(
        conn,
        config,
        # 解説・対訳・指摘を受けた直し。主が失敗したら予備でやり直す(GH#156)
        fallback_engine_factory(conn, config, env, logger, stage="dig", run_id=run_id),
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
    conn: sqlite3.Connection,
    config: Config,
    logger: logging.Logger,
    run_id: str,
    *,
    held: bool = False,
) -> None:
    # digest 段が skip(生成済み)でも当日分を export する(再実行の冪等性)。
    # 障害で当日の digest を確定しなかった日(GH#191)は当日分が無いので書かない
    if not held:
        export_digest(conn, config, _today_digest_id(conn), logger, run_id=run_id)
    # 既配信 item に付いた感想(Telegram リプライ)を該当ノートへ投影(GH#92)
    export_pending_notes(conn, config, logger, run_id=run_id)


def _deliver_today(
    conn: sqlite3.Connection,
    config: Config,
    env: dict[str, str],
    logger: logging.Logger,
    run_id: str,
    *,
    held: bool = False,
) -> None:
    # 配信済みは deliver_digest 側で skip(再実行の冪等性)。障害で当日の digest を
    # 確定しなかった日(GH#191)は送るものが無い(同じ日の再走で作って送る)
    if held:
        return
    deliver_digest(conn, config, create_api(env), _today_digest_id(conn), logger, run_id=run_id)


def _today_digest_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT id FROM digests WHERE digest_date = ?", (today_jst(),)
    ).fetchone()
    if row is None:
        raise RuntimeError("当日の digest が存在しない(digest 段の失敗を参照)")
    return row["id"]


def notify_llm_fallback(
    conn: sqlite3.Connection,
    config: Config,
    env: dict[str, str],
    run_id: str,
    logger: logging.Logger,
) -> None:
    """朝バッチで予備(GH#156)を使ったら 1 通知らせる。

    知らせの失敗はログだけにする(朝バッチの終了コードを汚さない)。
    """
    if config.llm_fallback.engine == "":
        return
    notice = run_fallback_notice(conn, run_id, config.llm_fallback.monthly_cap_usd)
    if notice is None:
        return
    logger.warning(notice.replace("\n", " / "), extra={"run_id": run_id, "stage": "run"})
    if not is_telegram_configured(config, env) or config.telegram.owner_chat_id is None:
        return
    try:
        create_api(env).send_message(config.telegram.owner_chat_id, notice)
    except Exception as e:
        logger.error(
            f"予備の利用の知らせを送れない: {type(e).__name__}: {e}",
            extra={"run_id": run_id, "stage": "run"},
        )
