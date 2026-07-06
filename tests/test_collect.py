import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.collect import collect_all
from app.connectors.base import FatalError, FetchResult, FetchStats, RawItem, RetryableError
from app.db import apply_migrations, connect

logger = logging.getLogger("test-collect")


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    conn.execute(
        "INSERT INTO sources (type, name, config) VALUES ('rss', 'feed-a', '{\"url\": \"https://a\"}')"
    )
    yield conn
    conn.close()


class FakeConnector:
    source_type = "rss"

    def __init__(self, items: list[RawItem], next_cursor: dict | None = None, error=None):
        self.items = items
        self.next_cursor = next_cursor or {"etag": "e1", "last_modified": None}
        self.error = error
        self.seen_cursors: list[dict] = []

    def fetch(self, source_row, cursor):
        self.seen_cursors.append(cursor)
        if self.error is not None:
            raise self.error
        return FetchResult(
            items=self.items,
            next_cursor=self.next_cursor,
            stats=FetchStats(
                items_fetched=len(self.items), api_reads=0, http_status=200, duration_ms=5
            ),
        )


def item(url: str, *, external_id=None, title=None, body=None) -> RawItem:
    # 既定の title/body は item ごとに変える(同一だと 24h 内容 dedup が発動する。
    # dedup 自体は tests/test_dedup.py が担当)。URL 文字列は h1 正規化で除去される
    # ため、識別子にはパス末尾を使う
    marker = url.rsplit("/", 1)[-1]
    return RawItem(
        external_id=external_id,
        url=url,
        title=title if title is not None else f"記事 {marker} のタイトルとして十分な長さ",
        author=None,
        published_at=datetime(2026, 7, 5, 9, 0, tzinfo=UTC),
        raw_content=body if body is not None else f"記事 {marker} の本文",
        meta={},
    )


def test_items_inserted_and_cursor_saved(conn: sqlite3.Connection) -> None:
    connector = FakeConnector([item("https://example.com/1"), item("https://example.com/2/")])
    collect_all(conn, {"rss": connector}, logger)

    rows = conn.execute(
        "SELECT canonical_url, status, published_at FROM items ORDER BY id"
    ).fetchall()
    assert [r["canonical_url"] for r in rows] == ["https://example.com/1", "https://example.com/2"]
    assert all(r["status"] == "new" for r in rows)
    assert rows[0]["published_at"] == "2026-07-05T09:00:00Z"

    source = conn.execute(
        "SELECT cursor, last_success_at, consecutive_failures FROM sources WHERE type='rss'"
    ).fetchone()
    assert json.loads(source["cursor"]) == {"etag": "e1", "last_modified": None}
    assert source["last_success_at"] is not None
    assert source["consecutive_failures"] == 0

    run = conn.execute("SELECT status, items_fetched FROM collect_runs").fetchone()
    assert (run["status"], run["items_fetched"]) == ("ok", 2)


def test_rerun_with_same_cursor_is_idempotent(conn: sqlite3.Connection) -> None:
    connector = FakeConnector([item("https://example.com/1")])
    collect_all(conn, {"rss": connector}, logger)
    before = conn.execute("SELECT * FROM items").fetchall()
    collect_all(conn, {"rss": connector}, logger)
    after = conn.execute("SELECT * FROM items").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]  # 冪等性契約
    # 2回目は保存済みカーソルが渡る
    assert connector.seen_cursors[1] == {"etag": "e1", "last_modified": None}


def test_error_source_isolated_and_recorded(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO sources (type, name, config) VALUES ('rss', 'feed-b', '{\"url\": \"https://b\"}')"
    )
    failing = FakeConnector([], error=RetryableError("timeout"))
    ok = FakeConnector([item("https://example.com/1")])

    class Router:
        source_type = "rss"

        def fetch(self, source_row, cursor):
            return (failing if source_row.name == "feed-a" else ok).fetch(source_row, cursor)

    collect_all(conn, {"rss": Router()}, logger)

    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1  # feed-b は取れている
    failed = conn.execute(
        "SELECT consecutive_failures, last_error FROM sources WHERE name='feed-a'"
    ).fetchone()
    assert failed["consecutive_failures"] == 1
    assert "timeout" in failed["last_error"]
    runs = conn.execute("SELECT status FROM collect_runs ORDER BY id").fetchall()
    assert {r["status"] for r in runs} == {"error", "ok"}


def test_fatal_error_also_isolated(conn: sqlite3.Connection) -> None:
    collect_all(conn, {"rss": FakeConnector([], error=FatalError("403"))}, logger)
    run = conn.execute("SELECT status, error FROM collect_runs").fetchone()
    assert run["status"] == "error"
    assert "FatalError" in run["error"]


def test_invalid_scheme_item_recorded_as_error(conn: sqlite3.Connection) -> None:
    connector = FakeConnector([item("javascript:alert(1)"), item("https://example.com/ok")])
    collect_all(conn, {"rss": connector}, logger)
    rows = {r["url"]: r["status"] for r in conn.execute("SELECT url, status FROM items")}
    assert rows["javascript:alert(1)"] == "error"
    assert rows["https://example.com/ok"] == "new"


def test_rss_external_id_fallback_to_urlhash(conn: sqlite3.Connection) -> None:
    collect_all(conn, {"rss": FakeConnector([item("https://example.com/1")])}, logger)
    row = conn.execute("SELECT external_id FROM items").fetchone()
    assert row["external_id"].startswith("urlhash:")


def test_unknown_connector_type_skipped(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO sources (type, name) VALUES ('x', 'home-tl')")
    collect_all(conn, {"rss": FakeConnector([item("https://example.com/1")])}, logger)
    # x はスキップ、rss は成功。collect_runs は rss の1行のみ
    assert conn.execute("SELECT count(*) FROM collect_runs").fetchone()[0] == 1


def test_manual_source_not_collected(conn: sqlite3.Connection) -> None:
    connector = FakeConnector([item("https://example.com/1")])
    collect_all(conn, {"rss": connector}, logger)
    assert len(connector.seen_cursors) == 1  # manual(id=1)は対象外で rss の1回のみ
