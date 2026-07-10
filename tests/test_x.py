from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.connectors.base import FatalError, RetryableError
from app.connectors.x import (
    XConnector,
    XCredentials,
    build_x_connector,
    has_x_credentials,
)
from app.models import Source

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
    """pages: 各 timeline リクエストが返す (status, body_dict, headers) のリスト。"""
    calls = {"timeline": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = urlsplit(str(request.url)).path
        if seen is not None:
            seen.append(request)
        if path.endswith("/users/me"):
            return httpx.Response(200, json={"data": {"id": me_id}})
        idx = calls["timeline"]
        calls["timeline"] += 1
        status, body, headers = pages[idx]
        return httpx.Response(status, json=body, headers=headers)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return XConnector(
        client=client, credentials=CREDS,
        daily_read_cap=daily_read_cap, unit_price_usd=unit_price,
    )


def test_basic_fetch_parses_and_costs() -> None:
    body = page(
        [tweet("300", "こんにちは"), tweet("299", "ふたつめ", author="a2")],
        users={"a1": "alice", "a2": "bob"},
    )
    conn = connector_from([(200, body, {})])
    result = conn.fetch(make_source(), {})

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


def test_pagination_stops_at_daily_read_cap() -> None:
    p1 = page([tweet(str(400 - i), "t") for i in range(100)], next_token="tok1")
    p2 = page([tweet(str(300 - i), "t") for i in range(100)], next_token="tok2")
    # cap=150 なら 2ページ(200件)取得後に cap 到達で3ページ目は取りに行かない
    conn = connector_from([(200, p1, {}), (200, p2, {})], daily_read_cap=150)
    result = conn.fetch(make_source(), {})
    assert result.stats.api_reads == 200
    assert result.next_cursor == {"since_id": "400"}


def test_rate_limit_remaining_zero_stops() -> None:
    p1 = page([tweet("500", "t")], next_token="tok1")
    conn = connector_from([(200, p1, {"x-rate-limit-remaining": "0"})])
    result = conn.fetch(make_source(), {})
    # 残0で打ち切り。next_token があっても2ページ目は取らない
    assert result.stats.api_reads == 1


def test_no_next_token_stops() -> None:
    p1 = page([tweet("500", "t")])  # next_token なし = TL 末尾 / since_id 到達
    conn = connector_from([(200, p1, {})])
    result = conn.fetch(make_source(), {})
    assert result.stats.api_reads == 1
    assert result.next_cursor == {"since_id": "500"}


def test_retweet_normalizes_to_original() -> None:
    body = page([tweet("600", "RT @x: ...", retweeted="42")], users={"a1": "alice"})
    conn = connector_from([(200, body, {})])
    result = conn.fetch(make_source(), {})
    (rt,) = result.items
    assert rt.external_id == "42"  # 元ツイに寄せて dedup
    assert rt.url == "https://x.com/i/web/status/42"
    assert rt.meta == {"retweet": True, "reply": False}


def test_reply_to_other_excluded_but_self_reply_kept() -> None:
    body = page([
        tweet("700", "他人宛リプライ", in_reply_to="stranger"),
        tweet("701", "自分宛リプライ", in_reply_to="me"),
        tweet("702", "通常ポスト"),
    ])
    conn = connector_from([(200, body, {})], me_id="me")
    result = conn.fetch(make_source(), {})
    kept = {i.external_id for i in result.items}
    assert kept == {"701", "702"}  # 他人宛リプライ(700)は除外
    assert result.stats.api_reads == 3  # 除外しても読取件数(課金)は3


def test_since_id_passed_as_param() -> None:
    seen: list[httpx.Request] = []
    body = page([tweet("800", "t")])
    conn = connector_from([(200, body, {})], seen=seen)
    conn.fetch(make_source(), {"since_id": "790"})
    timeline_req = next(r for r in seen if "timelines" in str(r.url))
    assert parse_qs(urlsplit(str(timeline_req.url)).query)["since_id"] == ["790"]


def test_empty_timeline_keeps_cursor() -> None:
    conn = connector_from([(200, page([]), {})])
    result = conn.fetch(make_source(), {"since_id": "900"})
    assert result.items == []
    assert result.stats.api_reads == 0
    assert result.next_cursor == {"since_id": "900"}  # 取得ゼロなら据え置き


def test_auth_error_is_fatal() -> None:
    conn = connector_from([(200, page([]), {})], me_id="me")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    conn._client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(FatalError, match="認証失敗"):
        conn.fetch(make_source(), {})


def test_429_returns_partial() -> None:
    p1 = page([tweet("500", "t")], next_token="tok1")
    conn = connector_from([(200, p1, {}), (429, {}, {})], daily_read_cap=500)
    result = conn.fetch(make_source(), {})
    assert result.stats.api_reads == 1  # 1ページ取得後に 429 → 既取得分を返す
    assert result.next_cursor == {"since_id": "500"}


def test_5xx_on_first_page_is_retryable() -> None:
    conn = connector_from([(503, {}, {})])
    with pytest.raises(RetryableError, match="503"):
        conn.fetch(make_source(), {})


def test_5xx_after_first_page_returns_partial() -> None:
    p1 = page([tweet("500", "t")], next_token="tok1")
    conn = connector_from([(200, p1, {}), (503, {}, {})], daily_read_cap=500)
    result = conn.fetch(make_source(), {})
    assert result.stats.api_reads == 1  # 途中の 5xx は既取得分で打ち切り


def test_has_x_credentials() -> None:
    full = {k: "v" for k in
            ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")}
    assert has_x_credentials(full) is True
    del full["X_ACCESS_TOKEN"]
    assert has_x_credentials(full) is False


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

    conn._client = httpx.Client(transport=httpx.MockTransport(handler))
    conn.fetch(make_source(), {})
    assert all(r.headers.get("authorization", "").startswith("OAuth ") for r in seen)
