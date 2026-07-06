"""RSS コネクタ(設計 02 §3)。

- 取得は共通 HTTP クライアント(feedparser 内蔵 HTTP は使わない。timeout・UA・
  エラー分類・条件付き GET を全コネクタで統一するため)
- 条件付き GET: cursor = {"etag", "last_modified"} を If-None-Match / If-Modified-Since に
  載せ、304 は正常 no-op(items 0 件・cursor 維持)
- 410 Gone は即 FatalError。301 経由は meta 経由で警告表示のみ(URL 書き換えは人が行う)
"""

from __future__ import annotations

import json
import logging
import time
from calendar import timegm
from datetime import UTC, datetime

import feedparser

from app.connectors.base import FatalError, FetchResult, FetchStats, RawItem, RetryableError
from app.http import HttpClient
from app.models import Source


class RssConnector:
    source_type = "rss"

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def fetch(self, source_row: Source, cursor: dict) -> FetchResult:
        started = time.monotonic()
        config = json.loads(source_row.config) if source_row.config else {}
        feed_url = config.get("url")
        if not feed_url:
            raise FatalError(f"sources.config に url がない: {source_row.name}(設定不備)")

        headers = {}
        if cursor.get("etag"):
            headers["If-None-Match"] = cursor["etag"]
        if cursor.get("last_modified"):
            headers["If-Modified-Since"] = cursor["last_modified"]

        fetched = self._http.get(feed_url, headers=headers)
        response = fetched.response
        duration_ms = int((time.monotonic() - started) * 1000)

        if response.status_code == 304:
            stats = FetchStats(
                items_fetched=0, api_reads=0, http_status=304, duration_ms=duration_ms
            )
            return FetchResult(items=[], next_cursor=cursor, stats=stats)
        if response.status_code == 410:
            raise FatalError(f"410 Gone(フィード恒久消滅): {feed_url}")
        if response.status_code in (401, 403):
            raise FatalError(f"HTTP {response.status_code}(認可エラー): {feed_url}")
        if response.status_code >= 400:
            raise RetryableError(f"HTTP {response.status_code}: {feed_url}")

        feed = feedparser.parse(response.content)
        if feed.bozo and not feed.entries:
            raise RetryableError(f"フィードのパース失敗: {feed_url}({feed.bozo_exception})")

        if fetched.permanently_moved:
            # 警告表示のみ。sources.config の URL 書き換えは人が行う(設計 02 §3)
            logging.getLogger("intel").warning(
                "フィードが恒久移転(301)。sources.config の URL 更新を検討",
                extra={"source_id": source_row.id, "extra": {"final_url": fetched.final_url}},
            )

        items = [item for entry in feed.entries if (item := _to_raw_item(entry)) is not None]
        next_cursor = {
            "etag": response.headers.get("etag"),
            "last_modified": response.headers.get("last-modified"),
        }
        return FetchResult(
            items=items,
            next_cursor=next_cursor,
            stats=FetchStats(
                items_fetched=len(items),
                api_reads=0,
                http_status=response.status_code,
                duration_ms=duration_ms,
            ),
        )


def _to_raw_item(entry: feedparser.util.FeedParserDict) -> RawItem | None:
    link = entry.get("link")
    if not link:
        return None  # URL のない entry は取り込めない(items.url NOT NULL)

    raw_content = None
    if entry.get("content"):
        raw_content = entry["content"][0].get("value")
    if not raw_content:
        raw_content = entry.get("summary")

    published_at = None
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        published_at = datetime.fromtimestamp(timegm(parsed), tz=UTC)

    return RawItem(
        external_id=entry.get("id"),  # 無ければ None(collect が正規化 URL hash で補完)
        url=link,
        title=entry.get("title"),
        author=entry.get("author"),
        published_at=published_at,
        raw_content=raw_content,
        meta={},
    )
