"""GDELT の DOC 2.0 API の connector(出来事の調査の報道の検索。GH#415)。"""

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.connectors.gdelt import DOC_API_URL, GdeltArticle, GdeltError, search_articles
from tests.test_extraction import make_http

START = datetime(2026, 11, 12, tzinfo=UTC)
END = datetime(2026, 11, 14, 23, 59, 59, tzinfo=UTC)


def responder(status: int = 200, body: str = "", seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, text=body, headers={"content-type": "application/json"})

    return handler


def test_search_parses_articles_and_sends_fixed_parameters() -> None:
    body = json.dumps(
        {
            "articles": [
                {
                    "url": "https://www.Example.com/a?utm_source=x",
                    "title": "Microsoft outage\n drags on",
                    "domain": "example.com",
                    "language": "English",
                    "seendate": "20261113T180000Z",
                },
                {"url": "ftp://bad.example/a", "title": "スキームが違う"},
                {"url": "https://b.example/", "title": "   "},
                {"url": 1, "title": "形が違う"},
                "形が違う",
            ]
        }
    )
    seen: list[httpx.Request] = []
    http = make_http(responder(body=body, seen=seen))

    articles = search_articles(http, '"Microsoft 365" outage', start=START, end=END)

    assert articles == [
        GdeltArticle(
            url="https://www.example.com/a",  # 正規化(設計 01 §4)を通す
            title="Microsoft outage drags on",
            domain="example.com",
            language="English",
            seen_at="20261113T180000Z",
        )
    ]
    [request] = seen
    assert f"https://{request.headers['host']}{request.url.path}" == DOC_API_URL
    params = {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}
    assert params == {
        "query": '"Microsoft 365" outage',
        "mode": "artlist",
        "format": "json",
        "maxrecords": "25",
        "sort": "hybridrel",
        "startdatetime": "20261112000000",
        "enddatetime": "20261114235959",
    }


@pytest.mark.parametrize("body", ["", "{}", '{"articles": []}'])
def test_search_without_results_is_empty(body) -> None:
    assert search_articles(make_http(responder(body=body)), "abc", start=START, end=END) == []


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, "Your search contained a keyword that was too short."),  # 誤りは平文で返る
        (200, "[1, 2]"),
        (200, '{"articles": {}}'),
        (400, "bad request"),
        (404, ""),
    ],
)
def test_search_failure_raises(status, body) -> None:
    with pytest.raises(GdeltError):
        search_articles(make_http(responder(status, body)), "abc", start=START, end=END)


def test_search_does_not_retry_on_429() -> None:
    # 429 の後の短い間隔の試し直しも数えて止められるので、1 回だけ送って検索の失敗にする
    seen: list[httpx.Request] = []
    with pytest.raises(GdeltError):
        search_articles(make_http(responder(429, seen=seen)), "abc", start=START, end=END)
    assert len(seen) == 1
