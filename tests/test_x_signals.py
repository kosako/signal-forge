import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.analysis.stage1 import preselect
from app.config import Config, XSignalsConfig
from app.connectors.x_oauth2 import (
    OAUTH2_SCOPES,
    TOKEN_URL,
    USERS_ME_URL,
    XOAuth2Session,
    XOAuth2Token,
)
from app.connectors.x_signals import XSignalsError, collect_x_signals, signal_reads_on
from app.db import apply_migrations, connect
from app.jst import today_jst
from app.normalize import normalize_url

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
    """URL 部分一致 → ページ列(dict = 200 body / int = ステータス)の応答。

    ページは要求の pagination_token で引く(トークン無し = 先頭ページ、トークンあり =
    そのトークンを next_token に持つページの次。GH#256)。トークンを落とす回帰では
    先頭ページが返るので、取得結果と読取数のずれで検出できる。末尾ページの next_token
    の先(未定義の続き)は空の 200 を返し、発行していないトークンは AssertionError にする。
    実 API と同じく、ページの data は max_results 件までに切り詰めて返す
    (要求したページの大きさで読取数が決まることを再現する。GH#174)。
    """

    def __init__(self, routes: dict[str, list]):
        self.routes = {key: list(pages) for key, pages in routes.items()}
        self.token_index: dict[str, dict[str, int]] = {}
        for key, pages in self.routes.items():
            index: dict[str, int] = {}
            for i, item in enumerate(pages):
                token = item.get("meta", {}).get("next_token") if isinstance(item, dict) else None
                if token:
                    assert token not in index, f"{key} のページのトークンが重複: {token}"
                    index[token] = i + 1
            self.token_index[key] = index
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, params: dict | None = None) -> httpx.Response:
        params = dict(params or {})
        self.calls.append((url, params))
        for key, pages in self.routes.items():
            if key in url:
                token = params.get("pagination_token")
                if token is None:
                    position = 0
                else:
                    assert token in self.token_index[key], f"未発行のページのトークン: {token}"
                    position = self.token_index[key][token]
                if position >= len(pages):
                    return httpx.Response(200, json={"data": []})
                item = pages[position]
                if isinstance(item, int):
                    return httpx.Response(item)
                if "max_results" in params and "data" in item:
                    item = {**item, "data": item["data"][: params["max_results"]]}
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


def page_tokens(session: FakeSession, key: str) -> list[str | None]:
    """要求ごとの pagination_token(無しは None)。2 ページ目以降の引数の検証用(GH#256)。"""
    return [params.get("pagination_token") for params in calls_to(session, key)]


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
    # 新着が続く間はページを進め、強シグナル(bookmark)が先に予算を使う(GH#174)
    session = session_with(
        [
            page(*[tw(f"b{i:02d}") for i in range(5)], next_token="p2"),
            page(*[tw(f"b{i:02d}") for i in range(5, 12)], next_token="more"),
        ],
        [page(tw("l1"))],
    )
    totals = collect_x_signals(conn, make_config(tmp_path, cap=12), session, logger)
    assert totals["api_reads"] == 12  # bookmark で予算を使い切る
    # 差分取得の 1 ページ目は最小ページ、既知到達で止まらなければ 2 ページ目から残枠を要求上限に反映
    assert [c["max_results"] for c in calls_to(session, "/bookmarks")] == [5, 7]
    assert page_tokens(session, "/bookmarks") == [None, "p2"]  # 2 ページ目は next_token で要求
    assert [r["tweet_id"] for r in rows(conn, "bookmark")] == [f"b{i:02d}" for i in range(12)]
    assert calls_to(session, "/liked_tweets") == []  # 残 0 < 5 で like は skip
    assert rows(conn, "like") == []


DAY = "2026-10-24"  # 日次 cap の判定に使う当日(JST)。実時計に依存させない(GH#178)


def run_on(conn, monkeypatch, day: str, utc_time: str, run) -> dict[str, int]:
    """当日を day に固定して取込を 1 回走らせ、その台帳の行の時刻を utc_time に揃える。"""
    monkeypatch.setattr("app.connectors.x_signals.today_jst", lambda: day)
    before = conn.execute("SELECT coalesce(max(id), 0) AS id FROM x_signal_runs").fetchone()["id"]
    totals = run()
    conn.execute("UPDATE x_signal_runs SET started_at = ? WHERE id > ?", (utc_time, before))
    return totals


def test_second_run_same_day_reads_only_remaining_budget(conn, tmp_path, monkeypatch) -> None:
    # GH#178: cap は暦日の上限。同じ日の 2 回目の実行(朝バッチの再走・単独の intel x-signals)は
    # 台帳(x_signal_runs)の当日の合計を引いた残りだけを読む。翌日(JST)は枠が戻る
    config = make_config(tmp_path, cap=15)

    def collect(session):
        return lambda: collect_x_signals(conn, config, session, logger)

    first = session_with(
        [page(*[tw(f"b{i}") for i in range(5)])], [page(*[tw(f"l{i}") for i in range(5)])]
    )
    totals = run_on(conn, monkeypatch, DAY, "2026-10-23T21:30:00Z", collect(first))  # JST 6:30
    assert totals["api_reads"] == 10
    second = session_with(
        [page(*[tw(f"b{i}") for i in range(5, 10)], next_token="more")],
        [page(*[tw(f"l{i}") for i in range(5, 10)])],
    )
    totals = run_on(conn, monkeypatch, DAY, "2026-10-24T14:59:59Z", collect(second))
    assert totals["api_reads"] == 5  # 残り 5 = bookmark の最小ページ 1 枚ぶん
    assert [c["max_results"] for c in calls_to(second, "/bookmarks")] == [5]
    assert calls_to(second, "/liked_tweets") == []  # 残 0 < 5 で skip
    third = session_with([page(tw("b_x"))], [page(tw("l_x"))])
    totals = run_on(conn, monkeypatch, DAY, "2026-10-24T14:59:59Z", collect(third))
    assert totals["api_reads"] == 0
    assert calls_to(third, "/bookmarks") == [] and calls_to(third, "/liked_tweets") == []
    # 翌日(JST 0:00 以降)は日次 cap が丸ごと戻る
    next_day = session_with([page(tw("b_y"))], [page(tw("l_y"))])
    totals = run_on(conn, monkeypatch, "2026-10-25", "2026-10-24T15:00:00Z", collect(next_day))
    assert totals["api_reads"] == 2
    reads = [r["api_reads"] for r in conn.execute("SELECT api_reads FROM x_signal_runs")]
    assert reads == [10, 5, 0, 2]  # 当日の合計は cap(15)ちょうど


def test_signal_reads_on_sums_jst_calendar_day(conn) -> None:
    # GH#178: 当日 = JST の暦日(UTC 15:00 が日付の境界)。失敗の行の読取も数え、backfill の
    # 行(別枠)は数えない
    for started_at, status, reads, backfill in (
        ("2026-10-23T14:59:59Z", "ok", 7, 0),  # JST 10/23 23:59:59 = 前日
        ("2026-10-23T15:00:00Z", "ok", 10, 0),  # JST 10/24 0:00 = 当日の始まり
        ("2026-10-24T06:00:00Z", "failed", 3, 0),
        ("2026-10-24T07:00:00Z", "ok", 100, 1),  # backfill
        ("2026-10-24T14:59:59Z", "ok", 20, 0),  # JST 10/24 23:59:59 = 当日の終わり
        ("2026-10-24T15:00:00Z", "ok", 40, 0),  # JST 10/25 0:00 = 翌日
    ):
        conn.execute(
            "INSERT INTO x_signal_runs (started_at, status, api_reads, backfill) "
            "VALUES (?, ?, ?, ?)",
            (started_at, status, reads, backfill),
        )
    assert signal_reads_on(conn, "2026-10-24") == 33
    assert signal_reads_on(conn, "2026-10-23") == 7
    assert signal_reads_on(conn, "2026-10-26") == 0


def test_known_bookmarks_leave_budget_for_later_kinds(conn, tmp_path) -> None:
    # 既知のブックマークが上限以上あり新着が無い日も、既知の読み直しで予算を
    # 使い切らず like / self_rt を取り込む(GH#174)
    known = [tw(f"b{i:02d}") for i in range(20)]
    for tweet in known:
        conn.execute(
            "INSERT INTO x_signals (kind, tweet_id) VALUES ('bookmark', ?)", (tweet["id"],)
        )
    session = session_with(
        [page(*known, next_token="older")], [page(tw("l1"))], [page(rt("1000", "900"))]
    )
    totals = collect_x_signals(conn, make_config(tmp_path, cap=15), session, logger)
    # 1 ページ目は最小ページで、既知到達で次ページに行かない
    assert [c["max_results"] for c in calls_to(session, "/bookmarks")] == [5]
    assert calls_to(session, "/liked_tweets")[0]["max_results"] == 5
    assert [r["tweet_id"] for r in rows(conn, "like")] == ["l1"]
    assert [r["tweet_id"] for r in rows(conn, "self_rt")] == ["900"]
    assert totals == {"api_reads": 7, "new_signals": 2}  # bookmark 5 + like 1 + self_rt 1


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
    assert totals["api_reads"] == 3  # 1 ページ目 2 件 + 2 ページ目 1 件(先頭の読み直しなし)
    assert [r["tweet_id"] for r in rows(conn, "bookmark")] == ["b1", "b2", "b3"]
    assert calls_to(session, "/bookmarks")[0]["max_results"] == 100  # 日次 cap でなく limit
    assert page_tokens(session, "/bookmarks") == [None, "p2"]  # 2 ページ目は next_token で要求


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


def test_backfill_does_not_use_daily_budget(conn, tmp_path, monkeypatch) -> None:
    # GH#178: backfill は日次 cap の外の別枠。その読取は台帳に backfill = 1 で残り(費用には
    # 入る)、同じ日の通常の取込は日次 cap を丸ごと使える(--kind bookmark の backfill で
    # like / self_rt が止まらない)
    config = make_config(tmp_path, cap=10)
    backfill = session_with([page(*[tw(f"old_b{i}") for i in range(10)], next_token="more")], [])
    totals = run_on(conn, monkeypatch, DAY, "2026-10-24T01:00:00Z", lambda: collect_x_signals(
        conn, config, backfill, logger, backfill_limit=10, kinds=("bookmark",)
    ))
    assert totals["api_reads"] == 10
    assert signal_reads_on(conn, DAY) == 0  # 通常の取込の使用量に数えない
    normal = session_with([page(tw("b_new"))], [page(*[tw(f"l{i}") for i in range(5)])])
    totals = run_on(conn, monkeypatch, DAY, "2026-10-24T02:00:00Z", lambda: collect_x_signals(
        conn, config, normal, logger
    ))
    assert totals["api_reads"] == 6  # bookmark 1 + like 5(残り 4 < 5 で self_rt は skip)
    assert [r["tweet_id"] for r in rows(conn, "like")] == [f"l{i}" for i in range(5)]
    runs = conn.execute("SELECT api_reads, backfill FROM x_signal_runs ORDER BY id").fetchall()
    assert [tuple(r) for r in runs] == [(10, 1), (6, 0)]  # 費用の台帳には両方が残る


def test_backfill_keeps_own_limit_after_daily_budget_is_used(conn, tmp_path, monkeypatch) -> None:
    # GH#178: 同じ日に通常の取込が cap を使い切っていても、backfill は台帳を見ずに
    # kind ごとの --limit まで読む
    config = make_config(tmp_path, cap=10)
    used_up = session_with(
        [
            page(*[tw(f"b{i}") for i in range(5)], next_token="p2"),
            page(*[tw(f"b{i}") for i in range(5, 10)]),
        ],
        [],
    )
    run_on(conn, monkeypatch, DAY, "2026-10-24T01:00:00Z", lambda: collect_x_signals(
        conn, config, used_up, logger
    ))
    assert signal_reads_on(conn, DAY) == 10  # 当日の枠は使い切った
    backfill = session_with(
        [page(*[tw(f"old_b{i}") for i in range(5)], next_token="more")],
        [page(*[tw(f"old_l{i}") for i in range(5)], next_token="more")],
    )
    totals = run_on(conn, monkeypatch, DAY, "2026-10-24T02:00:00Z", lambda: collect_x_signals(
        conn, config, backfill, logger, backfill_limit=5
    ))
    assert totals["api_reads"] == 10  # bookmark 5 + like 5(日次 cap の残り 0 に縛られない)


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
    totals = collect_x_signals(conn, make_config(tmp_path), session, logger)
    assert len(calls_to(session, "42/tweets")) == 2  # 既知 RT 元でも次ページへ進む
    assert page_tokens(session, "42/tweets") == [None, "p2"]  # 2 ページ目は next_token で要求
    assert totals["api_reads"] == 2  # 各ページ 1 件(先頭の読み直しなし)
    assert [r["tweet_id"] for r in rows(conn, "self_rt")] == ["900", "901"]
    cursor = conn.execute(
        "SELECT since_id FROM x_signal_cursors WHERE kind='self_rt'"
    ).fetchone()
    assert cursor["since_id"] == "300"


def self_rt_cursor(conn) -> str:
    return conn.execute(
        "SELECT since_id FROM x_signal_cursors WHERE kind='self_rt'"
    ).fetchone()["since_id"]


def test_self_rt_backfill_reads_past_saved_cursor(conn, tmp_path) -> None:
    # backfill は「kind ごと直近 N 件」: 保存済みのカーソルがあっても since_id を送らず、
    # カーソルより古い過去分も補う(GH#231)
    conn.execute("INSERT INTO x_signal_cursors (kind, since_id) VALUES ('self_rt', '1000')")
    session = session_with([], [], [page(rt("1100", "901"), rt("500", "900"))])
    collect_x_signals(
        conn, make_config(tmp_path, cap=0), session, logger,
        backfill_limit=10, kinds=("self_rt",),
    )
    call = calls_to(session, "42/tweets")[0]
    assert "since_id" not in call
    assert call["max_results"] == 10  # backfill は 1 ページ目から limit まで(GH#174 の対象外)
    assert [r["tweet_id"] for r in rows(conn, "self_rt")] == ["900", "901"]
    assert self_rt_cursor(conn) == "1100"  # 今回の最大 id へ進める


def test_self_rt_backfill_does_not_move_cursor_back(conn, tmp_path) -> None:
    # 最新の投稿の削除などで backfill の最大 id が既存のカーソルより小さくても、通常用の
    # カーソルは後ろへ戻さない(戻すと次の差分取得が取込済みの範囲を読み直す。GH#231)
    conn.execute("INSERT INTO x_signal_cursors (kind, since_id) VALUES ('self_rt', '1000')")
    session = session_with([], [], [page(rt("500", "900"))])
    collect_x_signals(
        conn, make_config(tmp_path, cap=0), session, logger,
        backfill_limit=10, kinds=("self_rt",),
    )
    assert [r["tweet_id"] for r in rows(conn, "self_rt")] == ["900"]
    assert self_rt_cursor(conn) == "1000"
    daily = session_with([], [], [page(rt("1200", "902"))])
    collect_x_signals(conn, make_config(tmp_path), daily, logger)
    assert calls_to(daily, "42/tweets")[0]["since_id"] == "1000"  # 差分取得は従来どおり
    assert self_rt_cursor(conn) == "1200"


def test_self_rt_cursor_not_moved_back_past_concurrent_advance(conn, tmp_path) -> None:
    # 取得中に別の実行(朝バッチなど)がカーソルを進めても戻さない。比較は取得の開始時
    # でなく保存時の値で行う(GH#231)
    conn.execute("INSERT INTO x_signal_cursors (kind, since_id) VALUES ('self_rt', '1000')")
    session = session_with([], [], [page(rt("1100", "901"))])
    fetch = session.get

    def get_while_other_run_advances(url: str, params: dict | None = None) -> httpx.Response:
        if "42/tweets" in url:
            other = connect(tmp_path / "intel.db")
            other.execute("UPDATE x_signal_cursors SET since_id = '1300' WHERE kind = 'self_rt'")
            other.close()
        return fetch(url, params)

    session.get = get_while_other_run_advances
    collect_x_signals(
        conn, make_config(tmp_path, cap=0), session, logger,
        backfill_limit=10, kinds=("self_rt",),
    )
    assert [r["tweet_id"] for r in rows(conn, "self_rt")] == ["901"]
    assert self_rt_cursor(conn) == "1300"


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


def fail_signal_insert(conn, tweet_id: str, message: str = "boom", action: str = "ABORT") -> None:
    """tweet_id のシグナルの保存を DB のエラーにする(保存の途中の sqlite3.Error の再現)。

    action='ROLLBACK' は SQLite がトランザクションを自動で戻す失敗(容量不足・I/O エラー等)の再現。
    """
    conn.execute(
        f"CREATE TRIGGER fail_{tweet_id} BEFORE INSERT ON x_signals "
        f"WHEN NEW.tweet_id = '{tweet_id}' BEGIN SELECT RAISE({action}, '{message}'); END"
    )


@pytest.mark.parametrize("action", ["ABORT", "ROLLBACK"])
def test_storage_failure_still_records_reads_in_ledger(conn, tmp_path, action) -> None:
    # GH#330: kind の隔離の外の例外(保存の sqlite3.Error)は段の失敗として抜けるが、取得済みの
    # 読取数と費用は failed の行で台帳に残す(漏れると同じ日の再実行が日次 cap を超えて読む)。
    # SQLite が自動でロールバック済みでも、後始末の ROLLBACK の例外で元の例外を置き換えない
    fail_signal_insert(conn, "l3", action=action)
    session = session_with(
        [page(*[tw(f"b{i}") for i in range(5)])], [page(*[tw(f"l{i}") for i in range(5)])]
    )
    with pytest.raises(sqlite3.IntegrityError, match="boom"):
        collect_x_signals(conn, make_config(tmp_path, cap=15), session, logger, run_id="r1")
    run = conn.execute("SELECT * FROM x_signal_runs").fetchone()
    assert run["status"] == "failed"
    assert run["api_reads"] == 10  # bookmark 5 + 失敗したページの like 5(読取は消費済み)
    assert run["estimated_cost_usd"] == pytest.approx(10 * 0.004)
    assert run["new_signals"] == 5  # 保存できた bookmark だけ(like のページはロールバック)
    assert run["run_id"] == "r1"
    assert run["error"] == "like: IntegrityError: boom"
    assert signal_reads_on(conn, today_jst()) == 10  # 同じ日の再実行の残りは 5
    assert calls_to(session, "42/tweets") == []  # 後続の kind には進まない(段の失敗)
    assert rows(conn, "like") == []  # like のページは残らない


class RollbackFails:
    """ROLLBACK を送ると、戻した上で OperationalError を出す接続(後始末の失敗の再現)。"""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def execute(self, sql: str, *args):
        if sql == "ROLLBACK":
            self._conn.execute("ROLLBACK")
            raise sqlite3.OperationalError("rollback failed")
        return self._conn.execute(sql, *args)


def test_rollback_failure_keeps_original_error(conn, tmp_path) -> None:
    # GH#330: ページの ROLLBACK 自体が失敗しても、元の保存のエラーを送出し台帳に残す
    fail_signal_insert(conn, "l1")
    session = session_with([], [page(*[tw(f"l{i}") for i in range(5)])])
    with pytest.raises(sqlite3.IntegrityError, match="boom"):
        collect_x_signals(RollbackFails(conn), make_config(tmp_path), session, logger)
    run = conn.execute("SELECT * FROM x_signal_runs").fetchone()
    assert run["status"] == "failed"
    assert run["api_reads"] == 5
    assert run["error"] == "like: IntegrityError: boom"


def test_ledger_failure_does_not_hide_original_error(conn, tmp_path, caplog) -> None:
    # GH#330: 例外の経路で台帳を書けなくても、取込の元の例外を送出する
    fail_signal_insert(conn, "b1")
    conn.execute(
        "CREATE TRIGGER fail_ledger BEFORE INSERT ON x_signal_runs "
        "BEGIN SELECT RAISE(ABORT, 'ledger'); END"
    )
    session = session_with([page(tw("b1"))], [])
    with (
        caplog.at_level(logging.ERROR, logger=logger.name),
        pytest.raises(sqlite3.IntegrityError, match="boom"),
    ):
        collect_x_signals(conn, make_config(tmp_path), session, logger)
    assert conn.execute("SELECT count(*) FROM x_signal_runs").fetchone()[0] == 0
    assert "台帳を書けない" in caplog.text


def test_refresh_transport_failure_mid_run_records_reads(conn, tmp_path) -> None:
    # GH#330: 先のページの後の refresh の通信の失敗は、分類済みの OAuth のエラーとして kind 単位に
    # 隔離され、取得済みの読取数と費用が台帳に残る(素の httpx の例外で段を抜けない)
    clock = [datetime(2026, 7, 13, 12, 0, tzinfo=UTC)]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == TOKEN_URL:
            raise httpx.ConnectError("token の接続に失敗")
        if url == USERS_ME_URL:
            return httpx.Response(200, json=ME)
        if "/bookmarks" in url:
            clock[0] = datetime(2026, 7, 13, 14, 0, tzinfo=UTC)  # 次の要求の前に失効間近
            return httpx.Response(200, json=page(*[tw(f"b{i}") for i in range(5)]))
        raise AssertionError(f"refresh の失敗の後に要求した: {url}")

    token = XOAuth2Token(
        access_token="at",
        refresh_token="rt",
        expires_at="2026-07-13T14:00:00Z",
        scope=" ".join(OAUTH2_SCOPES),
    )
    session = XOAuth2Session(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        config_dir=tmp_path,
        client_id="cid",
        client_secret=None,
        token=token,
        sleep=lambda _s: None,
        now=lambda: clock[0],
    )
    totals = collect_x_signals(conn, make_config(tmp_path), session, logger)
    assert totals == {"api_reads": 5, "new_signals": 5}
    run = conn.execute("SELECT * FROM x_signal_runs").fetchone()
    assert run["status"] == "ok"  # bookmark は成功(like / self_rt は refresh の失敗)
    assert run["api_reads"] == 5
    assert run["estimated_cost_usd"] == pytest.approx(5 * 0.004)
    assert "like: refresh 失敗" in run["error"]
    assert "ConnectError" in run["error"]
    assert "token の接続に失敗" not in run["error"]  # 通信の例外の文は載せない(型の名前だけ)


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


@pytest.mark.parametrize(
    "host", ["mobile.twitter.com", "twitter.com", "www.twitter.com", "x.com", "www.x.com"]
)
def test_bookmark_x_host_alias_link_keeps_bookmarked_tweet(conn, tmp_path, host) -> None:
    # X のホストの別名の投稿リンクは外部記事でない。外部記事と誤ると正規化でリンク先の
    # 別の投稿の URL になり、ブックマークした投稿の本文が失われる(GH#232)
    tweet = linked_tw("111", f"https://{host}/someone/status/999", text="古いリンクの紹介")
    session = session_with([page(tweet)], [])
    collect_x_signals(conn, make_config(tmp_path), session, logger)
    item = item_for(conn, "111")
    assert item["canonical_url"] == "https://x.com/i/web/status/111"
    assert "古いリンクの紹介" in item["raw_payload"]


def test_bookmark_skips_mobile_twitter_link_before_external_article(conn, tmp_path) -> None:
    # 先頭の mobile.twitter.com の投稿リンクを飛ばし、後ろの外部記事を canonical に(GH#232)
    tweet = tw("111")
    tweet["entities"] = {
        "urls": [
            {
                "url": "https://t.co/a",
                "expanded_url": "https://mobile.twitter.com/someone/status/999",
            },
            {"url": "https://t.co/b", "expanded_url": "https://example.com/article"},
        ]
    }
    session = session_with([page(tweet)], [])
    collect_x_signals(conn, make_config(tmp_path), session, logger)
    item = item_for(conn, "111")
    assert item["canonical_url"] == "https://example.com/article"
    assert '"raw_content"' not in (item["raw_payload"] or "")


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


def test_bookmark_unsplittable_link_falls_back_to_tweet_url(conn, tmp_path) -> None:
    # GH#329: 分解できないリンク(括弧の不整合な netloc)は候補から外し、投稿の URL へ戻す。
    # 同じページの正常な項目と後続の kind も保存する(ページのロールバックと段の離脱にしない)
    broken = linked_tw("111", "https://[example.com/post", text="壊れたリンクの紹介")
    session = session_with(
        [page(broken, linked_tw("222", "https://example.com/ok"))], [page(tw("l1"))]
    )
    collect_x_signals(conn, make_config(tmp_path), session, logger)
    item = item_for(conn, "111")
    assert item["canonical_url"] == "https://x.com/i/web/status/111"
    assert "壊れたリンクの紹介" in item["raw_payload"]  # 投稿の全文が本文
    assert item_for(conn, "222")["canonical_url"] == "https://example.com/ok"
    assert [r["tweet_id"] for r in rows(conn, "like")] == ["l1"]
    run = conn.execute("SELECT * FROM x_signal_runs").fetchone()
    assert run["status"] == "ok"
    assert run["error"] is None


def test_bookmark_skips_unsplittable_link_before_external_article(conn, tmp_path) -> None:
    # GH#329: 分解できない先頭のリンクを飛ばし、後ろの外部記事を canonical に
    tweet = tw("111")
    tweet["entities"] = {
        "urls": [
            {"url": "https://t.co/a", "expanded_url": "http://[::1/broken"},
            {"url": "https://t.co/b", "expanded_url": "https://example.com/article"},
        ]
    }
    session = session_with([page(tweet)], [])
    collect_x_signals(conn, make_config(tmp_path), session, logger)
    assert item_for(conn, "111")["canonical_url"] == "https://example.com/article"


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


def rss_item_with_status(conn, url: str, status: str, skip_reason: str | None) -> int:
    conn.execute("INSERT OR IGNORE INTO sources (type, name) VALUES ('rss', 'feed')")
    source_id = conn.execute("SELECT id FROM sources WHERE name='feed'").fetchone()["id"]
    return conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, content_hash, status, skip_reason) "
        "VALUES (?, ?, ?, '', ?, ?)",
        (source_id, url, url, status, skip_reason),
    ).lastrowid


def status_of(conn, item_id: int) -> tuple[str, str | None]:
    row = conn.execute(
        "SELECT status, skip_reason FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    return (row["status"], row["skip_reason"])


@pytest.mark.parametrize("reason", ["stale", "x_noise", "daily_cap"])
def test_bookmark_merge_reactivates_item_skipped_by_exempt_rule(conn, tmp_path, reason) -> None:
    # 前日以前にブックマークへ適用しない規則で skip 済みの item への合流は new に戻す
    # (戻さないと事前選別にも保存節にも載らない。GH#176)
    existing_id = rss_item_with_status(conn, "https://example.com/post", "skipped", reason)

    session = session_with([page(linked_tw("b1", "https://example.com/post"))], [])
    collect_x_signals(conn, make_config(tmp_path), session, logger)

    assert item_for(conn, "b1")["id"] == existing_id
    assert status_of(conn, existing_id) == ("new", None)


@pytest.mark.parametrize(
    ("status", "reason"),
    [("skipped", "blocklist"), ("analyzed", None), ("digested", None)],
)
def test_bookmark_merge_keeps_other_statuses(conn, tmp_path, status, reason) -> None:
    # blocklist(明示 NG)はブックマークにも適用する規則なので戻さない。
    # 分析済み・配信済みの合流先にも触れない(保存節は台帳で拾う)
    existing_id = rss_item_with_status(conn, "https://example.com/post", status, reason)

    session = session_with([page(linked_tw("b1", "https://example.com/post"))], [])
    collect_x_signals(conn, make_config(tmp_path), session, logger)

    assert item_for(conn, "b1")["id"] == existing_id
    assert status_of(conn, existing_id) == (status, reason)


def test_bookmark_merge_into_previous_day_skip_is_preselected(conn, tmp_path) -> None:
    # Issue の経路: ホームTL で stale になったリンクなし投稿を後からブックマークすると、
    # canonical(ツイート URL)が同じ既存 item に合流する。翌朝の事前選別で採用される
    config = make_config(tmp_path)
    conn.execute("INSERT INTO sources (type, name) VALUES ('x', 'home-tl')")
    home_id = conn.execute("SELECT id FROM sources WHERE name='home-tl'").fetchone()["id"]
    canonical = normalize_url("https://x.com/i/web/status/b1")
    existing_id = conn.execute(
        "INSERT INTO items (source_id, external_id, url, canonical_url, published_at, "
        "content_hash) VALUES (?, 'b1', ?, ?, '2026-07-13T00:00:00Z', '')",
        (home_id, canonical, canonical),
    ).lastrowid
    preselect(conn, config, logger)  # 前日の朝: 72h 超で stale
    assert status_of(conn, existing_id) == ("skipped", "stale")

    session = session_with([page(tw("b1", text="リンクなしの学び"))], [])
    collect_x_signals(conn, config, session, logger)

    assert item_for(conn, "b1")["id"] == existing_id
    assert existing_id in preselect(conn, config, logger).selected_ids


OLD_FETCHED_AT = "2026-01-01T00:00:00Z"  # 本文キャッシュの TTL(既定 7 日)を十分に過ぎた時刻


def expired_skipped_x_item(conn, tweet_id: str, *, source_type: str = "x") -> int:
    """TTL の purge で raw_payload を失い、前日以前に stale で skip 済みの投稿の item。"""
    conn.execute(
        "INSERT OR IGNORE INTO sources (type, name) VALUES (?, ?)",
        (source_type, f"{source_type}-src"),
    )
    source_id = conn.execute(
        "SELECT id FROM sources WHERE name = ?", (f"{source_type}-src",)
    ).fetchone()["id"]
    canonical = normalize_url(f"https://x.com/i/web/status/{tweet_id}")
    return conn.execute(
        "INSERT INTO items (source_id, external_id, url, canonical_url, content_hash, "
        "status, skip_reason, raw_payload, fetched_at) "
        "VALUES (?, ?, ?, ?, 'sha256:old', 'skipped', 'stale', NULL, ?)",
        (source_id, tweet_id, canonical, canonical, OLD_FETCHED_AT),
    ).lastrowid


def test_bookmark_revive_restores_fresh_full_text_for_extraction(conn, tmp_path) -> None:
    # GH#332: TTL 切れで本文を失った skip 済みの投稿をブックマークで戻すとき、今回取得した
    # 全文を本文キャッシュへ書き戻す。抽出段は投稿の URL の HTTP 取得に頼らず全文を使い、
    # 書き戻した本文は古い fetched_at のせいで次の purge に直ちに消されない
    from app.extraction import extract_pending
    from app.http import HttpClient
    from app.purge import purge_expired

    config = make_config(tmp_path)
    existing_id = expired_skipped_x_item(conn, "b1")
    long_text = "長文の学びの段落。" * 80
    tweet = tw("b1", text="切り詰め")
    tweet["note_tweet"] = {"text": long_text}
    collect_x_signals(conn, config, session_with([page(tweet)], []), logger)

    item = conn.execute("SELECT * FROM items WHERE id = ?", (existing_id,)).fetchone()
    assert (item["status"], item["skip_reason"]) == ("new", None)
    payload = json.loads(item["raw_payload"])
    assert payload["raw_content"] == long_text
    assert payload["cached_at"] > OLD_FETCHED_AT  # TTL を今から数え直す
    assert item["fetched_at"] == OLD_FETCHED_AT  # 初回の取込の時刻は動かさない
    # 合流先の識別は変えない
    assert item["canonical_url"] == normalize_url("https://x.com/i/web/status/b1")
    assert item["content_hash"] == "sha256:old"

    purge_expired(conn, config, logger)
    assert conn.execute(
        "SELECT raw_payload FROM items WHERE id = ?", (existing_id,)
    ).fetchone()["raw_payload"] is not None

    calls: list[str] = []

    def unreachable(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(500)

    http = HttpClient(
        transport=httpx.MockTransport(unreachable),
        resolve_host=lambda host: ["93.184.216.34"],
        sleep=lambda _s: None,
    )
    counts = extract_pending(conn, config, http, logger)
    assert counts["ok"] == 1
    assert calls == []
    doc = conn.execute(
        "SELECT extracted_text FROM documents WHERE item_id = ?", (existing_id,)
    ).fetchone()
    assert "長文の学びの段落" in doc["extracted_text"]


def test_bookmark_revive_does_not_restore_text_into_non_x_item(conn, tmp_path) -> None:
    # X の本文は X の TTL でしか持たない(設計 01 §6)。他のソースの TTL で残さないため、
    # X 由来でない合流先には書き戻さない(status だけ戻す。GH#176 の挙動のまま)
    existing_id = expired_skipped_x_item(conn, "b1", source_type="rss")
    collect_x_signals(
        conn, make_config(tmp_path), session_with([page(tw("b1", text="全文"))], []), logger
    )
    item = conn.execute("SELECT * FROM items WHERE id = ?", (existing_id,)).fetchone()
    assert (item["status"], item["raw_payload"], item["fetched_at"]) == (
        "new", None, OLD_FETCHED_AT,
    )


def test_bookmark_revive_keeps_live_cache(conn, tmp_path) -> None:
    # 本文キャッシュが残っている合流先(documents が TTL 内)には書き戻さない
    existing_id = expired_skipped_x_item(conn, "b1")
    conn.execute(
        "INSERT INTO documents (item_id, extracted_text, extraction_status, expires_at) "
        "VALUES (?, '残っている本文', 'ok', '2999-01-01T00:00:00Z')",
        (existing_id,),
    )
    collect_x_signals(
        conn, make_config(tmp_path), session_with([page(tw("b1", text="全文"))], []), logger
    )
    item = conn.execute("SELECT * FROM items WHERE id = ?", (existing_id,)).fetchone()
    assert (item["status"], item["raw_payload"], item["fetched_at"]) == (
        "new", None, OLD_FETCHED_AT,
    )


def test_bookmark_merge_without_revive_does_not_touch_cache(conn, tmp_path) -> None:
    # skip を戻さない合流(blocklist)では本文を書き戻さない(分析の対象に戻らないため)
    existing_id = expired_skipped_x_item(conn, "b1")
    conn.execute(
        "UPDATE items SET skip_reason = 'blocklist' WHERE id = ?", (existing_id,)
    )
    collect_x_signals(
        conn, make_config(tmp_path), session_with([page(tw("b1", text="全文"))], []), logger
    )
    item = conn.execute("SELECT * FROM items WHERE id = ?", (existing_id,)).fetchone()
    assert (item["status"], item["raw_payload"], item["fetched_at"]) == (
        "skipped", None, OLD_FETCHED_AT,
    )


def test_bookmark_revive_keeps_existing_payload(conn, tmp_path) -> None:
    # raw_payload が残っている合流先(documents は無い)は上書きしない。抽出段は残っている
    # 本文を使い、TTL の起点も変えない
    existing_id = expired_skipped_x_item(conn, "b1")
    kept = '{"raw_content": "残っている本文"}'
    conn.execute("UPDATE items SET raw_payload = ? WHERE id = ?", (kept, existing_id))
    collect_x_signals(
        conn, make_config(tmp_path), session_with([page(tw("b1", text="全文"))], []), logger
    )
    item = conn.execute("SELECT * FROM items WHERE id = ?", (existing_id,)).fetchone()
    assert (item["status"], item["raw_payload"], item["fetched_at"]) == (
        "new", kept, OLD_FETCHED_AT,
    )


def test_bookmark_revive_keeps_quiet_alert_of_original_source(conn, tmp_path) -> None:
    # 本文の書き戻しは元のソースの新着に数えない。新着の途絶えた有効な X のソースの
    # 「新しい記事が入っていない」は、古い投稿をブックマークで戻しても消えない(GH#332)
    from app.config import XConfig
    from app.source_health import source_health_report

    existing_id = expired_skipped_x_item(conn, "b1")
    conn.execute("UPDATE sources SET created_at = ? WHERE name = 'x-src'", (OLD_FETCHED_AT,))
    collect_x_signals(
        conn, make_config(tmp_path), session_with([page(tw("b1", text="全文"))], []), logger
    )
    assert conn.execute(
        "SELECT raw_payload FROM items WHERE id = ?", (existing_id,)
    ).fetchone()["raw_payload"] is not None  # 書き戻しの経路を通った

    config = Config(config_dir=tmp_path, x=XConfig(enabled=True))
    report = source_health_report(conn, config, today_jst())
    assert "💤 「x-src」に 14 日間、新しい記事が入っていない" in report


def test_bookmark_revive_with_link_keeps_no_text(conn, tmp_path) -> None:
    # リンク付きのブックマークは記事が本体で、投稿の本文を raw_content に持たない(設計 02)
    conn.execute("INSERT INTO sources (type, name) VALUES ('x', 'x-src')")
    source_id = conn.execute("SELECT id FROM sources WHERE name='x-src'").fetchone()["id"]
    existing_id = conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, content_hash, status, "
        "skip_reason, fetched_at) VALUES (?, 'https://example.com/post', "
        "'https://example.com/post', '', 'skipped', 'stale', ?)",
        (source_id, OLD_FETCHED_AT),
    ).lastrowid
    session = session_with([page(linked_tw("b1", "https://example.com/post"))], [])
    collect_x_signals(conn, make_config(tmp_path), session, logger)
    item = conn.execute("SELECT * FROM items WHERE id = ?", (existing_id,)).fetchone()
    assert (item["status"], item["raw_payload"], item["fetched_at"]) == (
        "new", None, OLD_FETCHED_AT,
    )


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


# --- 深掘りキューへの自動投入(GH#104) ---


def dig_queue_row(conn, item_id):
    return conn.execute(
        "SELECT * FROM dig_queue WHERE item_id = ?", (item_id,)
    ).fetchone()


def test_new_bookmark_enqueued_for_dig(conn, tmp_path) -> None:
    # 新規 🔖 は無条件でキュー入り(設計 06 §3)
    session = session_with([page(linked_tw("b1", "https://example.com/post"))], [])
    collect_x_signals(conn, make_config(tmp_path), session, logger)

    item = item_for(conn, "b1")
    row = dig_queue_row(conn, item["id"])
    assert (row["status"], row["source"]) == ("queued", "bookmark")
    assert row["requested_at"] is None  # 自動投入 = 優先順位は新着順のみ


def test_backfill_bookmark_not_enqueued(conn, tmp_path) -> None:
    # backfill 再読取(過去分)は自動キューの対象外 = リプライ指定時のみ(設計 06 §3)
    conn.execute("INSERT INTO x_signals (kind, tweet_id) VALUES ('bookmark', 'b_old')")
    session = session_with([page(linked_tw("b_old", "https://example.com/old"))], [], [])
    collect_x_signals(
        conn, make_config(tmp_path), session, logger,
        backfill_limit=10, kinds=("bookmark",),
    )
    item = item_for(conn, "b_old")
    assert item is not None  # item 化は行う
    assert dig_queue_row(conn, item["id"]) is None  # キューには積まない


def test_new_bookmark_enqueue_targets_merged_item(conn, tmp_path) -> None:
    # RSS 取込済み記事への合流時は、キューも live item(合流先)を指す
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

    row = dig_queue_row(conn, existing_id)
    assert row is not None and row["source"] == "bookmark"
