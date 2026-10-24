"""X コネクタ(逆時系列ホームTL + type='x' の共有部。設計 02 §2 / ADR 0001)。

- 認証は OAuth 1.0a user context(ホームTLは app-only Bearer 不可。Day 0 probe で実証)。
  署名は oauthlib、送信は httpx(全コネクタの HTTP を httpx に統一)。X API は固定信頼
  ホストのため SSRF ガード(app.http)は通さない。署名・リトライは XApiSession に集約し、
  検索コネクタ(connectors/x_search.py)と共有する。
- 打ち切りは3条件(いずれも「古い側を捨てる」。見逃し防止は非目標。設計 02 §2):
  (a) next_token なし = since_id 到達 / TL 末尾、(b) daily_read_cap 到達、
  (c) rate limit 残 0(15分待ちはしない)。
- 2 ページ目以降の失敗(HTTP の失敗・通信のリトライ尽き)は、取得ぶんを返しつつ理由を
  `FetchResult.partial_error` に載せ、since_id は据え置く(GH#182。検索と同じ形 = GH#183)。
  取得ぶんを捨てると、その読み取りの費用も台帳に残らないため。2 ページ目以降の 401/403 は
  致命の失敗(FatalError)のまま送出し、それまでの読取数と費用を例外に添える(GH#178。collect が
  失敗の記録に残す。取得ぶんは保存せず since_id も据え置く)。
- RT は参照先(元ツイ)の id を URL に載せて dedup に乗せる(normalize_url が tweet URL を
  status id で `x.com/i/web/status/{id}` に統一するため、元ツイの直接ポストと重複排除される)。
  自分宛以外のリプライは除外(設計 02 §2)。
- api_reads(課金対象の読取件数)× 単価を FetchStats.estimated_cost_usd に載せる。
- daily_read_cap は JST の暦日の上限で、同じ日の実行で共有する(GH#178)。合成点が台帳
  (collect_runs.api_reads)の当日の合計を `x_reads_today` で引き、コネクタは上限から
  それを引いた残りだけを読む(fetch は DB に触らない契約のまま。設計 02 §1)。
- type='x' の sources 行はホームTL と検索の2種があり、sources.config の mode で識別する
  (NULL = home。設計 02 §2)。collect の「type → Connector 1:1」契約は XRouter が保つ。
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
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
from app.jst import jst_day_bounds_utc, today_jst
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
        reads_used_today: int = 0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._session = XApiSession(client=client, credentials=credentials, sleep=sleep)
        # 今回読める上限 = 日次の上限 − 同じ日(JST)の実行が読んだぶん(GH#178)
        self._read_budget = max(0, daily_read_cap - reads_used_today)
        self._unit_price = unit_price_usd

    def fetch(self, source_row: Source, cursor: dict) -> FetchResult:
        started = time.monotonic()
        me_id = self._get_me_id()
        tweets, users, reads, http_status, error = self._fetch_timeline(
            me_id, cursor.get("since_id")
        )
        items = _merge_same_original([
            item for tweet in tweets if (item := _to_raw_item(tweet, users, me_id)) is not None
        ])
        # 次回 since_id は今回取得した最大 tweet id(予算・rate limit で打ち切っても古い側の穴は
        # 追わない。設計 02 §2)。部分取得のあとの失敗では据え置く(GH#182。失敗で取れなかった
        # 古い側は方針で捨てたものではないので、翌日に旧 since_id から取り直して UNIQUE で吸収する)
        next_since = (
            str(max(int(t["id"]) for t in tweets))
            if tweets and error is None
            else cursor.get("since_id")
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
            partial_error=error,
            # 当日の枠が残っておらずタイムラインを読まなかった(GH#178)。復旧を確かめていないので
            # collect にソースの成否の状態を変えさせない(失敗の続きを成功で消さない)
            budget_exhausted=self._read_budget == 0,
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
    ) -> tuple[list[dict], dict[str, str], int, int | None, Exception | None]:
        """returns (tweets, users, reads, http_status, error)。

        error = 2 ページ目以降の HTTP の失敗・通信のリトライ尽き(GH#182)。1 ページ目の失敗は例外。
        """
        url = f"{API_BASE}/users/{me_id}/timelines/reverse_chronological"
        tweets: list[dict] = []
        users: dict[str, str] = {}
        reads = 0
        token: str | None = None
        http_status: int | None = None
        while reads < self._read_budget:
            params: dict[str, str | int] = {
                # cap を1件も超えないよう残枠を上限に(設計 02 §2 / ADR 0001 予算配分)
                "max_results": min(MAX_RESULTS, self._read_budget - reads),
                "tweet.fields": TWEET_FIELDS,
                "expansions": EXPANSIONS,
            }
            if since_id:
                params["since_id"] = since_id
            if token:
                params["pagination_token"] = token
            try:
                response = self._session.get(url, params)
            except RetryableError as e:
                # 通信のリトライ尽きも HTTP の失敗と同じに扱う(GH#182)。既取得分を例外で
                # 捨てると、その読み取りの費用も台帳に残らない
                if not tweets:
                    raise
                return tweets, users, reads, http_status, e
            http_status = response.status_code
            # 2 ページ目以降の 401/403 も致命の失敗のまま送出するが、それまでの読取数と費用を
            # 添えて collect に台帳へ残させる(GH#178。取得ぶんは保存せず since_id は据え置く。
            # 読取が台帳から漏れると、同じ日の再実行が日次の上限を超えて読む)
            raise_for_auth(
                response,
                "timeline",
                api_reads=reads,
                estimated_cost_usd=round(reads * self._unit_price, 6),
            )
            if response.status_code != 200:
                # 1ページ目から失敗(429/5xx/4xx)は「取得0件の成功」で隠さず失敗として返す
                # (consecutive_failures を上げ doctor #14 / watchdog に拾わせる)。
                # 既取得分があれば部分取得で打ち切り、取得ぶんは保存しつつソースの失敗として
                # 数えさせる(GH#182。検索と同じ形 = GH#183)
                error = status_error(response, url)
                if not tweets:
                    raise error
                return tweets, users, reads, http_status, error
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
        return tweets, users, reads, http_status, None


def raise_for_auth(
    response: httpx.Response, where: str, *, api_reads: int = 0, estimated_cost_usd: float = 0.0
) -> None:
    """401/403 は FatalError(致命)。部分取得のあとなら、それまでの読取数と費用を添える(GH#178)。"""
    if response.status_code in (401, 403):
        raise FatalError(
            f"X 認証失敗({where}): HTTP {response.status_code}(キー・権限を確認)",
            api_reads=api_reads,
            estimated_cost_usd=estimated_cost_usd,
        )


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
    daily_read_cap: int,
    unit_price_usd: float,
    env: dict[str, str],
    *,
    reads_used_today: int = 0,
) -> XConnector:
    """cli の合成点から呼ぶ(前提: has_x_credentials(env) が真)。

    reads_used_today = 同じ日のホームTL の読取数(`x_reads_today` の home。GH#178)。
    """
    return XConnector(
        client=build_x_client(),
        credentials=credentials_from_env(env),
        daily_read_cap=daily_read_cap,
        unit_price_usd=unit_price_usd,
        reads_used_today=reads_used_today,
    )


def x_reads_today(conn: sqlite3.Connection, day: str | None = None) -> dict[str, int]:
    """JST の暦日 day(既定 = 今日)に collect が台帳へ記録した X の読取数を mode ごとに返す。

    日次の読取上限は 1 回の実行の上限でなく暦日の上限(ADR 0001 の合算日次ハード上限)。
    同じ日の再実行(失敗のあとの手動の `intel run`・単独の `intel collect`)が上限を超えて
    読まないよう、合成点がここで当日の合計を出し、コネクタに残りだけを読ませる(GH#178)。
    日付は collect_runs.started_at(UTC)を JST の暦日に直して決める(dig の日次上限と同じ
    暦日)。部分取得のあとの失敗(status='error')の読取も数える(費用は掛かっている)。
    mode の判定は x_source_mode と同じ(config NULL = home)。mode が不正な行・bookmark 行は
    fetch の前に失敗するので読取は常に 0 で、どちらに数えても合計は変わらない。
    """
    start, end = jst_day_bounds_utc(day or today_jst())
    rows = conn.execute(
        """
        SELECT CASE WHEN json_extract(sources.config, '$.mode') = 'search'
                    THEN 'search' ELSE 'home' END AS mode,
               sum(collect_runs.api_reads) AS reads
        FROM collect_runs
        JOIN sources ON sources.id = collect_runs.source_id
        WHERE sources.type = 'x'
          AND collect_runs.started_at >= ? AND collect_runs.started_at < ?
        GROUP BY mode
        """,
        (start, end),
    ).fetchall()
    reads = {"home": 0, "search": 0}
    reads.update({row["mode"]: row["reads"] for row in rows})
    return reads


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
        # self_retweet = 自分の RT(本人が既に見たもの)。Stage 1 の規則 3 が reply と合わせて除外する
        # (設計 03 §2)。他人の RT は元ツイに正規化して残す
        meta={
            "retweet": retweeted_id is not None,
            "reply": in_reply_to is not None,
            "self_retweet": retweeted_id is not None and tweet.get("author_id") == me_id,
        },
    )


def _merge_same_original(items: list[RawItem]) -> list[RawItem]:
    """同じ元ツイートを指す行(RT の正規化で external_id が重なる)を 1 行にまとめる。

    先に出た行(新しい側)を残し、どれかが自分の RT なら self_retweet を立てる。collect の
    重複排除は先着の meta だけを残すので、ここでまとめないと規則 3 の除外が並び順で変わる
    (GH#133)。前日までの取得や検索ソースで先に入った行は書き換えない(設計 02 §2)。
    """
    merged: dict[str, RawItem] = {}
    for item in items:
        kept = merged.setdefault(item.external_id, item)
        if kept is not item and item.meta.get("self_retweet"):
            merged[item.external_id] = replace(kept, meta={**kept.meta, "self_retweet": True})
    return list(merged.values())


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
