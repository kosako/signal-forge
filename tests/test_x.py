from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.cli import _build_connectors
from app.config import Config, XConfig, XSearchConfig
from app.connectors.base import FatalError, FetchResult, FetchStats, RetryableError
from app.connectors.x import (
    XApiSession,
    XConnector,
    XCredentials,
    XRouter,
    build_x_connector,
    fetch_me,
    has_x_credentials,
)
from app.connectors.x_search import XSearchConnector
from app.http import HttpClient
from app.models import Source

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
                   unit_price=0.004, seen=None) -> XConnector:
    """pages[i] = (status, body_dict, headers)。timeline リクエストは pagination_token で
    index を引く(リトライで同 token を再送しても同じ応答 = リトライ安全)。200 応答は
    max_results で data を切り詰めて実 API の挙動を模す。"""
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
        if status == 200:
            limit = int(query.get("max_results", ["100"])[0])
            body = {**body, "data": body.get("data", [])[:limit]}
        return httpx.Response(status, json=body, headers=headers)

    return XConnector(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        credentials=CREDS,
        daily_read_cap=daily_read_cap,
        unit_price_usd=unit_price,
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


def test_rate_limit_remaining_zero_stops() -> None:
    p1 = page([tweet("500", "t")], next_token="tok1")
    result = connector_from([(200, p1, {"x-rate-limit-remaining": "0"})]).fetch(make_source(), {})
    assert result.stats.api_reads == 1  # 残0で打ち切り。next_token があっても2ページ目は取らない


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
    assert rt.meta == {"retweet": True, "reply": False}


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
        make_source(), {}
    )
    assert result.stats.api_reads == 1  # 1ページ取得後の 429 → 既取得分を返す
    assert result.next_cursor == {"since_id": "500"}


def test_auth_error_is_fatal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    conn = XConnector(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        credentials=CREDS, daily_read_cap=130, unit_price_usd=0.004, sleep=lambda _s: None,
    )
    with pytest.raises(FatalError, match="認証失敗"):
        conn.fetch(make_source(), {})


def test_5xx_on_first_page_is_retryable() -> None:
    with pytest.raises(RetryableError, match="503"):
        connector_from([(503, {}, {})]).fetch(make_source(), {})


def _me_session(handler) -> XApiSession:
    return XApiSession(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        credentials=CREDS,
        sleep=lambda _s: None,
    )


def test_fetch_me_returns_data() -> None:
    # doctor #5(GH#63)とホームTL が共用する users/me。username まで取れること
    session = _me_session(
        lambda request: httpx.Response(200, json={"data": {"id": "1", "username": "aki"}})
    )
    assert fetch_me(session) == {"id": "1", "username": "aki"}


def test_fetch_me_maps_statuses_to_errors() -> None:
    with pytest.raises(FatalError, match="認証失敗"):
        fetch_me(_me_session(lambda request: httpx.Response(403, json={})))
    with pytest.raises(RetryableError, match="429"):
        fetch_me(_me_session(lambda request: httpx.Response(429, json={})))
    with pytest.raises(RetryableError, match="id がない"):
        fetch_me(_me_session(lambda request: httpx.Response(200, json={"data": {}})))


def test_5xx_after_first_page_returns_partial() -> None:
    p1 = page([tweet("500", "t")], next_token="tok1")
    result = connector_from([(200, p1, {}), (503, {}, {})], daily_read_cap=500).fetch(
        make_source(), {}
    )
    assert result.stats.api_reads == 1  # 途中の 5xx はリトライ尽き後に既取得分で打ち切り


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


def test_build_connectors_wires_x_only_when_enabled_and_keyed() -> None:
    http = HttpClient()
    assert set(_build_connectors(_config(False), FULL_ENV, http)) == {"rss"}  # X 無効
    assert set(_build_connectors(_config(True), FULL_ENV, http)) == {"rss", "x"}  # 有効+キー
    # 有効だがキー欠落 → X は載せず RSS は残る(縮退。欠落は doctor #5)
    assert set(_build_connectors(_config(True), {}, http)) == {"rss"}


def test_build_connectors_wires_search_only_with_slots() -> None:
    http = HttpClient()
    router = _build_connectors(_config(True), FULL_ENV, http)["x"]
    assert isinstance(router, XRouter)
    assert router._search is None  # slots 空 = 検索無効
    router = _build_connectors(_config(True, slots=("ai",)), FULL_ENV, http)["x"]
    assert isinstance(router._home, XConnector)
    assert isinstance(router._search, XSearchConnector)


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
