"""今日の IT ニュース(GH#75 の「今日のトレンド」を GH#413 で出来事の単位にした)。

その日(JST)の非 degraded 分析を読み、IT 業界の全体にとって大きい出来事を 3〜6 個選んで
日本語でまとめる。出来事ごとに、報じた記事のリンクを最大 MAX_LINKS 本付ける。ダイジェスト冒頭
(Telegram ヘッダメッセージ + Obsidian markdown)に載せ、「全体感を掴む → 個別アイテム → 深掘り」の
入口にする。下の 10 件(関心で選ぶ)とは別に、関心の低い大きいニュースもここで拾う。

- 付加機能: 失敗・対象少数は None を返して節を省く(digest 本体は止めない)
- 入力の各フィールドは未検証データ。nonce フェンス + sanitize で data として扱う
- item_ids は幻の id を allowlist で排除(接地確認。接地の無い出来事は捨てる)
- リンクの URL は items の正規 URL だけ(LLM の出力の URL は使わない)。どの記事をリンクにするかは
  コードが決める(X の投稿を後ろにし、同じ host は 1 本)
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
from pathlib import Path

from pydantic import BaseModel, Field

from app.analysis.llm import LLMEngine, LLMError, complete_with_retry
from app.jst import jst_day_bounds_utc, today_jst
from app.models import (
    NEWS_MAX_EVENTS,
    NEWS_MAX_LINKS,
    NewsEvent,
    NewsLink,
    NewsSection,
    link_host,
)
from app.prompting import fill_template, sanitize

PROMPT_VERSION = "trend-v003"
PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / f"{PROMPT_VERSION}.md"

MIN_ITEMS = 5  # これ未満は全体の出来事として意味が出ないので節を省く
# LLM 入力の上限(コスト・文脈保護)。1 日の分析の件数(日次上限。既定 150)より大きくして、
# 関心の低い大きいニュースを relevance の順で削らない(GH#413)
MAX_INPUT_ITEMS = 200
MAX_EVENTS = NEWS_MAX_EVENTS
MAX_LABEL = 40
# 出来事の要約の切り詰め上限。prompt(v003)が 120 字以内を指示するため、遵守時は発動しない
# 安全網(指示を無視した超過だけ切る)。6 件満載でもヘッダ 1 通は Telegram 4096 字に収まる
MAX_SUMMARY = 160
MAX_LINKS = NEWS_MAX_LINKS
# 個人の投稿の host。報道の記事・公式の発表より後ろに回す(リンクの順だけで、外しはしない)
_SOCIAL_HOSTS = frozenset({"x.com", "twitter.com", "mobile.twitter.com"})


class TrendTheme(BaseModel):
    label: str = Field(max_length=MAX_LABEL)
    summary: str
    item_ids: list[int]


class TrendSummary(BaseModel):
    themes: list[TrendTheme]


def synthesize_trend(
    conn: sqlite3.Connection,
    engine: LLMEngine,
    logger: logging.Logger,
    *,
    run_id: str | None = None,
) -> NewsSection | None:
    """当日の分析から今日の IT ニュースの節を作る。対象少数・失敗・接地なしは None。

    出来事の文字列は markup を含まない(consumer 側で escape/整形する)。
    """
    rows = _fetch_today_items(conn)
    if len(rows) < MIN_ITEMS:
        logger.info(
            "トレンド対象が少数のため節を省略",
            extra={"run_id": run_id, "stage": "digest", "extra": {"items": len(rows)}},
        )
        return None
    rows = rows[:MAX_INPUT_ITEMS]
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
    return _section(result, {row["item_id"]: row for row in rows})


def _fetch_today_items(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """本日 JST に分析された非 degraded アイテム(最新分析)。relevance 上位から。

    上限も持つ(未来時刻・時計ずれの分析を翌日ぶんとして拾わない)。'digested' 済みの
    item も含む(--force 再生成時に当日分を落とさない。status で絞らず analyzed_at で絞る)。
    """
    start, end = jst_day_bounds_utc(today_jst())
    return conn.execute(
        """
        SELECT items.id AS item_id, items.canonical_url,
               sources.type || ':' || sources.name AS source_label,
               a.title_ja, a.topic, a.category, a.relevance_score
        FROM items
        JOIN sources ON sources.id = items.source_id
        JOIN analyses a ON a.id = (
            SELECT id FROM analyses WHERE item_id = items.id
            ORDER BY analyzed_at DESC, id DESC LIMIT 1
        )
        WHERE a.degraded = 0 AND a.analyzed_at >= ? AND a.analyzed_at < ?
          -- ブックマークは過去の任意時点の内容のため「今日のトレンド」に混ぜない
          -- (合流ケースでも属性が残るよう x_signals の紐付けで判定。GH#99)
          AND NOT EXISTS (
            SELECT 1 FROM x_signals
            WHERE x_signals.kind = 'bookmark' AND x_signals.item_id = items.id
          )
        ORDER BY a.relevance_score DESC, items.id
        """,
        (start, end),
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
                # 自前の数値(外部由来でない)。出来事の大きさが同じくらいのときだけ使わせる
                f"relevance: {round(row['relevance_score'])}",
            ]
        )
        for row in rows
    )
    mapping = {
        "items": items,
        "schema": json.dumps(TrendSummary.model_json_schema(), ensure_ascii=False),
        "nonce": nonce,
    }
    return fill_template(template, mapping)


def _section(result: TrendSummary, rows_by_id: dict[int, sqlite3.Row]) -> NewsSection | None:
    """接地のある出来事(一覧にある id を 1 つ以上含む)だけを節にする。

    要約は MAX_SUMMARY で切り詰めて、ヘッダ 1 通が Telegram の上限を超えないようにする。
    """
    events: list[NewsEvent] = []
    for theme in result.themes:
        label = theme.label.strip()
        summary = theme.summary.strip()
        links = _pick_links(theme.item_ids, rows_by_id)
        if not label or not summary or not links:  # 幻の id だけの出来事を排除
            continue
        if len(summary) > MAX_SUMMARY:
            summary = summary[: MAX_SUMMARY - 1].rstrip() + "…"
        events.append(NewsEvent(label=label, summary=summary, links=links))
        if len(events) >= MAX_EVENTS:
            break
    return NewsSection(events=tuple(events)) if events else None


def _pick_links(item_ids: list[int], rows_by_id: dict[int, sqlite3.Row]) -> tuple[NewsLink, ...]:
    """出来事のリンクにする記事を、一覧にある id から最大 MAX_LINKS 本選ぶ。

    LLM の並べた順(報道・公式を先に)を保ったまま、X の投稿を後ろに回す。同じ host は 1 本にする
    (同じサイトの記事を並べても報道元が増えないため)。
    """
    rows: list[sqlite3.Row] = []
    for item_id in dict.fromkeys(item_ids):  # 重複を除き、順を保つ
        row = rows_by_id.get(item_id)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda row: link_host(row["canonical_url"]) in _SOCIAL_HOSTS)  # 安定ソート
    links: list[NewsLink] = []
    hosts: set[str] = set()
    for row in rows:
        host = link_host(row["canonical_url"])
        if not host or host in hosts:
            continue
        hosts.add(host)
        links.append(NewsLink(item_id=row["item_id"], url=row["canonical_url"]))
        if len(links) >= MAX_LINKS:
            break
    return tuple(links)
