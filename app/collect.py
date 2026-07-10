"""intel collect — 全ソースの取り込み(設計 02 §1 の冪等性契約の実装点)。

- コネクタは RawItem を返すだけ。正規化(u1)・content_hash(h1)・2層 dedup・
  カーソル保存はここで一元化する
- カーソル更新は「そのソースの全 INSERT 成功後、同一トランザクション」
  (途中クラッシュ → カーソル未更新 → 次回重複取得 → UNIQUE で吸収)
- ソース単位のエラー隔離: 1ソースの失敗で他ソースを止めない
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable

from app.connectors.base import Connector, FatalError, RawItem, RetryableError
from app.db import insert_item_with_dedup
from app.models import Source
from app.normalize import (
    UrlError,
    content_hash,
    is_dedup_eligible,
    normalize_text_for_hash,
    normalize_url,
)

RAW_CONTENT_MAX_CHARS = 50_000  # items.raw_payload の切り詰め上限(設計 01 §6)

_NOW_UTC = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"


def collect_all(
    conn: sqlite3.Connection,
    connectors: dict[str, Connector],
    logger: logging.Logger,
    *,
    run_id: str | None = None,
    resolve_short: Callable[[str], str] | None = None,
) -> None:
    rows = conn.execute(
        "SELECT * FROM sources WHERE enabled = 1 AND type <> 'manual' ORDER BY id"
    ).fetchall()
    for row in rows:
        source = Source.from_row(row)
        connector = connectors.get(source.type)
        if connector is None:
            logger.warning(
                "コネクタ未実装のためスキップ",
                extra={"run_id": run_id, "stage": "collect", "source_id": source.id},
            )
            continue
        _collect_source(conn, source, connector, logger, run_id=run_id, resolve_short=resolve_short)


def _collect_source(
    conn: sqlite3.Connection,
    source: Source,
    connector: Connector,
    logger: logging.Logger,
    *,
    run_id: str | None,
    resolve_short: Callable[[str], str] | None,
) -> None:
    log_ctx = {"run_id": run_id, "stage": "collect", "source_id": source.id}
    run_row_id = conn.execute(
        "INSERT INTO collect_runs (source_id) VALUES (?)", (source.id,)
    ).lastrowid

    try:
        cursor = json.loads(source.cursor) if source.cursor else {}
        result = connector.fetch(source, cursor)
        counts = _store_result(conn, source, result.items, result.next_cursor, resolve_short)
    except Exception as e:
        # 分類済み(Retryable/Fatal)以外の想定外例外もソース単位で隔離する
        # (1本の壊れたフィード・コネクタのバグで他ソースの collect を止めない。
        # 設計 00 §6 / 02 §1 の「他ソースは必ず継続」の保証)
        _record_failure(conn, source, run_row_id, e)
        log = logger.warning if isinstance(e, RetryableError | FatalError) else logger.error
        log(
            f"collect 失敗。ソースをスキップ: {_safe_error_text(e)}",
            extra=log_ctx,
            exc_info=not isinstance(e, RetryableError | FatalError),
        )
        return

    conn.execute(
        f"""
        UPDATE collect_runs
        SET finished_at = {_NOW_UTC}, status = 'ok', items_fetched = ?, api_reads = ?,
            estimated_cost_usd = ?
        WHERE id = ?
        """,
        (
            result.stats.items_fetched,
            result.stats.api_reads,
            result.stats.estimated_cost_usd,  # X は api_reads×単価、RSS は 0.0(設計 02 §2)
            run_row_id,
        ),
    )
    logger.info(
        "collect 完了",
        extra={
            **log_ctx,
            "extra": {
                "http_status": result.stats.http_status,
                "duration_ms": result.stats.duration_ms,
                **counts,
            },
        },
    )


def _store_result(
    conn: sqlite3.Connection,
    source: Source,
    items: list[RawItem],
    next_cursor: dict,
    resolve_short: Callable[[str], str] | None,
) -> dict[str, int]:
    """全 RawItem の INSERT とカーソル保存を同一トランザクションで行う。"""
    counts = {"inserted": 0, "ignored": 0, "duplicate": 0, "error": 0}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for raw in items:
            counts[_store_item(conn, source, raw, resolve_short)] += 1
        conn.execute(
            f"""
            UPDATE sources
            SET cursor = ?, last_success_at = {_NOW_UTC}, consecutive_failures = 0,
                last_error = NULL
            WHERE id = ?
            """,
            (json.dumps(next_cursor), source.id),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return counts


def _store_item(
    conn: sqlite3.Connection,
    source: Source,
    raw: RawItem,
    resolve_short: Callable[[str], str] | None,
) -> str:
    try:
        canonical = normalize_url(raw.url, resolve_short=resolve_short)
    except UrlError:
        # scheme 不正等はアイテムごと reject して status='error' で記録(設計 01 §4 手順0)
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO items
              (source_id, external_id, url, canonical_url, title, author, published_at,
               content_hash, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, '', 'error')
            """,
            (
                source.id,
                raw.external_id,
                raw.url,
                raw.url,  # 正規化不能のため生 URL を UNIQUE キーに使う
                raw.title,
                raw.author,
                _iso(raw),
            ),
        )
        return "error" if cursor.rowcount else "ignored"

    normalized_text = normalize_text_for_hash(raw.title, raw.raw_content)
    external_id = raw.external_id
    if external_id is None and source.type == "rss":
        # RSS で guid が無い場合は正規化 URL の hash にフォールバック(設計 02 §3)
        external_id = "urlhash:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    outcome = insert_item_with_dedup(
        conn,
        source_id=source.id,
        external_id=external_id,
        url=raw.url,
        canonical_url=canonical,
        title=raw.title,
        author=raw.author,
        published_at=_iso(raw),
        raw_payload=_raw_payload(raw),
        content_hash=content_hash(normalized_text),
        dedup_eligible=is_dedup_eligible(normalized_text),
    )
    return outcome.outcome


def _raw_payload(raw: RawItem) -> str | None:
    """items.raw_payload の JSON 形(設計 01 §6)。無ければ NULL。"""
    payload: dict[str, object] = {}
    if raw.raw_content:
        payload["raw_content"] = raw.raw_content[:RAW_CONTENT_MAX_CHARS]
    if raw.meta:
        payload["meta"] = raw.meta
    return json.dumps(payload, ensure_ascii=False) if payload else None


def _iso(raw: RawItem) -> str | None:
    if raw.published_at is None:
        return None
    return raw.published_at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_error_text(error: Exception) -> str:
    """例外の __str__ 自体が壊れていても隔離を崩さない(分類外例外も扱うため)。"""
    try:
        return f"{type(error).__name__}: {error}"
    except Exception:
        return type(error).__name__


def _record_failure(
    conn: sqlite3.Connection, source: Source, run_row_id: int, error: Exception
) -> None:
    message = _safe_error_text(error)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            f"""
            UPDATE collect_runs
            SET finished_at = {_NOW_UTC}, status = 'error', error = ?
            WHERE id = ?
            """,
            (message, run_row_id),
        )
        conn.execute(
            f"""
            UPDATE sources
            SET last_error_at = {_NOW_UTC}, last_error = ?,
                consecutive_failures = consecutive_failures + 1
            WHERE id = ?
            """,
            (message, source.id),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
