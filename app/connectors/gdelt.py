"""GDELT の DOC 2.0 API で報道を探す(出来事の調査。GH#415・ADR 0006)。

公開の API(無料・キー不要)。記事の一覧(ArtList)を JSON で受け、記事の URL と題を返す。読み取り
専用で DB に触れない。HTTP は共通の HttpClient(SSRF の守り・リトライ・受信量の上限)を通す。

- 間隔の制限: 5 秒に 1 回まで(超えると 429)。429 の後の短い間隔の試し直しも数えられて、しばらく
  止められる(2026-11-14 の実測。90 秒空けても 429 が続いた)ので、試し直さずに 1 回だけ送る
- 返す URL は http/https だけで、正規化(設計 01 §4)を通したもの。題は 1 行にして長さを切る
- 応答は外部由来の未検証データ。呼び出し側は材料として nonce のフェンスで囲んで LLM に渡す
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

from app.connectors.base import FatalError, RetryableError
from app.http import HttpClient
from app.normalize import UrlError, normalize_url

DOC_API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
MAX_RECORDS = 25  # 1 回の検索で受け取る記事の上限(配信記事の重なりを除いた後に数本を選ぶ)
_TITLE_MAX_CHARS = 300


class GdeltError(Exception):
    """検索の失敗(HTTP の失敗・形の違う応答)。呼び出し側は報道の検索なしで続ける。"""


@dataclass(frozen=True)
class GdeltArticle:
    url: str  # 正規化した URL(http/https)
    title: str
    domain: str
    language: str
    seen_at: str  # GDELT が見つけた時刻(YYYYMMDDTHHMMSSZ。そのまま)


def search_articles(
    http: HttpClient,
    query: str,
    *,
    start: datetime,
    end: datetime,
    max_records: int = MAX_RECORDS,
) -> list[GdeltArticle]:
    """語に合う記事を、GDELT の関連の順(hybridrel)で返す。見つからなければ空。

    start・end は UTC の datetime(GDELT の検索の範囲。直近 3 か月の中であること)。
    """
    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": str(max_records),
        "sort": "hybridrel",
        "startdatetime": start.strftime("%Y%m%d%H%M%S"),
        "enddatetime": end.strftime("%Y%m%d%H%M%S"),
    }
    try:
        fetched = http.get(f"{DOC_API_URL}?{urlencode(params)}", retries=0)
    except (RetryableError, FatalError) as e:
        raise GdeltError(f"{type(e).__name__}") from e
    status = fetched.response.status_code
    if status != 200:
        raise GdeltError(f"HTTP {status}")
    text = fetched.response.text.strip()
    if not text:
        return []
    try:
        # GDELT の JSON は題の中の制御文字をそのまま含むことがある(strict=False で読む)
        data = json.loads(text, strict=False)
    except ValueError:
        # 語が短すぎるなどの誤りは、JSON ではなく平文の説明で返る
        raise GdeltError("応答が JSON でない") from None
    if not isinstance(data, dict):
        raise GdeltError("応答の形が違う")
    articles = data.get("articles", [])
    if not isinstance(articles, list):
        raise GdeltError("応答の形が違う")
    return [a for a in (_article(raw) for raw in articles) if a is not None]


def _article(raw: object) -> GdeltArticle | None:
    if not isinstance(raw, dict):
        return None
    url, title = raw.get("url"), raw.get("title")
    if not isinstance(url, str) or not isinstance(title, str):
        return None
    try:
        canonical = normalize_url(url)
    except UrlError:  # http/https でない・分解できない URL は材料にしない
        return None
    flat = " ".join(title.split())[:_TITLE_MAX_CHARS]
    if not flat:
        return None
    return GdeltArticle(
        url=canonical,
        title=flat,
        domain=str(raw.get("domain") or ""),
        language=str(raw.get("language") or ""),
        seen_at=str(raw.get("seendate") or ""),
    )
