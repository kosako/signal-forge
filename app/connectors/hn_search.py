"""Hacker News の検索で、今の重点の語に合う投稿を拾う(GH#433 の 5 本目・設計 02 §6)。

Algolia の HN Search API(https://hn.algolia.com/api。公式の無料の API で、認証は要らない)を
読むだけで、DB に触れない(設計 02 §1)。取り込みは app/focus_collect.py が collect の共通の
保存の道で行う。HTTP は共通の HttpClient(SSRF の守り・リトライ・受信量の上限)を通す。

- 語は今の重点の検索の語(LLM が興味シグナルの外部の本文から作った文字列)。呼び出し側が
  `app.focus.search_terms` で検査した語を、query の値として URL の符号化だけで渡す。演算子や
  引用は付けない(期間と点数の条件は、コードが numericFilters に付ける)
- 対象は投稿(tags=story)。作られたのが直近 window_hours 時間以内で、点数が min_points 以上のもの
- 語を探すのは見出しだけ(restrictSearchableAttributes=title)。本文まで探すと、長い本文の投稿
  (Launch HN など)が語の一部の一致で多くの語に当たる
- 記事の URL が無い投稿(Ask HN など)は、HN の投稿のページを URL にし、投稿の本文を同梱する。
  URL が http/https として読めない投稿、点数・時刻の形が合わない投稿は、その 1 件だけを捨てる
- 失敗の文には検索の語を入れない(共通 HTTP クライアントの例外の文は URL を含むので、種類だけの
  文にし直す)。ログと台帳に語を残さないため
- 応答は外部由来の未検証データ。見出し・URL・本文は、RSS の記事と同じく collect・抽出・分析の
  段が扱う
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit

from app.connectors.base import FatalError, FetchResult, FetchStats, RawItem, RetryableError
from app.http import HttpClient
from app.models import Source

SEARCH_URL = "https://hn.algolia.com/api/v1/search"
ITEM_URL = "https://news.ycombinator.com/item?id={id}"
HITS_PER_QUERY = 20  # 1 つの語で受け取る投稿の上限(関連の順)
FUTURE_SLACK_S = 3600  # 作られた時刻がこれより先の投稿は捨てる(時計のずれの余裕。応答は未検証)
_TITLE_MAX_CHARS = 300


class HnFocusConnector:
    """今の重点の語ごとに HN を検索し、投稿を点数の高い順に返す。

    同じ投稿が複数の語に当たったら 1 つにする。並びは点数の高い順(同じなら新しい順、次に id)。
    collect はこの並びのまま、取り込み済みを除いて上限まで保存する(app/collect.py の
    collect_ranked)。カーソルは使わない(取り込み済みは UNIQUE で吸収する)。
    """

    source_type = "rss"

    def __init__(
        self,
        http: HttpClient,
        queries: Sequence[str],
        *,
        min_points: int,
        window_hours: int,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._http = http
        self._queries = list(queries)
        self._min_points = min_points
        self._window = timedelta(hours=window_hours)
        self._now = now

    def fetch(self, source_row: Source, cursor: dict) -> FetchResult:
        started = time.monotonic()
        now = self._now()
        since = int((now - self._window).timestamp())
        until = int(now.timestamp()) + FUTURE_SLACK_S
        found: dict[str, tuple[int, int, RawItem]] = {}  # 投稿の id → (点数, 作られた時刻, 記事)
        errors: list[Exception] = []
        status: int | None = None
        for query in self._queries:
            try:
                hits, status = self._search(query, since)
            except (RetryableError, FatalError) as e:
                errors.append(e)
                continue
            for hit in hits:
                # 条件は API に渡しているが、応答は未検証なので確かめ直す。形の合わない投稿は
                # その 1 件だけを捨てる(ほかの投稿は残す)
                parsed = _parse_hit(
                    hit, query, min_points=self._min_points, since=since, until=until
                )
                if parsed is None:
                    continue
                story_id, points, created, item = parsed
                if story_id not in found or points > found[story_id][0]:
                    found[story_id] = (points, created, item)
        if errors and len(errors) == len(self._queries):
            raise errors[0]  # どの語も読めなかった。ソースの失敗として記録する
        ordered = sorted(found.items(), key=lambda pair: (-pair[1][0], -pair[1][1], pair[0]))
        items = [item for _, (_, _, item) in ordered]
        return FetchResult(
            items=items,
            next_cursor={},
            stats=FetchStats(
                items_fetched=len(items),
                api_reads=0,
                estimated_cost_usd=0.0,
                http_status=status,
                duration_ms=int((time.monotonic() - started) * 1000),
            ),
            # 一部の語だけ読めなかった。読めた分は保存し、ソースの失敗として数える
            partial_error=errors[0] if errors else None,
        )

    def _search(self, query: str, since: int) -> tuple[list, int]:
        params = {
            "query": query,
            "tags": "story",
            "restrictSearchableAttributes": "title",
            "numericFilters": f"created_at_i>{since},points>={self._min_points}",
            "hitsPerPage": HITS_PER_QUERY,
        }
        try:
            fetched = self._http.get(f"{SEARCH_URL}?{urlencode(params)}")
        except RetryableError as e:
            # 共通 HTTP クライアントの例外の文は URL(= 検索の語)を含む。語をログと台帳
            # (collect_runs・sources の last_error)に残さないよう、種類だけの文にし直す
            raise RetryableError(f"HN の検索に失敗: {type(e).__name__}") from None
        except FatalError as e:
            raise FatalError(f"HN の検索に失敗: {type(e).__name__}") from None
        status = fetched.response.status_code
        if status == 429 or status >= 500:
            raise RetryableError(f"HN の検索が HTTP {status}")
        if status != 200:
            raise FatalError(f"HN の検索が HTTP {status}")
        try:
            payload = fetched.response.json()
        except ValueError:
            raise RetryableError("HN の検索の応答を JSON として読めない") from None
        hits = payload.get("hits") if isinstance(payload, dict) else None
        if not isinstance(hits, list):
            raise RetryableError("HN の検索の応答に hits が無い")
        return hits, status


def _parse_hit(
    hit: object, query: str, *, min_points: int, since: int, until: int
) -> tuple[str, int, int, RawItem] | None:
    """1 件の投稿を (id, 点数, 作られた時刻, 記事) にする。

    形・値が合わない投稿は None で、捨てる(点数が下限より低い、作られた時刻が期間の外、
    記事の URL が http/https として読めない、など)。例外は外へ出さない。
    """
    if not isinstance(hit, dict):
        return None
    story_id = hit.get("objectID")
    title = hit.get("title")
    points = hit.get("points")
    created = hit.get("created_at_i")
    if not (isinstance(story_id, str) and story_id.isascii() and story_id.isdigit()):
        return None
    if not isinstance(title, str) or not (flat := " ".join(title.split())[:_TITLE_MAX_CHARS]):
        return None
    if isinstance(points, bool) or not isinstance(points, int) or points < min_points:
        return None
    # 期間の内側だけを受ける(日時に変換できない値を、変換の前に除く)
    if isinstance(created, bool) or not isinstance(created, int) or not since <= created <= until:
        return None
    item_url = ITEM_URL.format(id=story_id)
    url = hit.get("url")
    text = hit.get("story_text")
    if url is None or (isinstance(url, str) and not url.strip()):
        # 記事の URL の無い投稿(Ask HN など)は、HN の投稿のページにし、投稿の本文を同梱する
        url, body = item_url, text if isinstance(text, str) and text.strip() else None
    elif isinstance(url, str) and _is_web_url(url):
        url, body = url.strip(), None
    else:
        return None
    author = hit.get("author")
    comments = hit.get("num_comments")
    item = RawItem(
        external_id=item_url,
        url=url,
        title=flat,
        author=author if isinstance(author, str) else None,
        published_at=datetime.fromtimestamp(created, tz=UTC),
        raw_content=body,
        meta={
            "hn_points": points,
            "hn_comments": comments if isinstance(comments, int) else None,
            "focus_query": query,
        },
    )
    return story_id, points, created, item


def _is_web_url(value: str) -> bool:
    try:
        parts = urlsplit(value.strip())
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:  # 分解できない URL(閉じていない [ など)
        return False
