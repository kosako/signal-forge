"""今の重点の語の Hacker News の検索(GH#433 の 5 本目・app/connectors/hn_search.py)。"""

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.connectors.base import FatalError, RetryableError
from app.connectors.hn_search import ITEM_URL, SEARCH_URL, HnFocusConnector
from app.models import Source
from tests.test_extraction import make_http

NOW = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)
SOURCE = Source(
    id=9, type="rss", name="hn:focus", config='{"mode": "hn_focus"}', cursor=None, enabled=0,
    last_success_at=None, last_error_at=None, last_error=None, consecutive_failures=0,
    created_at="2026-11-20T00:00:00Z",
)


def created(hours_ago: float) -> int:
    return int((NOW - timedelta(hours=hours_ago)).timestamp())


def hit(story_id: str, points: int, *, hours_ago: float = 1, url: str | None = None, **extra):
    return {
        "objectID": story_id,
        "title": f"  story\n{story_id} ",
        "url": f"https://example.com/{story_id}" if url is None else url,
        "points": points,
        "created_at_i": created(hours_ago),
        "author": "someone",
        "num_comments": 3,
        **extra,
    }


def responder(by_query: dict[str, object], seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(urlsplit(str(request.url)).query)
        if seen is not None:
            seen.append(query)
        answer = by_query[query["query"][0]]
        if isinstance(answer, int):
            return httpx.Response(answer, text="error")
        if isinstance(answer, str):
            return httpx.Response(200, text=answer)
        return httpx.Response(200, json={"hits": answer})

    return handler


def connector(http, queries, **kwargs) -> HnFocusConnector:
    options = {"min_points": 5, "window_hours": 48, "now": lambda: NOW, **kwargs}
    return HnFocusConnector(http, queries, **options)


def test_fetch_sends_period_and_points_and_merges_queries_by_points() -> None:
    seen: list = []
    calls: list[str] = []
    http = make_http(
        responder(
            {
                "software factory": [
                    hit("1", 12),
                    hit("2", 7),
                    hit("3", 2),  # 点数の下限より低い(応答でも確かめ直す)
                    hit("4", 50, hours_ago=49),  # 期間より古い
                    {"objectID": "5", "points": 9, "created_at_i": created(1)},  # 見出しが無い
                    hit("abc", 9),  # id が数字でない
                    hit("6", True),  # 点数が bool
                ],
                "Claude Code": [hit("1", 10), hit("7", 30)],  # 同じ投稿の低い点は捨てる
            },
            seen,
        ),
        calls,
    )

    result = connector(http, ["software factory", "Claude Code"]).fetch(SOURCE, {})

    since = created(48)
    assert [q["query"] for q in seen] == [["software factory"], ["Claude Code"]]
    for q in seen:
        assert q["tags"] == ["story"]
        assert q["restrictSearchableAttributes"] == ["title"]  # 本文の一部の一致で当てない
        assert q["numericFilters"] == [f"created_at_i>{since},points>=5"]
        assert q["hitsPerPage"] == ["20"]
    assert calls == [SEARCH_URL] * 2
    # 点数の高い順。同じ投稿は 1 つにし、点数の高い方の当たりを残す
    assert [item.external_id for item in result.items] == [
        ITEM_URL.format(id="7"),
        ITEM_URL.format(id="1"),
        ITEM_URL.format(id="2"),
    ]
    top = result.items[1]
    assert top.url == "https://example.com/1"
    assert top.title == "story 1"  # 1 行に潰す
    assert top.published_at == NOW - timedelta(hours=1)
    assert top.raw_content is None
    assert top.meta == {"hn_points": 12, "hn_comments": 3, "focus_query": "software factory"}
    assert result.partial_error is None
    assert result.next_cursor == {}
    assert (result.stats.items_fetched, result.stats.api_reads) == (3, 0)
    assert result.stats.estimated_cost_usd == 0.0


def test_same_points_are_ordered_newest_first_then_by_id() -> None:
    http = make_http(
        responder(
            {"q": [hit("20", 8, hours_ago=5), hit("10", 8, hours_ago=1), hit("30", 8, hours_ago=5)]}
        )
    )
    items = connector(http, ["q"]).fetch(SOURCE, {}).items
    assert [item.external_id for item in items] == [
        ITEM_URL.format(id="10"),
        ITEM_URL.format(id="20"),
        ITEM_URL.format(id="30"),
    ]


def test_post_without_article_url_uses_item_page_and_bundles_text() -> None:
    http = make_http(
        responder(
            {
                "q": [
                    hit("1", 9, url="", story_text="<p>Ask HN body</p>"),
                    {**hit("2", 8), "url": None},
                ]
            }
        )
    )
    first, second = connector(http, ["q"]).fetch(SOURCE, {}).items
    assert (first.url, first.raw_content) == (ITEM_URL.format(id="1"), "<p>Ask HN body</p>")
    assert (second.url, second.raw_content) == (ITEM_URL.format(id="2"), None)


def test_a_malformed_post_is_dropped_without_losing_the_others() -> None:
    # 応答は未検証の外部データ。分解できない URL や日時に変換できない時刻でも、その 1 件だけを捨てる
    http = make_http(
        responder(
            {
                "q": [
                    hit("1", 9, url="https://[broken"),
                    hit("2", 9, url="javascript:alert(1)"),  # http/https でない
                    hit("3", 9, url="https://"),  # host が無い
                    {**hit("4", 9), "created_at_i": 10**20},
                    {**hit("5", 9), "created_at_i": created(-2)},  # 2 時間先(時計のずれの外)
                    hit("6", 9),
                ]
            }
        )
    )
    items = connector(http, ["q"]).fetch(SOURCE, {}).items
    assert [item.external_id for item in items] == [ITEM_URL.format(id="6")]


def test_failure_text_does_not_carry_the_search_terms() -> None:
    # 共通 HTTP クライアントの例外の文は URL(= 語)を含む。ログと台帳に語を残さない
    http = make_http(responder({"secret words": 500}))
    with pytest.raises(RetryableError) as raised:
        connector(http, ["secret words"]).fetch(SOURCE, {})
    text = str(raised.value)
    assert "secret" not in text and "algolia" not in text
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


def test_partial_failure_keeps_the_queries_that_were_read() -> None:
    http = make_http(responder({"bad": 403, "good": [hit("1", 9)]}))
    result = connector(http, ["bad", "good"]).fetch(SOURCE, {})
    assert [item.external_id for item in result.items] == [ITEM_URL.format(id="1")]
    assert isinstance(result.partial_error, FatalError)


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (403, FatalError),
        (500, RetryableError),
        ("not json", RetryableError),
        (json.dumps({}), RetryableError),
    ],
)
def test_all_queries_failing_raises(answer, error) -> None:
    http = make_http(responder({"a": answer, "b": answer}))
    with pytest.raises(error):
        connector(http, ["a", "b"]).fetch(SOURCE, {})
