"""Google ニュースの RSS の検索(出来事の調査の予備。GH#415)。"""

from datetime import date
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.connectors.google_news import (
    SEARCH_URL,
    GoogleNewsError,
    NewsHeadline,
    search_headlines,
)
from tests.test_extraction import make_http

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>検索</title>
<item><title>マイクロソフト365障害、20時間経て広範に復旧 - 日本経済新聞</title>
<link>https://news.google.com/rss/articles/CBMiAAA?oc=5</link>
<pubDate>Fri, 13 Nov 2026 20:43:24 GMT</pubDate>
<source url="https://www.nikkei.com">日本経済新聞</source></item>
<item><title>媒体名の無い見出し</title><link>https://news.google.com/rss/articles/B</link></item>
<item><title>スキームが違う - X</title><link>ftp://news.google.com/a</link></item>
<item><title> - 空</title><link>https://news.google.com/rss/articles/C</link>
<source url="https://x.example">空</source></item>
</channel></rss>"""


def responder(status: int = 200, body: str = RSS, seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, text=body, headers={"content-type": "application/xml"})

    return handler


def test_search_parses_headlines_and_sends_period() -> None:
    seen: list[httpx.Request] = []
    http = make_http(responder(seen=seen))

    found = search_headlines(
        http, "Microsoft 365 障害", after=date(2026, 11, 12), before=date(2026, 11, 15)
    )

    assert found == [
        NewsHeadline(
            url="https://news.google.com/rss/articles/CBMiAAA?oc=5",
            title="マイクロソフト365障害、20時間経て広範に復旧",  # 末尾の「 - 媒体名」を外す
            outlet="日本経済新聞",
            outlet_host="nikkei.com",
            published_at="2026-11-13T20:43:24Z",
        ),
        NewsHeadline(
            url="https://news.google.com/rss/articles/B",
            title="媒体名の無い見出し",
            outlet="",
            outlet_host="",
            published_at=None,
        ),
    ]
    [request] = seen
    assert f"https://{request.headers['host']}{request.url.path}" == SEARCH_URL
    params = {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}
    assert params == {
        "q": "Microsoft 365 障害 after:2026-11-12 before:2026-11-15",  # 期間はコードが付ける
        "hl": "ja",
        "gl": "JP",
        "ceid": "JP:ja",
    }


@pytest.mark.parametrize(("status", "body"), [(503, RSS), (404, ""), (200, "not xml <<<")])
def test_search_failure_raises(status, body) -> None:
    with pytest.raises(GoogleNewsError):
        search_headlines(
            make_http(responder(status, body)), "語", after=date(2026, 11, 12),
            before=date(2026, 11, 15),
        )


def test_search_without_results_is_empty() -> None:
    empty = '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>'
    found = search_headlines(
        make_http(responder(body=empty)), "語", after=date(2026, 11, 12), before=date(2026, 11, 15)
    )
    assert found == []
