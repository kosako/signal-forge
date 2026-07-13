import logging
import sqlite3
from pathlib import Path

import httpx
import pytest

from app.config import Config, XSignalsConfig
from app.connectors.x_signals import XSignalsError, collect_x_signals
from app.db import apply_migrations, connect

logger = logging.getLogger("test-x-signals")

ME = {"data": {"id": "42", "username": "kosako"}}


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    yield conn
    conn.close()


def make_config(tmp_path: Path, cap: int = 50) -> Config:
    return Config(config_dir=tmp_path, x_signals=XSignalsConfig(daily_read_cap=cap))


def tw(tweet_id: str, author: str = "u1", text: str = "本文") -> dict:
    return {
        "id": tweet_id,
        "author_id": author,
        "text": text,
        "created_at": "2026-07-13T00:00:00.000Z",
    }


def page(*tweets: dict, next_token: str | None = None, users=(), ref_tweets=()) -> dict:
    body: dict = {"data": list(tweets)}
    includes: dict = {}
    if users:
        includes["users"] = list(users)
    if ref_tweets:
        includes["tweets"] = list(ref_tweets)
    if includes:
        body["includes"] = includes
    if next_token:
        body["meta"] = {"next_token": next_token}
    return body


class FakeSession:
    """URL 部分一致 → ページ列(dict = 200 body / int = ステータス)の順次応答。"""

    def __init__(self, routes: dict[str, list]):
        self.routes = {key: list(pages) for key, pages in routes.items()}
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, params: dict | None = None) -> httpx.Response:
        self.calls.append((url, dict(params or {})))
        for key, pages in self.routes.items():
            if key in url:
                if not pages:
                    return httpx.Response(200, json={"data": []})
                item = pages.pop(0)
                if isinstance(item, int):
                    return httpx.Response(item)
                return httpx.Response(200, json=item)
        raise AssertionError(f"経路未定義の URL: {url}")


def oauth2_with(bookmarks: list, likes: list) -> FakeSession:
    return FakeSession({"users/me": [ME], "/bookmarks": bookmarks, "/liked_tweets": likes})


def rows(conn, kind: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM x_signals WHERE kind = ? ORDER BY tweet_id", (kind,)
    ).fetchall()


def test_bookmarks_likes_self_rt_recorded_with_cost(conn, tmp_path) -> None:
    alice = {"id": "u1", "username": "alice"}
    oauth2 = oauth2_with(
        [page(tw("b1"), tw("b2"), users=[alice])], [page(tw("l1"), users=[alice])]
    )
    rt_source = {"id": "s1", "author_id": "u9", "text": "元ツイ本文",
                 "created_at": "2026-07-12T00:00:00.000Z"}
    oauth1 = FakeSession({
        "42/tweets": [page(
            {"id": "t1", "referenced_tweets": [{"type": "retweeted", "id": "s1"}]},
            users=[{"id": "u9", "username": "bob"}],
            ref_tweets=[rt_source],
        )],
    })
    totals = collect_x_signals(conn, make_config(tmp_path), oauth2, oauth1, logger)
    assert totals == {"api_reads": 4, "new_signals": 4}
    bookmark_rows = rows(conn, "bookmark")
    assert [r["tweet_id"] for r in bookmark_rows] == ["b1", "b2"]
    assert bookmark_rows[0]["author_username"] == "alice"
    assert bookmark_rows[0]["text_excerpt"] == "本文"
    # 自RT は RT 元ツイートに正規化(id・著者・本文とも元ツイ)
    rt_rows = rows(conn, "self_rt")
    assert [r["tweet_id"] for r in rt_rows] == ["s1"]
    assert rt_rows[0]["author_username"] == "bob"
    assert rt_rows[0]["text_excerpt"] == "元ツイ本文"
    run = conn.execute("SELECT * FROM x_signal_runs").fetchone()
    assert run["status"] == "ok" and run["api_reads"] == 4
    assert run["estimated_cost_usd"] == pytest.approx(4 * 0.004)


def test_diff_stops_at_known_tweet(conn, tmp_path) -> None:
    conn.execute(
        "INSERT INTO x_signals (kind, tweet_id) VALUES ('bookmark', 'b_old')"
    )
    oauth2 = oauth2_with(
        [page(tw("b_new"), tw("b_old"), next_token="page2"), page(tw("b_ancient"))],
        [],
    )
    collect_x_signals(conn, make_config(tmp_path), oauth2, None, logger)
    assert [r["tweet_id"] for r in rows(conn, "bookmark")] == ["b_new", "b_old"]
    bookmark_calls = [c for c in oauth2.calls if "/bookmarks" in c[0]]
    assert len(bookmark_calls) == 1  # 既知到達で2ページ目に行かない


def test_cap_limits_reads_and_skips_later_kinds(conn, tmp_path) -> None:
    oauth2 = oauth2_with(
        [page(*[tw(f"b{i}") for i in range(10)], next_token="more")],
        [page(tw("l1"))],
    )
    totals = collect_x_signals(conn, make_config(tmp_path, cap=10), oauth2, None, logger)
    assert totals["api_reads"] == 10  # bookmark で予算を使い切る
    bookmark_calls = [c for c in oauth2.calls if "/bookmarks" in c[0]]
    assert bookmark_calls[0][1]["max_results"] == 10  # 残枠を要求上限に反映
    assert rows(conn, "like") == []  # 残 0 < 5 で like は skip


def test_backfill_follows_all_pages_ignoring_known_and_cap(conn, tmp_path) -> None:
    conn.execute("INSERT INTO x_signals (kind, tweet_id) VALUES ('bookmark', 'b1')")
    oauth2 = oauth2_with(
        [page(tw("b1"), tw("b2"), next_token="p2"), page(tw("b3"))], []
    )
    totals = collect_x_signals(
        conn, make_config(tmp_path, cap=0), oauth2, None, logger, backfill=True
    )
    assert totals["new_signals"] == 2  # b2, b3(b1 は既知でも打ち切らず先へ)
    assert [r["tweet_id"] for r in rows(conn, "bookmark")] == ["b1", "b2", "b3"]
    bookmark_calls = [c for c in oauth2.calls if "/bookmarks" in c[0]]
    assert bookmark_calls[0][1]["max_results"] == 100  # cap 無視(全件)


def test_non_rt_posts_are_not_signals(conn, tmp_path) -> None:
    oauth1 = FakeSession({"42/tweets": [page({"id": "t1", "text": "自分の通常ポスト"})]})
    collect_x_signals(conn, make_config(tmp_path), oauth2_with([], []), oauth1, logger)
    assert rows(conn, "self_rt") == []  # 自分の発信は興味シグナルでない


def test_kind_isolation_on_auth_failure(conn, tmp_path) -> None:
    oauth2 = FakeSession(
        {"users/me": [ME], "/bookmarks": [403], "/liked_tweets": [page(tw("l1"))]}
    )
    totals = collect_x_signals(conn, make_config(tmp_path), oauth2, None, logger)
    assert totals["new_signals"] == 1  # bookmark の 403(scope 不備)でも like は取る
    run = conn.execute("SELECT * FROM x_signal_runs").fetchone()
    assert run["status"] == "ok"
    assert "bookmark" in run["error"]


def test_all_kinds_failed_is_loud(conn, tmp_path) -> None:
    oauth2 = FakeSession({"users/me": [ME], "/bookmarks": [403], "/liked_tweets": [403]})
    with pytest.raises(XSignalsError):
        collect_x_signals(conn, make_config(tmp_path), oauth2, None, logger)
    run = conn.execute("SELECT * FROM x_signal_runs").fetchone()
    assert run["status"] == "failed"  # 全滅は loud + 台帳にも failed(GH#74 と同方針)


def test_429_stops_kind_without_error(conn, tmp_path) -> None:
    oauth2 = oauth2_with([429], [page(tw("l1"))])
    totals = collect_x_signals(conn, make_config(tmp_path), oauth2, None, logger)
    assert totals["new_signals"] == 1  # rate limit は待たず打ち切り、他 kind は継続
    assert conn.execute("SELECT count(*) FROM x_signal_runs").fetchone()[0] == 1


def test_rerun_is_idempotent(conn, tmp_path) -> None:
    pages = lambda: oauth2_with([page(tw("b1"))], [page(tw("l1"))])  # noqa: E731
    collect_x_signals(conn, make_config(tmp_path), pages(), None, logger)
    totals = collect_x_signals(conn, make_config(tmp_path), pages(), None, logger)
    assert totals["new_signals"] == 0
    assert conn.execute("SELECT count(*) FROM x_signals").fetchone()[0] == 2


def test_missing_oauth1_skips_self_rt_only(conn, tmp_path) -> None:
    oauth2 = oauth2_with([page(tw("b1"))], [])
    totals = collect_x_signals(conn, make_config(tmp_path), oauth2, None, logger)
    assert totals["new_signals"] == 1
    assert rows(conn, "self_rt") == []
