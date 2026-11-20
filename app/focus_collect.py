"""今の重点の語で Hacker News を検索し、記事として取り込む(GH#433 の 5 本目・設計 02 §6)。

朝バッチの focus 段の後・extract 段の前に走る(focus_collect 段)。取り込んだ記事は、RSS の記事と
同じく本文の抽出・事前選別・分析を通る。今の重点に当たれば、digest で点が足される(設計 03 §6)。

- 帰属先は enabled=0 の sources 行 `hn:focus`(type は rss、config の mode は hn_focus)。collect の
  段では回さない(ブックマークの `x:bookmark` と同じく、collect の「type → Connector 1:1」の契約に
  持ち込まない)。type を rss にするのは、記事の扱い(本文の抽出・保存の期間・ノートへの書き出し)が
  RSS の記事と同じだから
- 1 日(JST の暦日)に取り込むのは [focus].hn_items 件まで。同じ日の実行が取り込んだ分を引く。
  朝バッチに 2 本同時に走るのを止める仕組みは無いので、残りは保存の書き込みのトランザクションの
  中で数え直す。暦日は保存の時刻(items.fetched_at)と同じ DB の時計で決める(日付をまたいだ実行も、
  保存した記事と同じ日の使用量を数える)
- 語は今の重点の検索の語のうち、英字を含むもの(app.focus.search_terms)。外部の検索に送るのは
  語だけで、話題の名前と証拠は送らない
- 付加機能: 検索の失敗はソースの失敗として記録し(collect と同じ隔離)、朝バッチを止めない。ログには
  件数だけを書く(語は外部の本文に由来するので書かない)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from app.collect import collect_ranked
from app.config import Config
from app.connectors.hn_search import HnFocusConnector
from app.focus import search_terms
from app.http import HttpClient
from app.models import Source

SOURCE_NAME = "hn:focus"
SOURCE_MODE = "hn_focus"
_UTC_FORMAT = "'%Y-%m-%dT%H:%M:%SZ'"  # items.fetched_at の形(SQL の文字列の literal)


class FocusSourceError(Exception):
    """`hn:focus` の名前の sources 行が、この段の行の形でない(手で足した別のソースなど)。"""


@dataclass(frozen=True)
class FocusCollectResult:
    queries: int = 0  # 検索した語の数(0 = 検索しなかった)
    limit: int = 0  # 今回取り込める残りの件数


def collect_focus_items(
    conn: sqlite3.Connection,
    config: Config,
    http: HttpClient,
    logger: logging.Logger,
    *,
    run_id: str | None = None,
    now: datetime | None = None,
) -> FocusCollectResult:
    """今の重点の語で HN を検索し、点数の高い順に、その日の残りの件数まで取り込む。"""
    now = now or datetime.now(UTC)
    extra = {"run_id": run_id, "stage": "focus_collect"}
    queries = search_terms(conn, config, now=now)
    if not queries:
        logger.info("今の重点に英字の検索の語が無いので、HN を検索しない", extra=extra)
        return FocusCollectResult()
    source = ensure_source(conn)

    def remaining(db: sqlite3.Connection) -> int:
        return config.focus.hn_items - collected_today(db, source.id)

    last_id = conn.execute(
        "SELECT coalesce(max(id), 0) FROM items WHERE source_id = ?", (source.id,)
    ).fetchone()[0]
    limit = remaining(conn)
    if limit <= 0:
        logger.info("今日の HN の取り込みは上限に達している", extra=extra)
        return FocusCollectResult()
    connector = HnFocusConnector(
        http,
        queries,
        min_points=config.focus.hn_min_points,
        window_hours=config.focus.hn_window_hours,
        now=lambda: now,
    )
    # 残りは取得の後と、保存の書き込みのトランザクションの中でも数え直す(重なった実行で上限を
    # 超えない)。数える暦日は、そのときの DB の時計で決める(段を始めた now ではない)
    collect_ranked(conn, source, connector, logger, remaining=remaining, run_id=run_id)
    logger.info(
        "今の重点の語で HN を検索した",
        extra={
            **extra,
            "extra": {
                "queries": len(queries),
                "limit": limit,
                "inserted": conn.execute(
                    "SELECT count(*) FROM items WHERE source_id = ? AND id > ?",
                    (source.id, last_id),
                ).fetchone()[0],
            },
        },
    )
    return FocusCollectResult(queries=len(queries), limit=limit)


def ensure_source(conn: sqlite3.Connection) -> Source:
    """記事の帰属先の sources 行を保証して返す(enabled=0。collect の段の走査の外)。"""
    conn.execute(
        "INSERT OR IGNORE INTO sources (type, name, config, enabled) VALUES ('rss', ?, ?, 0)",
        (SOURCE_NAME, json.dumps({"mode": SOURCE_MODE})),
    )
    source = Source.from_row(
        conn.execute("SELECT * FROM sources WHERE name = ?", (SOURCE_NAME,)).fetchone()
    )
    try:
        mode = json.loads(source.config or "{}").get("mode")
    except (ValueError, AttributeError):
        mode = None
    if source.type != "rss" or source.enabled or mode != SOURCE_MODE:
        # 同じ名前の別のソースに、検索の記事を混ぜない(collect の段がフィードとして読む行など)
        raise FocusSourceError(f"sources の {SOURCE_NAME} が今の重点の検索の行の形でない")
    return source


def collected_today(conn: sqlite3.Connection, source_id: int) -> int:
    """今の JST の暦日に、このソースへ取り込んだ記事の数(日次の上限の使用量)。

    暦日は DB の時計で決める。items.fetched_at の既定値(保存の時刻)と同じ時計なので、段の途中で
    日付をまたいでも、保存した記事と同じ日の使用量を数える(段を始めた時刻で数えると、日付を
    またいだ実行が前の日の使用量を見て、次の日の上限を超える)。JST は +9 時間で、夏時間は無い。
    """
    return conn.execute(
        f"""
        SELECT count(*) FROM items
        WHERE source_id = ?
          AND fetched_at >= strftime({_UTC_FORMAT}, date('now', '+9 hours'), '-9 hours')
          AND fetched_at < strftime({_UTC_FORMAT}, date('now', '+9 hours'), '+1 day', '-9 hours')
        """,
        (source_id,),
    ).fetchone()[0]
