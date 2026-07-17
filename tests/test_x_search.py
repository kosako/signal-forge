from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.config import X_SEARCH_QUERY_SUFFIX
from app.connectors.base import FatalError, RetryableError
from app.connectors.x import XCredentials
from app.connectors.x_search import XSearchConnector, _split_budget
from app.models import Source

CREDS = XCredentials(
    api_key="ck", api_secret="cs", access_token="at", access_token_secret="ats"
)


def make_source(**overrides) -> Source:
    values = dict(
        id=2,
        type="x",
        name="x:search",
        config='{"mode": "search"}',
        cursor=None,
        enabled=1,
        last_success_at=None,
        last_error_at=None,
        last_error=None,
        consecutive_failures=0,
        created_at="2026-07-11T00:00:00Z",
    )
    values.update(overrides)
    return Source(**values)


def tweet(tid: str, text: str = "t", *, author="a1",
          created="2026-07-11T06:00:00.000Z") -> dict:
    return {"id": tid, "text": text, "author_id": author, "created_at": created}


def page(tweets: list[dict], *, next_token=None, users=None) -> dict:
    body: dict = {"data": tweets}
    if next_token:
        body["meta"] = {"next_token": next_token}
    if users:
        body["includes"] = {"users": [{"id": uid, "username": name} for uid, name in users.items()]}
    return body


def connector_from(pages: dict[str, list[tuple]], *, slots, daily_read_cap=35,
                   unit_price=0.004, seen=None) -> XSearchConnector:
    """pages[slot] = [(status, body, headers), ...](ページ順)。リクエストの next_token で
    ページ index を引く(リトライで同 token を再送しても同じ応答 = リトライ安全)。
    200 応答は max_results で data を切り詰めて実 API の挙動を模す。"""
    token_index: dict[str, int] = {}
    for series in pages.values():
        for i, (_s, body, _h) in enumerate(series):
            tok = body.get("meta", {}).get("next_token")
            if tok:
                token_index[tok] = i + 1

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        query = parse_qs(urlsplit(str(request.url)).query)
        raw_query = query["query"][0]
        assert raw_query.endswith(f" {X_SEARCH_QUERY_SUFFIX}")  # 演算子の自動付加
        slot = raw_query.removesuffix(f" {X_SEARCH_QUERY_SUFFIX}")
        token = query.get("next_token", [None])[0]
        status, body, headers = pages[slot][0 if token is None else token_index[token]]
        if status == 200:
            limit = int(query["max_results"][0])
            body = {**body, "data": body.get("data", [])[:limit]}
        return httpx.Response(status, json=body, headers=headers)

    return XSearchConnector(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        credentials=CREDS,
        slots=tuple(slots),
        daily_read_cap=daily_read_cap,
        unit_price_usd=unit_price,
        sleep=lambda _s: None,
    )


def test_three_slots_fetch_parse_and_cost() -> None:
    seen: list[httpx.Request] = []
    pages = {
        "ai agents": [(200, page([tweet("300", "こんにちは")], users={"a1": "alice"}), {})],
        "llm": [(200, page([tweet("299", author="a2")], users={"a2": "bob"}), {})],
        "mcp": [(200, page([]), {})],
    }
    result = connector_from(pages, slots=("ai agents", "llm", "mcp"), seen=seen).fetch(
        make_source(), {}
    )
    assert result.stats.items_fetched == 2
    assert result.stats.api_reads == 2
    assert result.stats.estimated_cost_usd == pytest.approx(2 * 0.004)
    first, second = result.items
    assert first.external_id == "300"
    assert first.url == "https://x.com/i/web/status/300"
    assert first.author == "alice"
    assert first.raw_content == "こんにちは"
    assert first.title is None
    assert first.published_at.isoformat() == "2026-07-11T06:00:00+00:00"
    assert first.meta == {"query": "ai agents"}
    assert second.author == "bob"
    assert second.meta == {"query": "llm"}
    assert result.next_cursor == {"since_id": "300"}  # 全スロット横断の最大 tweet id
    # 予算はスロット数で均等割・端数は先頭から(35 → 12/12/11)
    limits = [parse_qs(urlsplit(str(r.url)).query)["max_results"][0] for r in seen]
    assert limits == ["12", "12", "11"]


def test_since_id_passed_and_kept_when_no_results() -> None:
    seen: list[httpx.Request] = []
    pages = {"ai": [(200, page([]), {})]}
    result = connector_from(pages, slots=("ai",), seen=seen).fetch(
        make_source(), {"since_id": "900"}
    )
    assert result.items == []
    assert result.stats.api_reads == 0
    assert result.next_cursor == {"since_id": "900"}  # 取得ゼロなら据え置き
    assert parse_qs(urlsplit(str(seen[0].url)).query)["since_id"] == ["900"]


def test_no_cursor_and_no_results_returns_empty_cursor() -> None:
    pages = {"ai": [(200, page([]), {})]}
    assert connector_from(pages, slots=("ai",)).fetch(make_source(), {}).next_cursor == {}


def test_pagination_stops_below_min_page() -> None:
    # cap 105・1スロット: 100 件取得後、残枠 5 < 10(max_results 下限)→ 2ページ目は取らない
    seen: list[httpx.Request] = []
    p1 = page([tweet(str(500 - i)) for i in range(100)], next_token="tok1")
    p2 = page([tweet(str(400 - i)) for i in range(100)], next_token="tok2")
    result = connector_from(
        {"ai": [(200, p1, {}), (200, p2, {})]}, slots=("ai",), daily_read_cap=105, seen=seen
    ).fetch(make_source(), {})
    assert result.stats.api_reads == 100  # cap はハード上限(超過方向に丸めない)
    assert len(seen) == 1
    assert result.next_cursor == {"since_id": "500"}


def test_pagination_second_page_uses_next_token_and_remainder() -> None:
    # cap 110: 100 件 + max_results=10 の2ページ目
    seen: list[httpx.Request] = []
    p1 = page([tweet(str(500 - i)) for i in range(100)], next_token="tok1")
    p2 = page([tweet(str(400 - i)) for i in range(10)])
    result = connector_from(
        {"ai": [(200, p1, {}), (200, p2, {})]}, slots=("ai",), daily_read_cap=110, seen=seen
    ).fetch(make_source(), {})
    assert result.stats.api_reads == 110
    q2 = parse_qs(urlsplit(str(seen[1].url)).query)
    assert q2["max_results"] == ["10"]
    assert q2["next_token"] == ["tok1"]  # search/recent は pagination_token でなく next_token


def test_rate_limit_zero_stops_remaining_slots_and_keeps_cursor() -> None:
    seen: list[httpx.Request] = []
    pages = {
        "a": [(200, page([tweet("500")], next_token="tok1"), {"x-rate-limit-remaining": "0"})],
        "b": [(200, page([tweet("600")]), {})],
    }
    result = connector_from(pages, slots=("a", "b"), seen=seen).fetch(
        make_source(), {"since_id": "100"}
    )
    assert result.stats.api_reads == 1  # 残 0 → スロット b は当日分を諦める
    assert len(seen) == 1
    # 未実行スロット(b)の当日ぶんを watermark の背後に隠さない → カーソル据え置き
    assert result.next_cursor == {"since_id": "100"}


def test_first_page_429_with_zero_total_raises() -> None:
    # 全スロット累計 0 件での失敗を「0件成功」で隠さない(ホームTL と同方針)
    pages = {"a": [(429, {}, {})], "b": [(200, page([tweet("1")]), {})]}
    with pytest.raises(RetryableError, match="429"):
        connector_from(pages, slots=("a", "b")).fetch(make_source(), {})


def test_failure_after_prior_slot_returns_partial_without_advancing_cursor() -> None:
    seen: list[httpx.Request] = []
    pages = {
        "a": [(200, page([tweet("500")]), {})],
        "b": [(503, {}, {})],
        "c": [(200, page([tweet("600")]), {})],
    }
    result = connector_from(pages, slots=("a", "b", "c"), seen=seen).fetch(make_source(), {})
    assert [i.external_id for i in result.items] == ["500"]  # 部分取得。c は取らない
    # カーソルを 500 に進めると b/c の当日ぶんが恒久欠落する → 据え置き(翌日再取得)
    assert result.next_cursor == {}
    assert sum("query=b" in str(r.url) for r in seen) == 3  # 5xx は初回+2回リトライ


def test_auth_error_is_fatal() -> None:
    with pytest.raises(FatalError, match="認証失敗"):
        connector_from({"a": [(401, {}, {})]}, slots=("a",)).fetch(make_source(), {})


def test_same_tweet_in_two_slots_returned_per_slot() -> None:
    # 重複返却は collect の UNIQUE(source_id, external_id)が吸収する(設計 02 §1)
    pages = {
        "a": [(200, page([tweet("700")]), {})],
        "b": [(200, page([tweet("700")]), {})],
    }
    result = connector_from(pages, slots=("a", "b")).fetch(make_source(), {})
    assert [i.external_id for i in result.items] == ["700", "700"]
    assert result.stats.api_reads == 2  # 課金は2件(読取ベース)


def test_split_budget() -> None:
    assert _split_budget(35, 3) == [12, 12, 11]
    assert _split_budget(30, 3) == [10, 10, 10]
    assert _split_budget(35, 1) == [35]


def test_search_note_tweet_full_text_preferred() -> None:
    # GH#94: 検索でも note_tweet の全文を raw_content に
    long = tweet("900", "切り詰めテキスト…")
    long["note_tweet"] = {"text": "検索の全文。" * 100}
    seen: list[httpx.Request] = []
    conn = connector_from({"ai": [(200, page([long]), {})]}, slots=["ai"], seen=seen)
    result = conn.fetch(make_source(), {})

    (item,) = result.items
    assert item.raw_content == "検索の全文。" * 100
    fields = parse_qs(urlsplit(str(seen[0].url)).query)["tweet.fields"][0]
    assert "note_tweet" in fields
