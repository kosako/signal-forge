"""今の重点の語で HN を検索して取り込む段(GH#433 の 5 本目・app/focus_collect.py)。"""

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.config import Config, FocusConfig
from app.connectors.hn_search import ITEM_URL
from app.db import apply_migrations, connect
from app.focus_collect import (
    SOURCE_NAME,
    FocusSourceError,
    collect_focus_items,
    collected_on,
    ensure_source,
)
from tests.test_extraction import make_http

logger = logging.getLogger("test-focus-collect")


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    yield conn
    conn.close()


@pytest.fixture
def now() -> datetime:
    # 取り込んだ行の fetched_at は DB の今の時刻なので、日次の上限の暦日も今の時刻で見る
    return datetime.now(UTC)


def make_config(tmp_path: Path, **focus) -> Config:
    return Config(config_dir=tmp_path, focus=FocusConfig(enabled=True, **focus))


def add_topic(conn, name: str, terms: list[str], status: str = "active") -> int:
    return conn.execute(
        "INSERT INTO focus_topics (name, terms, status, dismissed_at) VALUES (?, ?, ?, ?)",
        (
            name,
            json.dumps(terms, ensure_ascii=False),
            status,
            "2026-11-19T00:00:00Z" if status == "dismissed" else None,
        ),
    ).lastrowid


def hit(story_id: str, points: int, now: datetime, *, url: str | None = None) -> dict:
    return {
        "objectID": story_id,
        "title": f"story {story_id}",
        "url": url or f"https://example.com/{story_id}",
        "points": points,
        "created_at_i": int((now - timedelta(hours=1)).timestamp()),
        "author": "someone",
    }


def responder(by_query: dict[str, object], seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(urlsplit(str(request.url)).query)["query"][0]
        seen.append(query)
        answer = by_query.get(query, [])
        if isinstance(answer, int):
            return httpx.Response(answer, text="error")
        return httpx.Response(200, json={"hits": answer})

    return handler


def stored(conn) -> list[tuple]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT i.external_id, i.url, i.title, i.status FROM items i "
            "JOIN sources s ON s.id = i.source_id WHERE s.name = ? ORDER BY i.id",
            (SOURCE_NAME,),
        )
    ]


def test_collects_the_best_new_posts_into_the_focus_source(conn, tmp_path, now) -> None:
    terms = ["ソフトウェアファクトリー", "software factory", "Claude Code"]
    add_topic(conn, "ソフトウェアファクトリー", terms)
    add_topic(conn, "候補", ["candidate term"], status="candidate")
    add_topic(conn, "外した", ["dismissed term"], status="dismissed")
    # ほかのソースと同じ URL の投稿は除き、上限にも数えない
    conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, title, content_hash) "
        "VALUES (1, 'https://example.com/9', 'https://example.com/9', 't', 'sha256:x')"
    )
    seen: list[str] = []
    http = make_http(
        responder(
            {
                "software factory": [hit("9", 90, now), hit("1", 20, now), hit("2", 10, now)],
                "Claude Code": [hit("3", 30, now)],
            },
            seen,
        )
    )

    result = collect_focus_items(
        conn, make_config(tmp_path, hn_items=2), http, logger, run_id="R", now=now
    )

    # 日本語だけの語・候補と外した話題の語は送らない
    assert seen == ["software factory", "Claude Code"]
    assert (result.queries, result.limit) == (2, 2)
    assert stored(conn) == [
        (ITEM_URL.format(id="3"), "https://example.com/3", "story 3", "new"),
        (ITEM_URL.format(id="1"), "https://example.com/1", "story 1", "new"),
    ]
    source = conn.execute("SELECT * FROM sources WHERE name = ?", (SOURCE_NAME,)).fetchone()
    assert (source["type"], source["enabled"]) == ("rss", 0)
    assert json.loads(source["config"]) == {"mode": "hn_focus"}
    assert source["consecutive_failures"] == 0 and source["last_success_at"] is not None
    run = conn.execute("SELECT status, items_fetched FROM collect_runs").fetchone()
    assert tuple(run) == ("ok", 4)


def test_daily_limit_counts_what_the_same_day_already_collected(conn, tmp_path, now) -> None:
    add_topic(conn, "話題", ["software factory"])
    source = ensure_source(conn)
    for n in range(4):
        conn.execute(
            "INSERT INTO items (source_id, url, canonical_url, title, content_hash) "
            "VALUES (?, ?, ?, 't', ?)",
            (source.id, f"https://old/{n}", f"https://old/{n}", f"sha256:{n}"),
        )
    seen: list[str] = []
    answer = {"software factory": [hit("1", 20, now), hit("2", 10, now)]}
    http = make_http(responder(answer, seen))
    config = make_config(tmp_path, hn_items=5)

    assert collect_focus_items(conn, config, http, logger, now=now).limit == 1
    assert [row[0] for row in stored(conn)][-1] == ITEM_URL.format(id="1")
    assert collected_on(conn, source.id, now) == 5

    # 上限に達した日は検索しない
    assert collect_focus_items(conn, config, http, logger, now=now).limit == 0
    assert seen == ["software factory"]
    # 次の暦日は数え直す
    assert collected_on(conn, source.id, now + timedelta(days=1)) == 0


def test_no_latin_terms_makes_no_request_and_no_source(conn, tmp_path, now) -> None:
    add_topic(conn, "日本語だけ", ["日本語の語"])
    seen: list[str] = []
    http = make_http(responder({}, seen))
    result = collect_focus_items(conn, make_config(tmp_path), http, logger, now=now)
    assert result.queries == 0 and seen == []
    assert conn.execute("SELECT 1 FROM sources WHERE name = ?", (SOURCE_NAME,)).fetchone() is None


def test_search_failure_is_recorded_on_the_source_without_raising(conn, tmp_path, now) -> None:
    add_topic(conn, "話題", ["software factory"])
    http = make_http(responder({"software factory": 403}, []))
    collect_focus_items(conn, make_config(tmp_path), http, logger, now=now)
    source = conn.execute("SELECT * FROM sources WHERE name = ?", (SOURCE_NAME,)).fetchone()
    assert source["consecutive_failures"] == 1 and "403" in source["last_error"]
    assert conn.execute("SELECT status FROM collect_runs").fetchone()[0] == "error"
    assert stored(conn) == []


def test_refuses_a_different_source_row_with_the_same_name(conn, tmp_path, now) -> None:
    # 手で足した RSS など、collect の段が読む行に検索の記事を混ぜない
    conn.execute(
        "INSERT INTO sources (type, name, config) VALUES ('rss', ?, ?)",
        (SOURCE_NAME, json.dumps({"url": "https://feed.example/rss"})),
    )
    add_topic(conn, "話題", ["software factory"])
    seen: list[str] = []
    with pytest.raises(FocusSourceError):
        collect_focus_items(
            conn, make_config(tmp_path), make_http(responder({}, seen)), logger, now=now
        )
    assert seen == []
