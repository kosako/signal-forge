"""X 検索コネクタ(`GET /2/tweets/search/recent`・3スロット固定。設計 02 §2 / ADR 0001)。

- スロット(検索クエリ)は config.toml の `[x_search].slots`(最大3・入れ替え式)。各クエリに
  `-is:retweet -is:reply` を自動付加し、RT・リプライを課金前にサーバ側で除外する
  (ホームTL の「RT は元ツイに正規化・他人宛リプライ除外」と方針整合。取ってから捨てない)。
- 予算(daily_read_cap)はスロット数で均等割(端数は先頭から)。スロット間の再配分はしない
  (1つの多産クエリが他スロットを飢えさせない)。残枠が max_results 下限(10)を切ったら
  そのスロットは打ち切る(cap はハード上限。超過方向に丸めない)。
- カーソルは全スロット横断の最大 tweet id を `{"since_id": ...}` に(snowflake id は時刻単調
  のため単一 watermark で足りる。スロット入れ替え後もそのまま有効)。**全スロットが完走した
  ときだけ進める**: 途中打ち切りで進めると未実行スロットのその日ぶんが watermark の背後に
  隠れて恒久欠落するため、打ち切り時は据え置き(翌日、旧 watermark から再取得して UNIQUE で
  吸収。取得済みぶんの再読課金は検索 cap 上限までで許容)。
- 打ち切り・失敗はホームTL と同じ方針: rate limit 残 0 で全体打ち切り / 累計 0 件での
  HTTP 失敗は例外(0件成功で障害を隠さない)/ 部分取得後の失敗は打ち切り返却。
- 同一ツイートが複数スロットに一致した場合は重複して返すが、collect の UNIQUE
  (source_id, external_id)で無害に吸収される(設計 02 §1 の冪等性契約)。
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from app.config import X_SEARCH_MIN_PAGE, X_SEARCH_QUERY_SUFFIX
from app.connectors.base import FetchResult, FetchStats, RawItem
from app.connectors.x import (
    API_BASE,
    MAX_RESULTS,
    XApiSession,
    XCredentials,
    build_x_client,
    credentials_from_env,
    parse_created_at,
    raise_for_auth,
    rate_limit_exhausted,
    status_error,
    tweet_full_text,
)
from app.models import Source

SEARCH_URL = f"{API_BASE}/tweets/search/recent"
# RT・リプライはクエリ演算子で除外済みのため referenced_tweets 等は要らない。
# note_tweet = 長文ポストの全文(GH#94。ホームTL と同じ理由)
TWEET_FIELDS = "created_at,author_id,note_tweet"
EXPANSIONS = "author_id"


class XSearchConnector:
    source_type = "x"

    def __init__(
        self,
        *,
        client: httpx.Client,
        credentials: XCredentials,
        slots: tuple[str, ...],
        daily_read_cap: int,
        unit_price_usd: float,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._session = XApiSession(client=client, credentials=credentials, sleep=sleep)
        self._slots = slots
        self._daily_read_cap = daily_read_cap
        self._unit_price = unit_price_usd

    def fetch(self, source_row: Source, cursor: dict) -> FetchResult:
        started = time.monotonic()
        since_id = cursor.get("since_id")
        users: dict[str, str] = {}
        fetched: list[tuple[str, list[dict]]] = []  # (スロットのクエリ, tweets)
        reads = 0
        http_status: int | None = None
        budgets = _split_budget(self._daily_read_cap, len(self._slots))
        stopped = False
        for query, budget in zip(self._slots, budgets, strict=True):
            tweets, status, stopped = self._fetch_slot(
                query, budget, since_id, users, have_prior=any(t for _q, t in fetched)
            )
            fetched.append((query, tweets))
            reads += len(tweets)
            if status is not None:
                http_status = status
            if stopped:
                break  # rate limit 残 0 / 部分取得後の失敗 → 残りスロットも当日分は諦める
        items = [
            _to_raw_item(tweet, users, query) for query, tweets in fetched for tweet in tweets
        ]
        all_ids = [int(t["id"]) for _q, tweets in fetched for t in tweets]
        # 次回 since_id は「全スロット完走」時のみ今回の最大 tweet id に進める。
        # 途中打ち切りで進めると未実行スロットのその日ぶんが恒久欠落する(据え置きなら
        # 翌日再取得され UNIQUE で吸収)。スロット内の予算打ち切りで捨てる古い側は
        # ホームTL と同じく追わない(設計 02 §2)
        next_since = str(max(all_ids)) if all_ids and not stopped else since_id
        return FetchResult(
            items=items,
            next_cursor={"since_id": next_since} if next_since else {},
            stats=FetchStats(
                items_fetched=len(items),
                api_reads=reads,
                estimated_cost_usd=round(reads * self._unit_price, 6),
                http_status=http_status,
                duration_ms=int((time.monotonic() - started) * 1000),
            ),
        )

    def _fetch_slot(
        self,
        query: str,
        budget: int,
        since_id: str | None,
        users: dict[str, str],
        *,
        have_prior: bool,
    ) -> tuple[list[dict], int | None, bool]:
        """1スロット分のページング取得。returns (tweets, http_status, stop_all)。"""
        tweets: list[dict] = []
        token: str | None = None
        http_status: int | None = None
        while budget - len(tweets) >= X_SEARCH_MIN_PAGE:
            params: dict[str, str | int] = {
                "query": f"{query} {X_SEARCH_QUERY_SUFFIX}",
                "max_results": min(MAX_RESULTS, budget - len(tweets)),
                "tweet.fields": TWEET_FIELDS,
                "expansions": EXPANSIONS,
            }
            if since_id:
                params["since_id"] = since_id
            if token:
                params["next_token"] = token  # search/recent は pagination_token でなく next_token
            response = self._session.get(SEARCH_URL, params)
            http_status = response.status_code
            raise_for_auth(response, "search/recent")
            if response.status_code != 200:
                # 累計 0 件での失敗は「0件成功」で隠さず例外(ホームTL と同方針)。
                # どこかのスロットが取得済みなら部分取得として全体打ち切り
                if not have_prior and not tweets:
                    raise status_error(response, SEARCH_URL)
                return tweets, http_status, True
            body = response.json()
            page = body.get("data", [])
            for user in body.get("includes", {}).get("users", []):
                users[user["id"]] = user.get("username")
            tweets.extend(page)
            if not page:
                break  # 空ページ = 新規なし。next_token 循環によるループも塞ぐ
            if rate_limit_exhausted(response):
                return tweets, http_status, True  # 残 0 → 全体打ち切り(15分待ちはしない)
            token = body.get("meta", {}).get("next_token")
            if not token:
                break  # since_id 到達 / 7日窓の末尾
        return tweets, http_status, False


def build_x_search_connector(
    slots: tuple[str, ...], daily_read_cap: int, unit_price_usd: float, env: dict[str, str]
) -> XSearchConnector:
    """cli の合成点から呼ぶ(前提: has_x_credentials(env) が真・slots 非空)。"""
    return XSearchConnector(
        client=build_x_client(),
        credentials=credentials_from_env(env),
        slots=slots,
        daily_read_cap=daily_read_cap,
        unit_price_usd=unit_price_usd,
    )


def _split_budget(total: int, n: int) -> list[int]:
    """均等割・端数は先頭から(35, 3 → [12, 12, 11]。ADR 0001「3スロット × 約12件」)。"""
    base, remainder = divmod(total, n)
    return [base + (1 if i < remainder else 0) for i in range(n)]


def _to_raw_item(tweet: dict, users: dict[str, str], query: str) -> RawItem:
    return RawItem(
        external_id=tweet["id"],
        url=f"https://x.com/i/web/status/{tweet['id']}",  # normalize_url が status id で統一
        title=None,  # tweet に title はない(本文は raw_content)
        author=users.get(tweet.get("author_id")),
        published_at=parse_created_at(tweet.get("created_at")),
        raw_content=tweet_full_text(tweet),
        meta={"query": query},  # どのスロットが拾ったか(週次レビューの再配分材料)
    )
