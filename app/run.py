"""intel run — 朝バッチの直列実行(設計 00 §2・§6)。

順序: collect → extract → analyze → digest → export → deliver → purge → backup
(backup は設計 05 §5 の「朝バッチ末尾」)。
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
from app.digest.builder import generate_digest, today_jst
from app.export import export_digest
from app.extraction import extract_pending
from app.http import HttpClient
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
    stage("extract", lambda: extract_pending(conn, config, http, logger, run_id=run_id))
    stage("analyze", lambda: _analyze(conn, config, engine_factory, logger, run_id))
    stage("digest", lambda: generate_digest(
        conn, config, logger, run_id=run_id, force=force_digest
    ))
    stage("export", lambda: _export_today(conn, config, logger, run_id))
    if is_telegram_configured(config, env):
        stage("deliver", lambda: _deliver_today(conn, config, env, logger, run_id))
    else:
        # 完全未設定 = M2a 前の未セットアップ。欠落検知は doctor #4 の責務(設計 05 §2)
        logger.warning(
            "Telegram 未設定のため deliver 段を skip",
            extra={"run_id": run_id, "stage": "deliver"},
        )
    stage("purge", lambda: purge_expired(conn, config, logger, run_id=run_id))
    stage("backup", lambda: backup_daily(conn, config, logger, run_id=run_id))

    logger.info(
        "run 完了",
        extra={"run_id": run_id, "stage": "run", "extra": {"failures": failures}},
    )
    return failures


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


def _export_today(
    conn: sqlite3.Connection, config: Config, logger: logging.Logger, run_id: str
) -> None:
    # digest 段が skip(生成済み)でも当日分を export する(再実行の冪等性)
    export_digest(conn, config, _today_digest_id(conn), logger, run_id=run_id)


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
