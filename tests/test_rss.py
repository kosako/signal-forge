import httpx
import pytest

from app.connectors.base import FatalError, RetryableError
from app.connectors.rss import RssConnector
from app.http import HttpClient
from app.models import Source

FEED = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Sample</title>
  <entry>
    <id>tag:example.com,2026:entry-1</id>
    <title>記事1</title>
    <link href="https://example.com/articles/1"/>
    <author><name>Alice</name></author>
    <published>2026-07-05T09:00:00Z</published>
    <content type="html">&lt;p&gt;全文コンテンツ&lt;/p&gt;</content>
  </entry>
  <entry>
    <title>記事2(id なし・summary のみ)</title>
    <link href="https://example.com/articles/2"/>
    <summary>要約テキスト</summary>
  </entry>
  <entry>
    <title>link なし(取り込めない)</title>
  </entry>
</feed>
"""


def make_source(**overrides) -> Source:
    values = dict(
        id=2,
        type="rss",
        name="sample",
        config='{"url": "https://example.com/feed.xml"}',
        cursor=None,
        enabled=1,
        last_success_at=None,
        last_error_at=None,
        last_error=None,
        consecutive_failures=0,
        created_at="2026-07-06T00:00:00Z",
    )
    values.update(overrides)
    return Source(**values)


def make_connector(handler) -> RssConnector:
    client = HttpClient(
        transport=httpx.MockTransport(handler),
        resolve_host=lambda host: ["93.184.216.34"],
        sleep=lambda _s: None,
    )
    return RssConnector(client)


def test_fetch_parses_entries_and_cursor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=FEED.encode(), headers={"etag": 'W/"abc"', "last-modified": "yesterday"}
        )

    result = make_connector(handler).fetch(make_source(), {})
    assert result.stats.items_fetched == 2  # link なし entry は落ちる
    first, second = result.items
    assert first.external_id == "tag:example.com,2026:entry-1"
    assert first.title == "記事1"
    assert first.author == "Alice"
    assert first.raw_content == "<p>全文コンテンツ</p>"
    assert first.published_at.isoformat() == "2026-07-05T09:00:00+00:00"
    assert second.external_id is None
    assert second.raw_content == "要約テキスト"
    assert result.next_cursor == {"etag": 'W/"abc"', "last_modified": "yesterday"}


def test_conditional_get_headers_sent() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(304)

    cursor = {"etag": 'W/"abc"', "last_modified": "yesterday"}
    result = make_connector(handler).fetch(make_source(), cursor)
    assert seen["if-none-match"] == 'W/"abc"'
    assert seen["if-modified-since"] == "yesterday"
    assert result.items == []
    assert result.next_cursor == cursor  # 304 はカーソル維持
    assert result.stats.http_status == 304


def test_410_gone_is_fatal() -> None:
    connector = make_connector(lambda request: httpx.Response(410))
    with pytest.raises(FatalError, match="410"):
        connector.fetch(make_source(), {})


def test_403_is_fatal() -> None:
    connector = make_connector(lambda request: httpx.Response(403))
    with pytest.raises(FatalError, match="403"):
        connector.fetch(make_source(), {})


def test_garbage_body_is_retryable() -> None:
    connector = make_connector(lambda request: httpx.Response(200, content=b"\x00\x01 not xml"))
    with pytest.raises(RetryableError, match="パース失敗"):
        connector.fetch(make_source(), {})


def test_missing_url_config_is_fatal() -> None:
    connector = make_connector(lambda request: httpx.Response(200))
    with pytest.raises(FatalError, match="設定不備"):
        connector.fetch(make_source(config="{}"), {})
