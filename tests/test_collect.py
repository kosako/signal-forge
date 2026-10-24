import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.collect import collect_all
from app.connectors.base import FatalError, FetchResult, FetchStats, RawItem, RetryableError
from app.db import apply_migrations, connect
from app.http import HttpClient
from app.source_health import X_FAILURE_ALERT, source_failure_alerts

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

    def __init__(
        self, items: list[RawItem], next_cursor: dict | None = None, error=None, moved_url=None
    ):
        self.items = items
        self.next_cursor = next_cursor or {"etag": "e1", "last_modified": None}
        self.error = error
        self.moved_url = moved_url
        self.seen_cursors: list[dict] = []

    def fetch(self, source_row, cursor):
        self.seen_cursors.append(cursor)
        if self.error is not None:
            raise self.error
        return FetchResult(
            items=self.items,
            next_cursor=self.next_cursor,
            stats=FetchStats(
                items_fetched=len(self.items),
                api_reads=0,
                estimated_cost_usd=0.0,
                http_status=200,
                duration_ms=5,
            ),
            moved_url=self.moved_url,
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


class CostFakeConnector:
    """X 相当: api_reads と estimated_cost_usd(api_reads×単価)を返す。"""

    def fetch(self, source_row, cursor):
        return FetchResult(
            items=[],
            next_cursor={"since_id": "1"},
            stats=FetchStats(
                items_fetched=0, api_reads=50, estimated_cost_usd=0.2,
                http_status=200, duration_ms=1,
            ),
        )


def test_estimated_cost_persisted_to_collect_runs(conn: sqlite3.Connection) -> None:
    # 監査の 0.0 ハードコード修正: コネクタの estimated_cost_usd が collect_runs に保存される
    conn.execute("INSERT INTO sources (type, name) VALUES ('x', 'home')")
    collect_all(conn, {"rss": FakeConnector([]), "x": CostFakeConnector()}, logger)
    row = conn.execute(
        "SELECT api_reads, estimated_cost_usd FROM collect_runs "
        "WHERE source_id = (SELECT id FROM sources WHERE type='x')"
    ).fetchone()
    assert row["api_reads"] == 50
    assert row["estimated_cost_usd"] == pytest.approx(0.2)


class PartialFakeConnector:
    """X 検索相当: 先のスロットの取得ぶんと、後ろのスロットの失敗の理由を返す(GH#183)。"""

    def fetch(self, source_row, cursor):
        return FetchResult(
            items=[item("https://x.com/i/web/status/500", external_id="500")],
            next_cursor=cursor,  # 打ち切りなので watermark は据え置き(設計 02 §2)
            stats=FetchStats(
                items_fetched=1, api_reads=1, estimated_cost_usd=0.004,
                http_status=402, duration_ms=1,
            ),
            partial_error=FatalError("X が HTTP 402: https://api.twitter.com/2/tweets/search/recent"),
        )


def test_partial_failure_saves_items_and_counts_as_source_failure(
    conn: sqlite3.Connection,
) -> None:
    # GH#183: 部分取得のあとの失敗は、取得ぶんとカーソルを保存しつつソースの失敗として数える
    # (連続失敗の警告に載せる)。成功の時刻は変えず、run は費用つきの error で残す
    conn.execute(
        "INSERT INTO sources (type, name, config, cursor, last_success_at, consecutive_failures) "
        "VALUES ('x', 'x:search', '{\"mode\": \"search\"}', '{\"since_id\": \"100\"}', "
        "'2026-10-20T21:30:00Z', 2)"
    )
    collect_all(conn, {"rss": FakeConnector([]), "x": PartialFakeConnector()}, logger)

    assert conn.execute(
        "SELECT external_id FROM items WHERE source_id = "
        "(SELECT id FROM sources WHERE type='x')"
    ).fetchall()[0][0] == "500"
    source = conn.execute(
        "SELECT cursor, last_success_at, last_error_at, last_error, consecutive_failures "
        "FROM sources WHERE type='x'"
    ).fetchone()
    assert json.loads(source["cursor"]) == {"since_id": "100"}
    assert source["last_success_at"] == "2026-10-20T21:30:00Z"
    assert source["last_error_at"] is not None
    assert "402" in source["last_error"]
    assert source["consecutive_failures"] == 3
    run = conn.execute(
        "SELECT status, error, items_fetched, api_reads, estimated_cost_usd FROM collect_runs "
        "WHERE source_id = (SELECT id FROM sources WHERE type='x')"
    ).fetchone()
    assert (run["status"], run["items_fetched"], run["api_reads"]) == ("error", 1, 1)
    assert "402" in run["error"]
    assert run["estimated_cost_usd"] == pytest.approx(0.004)  # 掛かった費用は doctor #9 に入る
    assert source_failure_alerts(conn, "x", X_FAILURE_ALERT) == [
        "「x:search」が 3 回連続で失敗(直近: FatalError: X が HTTP 402: "
        "https://api.twitter.com/2/tweets/search/recent)"
    ]


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


def test_moved_feed_is_recorded_and_cleared(conn: sqlite3.Connection) -> None:
    # 恒久移転(301)の先を残し、移転なしで取れたら消す(GH#150。config の URL は書き換えない)
    def moved() -> str | None:
        return conn.execute("SELECT moved_url FROM sources WHERE type='rss'").fetchone()[0]

    collect_all(conn, {"rss": FakeConnector([], moved_url="https://new.example/feed")}, logger)
    assert moved() == "https://new.example/feed"
    config = conn.execute("SELECT config FROM sources WHERE type='rss'").fetchone()[0]
    assert json.loads(config) == {"url": "https://a"}

    collect_all(conn, {"rss": FakeConnector([])}, logger)
    assert moved() is None


def test_failed_fetch_keeps_moved_url(conn: sqlite3.Connection) -> None:
    # 取れなかった回は移転の記録を変えない(分からないので消さない)
    collect_all(conn, {"rss": FakeConnector([], moved_url="https://new.example/feed")}, logger)
    collect_all(conn, {"rss": FakeConnector([], error=RetryableError("一時障害"))}, logger)
    assert conn.execute("SELECT moved_url FROM sources WHERE type='rss'").fetchone()[0] == (
        "https://new.example/feed"
    )


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


def test_unsplittable_url_item_recorded_as_error_and_rest_saved(
    conn: sqlite3.Connection,
) -> None:
    # 分解できない URL(urlsplit の ValueError)も 1 件の error に留め、同じソースの正常な
    # 記事とカーソルは保存する(ソース全体をロールバックしない。GH#186)
    connector = FakeConnector([item("https://example.com]/bad"), item("https://example.com/ok")])
    collect_all(conn, {"rss": connector}, logger)
    rows = {r["url"]: r["status"] for r in conn.execute("SELECT url, status FROM items")}
    assert rows == {"https://example.com]/bad": "error", "https://example.com/ok": "new"}
    source = conn.execute(
        "SELECT cursor, consecutive_failures FROM sources WHERE type='rss'"
    ).fetchone()
    assert json.loads(source["cursor"]) == {"etag": "e1", "last_modified": None}
    assert source["consecutive_failures"] == 0
    run = conn.execute("SELECT status FROM collect_runs").fetchone()
    assert run["status"] == "ok"


def test_short_url_redirecting_to_unsplittable_url_recorded_as_error(
    conn: sqlite3.Connection,
) -> None:
    # 短縮 URL の解決の途中(転送先の Location)で分解できない URL に当たっても、
    # その 1 件の error に留めて正常な記事は保存する(GH#186)
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": "https://example.com]/bad"})

    http = HttpClient(
        transport=httpx.MockTransport(handler),
        resolve_host=lambda host: ["93.184.216.34"],
        sleep=lambda _s: None,
    )
    connector = FakeConnector([item("https://t.co/bad"), item("https://example.com/ok")])
    collect_all(conn, {"rss": connector}, logger, resolve_short=http.resolve_short_url)
    rows = {r["url"]: r["status"] for r in conn.execute("SELECT url, status FROM items")}
    assert rows == {"https://t.co/bad": "error", "https://example.com/ok": "new"}
    source = conn.execute("SELECT consecutive_failures FROM sources WHERE type='rss'").fetchone()
    assert source["consecutive_failures"] == 0


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


def test_unexpected_exception_also_isolated(conn: sqlite3.Connection) -> None:
    # 分類外の例外(コネクタのバグ等)でも他ソースの collect を止めない(設計 00 §6)
    conn.execute(
        "INSERT INTO sources (type, name, config) VALUES ('rss', 'feed-b', '{\"url\": \"https://b\"}')"
    )
    crashing = FakeConnector([], error=RuntimeError("connector bug"))
    ok = FakeConnector([item("https://b/1")])

    class Router:
        source_type = "rss"

        def fetch(self, source_row, cursor):
            return (crashing if source_row.name == "feed-a" else ok).fetch(source_row, cursor)

    collect_all(conn, {"rss": Router()}, logger)

    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1  # feed-b は取れている
    run = conn.execute(
        "SELECT status, error FROM collect_runs ORDER BY id LIMIT 1"
    ).fetchone()
    assert run["status"] == "error"
    assert "RuntimeError" in run["error"]
