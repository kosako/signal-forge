"""本文抽出 — items から documents(TTL 付き本文キャッシュ)を生成する。

- 対象: status='new' かつ documents 未生成の items(duplicate/skipped/error は対象外)
- raw_payload の raw_content(RSS 全文フィード等)から十分な本文が取れれば HTTP を
  節約する。取れないときのみ共通 HTTP クライアント(SSRF ガード付き)で取得
- failed も documents 行を作って黙って脱落させない(タイトルのみの degraded 分析に
  回る。設計 00 §6)
- expires_at は挿入時に確定: ソース別 TTL(RSS/manual 30日・X 7日。設計 01 §6)
"""

from __future__ import annotations

import json
import logging
import sqlite3

import trafilatura

from app.config import Config
from app.connectors.base import FatalError, RetryableError
from app.http import HttpClient

# これ未満の抽出結果は partial(本文の体をなしていない可能性が高く、分析品質に注意が要る)
MIN_OK_CHARS = 200


def extract_pending(
    conn: sqlite3.Connection,
    config: Config,
    http: HttpClient,
    logger: logging.Logger,
    *,
    run_id: str | None = None,
) -> dict[str, int]:
    """未抽出の items を処理し、status 別件数を返す。アイテム単位でエラー隔離する。"""
    rows = conn.execute(
        """
        SELECT items.*, sources.type AS source_type FROM items
        JOIN sources ON sources.id = items.source_id
        WHERE items.status = 'new'
          AND NOT EXISTS (SELECT 1 FROM documents WHERE documents.item_id = items.id)
        ORDER BY items.id
        """
    ).fetchall()

    counts = {"ok": 0, "partial": 0, "failed": 0}
    for row in rows:
        status = _extract_item(conn, config, http, row)
        counts[status] += 1
        if status == "failed":
            logger.warning(
                "本文抽出に失敗(degraded 分析に回る)",
                extra={"run_id": run_id, "stage": "extract", "item_id": row["id"]},
            )
    logger.info(
        "extract 完了",
        extra={"run_id": run_id, "stage": "extract", "extra": counts},
    )
    return counts


def _extract_item(
    conn: sqlite3.Connection, config: Config, http: HttpClient, row: sqlite3.Row
) -> str:
    text: str | None = None
    error: str | None = None

    raw_content = _raw_content(row)
    if raw_content:
        # 全文フィード等の同梱本文から先に試み、十分ならネットワークに出ない
        text = _extract_text(raw_content)

    if not _is_sufficient(text):
        fetched_text, error = _fetch_and_extract(http, row["canonical_url"])
        # HTTP 側が失敗しても raw_content 由来の短い本文があればそちらを使う
        if fetched_text is not None and len(fetched_text) > len(text or ""):
            text = fetched_text

    status = ("ok" if len(text) >= MIN_OK_CHARS else "partial") if text else "failed"

    ttl_days = (
        config.retention.x_cache_ttl_days
        if row["source_type"] == "x"
        else config.retention.cache_ttl_days
    )
    conn.execute(
        """
        INSERT INTO documents (item_id, extracted_text, extraction_status, extraction_error,
                               expires_at)
        VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?))
        """,
        (row["id"], text, status, error if status == "failed" else None, f"+{ttl_days} days"),
    )
    return status


def _raw_content(row: sqlite3.Row) -> str | None:
    if not row["raw_payload"]:
        return None
    return json.loads(row["raw_payload"]).get("raw_content")


def _extract_text(html: str) -> str | None:
    # 本文抽出(boilerplate 除去)を先に試み、短い断片や平文で None になったら
    # タグ除去の全文テキスト化(html2txt)、素の平文はそのまま本文扱いにする
    # (X の full_text・短い RSS summary を黙って落とさない)
    extracted = trafilatura.extract(html)
    if extracted and extracted.strip():
        return extracted.strip()
    # html2txt は断片(<p>…</p> のみ等)だと空を返すため、完全な文書に包んで渡す
    stripped = trafilatura.html2txt(f"<html><body>{html}</body></html>")
    if stripped and stripped.strip():
        return stripped.strip()
    if "<" not in html:
        return html.strip() or None
    return None


def _is_sufficient(text: str | None) -> bool:
    return text is not None and len(text) >= MIN_OK_CHARS


def _fetch_and_extract(http: HttpClient, url: str) -> tuple[str | None, str | None]:
    """(抽出テキスト, エラー文字列)。失敗は握りつぶさず文字列で返して呼び出し側が記録する。"""
    try:
        fetched = http.get(url)
    except (RetryableError, FatalError) as e:
        return None, f"{type(e).__name__}: {e}"
    if fetched.response.status_code != 200:
        return None, f"HTTP {fetched.response.status_code}"
    return _extract_text(fetched.response.text), None
