"""X 興味シグナル取込(S1b = GH#58 / ADR 0001)。

自分の X 行動(🔖ブックマーク・❤️いいね・🔁自RT)を探索シグナルとして x_signals に
記録する。digest には流さない(items に書かない = collect 経路から独立)。

- 認証は 3 種とも OAuth 2.0(XOAuth2Session)に統一(ADR 0001「認証経路を1本に保つ」。
  自RT は OAuth1 でも取れるが、S1a で組んだ OAuth2 に相乗りさせる実装選択)
- bookmarks / liked_tweets は since_id 非対応のため、新→旧に読み「既知 tweet_id に
  到達したら打ち切り」で差分化する(UNIQUE (kind, tweet_id) + INSERT OR IGNORE)。
  解除→再登録などで古い側に隠れた新規は追わない(見逃し防止は非目標)
- 自RT は /2/users/:id/tweets から referenced_tweets の type='retweeted' を拾う。
  通常投稿は保存されず既知到達方式が効かない(毎回同じ先頭ページを再課金する)ため、
  since_id カーソル(x_signal_cursors)で差分化する。カーソル規約はホームTL と同じ
  「打ち切り時も今回の最大 id へ進める」(古い側は追わない)
- 予算: [x_signals].daily_read_cap(既定 0 = 無効)。読取 = 取得ツイート数で
  collect と同じ意味論。途中失敗でも取得済みページぶんは必ず予算・台帳に計上する。
  cold start(初回全件)は日次 run でなく intel x-signals --backfill の手動実行
  (課金操作は人間ゲート。ADR 0001)
- text_excerpt は TTL キャッシュのみ(purge が retention.x_cache_ttl_days で
  NULL 化。Issue #2 の確定ポリシー)
- 失敗は kind 単位で隔離(bookmark の 403 = scope 不備でも like / self_rt は取る)。
  試行した全 kind が失敗(例外、または取得 0 のまま rate limit)したときのみ
  段の失敗として loud に落とす(GH#74 と同方針)
"""

from __future__ import annotations

import logging
import sqlite3

from app.config import X_SIGNALS_MIN_PAGE, Config
from app.connectors.base import FatalError
from app.connectors.x import raise_for_auth, rate_limit_exhausted
from app.connectors.x_oauth2 import USERS_ME_URL, XOAuth2Error, XOAuth2Session

API_BASE = "https://api.x.com/2"
PAGE_MAX = 100  # 各エンドポイントの 1 ページ上限
EXCERPT_MAX = 280  # text_excerpt の上限(TTL キャッシュ。恒久保存しない)
TWEET_FIELDS = "created_at,author_id,referenced_tweets"
EXPANSIONS = "author_id,referenced_tweets.id,referenced_tweets.id.author_id"

KIND_ENDPOINTS = {  # 取込は強シグナル優先の順(collect_x_signals のループ順)
    "bookmark": "/users/{me_id}/bookmarks",
    "like": "/users/{me_id}/liked_tweets",
    "self_rt": "/users/{me_id}/tweets",
}


class XSignalsError(Exception):
    """取込の失敗(kind 単位で隔離される)。"""


def collect_x_signals(
    conn: sqlite3.Connection,
    config: Config,
    session: XOAuth2Session,
    logger: logging.Logger,
    *,
    run_id: str | None = None,
    backfill: bool = False,
) -> dict[str, int]:
    """3 種のシグナルを cap 内で取込み、x_signal_runs に 1 行記録して集計を返す。"""
    me_id = _get_me_id(session)
    budget = None if backfill else config.x_signals.daily_read_cap
    totals = {"api_reads": 0, "new_signals": 0}
    outcomes: dict[str, str] = {}  # 試行した kind → 'ok' | 'failed'(skip は含めない)
    errors: list[str] = []
    for kind in KIND_ENDPOINTS:
        remaining = None if budget is None else budget - totals["api_reads"]
        if remaining is not None and remaining < X_SIGNALS_MIN_PAGE:
            logger.info(
                f"{kind} を skip(予算残 {remaining} < {X_SIGNALS_MIN_PAGE})",
                extra={"run_id": run_id, "stage": "x_signals"},
            )
            continue
        stats = {"api_reads": 0, "new_signals": 0, "rate_limited": 0}
        try:
            _collect_kind(conn, session, kind, me_id, remaining, run_id, stats, backfill)
        except (XSignalsError, XOAuth2Error, FatalError) as e:
            # kind 単位で隔離(bookmark の 403 = scope 不備でも like / self_rt は取る)
            outcomes[kind] = "failed"
            errors.append(f"{kind}: {e}")
            logger.error(
                f"x_signals {kind} 取込失敗(他 kind は継続): {e}",
                extra={"run_id": run_id, "stage": "x_signals"},
            )
        else:
            if stats["rate_limited"] and stats["api_reads"] == 0:
                # 1件も取れずに rate limit はその kind の失敗(黙って 0 件成功にしない)
                outcomes[kind] = "failed"
                errors.append(f"{kind}: rate limit(取得 0)")
            else:
                outcomes[kind] = "ok"
        finally:
            # 途中ページ失敗でも取得済みぶんを予算・台帳に必ず計上する(過少計上で
            # 後続 kind に予算が再配分され cap を実質超過するのを防ぐ)
            totals["api_reads"] += stats["api_reads"]
            totals["new_signals"] += stats["new_signals"]
    cost = totals["api_reads"] * config.x.unit_price_usd
    all_failed = bool(outcomes) and all(v == "failed" for v in outcomes.values())
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


def _get_me_id(session: XOAuth2Session) -> str:
    response = session.get(USERS_ME_URL)
    if response.status_code != 200:
        raise XSignalsError(f"users/me が HTTP {response.status_code}")
    me_id = response.json().get("data", {}).get("id")
    if not me_id:
        raise XSignalsError("users/me の応答に id がない")
    return str(me_id)


def _collect_kind(
    conn: sqlite3.Connection,
    session: XOAuth2Session,
    kind: str,
    me_id: str,
    budget: int | None,
    run_id: str | None,
    stats: dict[str, int],
    backfill: bool,
) -> None:
    """1 kind をページングで取込む。stats(api_reads / new_signals / rate_limited)を
    ページごとに更新する(例外時も取得済みぶんが呼び出し側に残る)。

    打ち切り条件: (a) 既知 tweet_id に到達(backfill 時は無効)(b) 予算残 < 下限
    (c) next_token なし (d) rate limit 残 0(429 は待たない)。
    """
    url = API_BASE + KIND_ENDPOINTS[kind].format(me_id=me_id)
    since_id = _load_cursor(conn, kind) if kind == "self_rt" else None
    max_wrapper_id = 0  # self_rt のカーソル素材(自分のタイムライン上の最大 tweet id)
    next_token: str | None = None
    while True:
        remaining = None if budget is None else budget - stats["api_reads"]
        if remaining is not None and remaining < X_SIGNALS_MIN_PAGE:
            break
        params: dict[str, str | int] = {
            "max_results": PAGE_MAX if remaining is None else min(PAGE_MAX, remaining),
            "tweet.fields": TWEET_FIELDS,
            "expansions": EXPANSIONS,
        }
        if kind == "self_rt":
            params["exclude"] = "replies"  # RT 判定は referenced_tweets で行う
            if since_id:
                params["since_id"] = since_id
        if next_token:
            params["pagination_token"] = next_token
        response = session.get(url, params)
        if response.status_code == 429:
            stats["rate_limited"] = 1
            break  # rate limit は待たず打ち切る(設計 02 §2 と同方針)
        raise_for_auth(response, f"x_signals {kind}")
        if response.status_code != 200:
            raise XSignalsError(f"{kind} が HTTP {response.status_code}")
        body = response.json()
        tweets = body.get("data", [])
        if not tweets:
            break
        stats["api_reads"] += len(tweets)
        if kind == "self_rt":
            max_wrapper_id = max(max_wrapper_id, *(int(t["id"]) for t in tweets))
        users, ref_tweets = _index_includes(body)
        hit_known = False
        # 接続は autocommit のため、ページ単位の原子性は明示トランザクションで作る
        # (半端なページが残ると、次回の既知到達打ち切りが未保存の残りを飛ばす)
        conn.execute("BEGIN IMMEDIATE")
        try:
            for tweet in tweets:
                row = _to_signal_row(kind, tweet, users, ref_tweets)
                if row is None:
                    continue  # self_rt の通常投稿・引用(読取は消費済み)
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO x_signals (kind, tweet_id, author_id, "
                    "author_username, text_excerpt, tweet_created_at, run_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (kind, *row, run_id),
                ).rowcount
                if inserted:
                    stats["new_signals"] += 1
                elif not backfill:
                    hit_known = True  # 既知に到達 = これより古い側は取込済み
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        if hit_known and not backfill:
            break
        next_token = body.get("meta", {}).get("next_token")
        if not next_token:
            break
        if rate_limit_exhausted(response):
            break
    if kind == "self_rt" and max_wrapper_id:
        # ホームTL と同じカーソル規約: 打ち切り時も今回の最大 id へ進める(古い側は
        # 追わない)。進めないと、通常投稿が多い日に同じ先頭ページを毎日再課金する
        conn.execute(
            "INSERT INTO x_signal_cursors (kind, since_id) VALUES ('self_rt', ?) "
            "ON CONFLICT(kind) DO UPDATE SET since_id = excluded.since_id",
            (str(max_wrapper_id),),
        )


def _load_cursor(conn: sqlite3.Connection, kind: str) -> str | None:
    row = conn.execute(
        "SELECT since_id FROM x_signal_cursors WHERE kind = ?", (kind,)
    ).fetchone()
    return row["since_id"] if row else None


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
