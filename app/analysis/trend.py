"""今日の全体トレンドまとめ(GH#75)。

その日(JST)の非 degraded 分析を topic をヒントに 3〜5 テーマへ束ね、日本語で俯瞰要約する。
ダイジェスト冒頭(Telegram ヘッダメッセージ + Obsidian markdown)に載せ、
「全体感を掴む → 個別アイテム → 深掘り」の入口にする。

- 付加機能: 失敗・対象少数は None を返して節を省く(digest 本体は止めない)
- 入力の各フィールドは未検証データ。nonce フェンス + sanitize で data として扱う
- item_ids は幻の id を allowlist で排除(接地確認。接地の無いテーマは捨てる)
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from app.analysis.analyzer import sanitize
from app.analysis.llm import LLMEngine, LLMError, complete_with_retry
from app.config import Config

JST = timezone(timedelta(hours=9))

PROMPT_VERSION = "trend-v001"
PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / f"{PROMPT_VERSION}.md"

MIN_ITEMS = 5  # これ未満は全体トレンドとして意味が出ないので節を省く
MAX_INPUT_ITEMS = 120  # LLM 入力の上限(コスト・文脈保護。relevance 上位を優先)
MAX_THEMES = 5
MAX_LABEL = 40


class TrendTheme(BaseModel):
    label: str = Field(max_length=MAX_LABEL)
    summary: str
    item_ids: list[int]


class TrendSummary(BaseModel):
    themes: list[TrendTheme]


def synthesize_trend(
    conn: sqlite3.Connection,
    config: Config,
    engine: LLMEngine,
    logger: logging.Logger,
    *,
    run_id: str | None = None,
) -> str | None:
    """当日の分析を俯瞰したトレンド markdown を返す。対象少数・失敗時は None。"""
    rows = _fetch_today_items(conn)
    if len(rows) < MIN_ITEMS:
        logger.info(
            "トレンド対象が少数のため節を省略",
            extra={"run_id": run_id, "stage": "digest", "extra": {"items": len(rows)}},
        )
        return None
    rows = rows[:MAX_INPUT_ITEMS]
    valid_ids = {row["item_id"] for row in rows}
    nonce = secrets.token_hex(4)
    prompt = _build_prompt(rows, nonce)
    try:
        result = complete_with_retry(engine, prompt, TrendSummary)
    except LLMError as e:
        # 付加機能なので失敗しても digest 本体は止めない(節を省くだけ)
        logger.warning(
            f"トレンド synthesis 失敗(節を省略): {e}",
            extra={"run_id": run_id, "stage": "digest"},
        )
        return None
    return _render(result, valid_ids) or None


def _today_jst_start_utc() -> str:
    """本日(JST 暦日)0:00 を UTC の ISO 文字列で返す(analyzed_at の下限)。"""
    start = datetime.now(UTC).astimezone(JST).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_today_items(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """本日 JST に分析された非 degraded アイテム(最新分析)。relevance 上位から。"""
    return conn.execute(
        """
        SELECT items.id AS item_id,
               sources.type || ':' || sources.name AS source_label,
               a.title_ja, a.topic, a.category
        FROM items
        JOIN sources ON sources.id = items.source_id
        JOIN analyses a ON a.id = (
            SELECT id FROM analyses WHERE item_id = items.id
            ORDER BY analyzed_at DESC, id DESC LIMIT 1
        )
        WHERE a.degraded = 0 AND a.analyzed_at >= ?
        ORDER BY a.relevance_score DESC, items.id
        """,
        (_today_jst_start_utc(),),
    ).fetchall()


def _build_prompt(rows: list[sqlite3.Row], nonce: str) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    items = "\n".join(
        f"[id={row['item_id']}] "
        + " | ".join(
            [
                sanitize(row["title_ja"] or "(タイトルなし)", 120, nonce),
                f"topic: {sanitize(row['topic'], 60, nonce)}",
                f"category: {sanitize(row['category'], 40, nonce)}",
                f"source: {sanitize(row['source_label'], 60, nonce)}",
            ]
        )
        for row in rows
    )
    mapping = {
        "items": items,
        "schema": json.dumps(TrendSummary.model_json_schema(), ensure_ascii=False),
        "nonce": nonce,
    }
    # 単一パス置換(入力由来の {{...}} を展開しない。analyzer と同方針)
    return re.sub(r"\{\{(\w+)\}\}", lambda m: mapping[m.group(1)], template)


def _render(result: TrendSummary, valid_ids: set[int]) -> str:
    """接地のあるテーマ(実在 id を1つ以上含む)だけを markdown 節にする。"""
    lines = ["## 今日のトレンド", ""]
    rendered = 0
    for theme in result.themes:
        label = theme.label.strip()
        summary = theme.summary.strip()
        grounded = any(i in valid_ids for i in theme.item_ids)  # 幻テーマを排除
        if not label or not summary or not grounded:
            continue
        lines.append(f"- **{label}** — {summary}")
        rendered += 1
        if rendered >= MAX_THEMES:
            break
    if rendered == 0:
        return ""
    lines.append("")
    return "\n".join(lines)
