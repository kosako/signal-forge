"""X 興味シグナル取込(S1b = GH#58 / ADR 0001)。

自分の X 行動(🔖ブックマーク・❤️いいね・🔁自RT)を探索シグナルとして x_signals に
記録する。digest には流さない(items に書かない = collect 経路から独立)。

- bookmarks / liked_tweets は since_id 非対応のため、新→旧に読み「既知 tweet_id に
  到達したら打ち切り」で差分化する(UNIQUE (kind, tweet_id) + INSERT OR IGNORE)。
  ブックマーク解除→再登録などで古い側に隠れた新規は追わない(見逃し防止は非目標。
  ホームTL と同方針)
- 自RT は /2/users/:id/tweets(OAuth 1.0a・既存キー)から referenced_tweets の
  type='retweeted' を拾う。著者・本文は RT 元ツイートのもの(興味の対象は元ツイ)
- 予算: [x_signals].daily_read_cap(既定 0 = 無効)。読取 = 取得ツイート数で
  collect と同じ意味論。cold start(初回全件)は日次 run でなく
  intel x-signals --backfill の手動実行(課金操作は人間ゲート。ADR 0001)
- text_excerpt は TTL キャッシュのみ(purge が retention.x_cache_ttl_days で
  NULL 化。Issue #2 の確定ポリシー)
- 失敗は kind 単位で隔離(bookmark が死んでも like / self_rt は取る)。全 kind 失敗
  かつ新規 0 のときのみ run を failed で記録する
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Protocol

import httpx

from app.config import Config
from app.connectors.base import FatalError
from app.connectors.x import API_BASE as OAUTH1_API_BASE
from app.connectors.x import raise_for_auth, rate_limit_exhausted
from app.connectors.x_oauth2 import USERS_ME_URL, XOAuth2Error, XOAuth2Session

API_BASE = "https://api.x.com/2"
PAGE_MAX = 100  # 各エンドポイントの 1 ページ上限
PAGE_MIN = 5  # liked_tweets の max_results 下限(最も厳しい値に揃える)
EXCERPT_MAX = 280  # text_excerpt の上限(TTL キャッシュ。恒久保存しない)
TWEET_FIELDS = "created_at,author_id,referenced_tweets"
EXPANSIONS = "author_id,referenced_tweets.id,referenced_tweets.id.author_id"

KIND_ENDPOINTS = {  # kind → me_id を埋めるエンドポイント(取込は強シグナル優先の順)
    "bookmark": "/users/{me_id}/bookmarks",
    "like": "/users/{me_id}/liked_tweets",
    "self_rt": "/users/{me_id}/tweets",
}


class XSignalsError(Exception):
    """取込の失敗(kind 単位で隔離される)。"""


class _Session(Protocol):  # XOAuth2Session / XApiSession の共通部分(GET のみ)
    def get(self, url: str, params: dict[str, str | int] | None = None) -> httpx.Response: ...


def collect_x_signals(
    conn: sqlite3.Connection,
    config: Config,
    oauth2_session: XOAuth2Session,
    oauth1_session: _Session | None,
    logger: logging.Logger,
    *,
    run_id: str | None = None,
    backfill: bool = False,
) -> dict[str, int]:
    """3 種のシグナルを cap 内で取込み、x_signal_runs に 1 行記録して集計を返す。

    oauth1_session が None(OAuth1 キー未設定)のときは self_rt を skip する。
    """
    me_id = _get_me_id(oauth2_session)
    budget = config.x_signals.daily_read_cap if not backfill else None
    totals = {"api_reads": 0, "new_signals": 0}
    errors: list[str] = []
    for kind in ("bookmark", "like", "self_rt"):  # 強シグナル優先で予算を消費
        session: _Session | None = oauth2_session if kind != "self_rt" else oauth1_session
        if session is None:
            logger.warning(
                "self_rt を skip(OAuth1 キー未設定)",
                extra={"run_id": run_id, "stage": "x_signals"},
            )
            continue
        remaining = None if budget is None else budget - totals["api_reads"]
        if remaining is not None and remaining < PAGE_MIN:
            logger.info(
                f"{kind} を skip(予算残 {remaining} < {PAGE_MIN})",
                extra={"run_id": run_id, "stage": "x_signals"},
            )
            continue
        try:
            stats = _collect_kind(
                conn, session, kind, me_id, remaining, run_id, backfill=backfill
            )
            totals["api_reads"] += stats["api_reads"]
            totals["new_signals"] += stats["new_signals"]
        except (XSignalsError, XOAuth2Error, FatalError) as e:
            # kind 単位で隔離(bookmark の 403 = scope 不備でも like / self_rt は取る)
            errors.append(f"{kind}: {e}")
            logger.error(
                f"x_signals {kind} 取込失敗(他 kind は継続): {e}",
                extra={"run_id": run_id, "stage": "x_signals"},
            )
    cost = totals["api_reads"] * config.x.unit_price_usd
    all_failed = bool(errors) and totals["new_signals"] == 0 and totals["api_reads"] == 0
    conn.execute(
        "INSERT INTO x_signal_runs (status, api_reads, estimated_cost_usd, new_signals, "
        "error, run_id) VALUES (?, ?, ?, ?, ?, ?)",
        (
            "failed" if all_failed else "ok",
            totals["api_reads"],
            cost,
            totals["new_signals"],
            " / ".join(errors) or None,
            run_id,
        ),
    )
    conn.commit()
    logger.info(
        "x_signals 取込完了",
        extra={
            "run_id": run_id,
            "stage": "x_signals",
            "extra": {**totals, "cost_usd": round(cost, 4), "errors": len(errors)},
        },
    )
    if all_failed:
        raise XSignalsError(" / ".join(errors))  # 全滅は段の失敗として loud に(GH#74 と同方針)
    return totals


def _get_me_id(oauth2_session: XOAuth2Session) -> str:
    response = oauth2_session.get(USERS_ME_URL)
    if response.status_code != 200:
        raise XSignalsError(f"users/me が HTTP {response.status_code}")
    me_id = response.json().get("data", {}).get("id")
    if not me_id:
        raise XSignalsError("users/me の応答に id がない")
    return str(me_id)


def _collect_kind(
    conn: sqlite3.Connection,
    session: _Session,
    kind: str,
    me_id: str,
    budget: int | None,
    run_id: str | None,
    *,
    backfill: bool,
) -> dict[str, int]:
    """1 kind をページングで取込む。戻り値 {api_reads, new_signals}。

    打ち切り条件: (a) 既知 tweet_id に到達(backfill 時は無効)(b) 予算残 < PAGE_MIN
    (c) next_token なし (d) rate limit 残 0(429)。
    """
    # self_rt は OAuth1 セッション(既存の api.twitter.com 署名ドメイン)に合わせる
    base = OAUTH1_API_BASE if kind == "self_rt" else API_BASE
    url = base + KIND_ENDPOINTS[kind].format(me_id=me_id)
    reads = 0
    new_signals = 0
    next_token: str | None = None
    while True:
        remaining = None if budget is None else budget - reads
        if remaining is not None and remaining < PAGE_MIN:
            break
        params: dict[str, str | int] = {
            "max_results": PAGE_MAX if remaining is None else min(PAGE_MAX, remaining),
            "tweet.fields": TWEET_FIELDS,
            "expansions": EXPANSIONS,
        }
        if kind == "self_rt":
            params["exclude"] = "replies"  # RT 判定は referenced_tweets で行う
        if next_token:
            params["pagination_token"] = next_token
        response = session.get(url, params)
        if response.status_code == 429:
            break  # rate limit は待たず打ち切る(設計 02 §2 と同方針)
        raise_for_auth(response, f"x_signals {kind}")
        if response.status_code != 200:
            raise XSignalsError(f"{kind} が HTTP {response.status_code}")
        body = response.json()
        tweets = body.get("data", [])
        if not tweets:
            break
        reads += len(tweets)
        users, ref_tweets = _index_includes(body)
        hit_known = False
        for tweet in tweets:
            row = _to_signal_row(kind, tweet, users, ref_tweets)
            if row is None:
                continue  # self_rt で RT でないツイート等(読取は消費済み)
            inserted = conn.execute(
                "INSERT OR IGNORE INTO x_signals (kind, tweet_id, author_id, "
                "author_username, text_excerpt, tweet_created_at, run_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (kind, *row, run_id),
            ).rowcount
            if inserted:
                new_signals += 1
            elif not backfill:
                hit_known = True  # 既知に到達 = これより古い側は取込済み
        conn.commit()  # ページ単位で確定(途中失敗でも取得済みぶんを失わない)
        if hit_known and not backfill:
            break
        next_token = body.get("meta", {}).get("next_token")
        if not next_token:
            break
        if rate_limit_exhausted(response):
            break
    return {"api_reads": reads, "new_signals": new_signals}


def _index_includes(body: dict) -> tuple[dict[str, str], dict[str, dict]]:
    includes = body.get("includes", {})
    users = {u["id"]: u.get("username", "") for u in includes.get("users", [])}
    ref_tweets = {t["id"]: t for t in includes.get("tweets", [])}
    return users, ref_tweets


def _to_signal_row(
    kind: str, tweet: dict, users: dict[str, str], ref_tweets: dict[str, dict]
) -> tuple[str, str | None, str | None, str | None, str | None] | None:
    """(tweet_id, author_id, author_username, text_excerpt, tweet_created_at) or None。

    self_rt は RT だけを拾い、著者・本文・時刻は RT 元ツイートのもの(興味の対象は
    元ツイ。ホームTL の「RT は元ツイに正規化」と同方針)。RT 元が includes に無い
    場合は id のみ記録する(シグナルとしては成立する)。
    """
    if kind != "self_rt":
        author_id = tweet.get("author_id")
        return (
            str(tweet["id"]),
            author_id,
            users.get(author_id),
            (tweet.get("text") or "")[:EXCERPT_MAX] or None,
            tweet.get("created_at"),
        )
    retweeted = next(
        (r for r in tweet.get("referenced_tweets", []) if r.get("type") == "retweeted"),
        None,
    )
    if retweeted is None:
        return None  # 通常ポスト・引用はシグナル対象外(自分の発信は別物)
    source = ref_tweets.get(retweeted["id"], {})
    author_id = source.get("author_id")
    return (
        str(retweeted["id"]),
        author_id,
        users.get(author_id),
        (source.get("text") or "")[:EXCERPT_MAX] or None,
        source.get("created_at"),
    )
