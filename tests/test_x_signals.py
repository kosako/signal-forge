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


def rt(wrapper_id: str, source_id: str) -> dict:
    return {"id": wrapper_id, "referenced_tweets": [{"type": "retweeted", "id": source_id}]}


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


def session_with(bookmarks: list, likes: list, self_tweets: list = ()) -> FakeSession:
    return FakeSession({
        "users/me": [ME],
        "/bookmarks": bookmarks,
        "/liked_tweets": likes,
        "42/tweets": list(self_tweets),
    })


def rows(conn, kind: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM x_signals WHERE kind = ? ORDER BY tweet_id", (kind,)
    ).fetchall()


def calls_to(session: FakeSession, key: str) -> list[dict]:
    return [params for url, params in session.calls if key in url]


def test_all_kinds_recorded_with_cost(conn, tmp_path) -> None:
    alice = {"id": "u1", "username": "alice"}
    rt_source = {"id": "900", "author_id": "u9", "text": "元ツイ本文",
                 "created_at": "2026-07-12T00:00:00.000Z"}
    session = session_with(
        [page(tw("b1"), tw("b2"), users=[alice])],
        [page(tw("l1"), users=[alice])],
        [page(rt("1000", "900"), users=[{"id": "u9", "username": "bob"}],
              ref_tweets=[rt_source])],
    )
    totals = collect_x_signals(conn, make_config(tmp_path), session, logger)
    assert totals == {"api_reads": 4, "new_signals": 4}
    bookmark_rows = rows(conn, "bookmark")
    assert [r["tweet_id"] for r in bookmark_rows] == ["b1", "b2"]
    assert bookmark_rows[0]["author_username"] == "alice"
    assert bookmark_rows[0]["text_excerpt"] == "本文"
    # 自RT は RT 元ツイートに正規化(id・著者・本文とも元ツイ)
    rt_rows = rows(conn, "self_rt")
    assert [r["tweet_id"] for r in rt_rows] == ["900"]
    assert rt_rows[0]["author_username"] == "bob"
    assert rt_rows[0]["text_excerpt"] == "元ツイ本文"
    run = conn.execute("SELECT * FROM x_signal_runs").fetchone()
    assert run["status"] == "ok" and run["api_reads"] == 4
    assert run["estimated_cost_usd"] == pytest.approx(4 * 0.004)


def test_diff_stops_at_known_tweet(conn, tmp_path) -> None:
    conn.execute("INSERT INTO x_signals (kind, tweet_id) VALUES ('bookmark', 'b_old')")
    session = session_with(
        [page(tw("b_new"), tw("b_old"), next_token="page2"), page(tw("b_ancient"))], []
    )
    collect_x_signals(conn, make_config(tmp_path), session, logger)
    assert [r["tweet_id"] for r in rows(conn, "bookmark")] == ["b_new", "b_old"]
    assert len(calls_to(session, "/bookmarks")) == 1  # 既知到達で2ページ目に行かない


def test_cap_limits_reads_and_skips_later_kinds(conn, tmp_path) -> None:
    session = session_with(
        [page(*[tw(f"b{i}") for i in range(10)], next_token="more")], [page(tw("l1"))]
    )
    totals = collect_x_signals(conn, make_config(tmp_path, cap=10), session, logger)
    assert totals["api_reads"] == 10  # bookmark で予算を使い切る
    assert calls_to(session, "/bookmarks")[0]["max_results"] == 10  # 残枠を要求上限に反映
    assert rows(conn, "like") == []  # 残 0 < 5 で like は skip


def test_partial_page_failure_still_counts_reads_and_cost(conn, tmp_path) -> None:
    # 1ページ目保存後に2ページ目が失敗しても、取得済みぶんを予算・台帳に計上する
    # (過少計上で後続 kind に予算が再配分され cap を実質超過しない。SF82-01)
    session = session_with(
        [page(tw("b1"), tw("b2"), next_token="p2"), 500, 500, 500],  # リトライ分も 500
        [page(tw("l1"))],
    )
    totals = collect_x_signals(conn, make_config(tmp_path, cap=10), session, logger)
    assert totals["api_reads"] == 3  # bookmark 2(失敗前)+ like 1
    run = conn.execute("SELECT * FROM x_signal_runs").fetchone()
    assert run["status"] == "ok"  # like は成功
    assert run["api_reads"] == 3
    assert run["estimated_cost_usd"] == pytest.approx(3 * 0.004)
    assert "bookmark" in run["error"]


def test_backfill_pages_past_known_within_limit(conn, tmp_path) -> None:
    conn.execute("INSERT INTO x_signals (kind, tweet_id) VALUES ('bookmark', 'b1')")
    session = session_with([page(tw("b1"), tw("b2"), next_token="p2"), page(tw("b3"))], [])
    totals = collect_x_signals(
        conn, make_config(tmp_path, cap=0), session, logger, backfill_limit=100
    )
    assert totals["new_signals"] == 2  # b2, b3(b1 は既知でも打ち切らず先へ)
    assert [r["tweet_id"] for r in rows(conn, "bookmark")] == ["b1", "b2", "b3"]
    assert calls_to(session, "/bookmarks")[0]["max_results"] == 100  # 日次 cap でなく limit


def test_backfill_limit_bounds_each_kind_independently(conn, tmp_path) -> None:
    # 全ページ辿りのコスト事故防止(GH#84): limit は kind ごとの読取上限
    session = session_with(
        [page(*[tw(f"b{i}") for i in range(5)], next_token="more")],
        [page(*[tw(f"l{i}") for i in range(5)], next_token="more")],
    )
    totals = collect_x_signals(
        conn, make_config(tmp_path, cap=0), session, logger, backfill_limit=5
    )
    assert totals["api_reads"] == 10  # bookmark 5 + like 5(kind ごと独立の上限)
    assert len(calls_to(session, "/bookmarks")) == 1  # limit 到達で次ページに行かない
    assert calls_to(session, "/liked_tweets")[0]["max_results"] == 5


def test_backfill_limit_stops_at_page_min_boundary_across_pages(conn, tmp_path) -> None:
    # limit=104: 1ページ目 100 件(max_results=100)→ 残 4 < 5 で2ページ目に行かない
    session = session_with(
        [page(*[tw(f"b{i}") for i in range(100)], next_token="p2"), page(tw("b_x"))], []
    )
    totals = collect_x_signals(
        conn, make_config(tmp_path, cap=0), session, logger, backfill_limit=104
    )
    assert totals["api_reads"] == 100
    bookmark_calls = calls_to(session, "/bookmarks")
    assert len(bookmark_calls) == 1
    assert bookmark_calls[0]["max_results"] == 100  # min(PAGE_MAX, remaining)


def test_cli_limit_validation_and_independence(tmp_path) -> None:
    from typer.testing import CliRunner

    from app.cli import app

    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "config.toml").write_text("", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        app, ["x-signals", "--config-dir", str(conf), "--backfill", "--limit", "4"]
    )
    assert result.exit_code == 2  # 下限 5 未満は typer が拒否
    # --backfill なしの --limit は通常経路に影響しない(client_id 未設定の fail fast が先)
    result = runner.invoke(app, ["x-signals", "--config-dir", str(conf), "--limit", "50"])
    assert result.exit_code == 1
    assert "client_id" in result.output


def test_self_rt_cursor_persisted_and_used_as_since_id(conn, tmp_path) -> None:
    # 通常投稿は保存されず既知到達が効かない → since_id カーソルで差分化(SF82-05)。
    # 打ち切り時も最大 id へ進める(ホームTL と同じ規約)
    first = session_with([], [], [page(rt("1000", "900"), {"id": "990", "text": "通常"})])
    collect_x_signals(conn, make_config(tmp_path), first, logger)
    cursor = conn.execute("SELECT since_id FROM x_signal_cursors WHERE kind='self_rt'").fetchone()
    assert cursor["since_id"] == "1000"  # 通常投稿込みの最大 wrapper id
    second = session_with([], [], [page(rt("1100", "901"))])
    collect_x_signals(conn, make_config(tmp_path), second, logger)
    assert calls_to(second, "42/tweets")[0]["since_id"] == "1000"  # 次回は差分だけ読む
    assert conn.execute(
        "SELECT since_id FROM x_signal_cursors WHERE kind='self_rt'"
    ).fetchone()["since_id"] == "1100"


def test_self_rt_known_source_does_not_stop_paging(conn, tmp_path) -> None:
    # 解除→再RT(wrapper は新しいが RT 元は既知)でページングを止めない(SF82-08)。
    # self_rt の差分境界は since_id のみ
    conn.execute("INSERT INTO x_signals (kind, tweet_id) VALUES ('self_rt', '900')")
    conn.execute("INSERT INTO x_signal_cursors (kind, since_id) VALUES ('self_rt', '100')")
    session = session_with(
        [], [], [page(rt("300", "900"), next_token="p2"), page(rt("200", "901"))]
    )
    collect_x_signals(conn, make_config(tmp_path), session, logger)
    assert len(calls_to(session, "42/tweets")) == 2  # 既知 RT 元でも次ページへ進む
    assert [r["tweet_id"] for r in rows(conn, "self_rt")] == ["900", "901"]
    cursor = conn.execute(
        "SELECT since_id FROM x_signal_cursors WHERE kind='self_rt'"
    ).fetchone()
    assert cursor["since_id"] == "300"


def test_page_rolls_back_atomically_on_midpage_failure(conn, tmp_path) -> None:
    # ページ途中の失敗はページ全体を rollback(半端ページが残ると次回の既知到達
    # 判定が未保存の残りを飛ばす。SF82-09)。想定外の応答形は kind の失敗に隔離
    session = session_with([page(tw("b1"), {"text": "id なし"})], [page(tw("l1"))])
    totals = collect_x_signals(conn, make_config(tmp_path), session, logger)
    assert rows(conn, "bookmark") == []  # 先行の b1 も残らない(原子性)
    assert totals["new_signals"] == 1  # like は継続
    run = conn.execute("SELECT * FROM x_signal_runs").fetchone()
    assert run["status"] == "ok"
    assert "bookmark" in run["error"]


def test_self_rt_malformed_tweet_is_isolated_to_kind(conn, tmp_path) -> None:
    # id 無しツイートが KeyError で kind 隔離を抜けない(SF82-09 の self_rt 経路)
    session = session_with([page(tw("b1"))], [], [page({"text": "id なし"})])
    totals = collect_x_signals(conn, make_config(tmp_path), session, logger)
    assert totals["new_signals"] == 1  # bookmark は成功
    run = conn.execute("SELECT * FROM x_signal_runs").fetchone()
    assert run["status"] == "ok"
    assert "self_rt" in run["error"]


def test_non_rt_posts_are_not_signals(conn, tmp_path) -> None:
    session = session_with([], [], [page({"id": "1", "text": "自分の通常ポスト"})])
    collect_x_signals(conn, make_config(tmp_path), session, logger)
    assert rows(conn, "self_rt") == []  # 自分の発信は興味シグナルでない


def test_kind_isolation_on_auth_failure(conn, tmp_path) -> None:
    session = session_with([403], [page(tw("l1"))])
    totals = collect_x_signals(conn, make_config(tmp_path), session, logger)
    assert totals["new_signals"] == 1  # bookmark の 403(scope 不備)でも like は取る
    run = conn.execute("SELECT * FROM x_signal_runs").fetchone()
    assert run["status"] == "ok"
    assert "bookmark" in run["error"]


def test_empty_result_is_success_not_failure(conn, tmp_path) -> None:
    # 0 件は正常な結果(全滅判定に「成功した kind の有無」を使う。SF82-04)
    session = session_with([403], [page()])  # like は 200 の空
    totals = collect_x_signals(conn, make_config(tmp_path), session, logger)
    assert totals["new_signals"] == 0
    assert conn.execute("SELECT status FROM x_signal_runs").fetchone()["status"] == "ok"


def test_all_kinds_failed_is_loud(conn, tmp_path) -> None:
    session = session_with([403], [403], [403])
    with pytest.raises(XSignalsError):
        collect_x_signals(conn, make_config(tmp_path), session, logger)
    run = conn.execute("SELECT * FROM x_signal_runs").fetchone()
    assert run["status"] == "failed"  # 全滅は loud + 台帳にも failed(GH#74 と同方針)


def test_all_kinds_rate_limited_with_zero_reads_is_loud(conn, tmp_path) -> None:
    # 何も取れずに全 kind rate limit は「0 件成功」に見せない(SF82-04)
    session = session_with([429], [429], [429])
    with pytest.raises(XSignalsError, match="rate limit"):
        collect_x_signals(conn, make_config(tmp_path), session, logger)
    assert conn.execute("SELECT status FROM x_signal_runs").fetchone()["status"] == "failed"


def test_429_after_data_is_cutoff_not_failure(conn, tmp_path) -> None:
    session = session_with([page(tw("b1"), next_token="p2"), 429], [page(tw("l1"))])
    totals = collect_x_signals(conn, make_config(tmp_path), session, logger)
    assert totals["new_signals"] == 2  # 取得後の 429 は打ち切り(失敗ではない)
    run = conn.execute("SELECT * FROM x_signal_runs").fetchone()
    assert run["status"] == "ok"
    assert run["error"] is None


def test_rerun_is_idempotent(conn, tmp_path) -> None:
    make = lambda: session_with([page(tw("b1"))], [page(tw("l1"))])  # noqa: E731
    collect_x_signals(conn, make_config(tmp_path), make(), logger)
    totals = collect_x_signals(conn, make_config(tmp_path), make(), logger)
    assert totals["new_signals"] == 0
    assert conn.execute("SELECT count(*) FROM x_signals").fetchone()[0] == 2


# --- ブックマークの item 化(GH#99) ---


def linked_tw(tweet_id: str, expanded_url: str, text: str = "記事の紹介 https://t.co/x") -> dict:
    tweet = tw(tweet_id, text=text)
    tweet["entities"] = {
        "urls": [{"url": "https://t.co/x", "expanded_url": expanded_url}]
    }
    return tweet


def item_for(conn, tweet_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT items.* FROM x_signals JOIN items ON items.id = x_signals.item_id
        WHERE x_signals.kind = 'bookmark' AND x_signals.tweet_id = ?
        """,
        (tweet_id,),
    ).fetchone()


def test_bookmark_with_link_itemized_as_article(conn, tmp_path) -> None:
    session = session_with([page(linked_tw("b1", "https://example.com/post"))], [])
    collect_x_signals(conn, make_config(tmp_path), session, logger)

    item = item_for(conn, "b1")
    assert item is not None
    assert item["canonical_url"] == "https://example.com/post"
    # 本体はリンク先記事: raw_content を持たず extract 段に取得させる
    assert '"raw_content"' not in (item["raw_payload"] or "")
    source = conn.execute(
        "SELECT * FROM sources WHERE id = ?", (item["source_id"],)
    ).fetchone()
    assert source["name"] == "x:bookmark"
    assert source["enabled"] == 0  # collect 対象外(帰属専用)


def test_bookmark_without_link_itemized_as_tweet_url(conn, tmp_path) -> None:
    session = session_with([page(tw("b1", text="リンクなしの学び"))], [])
    collect_x_signals(conn, make_config(tmp_path), session, logger)

    item = item_for(conn, "b1")
    assert item is not None
    assert "/status/b1" in item["canonical_url"]
    assert "リンクなしの学び" in item["raw_payload"]  # 全文が本文(extract が使う)
    assert item["published_at"] == "2026-07-13T00:00:00Z"  # created_at を items 形式へ


def test_bookmark_internal_link_falls_back_to_tweet_url(conn, tmp_path) -> None:
    # 引用など x.com 内リンクだけのツイートは「外部記事」ではない
    session = session_with(
        [page(linked_tw("b1", "https://x.com/someone/status/123"))], []
    )
    collect_x_signals(conn, make_config(tmp_path), session, logger)
    item = item_for(conn, "b1")
    assert "/status/b1" in item["canonical_url"]


def test_bookmark_note_tweet_link_and_full_text(conn, tmp_path) -> None:
    long_text = "長文の全文" * 60
    tweet = tw("b1", text="切り詰め280字")
    tweet["note_tweet"] = {
        "text": long_text,
        "entities": {"urls": [{"expanded_url": "https://blog.example.com/a"}]},
    }
    session = session_with([page(tweet)], [])
    collect_x_signals(conn, make_config(tmp_path), session, logger)
    item = item_for(conn, "b1")
    assert item["canonical_url"] == "https://blog.example.com/a"  # note 側 entities も見る


def test_bookmark_merges_into_existing_item(conn, tmp_path) -> None:
    # 同じ記事が RSS で取込済みなら、新規 item を作らずそこへ紐付ける
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed')")
    source_id = conn.execute("SELECT id FROM sources WHERE name='feed'").fetchone()["id"]
    conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, content_hash) "
        "VALUES (?, 'https://example.com/post', 'https://example.com/post', '')",
        (source_id,),
    )
    existing_id = conn.execute("SELECT id FROM items").fetchone()["id"]

    session = session_with([page(linked_tw("b1", "https://example.com/post"))], [])
    collect_x_signals(conn, make_config(tmp_path), session, logger)

    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1
    linked = conn.execute(
        "SELECT item_id FROM x_signals WHERE tweet_id = 'b1'"
    ).fetchone()
    assert linked["item_id"] == existing_id


def test_backfill_relinks_known_signal_without_item(conn, tmp_path) -> None:
    # item 化導入前の既存シグナル(item_id NULL)を backfill 再読取で遡って item 化
    conn.execute("INSERT INTO x_signals (kind, tweet_id) VALUES ('bookmark', 'b_old')")
    session = session_with([page(linked_tw("b_old", "https://example.com/old"))], [], [])
    collect_x_signals(
        conn, make_config(tmp_path), session, logger,
        backfill_limit=10, kinds=("bookmark",),
    )
    item = item_for(conn, "b_old")
    assert item is not None
    assert item["canonical_url"] == "https://example.com/old"


def test_kinds_filter_reads_only_requested_endpoints(conn, tmp_path) -> None:
    session = session_with([page(tw("b1"))], [page(tw("l1"))], [page(tw("s1"))])
    totals = collect_x_signals(
        conn, make_config(tmp_path), session, logger, kinds=("bookmark",)
    )
    assert totals["api_reads"] == 1
    assert calls_to(session, "/liked_tweets") == []
    assert calls_to(session, "42/tweets") == []


def test_like_and_self_rt_are_not_itemized(conn, tmp_path) -> None:
    session = session_with([], [page(tw("l1"))], [])
    collect_x_signals(conn, make_config(tmp_path), session, logger)
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 0
    assert conn.execute(
        "SELECT item_id FROM x_signals WHERE tweet_id = 'l1'"
    ).fetchone()["item_id"] is None
