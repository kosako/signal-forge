"""Google ニュースの RSS の検索で、報道の見出しと媒体名を拾う。

出来事の調査の予備(GH#415・ADR 0006)。GDELT が断るか、使える報道が見つからないときにだけ
使う。読み取り専用で DB に触れない。HTTP は共通の HttpClient(SSRF の守り・リトライ・受信量の
上限)を通す。

- 日本語版(hl=ja・gl=JP)。期間は検索の語に after: / before: を付けて絞る(演算子はコードが付ける
  この 2 つだけ。呼び出し側の語は検査済み)
- 記事の URL は Google ニュースの中の符号の URL で、本文は取れない(押すとブラウザで記事に移る)。
  材料は見出しと媒体名だけにする
- 見出しの末尾の「 - 媒体名」は外す。媒体の host は <source url> から取る(表示だけに使う)
- 応答は外部由来の未検証データ。呼び出し側は材料として nonce のフェンスで囲んで LLM に渡す
"""

from __future__ import annotations

from calendar import timegm
from dataclasses import dataclass
from datetime import UTC, date, datetime
from urllib.parse import urlencode

import feedparser

from app.connectors.base import FatalError, RetryableError
from app.http import HttpClient
from app.models import link_host
from app.normalize import UrlError, normalize_url

SEARCH_URL = "https://news.google.com/rss/search"
MAX_ITEMS = 25  # 受け取った見出しのうち、選ぶ前に見る上限(関連の順)
_TITLE_MAX_CHARS = 300


class GoogleNewsError(Exception):
    """検索の失敗(HTTP の失敗・読めない応答)。呼び出し側は報道の検索なしで続ける。"""


@dataclass(frozen=True)
class NewsHeadline:
    url: str  # Google ニュースの記事の URL(正規化した http/https)
    title: str  # 末尾の「 - 媒体名」を外した見出し
    outlet: str  # 媒体名
    outlet_host: str  # 媒体のサイトの host(<source url> から。取れなければ空)
    published_at: str | None  # ISO 8601 の UTC


def search_headlines(
    http: HttpClient,
    query: str,
    *,
    after: date,
    before: date,
    max_items: int = MAX_ITEMS,
) -> list[NewsHeadline]:
    """語に合う報道の見出しを、Google ニュースの関連の順で返す。見つからなければ空。"""
    params = {
        "q": f"{query} after:{after.isoformat()} before:{before.isoformat()}",
        "hl": "ja",
        "gl": "JP",
        "ceid": "JP:ja",
    }
    try:
        fetched = http.get(f"{SEARCH_URL}?{urlencode(params)}")
    except (RetryableError, FatalError) as e:
        raise GoogleNewsError(f"{type(e).__name__}") from e
    status = fetched.response.status_code
    if status != 200:
        raise GoogleNewsError(f"HTTP {status}")
    feed = feedparser.parse(
        fetched.response.content, response_headers={"content-type": "application/xml"}
    )
    if feed.bozo and not feed.entries:
        raise GoogleNewsError("RSS を読めない")
    headlines = (_headline(entry) for entry in feed.entries[:max_items])
    return [h for h in headlines if h is not None]


def _headline(entry: feedparser.util.FeedParserDict) -> NewsHeadline | None:
    link, title = entry.get("link"), entry.get("title")
    if not isinstance(link, str) or not isinstance(title, str):
        return None
    try:
        url = normalize_url(link)
    except UrlError:
        return None
    source = entry.get("source") or {}
    outlet = " ".join(str(source.get("title") or "").split())
    flat = " ".join(title.split())
    suffix = f"- {outlet}"
    if outlet and (flat == suffix or flat.endswith(f" {suffix}")):
        flat = flat[: -len(suffix)].rstrip()
    flat = flat[:_TITLE_MAX_CHARS]
    if not flat:
        return None
    href = source.get("href")
    parsed = entry.get("published_parsed")
    published = (
        datetime.fromtimestamp(timegm(parsed), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if parsed
        else None
    )
    return NewsHeadline(
        url=url,
        title=flat,
        outlet=outlet,
        outlet_host=link_host(href) if isinstance(href, str) else "",
        published_at=published,
    )
