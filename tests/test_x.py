import logging
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.cli import _build_connectors
from app.collect import collect_all
from app.config import Config, XConfig, XSearchConfig
from app.connectors.base import FatalError, FetchResult, FetchStats, RetryableError
from app.connectors.x import (
    XConnector,
    XCredentials,
    XRouter,
    build_x_connector,
    has_x_credentials,
    x_reads_today,
)
from app.connectors.x_search import XSearchConnector
from app.db import apply_migrations, connect
from app.http import HttpClient
from app.models import Source
from app.source_health import X_FAILURE_ALERT, source_failure_alerts

FULL_ENV = {k: "v" for k in
            ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")}

CREDS = XCredentials(
    api_key="ck", api_secret="cs", access_token="at", access_token_secret="ats"
)


def make_source(**overrides) -> Source:
    values = dict(
        id=1,
        type="x",
        name="home",
        config=None,
        cursor=None,
        enabled=1,
        last_success_at=None,
        last_error_at=None,
        last_error=None,
        consecutive_failures=0,
        created_at="2026-07-10T00:00:00Z",
    )
    values.update(overrides)
    return Source(**values)


def tweet(tid: str, text: str, *, author="a1", created="2026-07-10T09:00:00.000Z",
          retweeted=None, in_reply_to=None) -> dict:
    t: dict = {"id": tid, "text": text, "author_id": author, "created_at": created}
    if retweeted is not None:
        t["referenced_tweets"] = [{"type": "retweeted", "id": retweeted}]
    if in_reply_to is not None:
        t["in_reply_to_user_id"] = in_reply_to
    return t


def page(tweets: list[dict], *, next_token=None, users=None) -> dict:
    body: dict = {"data": tweets}
    if next_token:
        body["meta"] = {"next_token": next_token}
    if users:
        body["includes"] = {"users": [{"id": uid, "username": name} for uid, name in users.items()]}
    return body


def connector_from(pages: list, *, me_id="me", daily_read_cap=130,
                   unit_price=0.004, seen=None, reads_used_today=0) -> XConnector:
    """pages[i] = (status, body_dict, headers)。timeline リクエストは pagination_token で
    index を引く(リトライで同 token を再送しても同じ応答 = リトライ安全)。200 応答は
    max_results で data を切り詰めて実 API の挙動を模す。status に例外を置くと、その
    ページの要求で送出する(通信の失敗。GH#182)。"""
    token_index = {
        body.get("meta", {}).get("next_token"): i + 1
        for i, (_s, body, _h) in enumerate(pages)
        if body.get("meta", {}).get("next_token")
    }

    def handler(request: httpx.Request) -> httpx.Response:
        split = urlsplit(str(request.url))
        if seen is not None:
            seen.append(request)
        if split.path.endswith("/users/me"):
            return httpx.Response(200, json={"data": {"id": me_id}})
        query = parse_qs(split.query)
        token = query.get("pagination_token", [None])[0]
        status, body, headers = pages[0 if token is None else token_index[token]]
        if isinstance(status, Exception):
            raise status
        if status == 200:
            limit = int(query.get("max_results", ["100"])[0])
            body = {**body, "data": body.get("data", [])[:limit]}
        return httpx.Response(status, json=body, headers=headers)

    return XConnector(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        credentials=CREDS,
        daily_read_cap=daily_read_cap,
        unit_price_usd=unit_price,
        reads_used_today=reads_used_today,
        sleep=lambda _s: None,
    )


def test_basic_fetch_parses_and_costs() -> None:
    body = page(
        [tweet("300", "こんにちは"), tweet("299", "ふたつめ", author="a2")],
        users={"a1": "alice", "a2": "bob"},
    )
    result = connector_from([(200, body, {})]).fetch(make_source(), {})

    assert result.stats.items_fetched == 2
    assert result.stats.api_reads == 2
    assert result.stats.estimated_cost_usd == pytest.approx(2 * 0.004)
    first, second = result.items
    assert first.external_id == "300"
    assert first.url == "https://x.com/i/web/status/300"
    assert first.author == "alice"
    assert first.raw_content == "こんにちは"
    assert first.title is None
    assert first.published_at.isoformat() == "2026-07-10T09:00:00+00:00"
    assert second.author == "bob"
    assert result.next_cursor == {"since_id": "300"}  # 最大 tweet id


def test_pagination_hard_caps_at_daily_read_cap() -> None:
    seen: list[httpx.Request] = []
    p1 = page([tweet(str(500 - i), "t") for i in range(100)], next_token="tok1")
    p2 = page([tweet(str(400 - i), "t") for i in range(100)], next_token="tok2")
    result = connector_from(
        [(200, p1, {}), (200, p2, {})], daily_read_cap=130, seen=seen
    ).fetch(make_source(), {})
    assert result.stats.api_reads == 130  # ちょうど cap。超過しない
    timeline = [r for r in seen if "timelines" in str(r.url)]
    assert parse_qs(urlsplit(str(timeline[-1].url)).query)["max_results"] == ["30"]  # 残枠


def test_reads_used_today_leaves_only_remaining_budget() -> None:
    # GH#178: cap は暦日の上限。同じ日の実行が読んだぶんを引いた残りだけを読む
    seen: list[httpx.Request] = []
    p1 = page([tweet(str(500 - i), "t") for i in range(100)], next_token="tok1")
    result = connector_from(
        [(200, p1, {})], daily_read_cap=130, reads_used_today=100, seen=seen
    ).fetch(make_source(), {})
    assert result.stats.api_reads == 30
    timeline = [r for r in seen if "timelines" in str(r.url)]
    assert [parse_qs(urlsplit(str(r.url)).query)["max_results"] for r in timeline] == [["30"]]
    assert not result.budget_exhausted
    # 使い切った日は読まず、since_id も据え置く(翌日に新しい側から取る)。読まなかった印を立て、
    # collect にソースの成否の状態を変えさせない
    seen.clear()
    result = connector_from(
        [(200, p1, {})], daily_read_cap=130, reads_used_today=130, seen=seen
    ).fetch(make_source(), {"since_id": "100"})
    assert result.stats.api_reads == 0
    assert [r for r in seen if "timelines" in str(r.url)] == []
    assert result.next_cursor == {"since_id": "100"}
    assert result.budget_exhausted


def test_rate_limit_remaining_zero_stops() -> None:
    p1 = page([tweet("500", "t")], next_token="tok1")
    result = connector_from([(200, p1, {"x-rate-limit-remaining": "0"})]).fetch(make_source(), {})
    assert result.stats.api_reads == 1  # 残0で打ち切り。next_token があっても2ページ目は取らない
    # 打ち切りは失敗ではない(応答は正常)。古い側の穴は追わず since_id を進める(設計 02 §2)
    assert result.partial_error is None
    assert result.next_cursor == {"since_id": "500"}


def test_no_next_token_stops() -> None:
    p1 = page([tweet("500", "t")])  # next_token なし = TL 末尾 / since_id 到達
    result = connector_from([(200, p1, {})]).fetch(make_source(), {})
    assert result.stats.api_reads == 1
    assert result.next_cursor == {"since_id": "500"}


def test_empty_page_stops_even_with_next_token() -> None:
    p1 = page([], next_token="tok1")  # 空ページに next_token → ループさせない
    result = connector_from([(200, p1, {})]).fetch(make_source(), {"since_id": "900"})
    assert result.items == []
    assert result.stats.api_reads == 0
    assert result.next_cursor == {"since_id": "900"}  # 取得ゼロなら据え置き


def test_retweet_normalizes_to_original() -> None:
    body = page([tweet("600", "RT @x: ...", retweeted="42")], users={"a1": "alice"})
    (rt,) = connector_from([(200, body, {})]).fetch(make_source(), {}).items
    assert rt.external_id == "42"  # 元ツイに寄せて dedup
    assert rt.url == "https://x.com/i/web/status/42"
    assert rt.meta == {"retweet": True, "reply": False, "self_retweet": False}


def test_self_retweet_marked_in_meta() -> None:
    # 自分の RT は Stage 1 の規則 3 が落とす(設計 03 §2)。他人の RT と区別できるよう meta に残す
    body = page([
        tweet("610", "RT @x: ...", author="me", retweeted="43"),
        tweet("611", "RT @y: ...", author="a1", retweeted="44"),
    ])
    result = connector_from([(200, body, {})], me_id="me").fetch(make_source(), {})
    metas = {i.external_id: i.meta for i in result.items}
    assert metas["43"] == {"retweet": True, "reply": False, "self_retweet": True}
    assert metas["44"] == {"retweet": True, "reply": False, "self_retweet": False}


def test_reply_to_other_excluded_but_self_reply_kept() -> None:
    body = page([
        tweet("700", "他人宛リプライ", in_reply_to="stranger"),
        tweet("701", "自分宛リプライ", in_reply_to="me"),
        tweet("702", "通常ポスト"),
    ])
    result = connector_from([(200, body, {})], me_id="me").fetch(make_source(), {})
    assert {i.external_id for i in result.items} == {"701", "702"}  # 他人宛(700)は除外
    assert result.stats.api_reads == 3  # 除外しても読取件数(課金)は3


def test_since_id_passed_as_param() -> None:
    seen: list[httpx.Request] = []
    connector_from([(200, page([tweet("800", "t")]), {})], seen=seen).fetch(
        make_source(), {"since_id": "790"}
    )
    timeline = next(r for r in seen if "timelines" in str(r.url))
    assert parse_qs(urlsplit(str(timeline.url)).query)["since_id"] == ["790"]


def test_first_page_429_is_retryable_not_silent_success() -> None:
    # page1 の 429 を「0件成功」で返すと consecutive_failures がリセットされ障害を隠す
    seen: list[httpx.Request] = []
    conn = connector_from([(429, {}, {})], daily_read_cap=130, seen=seen)
    with pytest.raises(RetryableError, match="429"):
        conn.fetch(make_source(), {})
    # 429 は待たず即打ち切り(リトライしない。設計 02 §2)
    assert sum("timelines" in str(r.url) for r in seen) == 1


def test_5xx_retries_then_recovers_and_resigns() -> None:
    # 5xx を初回+2回まではリトライし、途中で成功したら復帰する。署名は試行ごとに引き直す
    calls = {"n": 0}
    auths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if urlsplit(str(request.url)).path.endswith("/users/me"):
            return httpx.Response(200, json={"data": {"id": "me"}})
        calls["n"] += 1
        auths.append(request.headers.get("authorization", ""))
        if calls["n"] < 3:
            return httpx.Response(503, json={})
        return httpx.Response(200, json=page([tweet("1", "t")]))

    conn = XConnector(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        credentials=CREDS, daily_read_cap=130, unit_price_usd=0.004, sleep=lambda _s: None,
    )
    result = conn.fetch(make_source(), {})
    assert calls["n"] == 3  # 初回 + 2 リトライで成功
    assert result.stats.api_reads == 1
    assert len(set(auths)) == 3  # 試行ごとに再署名(nonce/timestamp が変わる)


def test_429_after_first_page_returns_partial() -> None:
    p1 = page([tweet("500", "t")], next_token="tok1")
    result = connector_from([(200, p1, {}), (429, {}, {})], daily_read_cap=500).fetch(
        make_source(), {"since_id": "100"}
    )
    assert result.stats.api_reads == 1  # 1ページ取得後の 429 → 既取得分を返す
    # 正常扱いで隠さずソースの失敗にさせ、since_id は据え置く(GH#182。検索と同じ形 = GH#183)
    assert isinstance(result.partial_error, RetryableError)
    assert "429" in str(result.partial_error)
    assert result.next_cursor == {"since_id": "100"}


def test_auth_error_is_fatal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    conn = XConnector(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        credentials=CREDS, daily_read_cap=130, unit_price_usd=0.004, sleep=lambda _s: None,
    )
    with pytest.raises(FatalError, match="認証失敗"):
        conn.fetch(make_source(), {})


@pytest.mark.parametrize("status", [401, 403])
def test_auth_error_after_first_page_is_fatal_with_reads_and_cost(status) -> None:
    # GH#178: 2 ページ目以降の 401/403 は致命の失敗のまま送出し、それまでの読取数と費用を添える
    # (collect が台帳に残す。漏れると同じ日の再実行が日次の上限を超えて読む)
    p1 = page([tweet(str(1000 - i), "t") for i in range(100)], next_token="tok1")
    with pytest.raises(FatalError, match="認証失敗") as raised:
        connector_from([(200, p1, {}), (status, {}, {})]).fetch(make_source(), {})
    assert raised.value.api_reads == 100
    assert raised.value.estimated_cost_usd == pytest.approx(0.4)
    # 1 ページ目の 401/403 は読取なし(0 件)
    with pytest.raises(FatalError, match="認証失敗") as raised:
        connector_from([(status, {}, {})]).fetch(make_source(), {})
    assert (raised.value.api_reads, raised.value.estimated_cost_usd) == (0, 0.0)


def test_5xx_on_first_page_is_retryable() -> None:
    with pytest.raises(RetryableError, match="503"):
        connector_from([(503, {}, {})]).fetch(make_source(), {})


def test_5xx_after_first_page_returns_partial() -> None:
    p1 = page([tweet("500", "t")], next_token="tok1")
    result = connector_from([(200, p1, {}), (503, {}, {})], daily_read_cap=500).fetch(
        make_source(), {}
    )
    assert result.stats.api_reads == 1  # 途中の 5xx はリトライ尽き後に既取得分で打ち切り
    assert isinstance(result.partial_error, RetryableError)
    assert "503" in str(result.partial_error)
    assert result.next_cursor == {}  # 据え置き(元の cursor が空なら空のまま。GH#182)


def test_transport_error_after_first_page_returns_partial_with_cost() -> None:
    # GH#182: 2 ページ目で通信のリトライが尽きても、1 ページ目の項目と読み取りの件数・費用を
    # 返す(例外で捨てると費用が台帳に残らない)。理由を載せてソースの失敗にさせ、since_id は
    # 据え置く(翌日に旧 since_id から取り直し、取得済みぶんは UNIQUE で吸収)
    seen: list[httpx.Request] = []
    p1 = page([tweet("500", "一つめ"), tweet("499", "二つめ")], next_token="tok1")
    result = connector_from(
        [(200, p1, {}), (httpx.ConnectError("boom"), {}, {})], daily_read_cap=500, seen=seen
    ).fetch(make_source(), {"since_id": "100"})
    assert [i.external_id for i in result.items] == ["500", "499"]
    assert result.stats.items_fetched == 2
    assert result.stats.api_reads == 2
    assert result.stats.estimated_cost_usd == pytest.approx(2 * 0.004)
    assert isinstance(result.partial_error, RetryableError)
    assert "接続失敗" in str(result.partial_error)
    assert result.next_cursor == {"since_id": "100"}
    second = [r for r in seen if "pagination_token=tok1" in str(r.url)]
    assert len(second) == 3  # 2 ページ目は初回 + 2 回リトライしてから諦める


def test_transport_error_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if urlsplit(str(request.url)).path.endswith("/users/me"):
            return httpx.Response(200, json={"data": {"id": "me"}})
        raise httpx.ConnectError("boom")

    conn = XConnector(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        credentials=CREDS, daily_read_cap=130, unit_price_usd=0.004, sleep=lambda _s: None,
    )
    with pytest.raises(RetryableError, match="接続失敗"):
        conn.fetch(make_source(), {})


def test_has_x_credentials() -> None:
    full = dict(FULL_ENV)
    assert has_x_credentials(full) is True
    del full["X_ACCESS_TOKEN"]
    assert has_x_credentials(full) is False


class _StubConnector:
    source_type = "x"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def fetch(self, source_row: Source, cursor: dict) -> FetchResult:
        self.calls.append((source_row.name, cursor))
        return FetchResult(items=[], next_cursor={}, stats=FetchStats(0, 0, 0.0, None, 0))


def test_router_routes_by_source_config_mode() -> None:
    home, search = _StubConnector(), _StubConnector()
    router = XRouter(home=home, search=search)
    router.fetch(make_source(), {})  # config NULL = home(既存行の互換)
    router.fetch(make_source(id=2, name="x:home", config='{"mode": "home"}'), {})
    router.fetch(make_source(id=3, name="x:search", config='{"mode": "search"}'), {"since_id": "9"})
    assert [c[0] for c in home.calls] == ["home", "x:home"]
    assert search.calls == [("x:search", {"since_id": "9"})]


def test_router_rejects_bad_config_fail_fast() -> None:
    # タイポで home に化けてホームTL を二重取得(二重課金)する事故を防ぐ
    router = XRouter(home=_StubConnector(), search=_StubConnector())
    with pytest.raises(FatalError, match="mode が不正"):
        router.fetch(make_source(config='{"mode": "serach"}'), {})
    with pytest.raises(FatalError, match="mode が不正"):
        router.fetch(make_source(config="{}"), {})  # config を書くなら mode 必須
    with pytest.raises(FatalError, match="不明なキー"):
        router.fetch(make_source(config='{"mdoe": "search"}'), {})
    # json_valid は null / 配列も通す → object でなければ FatalError(TypeError にしない)
    with pytest.raises(FatalError, match="JSON オブジェクトでない"):
        router.fetch(make_source(config="null"), {})
    with pytest.raises(FatalError, match="JSON オブジェクトでない"):
        router.fetch(make_source(config='["search"]'), {})


def test_router_without_search_wiring_fails_fast() -> None:
    router = XRouter(home=_StubConnector(), search=None)
    with pytest.raises(FatalError, match="slots が未設定"):
        router.fetch(make_source(config='{"mode": "search"}'), {})


def _config(x_enabled: bool, slots: tuple[str, ...] = ()) -> Config:
    return Config(
        config_dir=Path("/tmp/x-test"),
        x=XConfig(enabled=x_enabled),
        x_search=XSearchConfig(slots=slots),
    )


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    yield conn
    conn.close()


def test_build_connectors_wires_x_only_when_enabled_and_keyed(db) -> None:
    http = HttpClient()
    assert set(_build_connectors(_config(False), FULL_ENV, http, db)) == {"rss"}  # X 無効
    assert set(_build_connectors(_config(True), FULL_ENV, http, db)) == {"rss", "x"}  # 有効+キー
    # 有効だがキー欠落 → X は載せず RSS は残る(縮退。欠落は doctor #5)
    assert set(_build_connectors(_config(True), {}, http, db)) == {"rss"}


def test_build_connectors_wires_search_only_with_slots(db) -> None:
    http = HttpClient()
    router = _build_connectors(_config(True), FULL_ENV, http, db)["x"]
    assert isinstance(router, XRouter)
    assert router._search is None  # slots 空 = 検索無効
    router = _build_connectors(_config(True, slots=("ai",)), FULL_ENV, http, db)["x"]
    assert isinstance(router._home, XConnector)
    assert isinstance(router._search, XSearchConnector)


def add_x_source(db, name: str, config: str | None = None) -> int:
    return db.execute(
        "INSERT INTO sources (type, name, config) VALUES ('x', ?, ?)", (name, config)
    ).lastrowid


def add_collect_run(db, source_id: int, started_at: str, reads: int, status="ok") -> None:
    db.execute(
        "INSERT INTO collect_runs (source_id, started_at, status, api_reads) VALUES (?, ?, ?, ?)",
        (source_id, started_at, status, reads),
    )


def test_x_reads_today_sums_jst_calendar_day_per_mode(db) -> None:
    # GH#178: 当日 = JST の暦日(UTC 15:00 が日付の境界)。部分取得のあとの失敗の読取も数える
    home = add_x_source(db, "home")  # config NULL = home
    explicit_home = add_x_source(db, "x:home", '{"mode": "home"}')
    search = add_x_source(db, "x:search", '{"mode": "search"}')
    rss = db.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed')").lastrowid
    add_collect_run(db, home, "2026-10-23T14:59:59Z", 7)  # JST 10/23 23:59:59 = 前日
    add_collect_run(db, home, "2026-10-23T15:00:00Z", 10)  # JST 10/24 0:00 = 当日の始まり
    add_collect_run(db, explicit_home, "2026-10-24T03:00:00Z", 2)
    add_collect_run(db, home, "2026-10-24T05:00:00Z", 3, status="error")  # 部分取得のあとの失敗
    add_collect_run(db, home, "2026-10-24T14:59:59Z", 20)  # JST 10/24 23:59:59 = 当日の終わり
    add_collect_run(db, home, "2026-10-24T15:00:00Z", 40)  # JST 10/25 0:00 = 翌日
    add_collect_run(db, search, "2026-10-24T01:00:00Z", 5)
    add_collect_run(db, rss, "2026-10-24T01:00:00Z", 99)  # X 以外は数えない
    assert x_reads_today(db, "2026-10-24") == {"home": 35, "search": 5}
    assert x_reads_today(db, "2026-10-23") == {"home": 7, "search": 0}
    assert x_reads_today(db, "2026-10-26") == {"home": 0, "search": 0}


def test_build_connectors_passes_same_day_reads_from_ledger(db, monkeypatch) -> None:
    # GH#178: 合成点が台帳の当日の合計を mode ごとにコネクタへ渡す(前日の読取は数えない)
    monkeypatch.setattr("app.connectors.x.today_jst", lambda: "2026-10-24")
    home = add_x_source(db, "home")
    search = add_x_source(db, "x:search", '{"mode": "search"}')
    add_collect_run(db, home, "2026-10-23T14:59:59Z", 130)  # JST 10/23 = 前日
    add_collect_run(db, home, "2026-10-23T21:30:00Z", 100)  # JST 10/24 6:30
    add_collect_run(db, search, "2026-10-23T21:30:00Z", 20)
    router = _build_connectors(_config(True, slots=("ai",)), FULL_ENV, HttpClient(), db)["x"]
    assert router._home._read_budget == 30  # 130 - 100
    assert router._search._read_budget == 15  # 35 - 20


def test_second_collect_same_day_reads_only_remaining_budget(db, monkeypatch) -> None:
    # GH#178: 同じ日に普通に再実行しても(手動の intel run・intel collect)、日次の上限を超えて
    # 読まない。1 回目に読んだぶんが台帳(collect_runs)に残り、2 回目はその残りだけを読む。
    # 翌日(JST)は枠が戻る。当日と台帳の時刻は固定し、実時計に依存させない
    add_x_source(db, "home")
    next_id = iter(range(10_000, 0, -1))
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if urlsplit(str(request.url)).path.endswith("/users/me"):
            return httpx.Response(200, json={"data": {"id": "me"}})
        limit = parse_qs(urlsplit(str(request.url)).query)["max_results"][0]
        seen.append(limit)
        count = min(int(limit), 100)  # 毎回 100 件の新着(next_token なし = 1 ページで終わり)
        return httpx.Response(
            200, json=page([tweet(str(next(next_id)), "新着の投稿") for _ in range(count)])
        )

    def run_once(day: str, utc_time: str) -> None:
        monkeypatch.setattr("app.connectors.x.today_jst", lambda: day)
        before = db.execute("SELECT coalesce(max(id), 0) AS id FROM collect_runs").fetchone()["id"]
        router = _build_connectors(_config(True), FULL_ENV, HttpClient(), db)["x"]
        router._home._session._client = httpx.Client(transport=httpx.MockTransport(handler))
        collect_all(db, {"x": router}, logging.getLogger("test-x"))
        db.execute("UPDATE collect_runs SET started_at = ? WHERE id > ?", (utc_time, before))

    run_once("2026-10-24", "2026-10-23T21:30:00Z")  # JST 10/24 6:30(朝バッチ)
    run_once("2026-10-24", "2026-10-24T03:00:00Z")  # 同じ日の再走
    run_once("2026-10-24", "2026-10-24T14:59:59Z")  # 同じ日の終わり
    run_once("2026-10-25", "2026-10-24T15:00:00Z")  # 翌日の始まり
    reads = [r["api_reads"] for r in db.execute("SELECT api_reads FROM collect_runs ORDER BY id")]
    assert reads == [100, 30, 0, 100]  # 当日の合計は cap(130)ちょうど。翌日は枠が戻る
    assert seen == ["100", "30", "100"]


def test_auth_error_after_partial_fetch_counts_reads_for_same_day_rerun(db, monkeypatch) -> None:
    # GH#178: 途中まで読んだあとの 401 は、今までどおりソースの失敗(取得ぶんは保存せず
    # since_id は据え置く)のまま、読取数と費用を台帳(collect_runs)に残す。認証が直ったあとの
    # 同じ日の再実行は、その残りだけを読む(上限 130 で 100 + 30。読取が漏れると 100 + 130)
    monkeypatch.setattr("app.connectors.x.today_jst", lambda: "2026-10-24")
    source_id = add_x_source(db, "home")
    db.execute("UPDATE sources SET cursor = ? WHERE id = ?", ('{"since_id": "1"}', source_id))
    next_id = iter(range(10_000, 0, -1))
    seen: list[str] = []

    def handler_for(auth_fails_on_second_page: bool):
        def handler(request: httpx.Request) -> httpx.Response:
            split = urlsplit(str(request.url))
            if split.path.endswith("/users/me"):
                return httpx.Response(200, json={"data": {"id": "me"}})
            query = parse_qs(split.query)
            seen.append(query["max_results"][0])
            if "pagination_token" in query:
                return httpx.Response(401, json={})
            count = min(int(query["max_results"][0]), 100)
            body = page(
                [tweet(str(next(next_id)), "新着の投稿") for _ in range(count)],
                next_token="tok1" if auth_fails_on_second_page else None,
            )
            return httpx.Response(200, json=body)

        return handler

    def run_once(utc_time: str, *, auth_fails_on_second_page: bool) -> None:
        before = db.execute("SELECT coalesce(max(id), 0) AS id FROM collect_runs").fetchone()["id"]
        router = _build_connectors(_config(True), FULL_ENV, HttpClient(), db)["x"]
        router._home._session._client = httpx.Client(
            transport=httpx.MockTransport(handler_for(auth_fails_on_second_page))
        )
        collect_all(db, {"x": router}, logging.getLogger("test-x"))
        db.execute("UPDATE collect_runs SET started_at = ? WHERE id > ?", (utc_time, before))

    run_once("2026-10-23T21:30:00Z", auth_fails_on_second_page=True)  # JST 10/24 6:30
    failed = db.execute(
        "SELECT status, api_reads, estimated_cost_usd, error FROM collect_runs"
    ).fetchone()
    assert (failed["status"], failed["api_reads"]) == ("error", 100)
    assert failed["estimated_cost_usd"] == pytest.approx(0.4)
    assert "認証失敗" in failed["error"]
    source = db.execute(
        "SELECT cursor, consecutive_failures FROM sources WHERE id = ?", (source_id,)
    ).fetchone()
    assert source["cursor"] == '{"since_id": "1"}'  # 据え置き
    assert source["consecutive_failures"] == 1
    assert db.execute("SELECT count(*) FROM items").fetchone()[0] == 0  # 取得ぶんは保存しない
    assert x_reads_today(db) == {"home": 100, "search": 0}

    run_once("2026-10-24T03:00:00Z", auth_fails_on_second_page=False)  # 同じ日の再走
    reads = [r["api_reads"] for r in db.execute("SELECT api_reads FROM collect_runs ORDER BY id")]
    assert reads == [100, 30]
    assert seen == ["100", "30", "30"]


def test_same_day_rerun_without_budget_keeps_source_failure_state(db, monkeypatch) -> None:
    # GH#178: 検索の上限 35・3 スロットで、1 本目が 12 件読み、2 本目が 403 で失敗した日。
    # 同じ日の再走は残り 23(8/8/7)で、どのスロットも要求しない。要求していないので復旧は
    # 確かめておらず、連続失敗数・直近のエラー・成功の時刻を変えない(警告から外さない)
    monkeypatch.setattr("app.connectors.x.today_jst", lambda: "2026-10-24")
    source_id = add_x_source(db, "x:search", '{"mode": "search"}')
    db.execute(
        "UPDATE sources SET cursor = ?, last_success_at = ?, consecutive_failures = 2 WHERE id = ?",
        ('{"since_id": "1"}', "2026-10-21T21:30:00Z", source_id),
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(urlsplit(str(request.url)).query)
        seen.append(query["query"][0])
        if not query["query"][0].startswith("(a)"):
            return httpx.Response(403, json={})
        count = int(query["max_results"][0])
        tweets = [tweet(str(900 - i), "検索の投稿") for i in range(count)]
        return httpx.Response(200, json=page(tweets))

    def run_once(utc_time: str) -> None:
        before = db.execute("SELECT coalesce(max(id), 0) AS id FROM collect_runs").fetchone()["id"]
        config = _config(True, slots=("a", "b", "c"))
        router = _build_connectors(config, FULL_ENV, HttpClient(), db)["x"]
        router._search._session._client = httpx.Client(transport=httpx.MockTransport(handler))
        collect_all(db, {"x": router}, logging.getLogger("test-x"))
        db.execute("UPDATE collect_runs SET started_at = ? WHERE id > ?", (utc_time, before))

    def source_state() -> tuple:
        row = db.execute(
            "SELECT cursor, last_success_at, last_error_at, last_error, consecutive_failures "
            "FROM sources WHERE id = ?",
            (source_id,),
        ).fetchone()
        return tuple(row)

    run_once("2026-10-23T21:30:00Z")  # JST 10/24 6:30
    assert len(seen) == 2  # a(12 件)と b(403)
    failed = source_state()
    assert failed[0] == '{"since_id": "1"}'
    assert failed[1] == "2026-10-21T21:30:00Z"
    assert "認証失敗" in failed[3]
    assert failed[4] == 3
    assert x_reads_today(db) == {"home": 0, "search": 12}

    run_once("2026-10-24T03:00:00Z")  # 同じ日の再走
    assert len(seen) == 2  # 残り 23 = 8/8/7 はどれも下限 10 未満。要求しない
    assert source_state() == failed  # 失敗の状態のまま(成功で消さない)
    runs = db.execute("SELECT status, api_reads FROM collect_runs ORDER BY id").fetchall()
    assert [tuple(r) for r in runs] == [("error", 12), ("ok", 0)]
    assert source_failure_alerts(db, "x", X_FAILURE_ALERT) != []  # 警告に載ったまま


def test_build_x_connector_signs_requests() -> None:
    env = {"X_API_KEY": "ck", "X_API_SECRET": "cs",
           "X_ACCESS_TOKEN": "at", "X_ACCESS_TOKEN_SECRET": "ats"}
    conn = build_x_connector(130, 0.004, env)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if urlsplit(str(request.url)).path.endswith("/users/me"):
            return httpx.Response(200, json={"data": {"id": "me"}})
        return httpx.Response(200, json=page([tweet("1", "t")]))

    conn._session._client = httpx.Client(transport=httpx.MockTransport(handler))
    conn.fetch(make_source(), {})
    assert all(r.headers.get("authorization", "").startswith("OAuth ") for r in seen)


@pytest.mark.parametrize(
    "note_tweet",
    [None, "not-a-dict", {}, {"text": ""}, {"text": None}],  # 非 dict・text 欠落/空
)
def test_tweet_full_text_falls_back_to_text(note_tweet) -> None:
    # GH#94(Codex should): note_tweet の異常形はすべて従来の text にフォールバック
    from app.connectors.x import tweet_full_text

    t: dict = {"id": "1", "text": "従来テキスト"}
    if note_tweet is not None:
        t["note_tweet"] = note_tweet
    assert tweet_full_text(t) == "従来テキスト"


def test_note_tweet_full_text_preferred() -> None:
    # GH#94: 長文ポストは note_tweet の全文を raw_content に(text は280字切り詰め)。
    # note_tweet が無い普通のポストは従来どおり text
    long = tweet("900", "先頭280字で切られたテキスト…")
    long["note_tweet"] = {"text": "全文テキスト。" * 100}
    seen: list[httpx.Request] = []
    result = connector_from(
        [(200, page([long, tweet("899", "普通のポスト")]), {})], seen=seen
    ).fetch(make_source(), {})

    by_id = {i.external_id: i for i in result.items}
    assert by_id["900"].raw_content == "全文テキスト。" * 100
    assert by_id["899"].raw_content == "普通のポスト"
    timeline = next(r for r in seen if "timelines" in str(r.url))
    fields = parse_qs(urlsplit(str(timeline.url)).query)["tweet.fields"][0]
    assert "note_tweet" in fields  # フィールドを実際に要求している


def test_router_rejects_bookmark_row_as_collect_target() -> None:
    # bookmark 行は items の帰属専用(GH#99)。collect に来たら黙って home に化けず fail fast
    router = XRouter(home=_StubConnector(), search=_StubConnector())
    with pytest.raises(FatalError, match="mode=bookmark"):
        router.fetch(make_source(config='{"mode": "bookmark"}'), {})
