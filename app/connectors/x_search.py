"""X 検索コネクタ(`GET /2/tweets/search/recent`・3スロット固定。設計 02 §2 / ADR 0001)。

- スロット(検索クエリ)は config.toml の `[x_search].slots`(最大3・入れ替え式)。各クエリを
  括弧で囲んで `-is:retweet -is:reply` を自動付加し、RT・リプライを課金前にサーバ側で除外する
  (ホームTL の「RT は元ツイに正規化・他人宛リプライ除外」と方針整合。取ってから捨てない)。
  括弧で囲むのは OR を含むスロットでも除外を全体に掛けるため(GH#230)。
- 予算(daily_read_cap)はスロット数で均等割(端数は先頭から)。スロット間の再配分はしない
  (1つの多産クエリが他スロットを飢えさせない)。残枠が max_results 下限(10)を切ったら
  そのスロットは打ち切る(cap はハード上限。超過方向に丸めない)。cap は JST の暦日の上限で、
  同じ日の実行が読んだぶん(合成点が台帳から出す。connectors/x.py の x_reads_today)を引いた
  残りを均等割する(GH#178)。
- カーソルは全スロット横断の最大 tweet id を `{"since_id": ...}` に(snowflake id は時刻単調
  のため単一 watermark で足りる。スロット入れ替え後もそのまま有効)。そのために全スロットの
  取得の上限(end_time)を実行の開始で 1 つに固定する(GH#184): スロットは順に取るので、上限が
  要求ごとに違うと、先のスロットに実行中に入った新着が後のスロットの最大 id の背後に隠れて
  恒久欠落する。**全スロットが完走したときだけ進める**: 途中打ち切りで進めると未実行スロットの
  その日ぶんが watermark の背後に隠れて恒久欠落するため、打ち切り時は据え置き(翌日、旧
  watermark から再取得して UNIQUE で吸収。取得済みぶんの再読課金は検索 cap 上限までで許容)。
- search/recent は直近 7 日より古い since_id を 400 で拒否する。停止明けで watermark が窓の外に
  出ると毎日 400 で永久に進まないため、窓の端に近い(SINCE_ID_MAX_AGE より古い)since_id は
  外して取得する(GH#121。窓の外の欠落は API 上取り戻せない)。
- 打ち切り・失敗はホームTL と同じ方針: rate limit 残 0 で全体打ち切り / 累計 0 件での
  HTTP・通信の失敗は例外(0件成功で障害を隠さない)/ 部分取得後の失敗は打ち切り返却。部分取得後の
  失敗は取得ぶんを返しつつ理由を `FetchResult.partial_error` に載せ、collect にソースの失敗と
  して記録させる(GH#183。後ろのスロットの壊れたクエリの 400 などを正常扱いで隠さない)。
  通信のリトライ尽きも同じに扱う(GH#182。例外で取得ぶんを捨てると読み取りの費用も台帳に残らない)。
  予算・rate limit 残 0 による打ち切りは失敗にしない(応答は正常)。
- 同一ツイートが複数スロットに一致した場合は重複して返すが、collect の UNIQUE
  (source_id, external_id)で無害に吸収される(設計 02 §1 の冪等性契約)。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx

from app.config import X_SEARCH_MIN_PAGE, x_search_query
from app.connectors.base import FetchResult, FetchStats, RawItem, RetryableError
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
# search/recent が受け付ける since_id は直近 7 日(実測の 400 応答で確認・GH#121)。
# run の所要時間と時計のずれに 1 日の余裕を取る(この古さなら予算はどのみち新しい側で埋まる)
SINCE_ID_MAX_AGE = timedelta(days=6)
# 全スロット共通の取得の上限(end_time)を実行の開始からどれだけ前に置くか(GH#184)。
# search/recent は end_time を要求の時刻より 10 秒以上前に限り、指定しなければ要求の 30 秒前を
# 上限にする。既定と同じ 30 秒にして索引の遅れへの余裕を今までと変えない(時計のずれは 20 秒まで)
END_TIME_LAG = timedelta(seconds=30)
_SNOWFLAKE_EPOCH_MS = 1288834974657  # tweet id(snowflake)の時刻の基準 = 2010-11-04T01:42:54.657Z


def _utcnow() -> datetime:
    return datetime.now(UTC)


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
        reads_used_today: int = 0,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._session = XApiSession(client=client, credentials=credentials, sleep=sleep)
        self._slots = slots
        # 今回読める上限 = 日次の上限 − 同じ日(JST)の実行が読んだぶん(GH#178)
        self._read_budget = max(0, daily_read_cap - reads_used_today)
        self._unit_price = unit_price_usd
        self._now = now

    def fetch(self, source_row: Source, cursor: dict) -> FetchResult:
        started = time.monotonic()
        now = self._now()
        # 全スロットをこの時刻までで取る(GH#184。秒の単位で、この時刻は含まない)
        end_time = (now - END_TIME_LAG).strftime("%Y-%m-%dT%H:%M:%SZ")
        stored_since = cursor.get("since_id")
        since_id = stored_since  # 送る値。外しても保存値は据え置き用に残す(次回も同じ判定で外れる)
        if since_id and _snowflake_time(since_id) < now - SINCE_ID_MAX_AGE:
            # 送ると毎日 400 で watermark が永久に進まない(GH#121)。外して新しい側から取り直す
            logging.getLogger("intel").warning(
                "検索の since_id が 7 日窓の端より古い。since_id なしで取得する",
                extra={"source_id": source_row.id, "extra": {"since_id": since_id}},
            )
            since_id = None
        users: dict[str, str] = {}
        fetched: list[tuple[str, list[dict]]] = []  # (スロットのクエリ, tweets)
        reads = 0
        http_status: int | None = None
        budgets = _split_budget(self._read_budget, len(self._slots))
        stopped = False
        error: Exception | None = None
        for query, budget in zip(self._slots, budgets, strict=True):
            tweets, status, stopped, error = self._fetch_slot(
                query, budget, since_id, end_time, users, have_prior=any(t for _q, t in fetched)
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
        # ホームTL と同じく追わない(設計 02 §2)。全スロットを同じ end_time で取るので、
        # あるスロットを取ったあとに入った新着が、後のスロットの最大 id の背後に隠れない(GH#184)
        next_since = str(max(all_ids)) if all_ids and not stopped else stored_since
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
            partial_error=error,
        )

    def _fetch_slot(
        self,
        query: str,
        budget: int,
        since_id: str | None,
        end_time: str,
        users: dict[str, str],
        *,
        have_prior: bool,
    ) -> tuple[list[dict], int | None, bool, Exception | None]:
        """1スロット分のページング取得。returns (tweets, http_status, stop_all, error)。

        error = 部分取得のあとの HTTP・通信の失敗(GH#183・GH#182)。そのとき stop_all も真。
        """
        tweets: list[dict] = []
        token: str | None = None
        http_status: int | None = None
        while budget - len(tweets) >= X_SEARCH_MIN_PAGE:
            params: dict[str, str | int] = {
                "query": x_search_query(query),  # (スロット) + 除外句(GH#230)
                "max_results": min(MAX_RESULTS, budget - len(tweets)),
                "end_time": end_time,  # 全スロット共通の上限(GH#184)
                "tweet.fields": TWEET_FIELDS,
                "expansions": EXPANSIONS,
            }
            if since_id:
                params["since_id"] = since_id
            if token:
                params["next_token"] = token  # search/recent は pagination_token でなく next_token
            try:
                response = self._session.get(SEARCH_URL, params)
            except RetryableError as e:
                # 通信のリトライ尽きも HTTP の失敗と同じに扱う(GH#182)。既取得分を例外で
                # 捨てると、その読み取りの費用も台帳に残らない
                if not have_prior and not tweets:
                    raise
                return tweets, http_status, True, e
            http_status = response.status_code
            raise_for_auth(response, "search/recent")
            if response.status_code != 200:
                # 累計 0 件での失敗は「0件成功」で隠さず例外(ホームTL と同方針)。
                # どこかのスロットが取得済みなら部分取得として全体打ち切り。取得ぶんは保存しつつ
                # ソースの失敗として数えさせる(GH#183。400・402・429・5xx を正常扱いで隠さない)
                error = status_error(response, SEARCH_URL)
                if not have_prior and not tweets:
                    raise error
                return tweets, http_status, True, error
            body = response.json()
            page = body.get("data", [])
            for user in body.get("includes", {}).get("users", []):
                users[user["id"]] = user.get("username")
            tweets.extend(page)
            if not page:
                break  # 空ページ = 新規なし。next_token 循環によるループも塞ぐ
            if rate_limit_exhausted(response):
                return tweets, http_status, True, None  # 残 0 → 全体打ち切り(15分待ちはしない)
            token = body.get("meta", {}).get("next_token")
            if not token:
                break  # since_id 到達 / 7日窓の末尾
        return tweets, http_status, False, None


def build_x_search_connector(
    slots: tuple[str, ...],
    daily_read_cap: int,
    unit_price_usd: float,
    env: dict[str, str],
    *,
    reads_used_today: int = 0,
) -> XSearchConnector:
    """cli の合成点から呼ぶ(前提: has_x_credentials(env) が真・slots 非空)。

    reads_used_today = 同じ日の検索の読取数(`x_reads_today` の search。GH#178)。
    """
    return XSearchConnector(
        client=build_x_client(),
        credentials=credentials_from_env(env),
        slots=slots,
        daily_read_cap=daily_read_cap,
        unit_price_usd=unit_price_usd,
        reads_used_today=reads_used_today,
    )


def _snowflake_time(tweet_id: str) -> datetime:
    """tweet id の上位ビット(22 ビット右シフト)に入っている生成時刻(ms)。"""
    return datetime.fromtimestamp(((int(tweet_id) >> 22) + _SNOWFLAKE_EPOCH_MS) / 1000, UTC)


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
