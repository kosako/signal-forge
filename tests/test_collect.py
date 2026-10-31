import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.collect import collect_all
from app.config import CollectConfig
from app.connectors.base import FatalError, FetchResult, FetchStats, RawItem, RetryableError
from app.connectors.x import XConnector, XCredentials
from app.connectors.x_search import XSearchConnector
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


def test_x_home_transport_failure_on_second_page_keeps_first_page_and_cost(
    conn: sqlite3.Connection,
) -> None:
    # GH#182: ホームTL の 2 ページ目で通信のリトライが尽きても、1 ページ目の項目を保存し、
    # 読み取りの件数・費用を台帳に残す(doctor #9 の当月の集計に入れる)。ソースは失敗として
    # 数え、カーソル(since_id)は進めない
    conn.execute(
        "INSERT INTO sources (type, name, cursor, last_success_at) "
        "VALUES ('x', 'home', '{\"since_id\": \"100\"}', '2026-10-20T21:30:00Z')"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users/me"):
            return httpx.Response(200, json={"data": {"id": "me"}})
        if "pagination_token" in request.url.params:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={
            "data": [
                {"id": "300", "text": "一つめの投稿", "author_id": "a1",
                 "created_at": "2026-10-21T09:00:00.000Z"},
                {"id": "299", "text": "二つめの投稿", "author_id": "a1",
                 "created_at": "2026-10-21T08:00:00.000Z"},
            ],
            "meta": {"next_token": "tok1"},
        })

    connector = XConnector(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        credentials=XCredentials(
            api_key="ck", api_secret="cs", access_token="at", access_token_secret="ats"
        ),
        daily_read_cap=130,
        unit_price_usd=0.004,
        sleep=lambda _s: None,
    )
    collect_all(conn, {"rss": FakeConnector([]), "x": connector}, logger)

    stored = conn.execute(
        "SELECT external_id FROM items WHERE source_id = "
        "(SELECT id FROM sources WHERE type='x') ORDER BY external_id"
    ).fetchall()
    assert [r["external_id"] for r in stored] == ["299", "300"]
    run = conn.execute(
        "SELECT status, error, items_fetched, api_reads, estimated_cost_usd FROM collect_runs "
        "WHERE source_id = (SELECT id FROM sources WHERE type='x')"
    ).fetchone()
    assert (run["status"], run["items_fetched"], run["api_reads"]) == ("error", 2, 2)
    assert run["estimated_cost_usd"] == pytest.approx(2 * 0.004)
    assert "接続失敗" in run["error"]
    source = conn.execute(
        "SELECT cursor, last_success_at, last_error, consecutive_failures "
        "FROM sources WHERE type='x'"
    ).fetchone()
    assert json.loads(source["cursor"]) == {"since_id": "100"}
    assert source["last_success_at"] == "2026-10-20T21:30:00Z"
    assert "RetryableError" in source["last_error"]
    assert source["consecutive_failures"] == 1


def test_search_rerun_skipping_failed_slot_keeps_source_failure(
    conn: sqlite3.Connection,
) -> None:
    # GH#331: 上限 35・3 スロットで先の 2 スロットが計 6 件を読み、最後が 400 になった日。同じ日の
    # 再実行は残り 29 を 10/10/9 に分け、最後のスロットを要求しない。その実行が正常に終わっても、
    # 最後のスロットの成否は確かめていないので、ソースの失敗の状態を消さない(doctor・watchdog・
    # 週次の健康診断に残す)。全スロットを要求して成功した実行だけが失敗の状態を解除する
    conn.execute(
        "INSERT INTO sources (type, name, config, last_success_at) "
        "VALUES ('x', 'x:search', '{\"mode\": \"search\"}', '2026-10-20T21:30:00Z')"
    )
    broken = {"c": True}
    # 同じ日の再実行だけが返す新しい投稿(先の実行の id と重ならない)。その実行の取得ぶんが
    # 保存されたことを、次の実行の前に確かめるため
    rerun = {"on": False}

    def handler(request: httpx.Request) -> httpx.Response:
        slot = request.url.params["query"][1]  # "(a) -is:retweet -is:reply" の a
        if slot == "c" and broken["c"]:
            return httpx.Response(400, json={})
        base = {"a": 500, "b": 700, "c": 900}[slot] + (50 if rerun["on"] else 0)
        return httpx.Response(200, json={
            "data": [
                {"id": str(base - i), "text": f"{slot} の投稿 {base - i}", "author_id": "a1",
                 "created_at": "2026-10-21T09:00:00.000Z"}
                for i in range(3)
            ],
        })

    def search(reads_used_today: int) -> XSearchConnector:
        return XSearchConnector(
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            credentials=XCredentials(
                api_key="ck", api_secret="cs", access_token="at", access_token_secret="ats"
            ),
            slots=("a", "b", "c"),
            daily_read_cap=35,
            unit_price_usd=0.004,
            reads_used_today=reads_used_today,
            sleep=lambda _s: None,
        )

    def state() -> sqlite3.Row:
        return conn.execute(
            "SELECT last_success_at, last_error, consecutive_failures FROM sources "
            "WHERE type='x'"
        ).fetchone()

    def stored() -> set[str]:
        return {
            row["external_id"]
            for row in conn.execute(
                "SELECT external_id FROM items WHERE source_id = "
                "(SELECT id FROM sources WHERE type='x')"
            )
        }

    def last_run() -> sqlite3.Row:
        return conn.execute(
            "SELECT status, api_reads FROM collect_runs WHERE source_id = "
            "(SELECT id FROM sources WHERE type='x') ORDER BY id DESC LIMIT 1"
        ).fetchone()

    collect_all(conn, {"rss": FakeConnector([]), "x": search(0)}, logger)
    assert (last_run()["status"], last_run()["api_reads"]) == ("error", 6)
    failed = state()
    assert failed["consecutive_failures"] == 1
    assert "400" in failed["last_error"]
    assert stored() == {"500", "499", "498", "700", "699", "698"}

    # 同じ日の再実行(先の 6 件を引いた残り 29 → 10/10/9)。c は要求しない
    rerun["on"] = True
    collect_all(conn, {"rss": FakeConnector([]), "x": search(6)}, logger)
    rerun["on"] = False
    assert (last_run()["status"], last_run()["api_reads"]) == ("ok", 6)
    # 要求しなかったスロットがある実行も、取れた投稿は保存する(この実行だけが返した id)
    assert {"550", "549", "548", "750", "749", "748"} <= stored()
    assert len(stored()) == 12
    assert tuple(state()) == tuple(failed)  # 成功の時刻も直近のエラーも連続失敗数も変えない
    assert len(source_failure_alerts(conn, "x", 1)) == 1

    # 翌日(残り 35 → 12/12/11)。全スロットを要求して成功したので失敗の状態を解除する
    broken["c"] = False
    collect_all(conn, {"rss": FakeConnector([]), "x": search(0)}, logger)
    assert last_run()["status"] == "ok"
    recovered = state()
    assert recovered["consecutive_failures"] == 0
    assert recovered["last_error"] is None
    assert recovered["last_success_at"] != "2026-10-20T21:30:00Z"
    assert {"900", "899", "898"} <= stored()  # c の投稿も取れた
    assert len(stored()) == 15


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


class StoreFailingFakeConnector:
    """読取の費用つきで返すが、保存で失敗する項目(解決に失敗する短縮 URL)を含む。"""

    def fetch(self, source_row, cursor):
        return FetchResult(
            items=[item("https://bit.ly/abc")],
            next_cursor={"since_id": "999"},
            stats=FetchStats(
                items_fetched=1, api_reads=50, estimated_cost_usd=0.2,
                http_status=200, duration_ms=1,
            ),
        )


@pytest.mark.parametrize(
    ("connector", "resolve_fails", "reads", "cost"),
    [
        # 部分取得のあとの認証の失敗: 読取数と費用は例外に添えて来る
        (FakeConnector([], error=FatalError("401", api_reads=7, estimated_cost_usd=0.028)),
         False, 7, 0.028),
        # fetch は返したが保存で失敗した: 取得の統計の読取数と費用
        (StoreFailingFakeConnector(), True, 50, 0.2),
        # 読む前の失敗は 0(今までどおり)
        (FakeConnector([], error=RetryableError("timeout")), False, 0, 0.0),
    ],
)
def test_failure_keeps_reads_and_cost_already_spent(
    conn: sqlite3.Connection, connector, resolve_fails: bool, reads: int, cost: float
) -> None:
    # GH#178: 失敗の行にも、それまでに読んだ件数と費用を残す(X の日次の読取上限の台帳と
    # doctor #9 の当月の費用を過少にしない)。取得ぶんは保存せず、カーソルは据え置く
    conn.execute("UPDATE sources SET cursor = '{\"etag\": \"old\"}' WHERE type = 'rss'")

    def failing_resolver(url: str) -> str:
        raise RetryableError("短縮 URL の解決に失敗")

    collect_all(
        conn, {"rss": connector}, logger, resolve_short=failing_resolver if resolve_fails else None
    )

    run = conn.execute("SELECT status, api_reads, estimated_cost_usd FROM collect_runs").fetchone()
    assert (run["status"], run["api_reads"]) == ("error", reads)
    assert run["estimated_cost_usd"] == pytest.approx(cost)
    source = conn.execute(
        "SELECT cursor, consecutive_failures FROM sources WHERE type = 'rss'"
    ).fetchone()
    assert json.loads(source["cursor"]) == {"etag": "old"}
    assert source["consecutive_failures"] == 1
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 0


class FeedsFakeConnector:
    """ソースの名前ごとの項目とカーソルを返す。読取の件数と費用も返す(失敗の行に残るかを見る)。"""

    source_type = "rss"

    def __init__(self, feeds: dict[str, tuple[list[RawItem], dict]]):
        self.feeds = feeds
        self.seen_cursors: list[tuple[str, dict]] = []

    def fetch(self, source_row, cursor):
        self.seen_cursors.append((source_row.name, cursor))
        items, next_cursor = self.feeds[source_row.name]
        return FetchResult(
            items=items,
            next_cursor=next_cursor,
            stats=FetchStats(
                items_fetched=len(items), api_reads=len(items),
                estimated_cost_usd=0.004 * len(items), http_status=200, duration_ms=1,
            ),
        )


@pytest.mark.parametrize(
    "fail_on",
    [
        # 2 件目の項目の INSERT(短縮 URL を解決した先)。1 件目は同じトランザクションで INSERT 済み
        "INSERT ON items WHEN NEW.canonical_url = 'https://a.example/a2'",
        # 全項目の INSERT のあとのカーソルの更新
        "UPDATE OF cursor ON sources WHEN NEW.name = 'feed-a'",
    ],
)
def test_store_failure_mid_transaction_rolls_back_items_and_cursor(
    conn: sqlite3.Connection, fail_on: str
) -> None:
    # 設計 02 §1: 項目の INSERT とカーソルの保存は 1 つのトランザクション。途中で失敗したら
    # 両方を戻す(カーソルだけ進んで項目が欠ける取りこぼしを起こさない)。失敗はソース単位で
    # 隔離して後ろのソースは続け、障害が去った再実行で欠落も重複も出ない(GH#208)。
    # 短縮 URL の解決はトランザクションの前に済む(GH#179)が、そのあとの保存の失敗でも何も残さない
    conn.execute(
        "UPDATE sources SET cursor = '{\"etag\": \"old\"}',"
        " last_success_at = '2026-10-20T21:30:00Z' WHERE name = 'feed-a'"
    )
    conn.execute(
        "INSERT INTO sources (type, name, config) VALUES ('rss', 'feed-b', '{\"url\": \"https://b\"}')"
    )
    connector = FeedsFakeConnector({
        "feed-a": ([item("https://a.example/a1"), item("https://bit.ly/a2")], {"etag": "new-a"}),
        "feed-b": ([item("https://b.example/b1")], {"etag": "new-b"}),
    })

    def resolver(url: str) -> str:
        return {"https://bit.ly/a2": "https://a.example/a2"}[url]

    def stored(name: str) -> list[tuple]:
        return [tuple(r) for r in conn.execute(
            "SELECT url, canonical_url, status FROM items"
            " WHERE source_id = (SELECT id FROM sources WHERE name = ?) ORDER BY canonical_url",
            (name,),
        )]

    def source(name: str) -> sqlite3.Row:
        return conn.execute(
            "SELECT cursor, last_success_at, last_error, consecutive_failures FROM sources"
            " WHERE name = ?",
            (name,),
        ).fetchone()

    conn.execute(  # この接続の保存の途中だけを失敗させる(DB の書き込みの失敗の代わり)
        f"CREATE TEMP TRIGGER fail_store BEFORE {fail_on}"
        " BEGIN SELECT RAISE(ABORT, '保存の途中の失敗'); END"
    )
    collect_all(conn, {"rss": connector}, logger, resolve_short=resolver)

    assert not conn.in_transaction  # 書き込みのトランザクションを開いたまま残さない
    assert stored("feed-a") == []  # 先に INSERT した 1 件目も戻る
    failed = source("feed-a")
    assert json.loads(failed["cursor"]) == {"etag": "old"}  # カーソルは進めない
    assert failed["last_success_at"] == "2026-10-20T21:30:00Z"
    assert "保存の途中の失敗" in failed["last_error"]
    assert failed["consecutive_failures"] == 1
    runs = conn.execute(
        "SELECT s.name, r.finished_at, r.status, r.error, r.api_reads, r.estimated_cost_usd"
        " FROM collect_runs r JOIN sources s ON s.id = r.source_id ORDER BY r.id"
    ).fetchall()
    assert [(r["name"], r["status"]) for r in runs] == [("feed-a", "error"), ("feed-b", "ok")]
    assert runs[0]["finished_at"] is not None
    assert runs[0]["error"] == "IntegrityError: 保存の途中の失敗"
    # 保存で失敗しても、読んだ件数と費用は台帳に残す(日次の読取上限と月次の費用。GH#178)
    assert runs[0]["api_reads"] == 2
    assert runs[0]["estimated_cost_usd"] == pytest.approx(2 * 0.004)
    # 後ろのソースは続けて保存する
    assert stored("feed-b") == [("https://b.example/b1", "https://b.example/b1", "new")]
    assert json.loads(source("feed-b")["cursor"]) == {"etag": "new-b"}

    # 障害が去った再実行: feed-a は据え置いたカーソルから取り直し、欠落も重複も無い
    conn.execute("DROP TRIGGER fail_store")
    collect_all(conn, {"rss": connector}, logger, resolve_short=resolver)

    assert connector.seen_cursors[2:] == [
        ("feed-a", {"etag": "old"}),
        ("feed-b", {"etag": "new-b"}),
    ]
    assert stored("feed-a") == [
        ("https://a.example/a1", "https://a.example/a1", "new"),
        ("https://bit.ly/a2", "https://a.example/a2", "new"),
    ]
    assert stored("feed-b") == [("https://b.example/b1", "https://b.example/b1", "new")]
    recovered = source("feed-a")
    assert json.loads(recovered["cursor"]) == {"etag": "new-a"}
    assert recovered["last_error"] is None
    assert recovered["consecutive_failures"] == 0
    statuses = conn.execute("SELECT status FROM collect_runs ORDER BY id").fetchall()
    assert [r["status"] for r in statuses] == ["error", "ok", "ok", "ok"]


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


def test_short_url_to_oversized_target_does_not_abort_source(conn: sqlite3.Connection) -> None:
    # 短縮 URL の転送先の本文が取得の上限を超えても、URL は解決して保存し、同じソースの
    # 他の記事とカーソルも保存する(転送先の本文は読まない。GH#328)
    class EndlessBody(httpx.SyncByteStream):
        def __iter__(self):
            while True:
                yield b"x" * (1024 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "t.co":
            return httpx.Response(301, headers={"location": "https://example.com/big.pdf"})
        return httpx.Response(200, stream=EndlessBody())

    http = HttpClient(
        transport=httpx.MockTransport(handler),
        resolve_host=lambda host: ["93.184.216.34"],
        sleep=lambda _s: None,
    )
    connector = FakeConnector([item("https://t.co/pdf"), item("https://example.com/ok")])
    collect_all(conn, {"rss": connector}, logger, resolve_short=http.resolve_short_url)
    rows = {
        r["url"]: (r["canonical_url"], r["status"]) for r in conn.execute("SELECT * FROM items")
    }
    assert rows == {
        "https://t.co/pdf": ("https://example.com/big.pdf", "new"),
        "https://example.com/ok": ("https://example.com/ok", "new"),
    }
    source = conn.execute(
        "SELECT cursor, consecutive_failures FROM sources WHERE type='rss'"
    ).fetchone()
    assert json.loads(source["cursor"]) == {"etag": "e1", "last_modified": None}
    assert source["consecutive_failures"] == 0


def test_short_url_resolved_without_holding_write_lock(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    # 短縮 URL の解決(HTTP の待ち・再試行)の間は書き込みロックを持たない(GH#179)。
    # 解決を待っている間に、別の接続(受信デーモン相当)が待たずに書き込めることで確かめる
    other = connect(tmp_path / "intel.db")
    other.execute("PRAGMA busy_timeout=0")  # ロックを持たれていれば待たずに locked で落ちる
    resolved: list[str] = []

    def slow_resolver(url: str) -> str:
        other.execute("BEGIN IMMEDIATE")
        other.execute(
            "INSERT INTO items (source_id, url, canonical_url, content_hash)"
            " VALUES (1, 'https://manual.example/1', 'https://manual.example/1', '')"
        )
        other.execute("COMMIT")
        resolved.append(url)
        return "https://example.com/resolved"

    connector = FakeConnector([item("https://example.com/plain"), item("https://bit.ly/abc")])
    try:
        collect_all(conn, {"rss": connector}, logger, resolve_short=slow_resolver)
    finally:
        other.close()

    assert resolved == ["https://bit.ly/abc"]
    rows = {r["canonical_url"]: r["source_id"] for r in conn.execute("SELECT * FROM items")}
    assert rows == {
        "https://manual.example/1": 1,
        "https://example.com/plain": 2,
        "https://example.com/resolved": 2,
    }
    # 項目とカーソルは今までどおり一緒に保存される(設計 02 §1)
    source = conn.execute(
        "SELECT cursor, consecutive_failures FROM sources WHERE type='rss'"
    ).fetchone()
    assert json.loads(source["cursor"]) == {"etag": "e1", "last_modified": None}
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


# --- 件数と時間の上限(GH#340) ---


def dated(guid: str | None, day: int | None, *, path: str | None = None) -> RawItem:
    """guid と公開日(2026-07 の日。None = 日時なし)を持つ RSS の entry。"""
    marker = path or guid
    return RawItem(
        external_id=guid,
        url=f"https://example.com/{marker}",
        title=f"記事 {marker} のタイトルとして十分な長さ",
        author=None,
        published_at=None if day is None else datetime(2026, 7, day, tzinfo=UTC),
        raw_content=f"記事 {marker} の本文",
        meta={},
    )


def guids(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT external_id FROM items ORDER BY id")]


def budget_cuts(conn: sqlite3.Connection) -> list[tuple]:
    rows = conn.execute("SELECT kind, source_id, deferred FROM budget_cuts ORDER BY id")
    return [tuple(r) for r in rows]


def saved_cursor(conn: sqlite3.Connection) -> dict:
    return json.loads(conn.execute("SELECT cursor FROM sources WHERE type='rss'").fetchone()[0])


def test_rss_cap_keeps_newest_and_retries_rest_next_run(conn: sqlite3.Connection) -> None:
    # フィードの並びは新しい順とは限らない。新しい側を残し、残りはカーソルを進めずに次で取り直す
    entries = [dated("g3", 3), dated("g5", 5), dated("g1", 1), dated("g4", 4), dated("g2", 2)]
    connector = FakeConnector(entries)
    limits = CollectConfig(rss_max_items=2)

    collect_all(conn, {"rss": connector}, logger, limits=limits)
    assert guids(conn) == ["g5", "g4"]  # 保存はフィードの並び
    assert saved_cursor(conn) == {}  # 前のカーソル(無し)のまま。304 で残りを失わない
    assert budget_cuts(conn) == [("rss_items", 2, 3)]
    assert conn.execute(
        "SELECT last_success_at FROM sources WHERE type='rss'"
    ).fetchone()[0] is not None

    # 取り込み済みの guid は上限に数えないので、次の実行で残りの新しい側が入る
    collect_all(conn, {"rss": connector}, logger, limits=limits)
    assert connector.seen_cursors[1] == {}
    assert guids(conn) == ["g5", "g4", "g3", "g2"]

    collect_all(conn, {"rss": connector}, logger, limits=limits)
    assert guids(conn) == ["g5", "g4", "g3", "g2", "g1"]
    # 残りが上限に収まった回はカーソルを進め、打ち切りの記録も足さない
    assert saved_cursor(conn) == {"etag": "e1", "last_modified": None}
    assert len(budget_cuts(conn)) == 2


def test_rss_cap_treats_undated_as_oldest_in_feed_order(conn: sqlite3.Connection) -> None:
    entries = [dated("u1", None), dated("d1", 1), dated("u2", None), dated("u3", None)]
    limits = CollectConfig(rss_max_items=3)
    collect_all(conn, {"rss": FakeConnector(entries)}, logger, limits=limits)
    assert guids(conn) == ["u1", "d1", "u2"]  # 日時のあるもの、次に日時なしをフィードの並びで


def test_rss_cap_counts_guidless_entries_as_new(conn: sqlite3.Connection) -> None:
    # guid の無い entry は既知かを安く判定できないので、新しい記事として上限に数える
    entries = [dated(None, 1, path="a"), dated(None, 2, path="b"), dated(None, 3, path="c")]
    limits = CollectConfig(rss_max_items=2)
    collect_all(conn, {"rss": FakeConnector(entries)}, logger, limits=limits)
    urls = [r[0] for r in conn.execute("SELECT canonical_url FROM items ORDER BY id")]
    assert urls == ["https://example.com/b", "https://example.com/c"]
    assert budget_cuts(conn) == [("rss_items", 2, 1)]


def test_rss_known_guids_skip_short_url_resolution(conn: sqlite3.Connection) -> None:
    # 取り込み済みの guid は保存の前に除くので、短縮 URL を解決し直さない
    entry = RawItem(
        external_id="g1", url="https://bit.ly/abc", title="短縮 URL の記事のタイトル",
        author=None, published_at=None, raw_content="本文", meta={},
    )
    resolved: list[str] = []

    def resolver(url: str) -> str:
        resolved.append(url)
        return "https://example.com/long"

    collect_all(conn, {"rss": FakeConnector([entry])}, logger, resolve_short=resolver)
    collect_all(conn, {"rss": FakeConnector([entry])}, logger, resolve_short=resolver)
    assert resolved == ["https://bit.ly/abc"]
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1


def test_collect_time_budget_defers_rest_and_rotates(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO sources (type, name, config) VALUES ('rss', 'feed-b', '{\"url\": \"https://b\"}')"
    )
    seen: list[str] = []

    class Router:
        source_type = "rss"

        def fetch(self, source_row, cursor):
            seen.append(source_row.name)
            return FakeConnector([]).fetch(source_row, cursor)

    ticks = iter([0.0, 0.0, 600.0])  # 開始・1 本目の前・2 本目の前(上限ちょうど)
    limits = CollectConfig(time_budget_s=600)
    collect_all(conn, {"rss": Router()}, logger, limits=limits, clock=lambda: next(ticks))
    assert seen == ["feed-a"]
    assert budget_cuts(conn) == [("collect_time", None, 1)]
    # 回れなかったソースは状態を変えない(次の実行へ回すだけ)
    rest = conn.execute(
        "SELECT last_success_at, last_error, consecutive_failures FROM sources WHERE name='feed-b'"
    ).fetchone()
    assert tuple(rest) == (None, None, 0)

    # 次の実行は、前回の取得が古い(取得したことが無い)ソースから回る
    collect_all(conn, {"rss": Router()}, logger, clock=lambda: 0.0)
    assert seen == ["feed-a", "feed-b", "feed-a"]
