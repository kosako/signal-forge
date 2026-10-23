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


@pytest.mark.parametrize("not_modified", [False, True])
def test_permanent_redirect_reports_moved_url(not_modified) -> None:
    # 301 を経由したら移転先を返す(304 でも。GH#150)。一時的な転送(302)は移転にしない
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/feed.xml":
            return httpx.Response(301, headers={"location": "https://example.com/new/feed.xml"})
        if request.url.path == "/new/feed.xml":
            if not_modified:
                return httpx.Response(304)
            return httpx.Response(200, content=FEED.encode())
        return httpx.Response(404)

    result = make_connector(handler).fetch(make_source(), {})
    assert result.moved_url == "https://example.com/new/feed.xml"


@pytest.mark.parametrize(
    "chain",
    [
        [(308, "/new/feed.xml")],
        [(301, "/moved/feed.xml"), (302, "/cdn/feed.xml")],  # 恒久のあとに一時的な転送
        [(302, "/tmp/feed.xml"), (301, "/final/feed.xml")],  # 一時的な転送のあとに恒久
    ],
)
@pytest.mark.parametrize("not_modified", [False, True])
def test_redirect_chain_with_permanent_hop_reports_final_url(chain, not_modified) -> None:
    # 恒久移転を 1 度でも経由したら、連鎖の最後の URL を移転先にする
    hops = {"/feed.xml": chain[0]}
    for (_, here), nxt in zip(chain, chain[1:], strict=False):
        hops[here] = nxt
    final_path = chain[-1][1]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in hops:
            status, to = hops[request.url.path]
            return httpx.Response(status, headers={"location": f"https://example.com{to}"})
        if request.url.path == final_path:
            if not_modified:
                return httpx.Response(304)
            return httpx.Response(200, content=FEED.encode())
        return httpx.Response(404)

    result = make_connector(handler).fetch(make_source(), {})
    assert result.moved_url == f"https://example.com{final_path}"


def test_temporary_redirect_is_not_moved() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/feed.xml":
            return httpx.Response(302, headers={"location": "https://example.com/tmp/feed.xml"})
        return httpx.Response(200, content=FEED.encode())

    assert make_connector(handler).fetch(make_source(), {}).moved_url is None
    ok = make_connector(lambda request: httpx.Response(200, content=FEED.encode()))
    assert ok.fetch(make_source(), {}).moved_url is None


def test_410_gone_is_fatal() -> None:
    connector = make_connector(lambda request: httpx.Response(410))
    with pytest.raises(FatalError, match="410"):
        connector.fetch(make_source(), {})


def test_403_is_fatal() -> None:
    connector = make_connector(lambda request: httpx.Response(403))
    with pytest.raises(FatalError, match="403"):
        connector.fetch(make_source(), {})


def test_404_is_fatal_not_retried() -> None:
    # 恒久 4xx はリトライ対象にしない分類(設計 02 §1 / 05 §8)
    connector = make_connector(lambda request: httpx.Response(404))
    with pytest.raises(FatalError, match="404"):
        connector.fetch(make_source(), {})


def test_garbage_body_is_retryable() -> None:
    connector = make_connector(lambda request: httpx.Response(200, content=b"\x00\x01 not xml"))
    with pytest.raises(RetryableError, match="パース失敗"):
        connector.fetch(make_source(), {})


def test_missing_url_config_is_fatal() -> None:
    connector = make_connector(lambda request: httpx.Response(200))
    with pytest.raises(FatalError, match="設定不備"):
        connector.fetch(make_source(config="{}"), {})


RELATIVE_ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Relative</title>
  <entry>
    <id>tag:example.com,2026:rel-1</id>
    <title>ルートからの相対</title>
    <link href="/posts/1"/>
  </entry>
  <entry>
    <id>tag:example.com,2026:rel-2</id>
    <title>フィードの場所からの相対</title>
    <link href="posts/2"/>
  </entry>
  <entry xml:base="https://other.example/base/">
    <id>tag:example.com,2026:rel-3</id>
    <title>xml:base が優先</title>
    <link href="posts/3"/>
  </entry>
  <entry>
    <id>tag:example.com,2026:rel-4</id>
    <title>前の entry の xml:base を引き継がない</title>
    <link href="/posts/4"/>
  </entry>
</feed>
"""

RELATIVE_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>Relative</title>
    <item>
      <title>相対リンクの記事</title>
      <link>/posts/5</link>
      <guid isPermaLink="false">post-5</guid>
    </item>
    <item>
      <title>絶対リンクの記事</title>
      <link>https://example.com/posts/6</link>
      <guid>https://example.com/posts/6</guid>
    </item>
  </channel>
</rss>
"""


def test_relative_atom_links_resolved_against_final_feed_url() -> None:
    # 基準はリダイレクト後の最終 URL。xml:base があればそちらを使う(GH#185)
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/feed.xml":
            return httpx.Response(302, headers={"location": "https://example.com/blog/atom.xml"})
        return httpx.Response(200, content=RELATIVE_ATOM.encode())

    result = make_connector(handler).fetch(make_source(), {})
    assert [item.url for item in result.items] == [
        "https://example.com/posts/1",
        "https://example.com/blog/posts/2",
        "https://other.example/base/posts/3",
        "https://example.com/posts/4",
    ]
    # URI でない id(tag:)は基準 URI で書き換わらない
    assert result.items[0].external_id == "tag:example.com,2026:rel-1"


def test_relative_rss_link_resolved_against_feed_url() -> None:
    connector = make_connector(lambda request: httpx.Response(200, content=RELATIVE_RSS.encode()))
    first, second = connector.fetch(make_source(), {}).items
    assert first.url == "https://example.com/posts/5"
    assert first.external_id == "post-5"  # isPermaLink="false" の guid は解決しない
    assert second.url == "https://example.com/posts/6"


def test_relative_link_keeps_charset_detection_without_xml_declaration() -> None:
    # 基準 URI を渡しても文字コードの推定を変えない(宣言の無い UTF-8 は UTF-8 のまま。GH#185)
    feed = """<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>tag:example.com,2026:ja</id>
    <title>日本語のパス</title>
    <link href="/記事/1"/>
  </entry>
</feed>
"""
    connector = make_connector(lambda request: httpx.Response(200, content=feed.encode()))
    (only,) = connector.fetch(make_source(), {}).items
    assert only.url == "https://example.com/記事/1"
    assert only.title == "日本語のパス"


def test_unresolvable_guid_falls_back_to_none() -> None:
    # 基準 URI で解決できない guid は feedparser が空文字にする。空文字のままだと
    # (source_id, external_id) の UNIQUE で別の記事と衝突するため None にする
    feed = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>t</title>
  <item><title>a</title><link>/posts/7</link><guid>https://example.com]/7</guid></item>
</channel></rss>
"""
    connector = make_connector(lambda request: httpx.Response(200, content=feed.encode()))
    (only,) = connector.fetch(make_source(), {}).items
    assert only.url == "https://example.com/posts/7"
    assert only.external_id is None
