"""X コネクタ(逆時系列ホームTL + type='x' の共有部。設計 02 §2 / ADR 0001)。

- 認証は OAuth 1.0a user context(ホームTLは app-only Bearer 不可。Day 0 probe で実証)。
  署名は oauthlib、送信は httpx(全コネクタの HTTP を httpx に統一)。X API は固定信頼
  ホストのため SSRF ガード(app.http)は通さない。署名・リトライは XApiSession に集約し、
  検索コネクタ(connectors/x_search.py)と共有する。
- 打ち切りは3条件(いずれも「古い側を捨てる」。見逃し防止は非目標。設計 02 §2):
  (a) next_token なし = since_id 到達 / TL 末尾、(b) daily_read_cap 到達、
  (c) rate limit 残 0(15分待ちはしない)。
- RT は参照先(元ツイ)の id を URL に載せて dedup に乗せる(normalize_url が tweet URL を
  status id で `x.com/i/web/status/{id}` に統一するため、元ツイの直接ポストと重複排除される)。
  自分宛以外のリプライは除外(設計 02 §2)。
- api_reads(課金対象の読取件数)× 単価を FetchStats.estimated_cost_usd に載せる。
- type='x' の sources 行はホームTL と検索の2種があり、sources.config の mode で識別する
  (NULL = home。設計 02 §2)。collect の「type → Connector 1:1」契約は XRouter が保つ。
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

import httpx
from oauthlib.oauth1 import Client as OAuth1Client

from app.connectors.base import (
    Connector,
    FatalError,
    FetchResult,
    FetchStats,
    RawItem,
    RetryableError,
)
from app.http import BACKOFF_S, MAX_RETRIES  # 共通リトライ規約(設計 05 §8)
from app.models import Source

API_BASE = "https://api.twitter.com/2"
MAX_RESULTS = 100  # 1ページ上限(設計 02 §2)
# note_tweet = 長文ポストの全文(GH#94)。text は280字で切り詰められるため、記事系
# コンテンツの中身を analyze に渡すには必須。同一読取のフィールド追加で課金は不変
TWEET_FIELDS = "created_at,author_id,referenced_tweets,in_reply_to_user_id,note_tweet"
EXPANSIONS = "author_id"
TIMEOUT_S = 30
_ENV_KEYS = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")


@dataclass(frozen=True)
class XCredentials:
    api_key: str
    api_secret: str
    access_token: str
    access_token_secret: str


class XApiSession:
    """OAuth 1.0a 署名つき GET(共通リトライ規約。設計 05 §8)。

    ホームTL・検索の両コネクタが共有するトランスポート。
    429 はリトライせず即返す(rate limit は待たず打ち切る = 設計 02 §2)。
    署名は試行ごとに引き直す(OAuth nonce/timestamp を新規にして replay 拒否を避ける)。
    """

    def __init__(
        self,
        *,
        client: httpx.Client,
        credentials: XCredentials,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._oauth = OAuth1Client(
            credentials.api_key,
            client_secret=credentials.api_secret,
            resource_owner_key=credentials.access_token,
            resource_owner_secret=credentials.access_token_secret,
        )
        self._sleep = sleep

    def get(self, url: str, params: dict[str, str | int]) -> httpx.Response:
        full_url = f"{url}?{urlencode(params)}" if params else url
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            signed_url, headers, _ = self._oauth.sign(full_url, http_method="GET")
            try:
                response = self._client.get(signed_url, headers=headers)
            except httpx.HTTPError as e:
                last_error = RetryableError(f"X 接続失敗: {type(e).__name__}")
                self._backoff(attempt)
                continue
            if response.status_code >= 500 and attempt < MAX_RETRIES:
                self._backoff(attempt)
                continue
            return response
        raise last_error  # transport error のリトライ尽き(直前 continue で必ず設定済み)

    def _backoff(self, attempt: int) -> None:
        if attempt < MAX_RETRIES:
            self._sleep(BACKOFF_S[attempt] + random.random())


class XConnector:
    source_type = "x"

    def __init__(
        self,
        *,
        client: httpx.Client,
        credentials: XCredentials,
        daily_read_cap: int,
        unit_price_usd: float,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._session = XApiSession(client=client, credentials=credentials, sleep=sleep)
        self._daily_read_cap = daily_read_cap
        self._unit_price = unit_price_usd

    def fetch(self, source_row: Source, cursor: dict) -> FetchResult:
        started = time.monotonic()
        me_id = self._get_me_id()
        tweets, users, reads, http_status = self._fetch_timeline(me_id, cursor.get("since_id"))
        items = [
            item for tweet in tweets if (item := _to_raw_item(tweet, users, me_id)) is not None
        ]
        # 次回 since_id は今回取得した最大 tweet id(打ち切っても古い側の穴は追わない。設計 02 §2)
        next_since = (
            str(max(int(t["id"]) for t in tweets)) if tweets else cursor.get("since_id")
        )
        next_cursor = {"since_id": next_since} if next_since else {}
        return FetchResult(
            items=items,
            next_cursor=next_cursor,
            stats=FetchStats(
                items_fetched=len(items),
                api_reads=reads,
                estimated_cost_usd=round(reads * self._unit_price, 6),
                http_status=http_status,
                duration_ms=int((time.monotonic() - started) * 1000),
            ),
        )

    def _get_me_id(self) -> str:
        """逆時系列ホームTL のパスに要る認証ユーザー id(Posts Read には数えない)。"""
        response = self._session.get(f"{API_BASE}/users/me", {})
        raise_for_auth(response, "users/me")
        if response.status_code != 200:
            raise RetryableError(f"users/me が HTTP {response.status_code}")
        data = response.json().get("data")
        if not data or "id" not in data:
            raise RetryableError("users/me の応答に id がない")
        return data["id"]

    def _fetch_timeline(
        self, me_id: str, since_id: str | None
    ) -> tuple[list[dict], dict[str, str], int, int | None]:
        url = f"{API_BASE}/users/{me_id}/timelines/reverse_chronological"
        tweets: list[dict] = []
        users: dict[str, str] = {}
        reads = 0
        token: str | None = None
        http_status: int | None = None
        while reads < self._daily_read_cap:
            params: dict[str, str | int] = {
                # cap を1件も超えないよう残枠を上限に(設計 02 §2 / ADR 0001 予算配分)
                "max_results": min(MAX_RESULTS, self._daily_read_cap - reads),
                "tweet.fields": TWEET_FIELDS,
                "expansions": EXPANSIONS,
            }
            if since_id:
                params["since_id"] = since_id
            if token:
                params["pagination_token"] = token
            response = self._session.get(url, params)
            http_status = response.status_code
            raise_for_auth(response, "timeline")
            if response.status_code != 200:
                # 1ページ目から失敗(429/5xx/4xx)は「取得0件の成功」で隠さず失敗として返す
                # (consecutive_failures を上げ doctor #14 / watchdog に拾わせる)。
                # 既取得分があれば部分取得で打ち切り(古い側の欠落は許容。設計 02 §2)
                if not tweets:
                    raise status_error(response, url)
                break
            body = response.json()
            page = body.get("data", [])
            for user in body.get("includes", {}).get("users", []):
                users[user["id"]] = user.get("username")
            tweets.extend(page)
            reads += len(page)
            if not page:
                break  # 空ページ = 新規なし。next_token 循環によるループも塞ぐ
            if rate_limit_exhausted(response):
                break  # 残 0 → 打ち切り(15分待ちはしない。設計 02 §2)
            token = body.get("meta", {}).get("next_token")
            if not token:
                break  # since_id 到達 / TL 末尾
        return tweets, users, reads, http_status


def raise_for_auth(response: httpx.Response, where: str) -> None:
    if response.status_code in (401, 403):
        raise FatalError(f"X 認証失敗({where}): HTTP {response.status_code}(キー・権限を確認)")


def status_error(response: httpx.Response, url: str) -> Exception:
    # 429(rate limit)・5xx は一時的 → リトライ対象。他の 4xx は恒久
    if response.status_code == 429 or response.status_code >= 500:
        return RetryableError(f"X が HTTP {response.status_code}: {url}")
    return FatalError(f"X が HTTP {response.status_code}: {url}")


def has_x_credentials(env: dict[str, str]) -> bool:
    """4キーが揃っているか。合成点はこれで X 配線の要否を判定する
    (欠落は doctor #5 が WARN。collect には X ソースを載せず RSS を止めない)。"""
    return all(env.get(k) for k in _ENV_KEYS)


def build_x_connector(
    daily_read_cap: int, unit_price_usd: float, env: dict[str, str]
) -> XConnector:
    """cli の合成点から呼ぶ(前提: has_x_credentials(env) が真)。"""
    return XConnector(
        client=build_x_client(),
        credentials=credentials_from_env(env),
        daily_read_cap=daily_read_cap,
        unit_price_usd=unit_price_usd,
    )


def build_x_client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(TIMEOUT_S),
        headers={"User-Agent": "intel/0.1 (personal digest bot)"},
    )


def credentials_from_env(env: dict[str, str]) -> XCredentials:
    return XCredentials(
        api_key=env["X_API_KEY"],
        api_secret=env["X_API_SECRET"],
        access_token=env["X_ACCESS_TOKEN"],
        access_token_secret=env["X_ACCESS_TOKEN_SECRET"],
    )


def rate_limit_exhausted(response: httpx.Response) -> bool:
    remaining = response.headers.get("x-rate-limit-remaining")
    return remaining is not None and remaining.isdigit() and int(remaining) == 0


def _to_raw_item(tweet: dict, users: dict[str, str], me_id: str) -> RawItem | None:
    in_reply_to = tweet.get("in_reply_to_user_id")
    if in_reply_to is not None and in_reply_to != me_id:
        return None  # 自分宛以外のリプライは除外(設計 02 §2)

    referenced = tweet.get("referenced_tweets") or []
    retweeted_id = next((r["id"] for r in referenced if r.get("type") == "retweeted"), None)
    effective_id = retweeted_id or tweet["id"]  # RT は元ツイに寄せて dedup(設計 02 §2)

    return RawItem(
        external_id=effective_id,
        url=f"https://x.com/i/web/status/{effective_id}",  # normalize_url が status id で統一
        title=None,  # tweet に title はない(本文は raw_content)
        author=users.get(tweet.get("author_id")),
        published_at=parse_created_at(tweet.get("created_at")),
        raw_content=tweet_full_text(tweet),
        meta={"retweet": retweeted_id is not None, "reply": in_reply_to is not None},
    )


def tweet_full_text(tweet: dict) -> str | None:
    """本文は note_tweet(長文ポストの全文)優先・無ければ従来の text(GH#94)。

    RT された長文ポストの元ツイ全文は referenced_tweets.id expansion が要るため対象外
    (Issue #94 の境界)。検索コネクタと共用。
    """
    note = tweet.get("note_tweet")
    if isinstance(note, dict) and note.get("text"):
        return note["text"]
    return tweet.get("text")


def parse_created_at(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)  # X は "2026-07-10T09:08:43.000Z"(Z 対応は 3.11+)


def x_source_mode(source: Source) -> str:
    """type='x' 行の役割判定(設計 02 §2)。config NULL = home(ホームTL 行は config 不要)。

    config がある場合は mode 必須・不明キー/値は fail fast(タイポで home に化けて
    ホームTL を二重取得・二重課金する事故を防ぐ)。
    """
    if source.config is None:
        return "home"
    parsed = json.loads(source.config)  # DDL の json_valid CHECK 済み(設計 01 §3)
    if not isinstance(parsed, dict):
        # json_valid は null / 数値 / 配列も通す。分類可能な FatalError に揃える
        # (doctor #5 の「mode 不正行を WARN」の契約を TypeError で破らない)
        raise FatalError(f"X ソース '{source.name}' の config が JSON オブジェクトでない")
    unknown = sorted(set(parsed) - {"mode"})
    if unknown:
        raise FatalError(f"X ソース '{source.name}' の config に不明なキー: {', '.join(unknown)}")
    mode = parsed.get("mode")
    if mode not in ("home", "search", "bookmark"):
        raise FatalError(
            f"X ソース '{source.name}' の config.mode が不正: {mode!r}"
            "(home / search / bookmark のみ)"
        )
    return mode


class XRouter:
    """type='x' の sources 行(home / search)を mode で振り分ける(設計 02 §2)。

    collect の「type → Connector 1:1」契約(設計 02 §1)を保ったまま X 内の複線化を
    吸収する。search 未配線(slots 未設定)で mode=search の行が来たら fail fast
    (ソース単位で隔離され doctor #5 の整合検査にも載る)。
    """

    source_type = "x"

    def __init__(self, *, home: Connector, search: Connector | None) -> None:
        self._home = home
        self._search = search

    def fetch(self, source_row: Source, cursor: dict) -> FetchResult:
        mode = x_source_mode(source_row)
        if mode == "bookmark":
            # item 化は x_signals 段の担当(GH#99)。この行は items の帰属専用で
            # enabled=0 が正。誤って enabled 化されても黙って home に化けず fail fast
            raise FatalError(
                f"X ソース '{source_row.name}' は mode=bookmark(collect 対象外。"
                "enabled=0 に戻すこと)"
            )
        if mode == "search":
            if self._search is None:
                raise FatalError(
                    f"X ソース '{source_row.name}' は mode=search だが"
                    " [x_search].slots が未設定(config.toml を確認)"
                )
            return self._search.fetch(source_row, cursor)
        return self._home.fetch(source_row, cursor)
