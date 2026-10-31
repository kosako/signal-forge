"""X 興味シグナル取込(S1b = GH#58 / ADR 0001)+ ブックマークの item 化(GH#99)。

自分の X 行動(🔖ブックマーク・❤️いいね・🔁自RT)を探索シグナルとして x_signals に
記録する。like / self_rt はシグナルのみ(items に書かない = collect 経路から独立)。
🔖ブックマークだけは「保存アイテム」として items にも登録する(GH#99):
- 外部リンク付きツイート: 先頭の外部リンクを canonical_url に(記事本体を恒久保存。
  本文は extract 段が取得する)
- リンクなしツイート: ツイート URL を canonical_url に(type='x' の TTL 規約に従う)
- 帰属先は enabled=0 の sources 行 'x:bookmark'(mode=bookmark。collect 対象外)。
  digest ランキング・トレンドには参加しない(除外は ranker / builder / trend 側)

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
  上限は JST の暦日の上限で、同じ日の実行で共有する: 台帳(x_signal_runs.api_reads)の
  通常の取込の当日の合計を引いた残りだけを読む(GH#178。朝バッチの再走・単独の
  intel x-signals で上限を超えない)。backfill は日次の上限の外の別枠: 台帳を見ずに読み、
  その行は backfill = 1 で記録して通常の取込の使用量に数えない(費用の集計には入る)
  差分取得の各 kind の 1 ページ目は最小ページにし、既知到達なら残りを次の kind に
  残す(既知の読み直しで予算を使い切り like / self_rt が毎日 skip されない。GH#174)
  cold start は日次 run でなく intel x-signals --backfill の手動実行(課金操作は
  人間ゲート。ADR 0001)。backfill は「kind ごと直近 N 件を種に」の上限つき
  (全ページ辿りは過去のいいねが多いとコスト事故になる。GH#84)。自RT の backfill は
  since_id カーソルを送らず直近 N 件を読み、カーソルは後ろへ戻さない(GH#231)
- text_excerpt は TTL キャッシュのみ(purge が retention.x_cache_ttl_days で
  NULL 化。Issue #2 の確定ポリシー)
- 失敗は kind 単位で隔離(bookmark の 403 = scope 不備でも like / self_rt は取る)。
  試行した全 kind が失敗(例外、または取得 0 のまま rate limit)したときのみ
  段の失敗として loud に落とす(GH#74 と同方針)
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
from datetime import datetime
from urllib.parse import urlsplit

from app.config import X_SIGNALS_MIN_PAGE, Config
from app.connectors.base import FatalError
from app.connectors.x import raise_for_auth, rate_limit_exhausted, tweet_full_text
from app.connectors.x_oauth2 import USERS_ME_URL, XOAuth2Error, XOAuth2Session
from app.db import insert_item_with_dedup
from app.dig import enqueue_bookmark
from app.jst import jst_day_bounds_utc, today_jst
from app.models import RAW_CONTENT_MAX_CHARS
from app.normalize import (
    X_HOSTS,
    UrlError,
    content_hash,
    is_dedup_eligible,
    normalize_text_for_hash,
    normalize_url,
)

API_BASE = "https://api.x.com/2"
PAGE_MAX = 100  # 各エンドポイントの 1 ページ上限
EXCERPT_MAX = 280  # text_excerpt の上限(TTL キャッシュ。恒久保存しない)
# note_tweet = 長文の全文(GH#94 と同じ理由)/ entities = 外部リンク(item 化の
# canonical_url 素材。GH#99)。フィールド追加は同一読取のため課金は不変
TWEET_FIELDS = "created_at,author_id,referenced_tweets,note_tweet,entities"
EXPANSIONS = "author_id,referenced_tweets.id,referenced_tweets.id.author_id"

BOOKMARK_SOURCE_NAME = "x:bookmark"  # item の帰属先(enabled=0 = collect 対象外)

# 引用・メディア・自己リンクは「外部記事」でない(canonical 素材から除外)。X のホストの
# 別名は URL の正規化と同じ集合を使う(漏れたホストの投稿リンクを外部記事と誤り、
# 正規化で別の投稿の URL になるのを防ぐ。GH#232)
_INTERNAL_LINK_HOSTS = X_HOSTS | {"t.co", "pic.twitter.com"}

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
    backfill_limit: int | None = None,
    kinds: tuple[str, ...] | None = None,
) -> dict[str, int]:
    """3 種のシグナルを cap 内で取込み、x_signal_runs に 1 行記録して集計を返す。

    backfill_limit を渡すと cold start モード: 既知到達で打ち切らず、日次 cap の
    代わりに **kind ごと**の読取上限として適用する(最悪コスト = 3 × limit × 単価)。
    kinds で対象 kind を限定できる(既定 = 全種。過去ブックマークの再読取 backfill 等、
    コストを 1 kind に絞る用途。GH#99)。
    通常の取込の日次 cap は同じ日(JST)の実行で共有する(GH#178)。backfill は台帳を見ず、
    その行は backfill = 1 で記録して通常の取込の使用量に数えない(別枠)。
    """
    backfill = backfill_limit is not None
    # 同じ日の実行がすでに読んだぶん(GH#178)。backfill は日次 cap の外なので数えない
    used_today = 0 if backfill else signal_reads_on(conn, today_jst())
    me_id = _get_me_id(session)
    totals = {"api_reads": 0, "new_signals": 0}
    outcomes: dict[str, str] = {}  # 試行した kind → 'ok' | 'failed'(skip は含めない)
    errors: list[str] = []
    try:
        for kind in KIND_ENDPOINTS:
            if kinds is not None and kind not in kinds:
                continue
            if backfill:
                remaining = backfill_limit  # kind ごとに独立の上限(日次 cap と共有しない)
            else:
                remaining = config.x_signals.daily_read_cap - used_today - totals["api_reads"]
            if remaining < X_SIGNALS_MIN_PAGE:
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
    except BaseException as e:
        # 分類外の例外(保存の sqlite3.Error・想定外のバグなど)は今までどおり段の失敗として
        # 抜けるが、その前に取得済みの読取数と費用を failed の行で台帳に残す(設計 02 §2。
        # 漏れると同じ日の再実行が日次 cap を超えて読む。GH#330)。totals は kind の finally で
        # 加算済み。台帳を書けなくても元の例外を送出する
        errors.append(f"{kind}: {_error_text(e)}")
        try:
            _record_run(conn, config, totals, errors, failed=True, run_id=run_id, backfill=backfill)
        except Exception:
            logger.exception(
                "x_signals の台帳を書けない(取込の元の例外を送出する)",
                extra={"run_id": run_id, "stage": "x_signals"},
            )
        raise
    all_failed = bool(outcomes) and all(v == "failed" for v in outcomes.values())
    cost = _record_run(
        conn, config, totals, errors, failed=all_failed, run_id=run_id, backfill=backfill
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


def _record_run(
    conn: sqlite3.Connection,
    config: Config,
    totals: dict[str, int],
    errors: list[str],
    *,
    failed: bool,
    run_id: str | None,
    backfill: bool,
) -> float:
    """取込 1 回の読取数・費用を x_signal_runs へ 1 行記録し、費用を返す。"""
    cost = totals["api_reads"] * config.x.unit_price_usd
    conn.execute(
        "INSERT INTO x_signal_runs (status, api_reads, estimated_cost_usd, new_signals, "
        "error, run_id, backfill) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "failed" if failed else "ok",
            totals["api_reads"],
            cost,
            totals["new_signals"],
            " / ".join(errors) or None,
            run_id,
            1 if backfill else 0,  # backfill は日次 cap の別枠(GH#178。migration 023)
        ),
    )
    return cost


def _error_text(error: BaseException) -> str:
    """例外の __str__ 自体が壊れていても台帳の記録を崩さない(分類外の例外も扱うため)。"""
    try:
        return f"{type(error).__name__}: {error}"
    except Exception:
        return type(error).__name__


def signal_reads_on(conn: sqlite3.Connection, day: str) -> int:
    """JST の暦日 day に通常の取込が x_signal_runs へ記録した読取数の合計(GH#178)。

    日付は started_at(UTC)を JST の暦日に直して決める(collect の X の日次上限と同じ暦日)。
    失敗の行も数える(途中ページ失敗でも取得済みぶんは計上済み。費用は掛かっている)。
    backfill の行(backfill = 1)は日次 cap の別枠なので数えない(費用の集計には入る)。
    """
    start, end = jst_day_bounds_utc(day)
    return conn.execute(
        "SELECT coalesce(sum(api_reads), 0) AS reads FROM x_signal_runs "
        "WHERE started_at >= ? AND started_at < ? AND backfill = 0",
        (start, end),
    ).fetchone()["reads"]


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
    budget: int,
    run_id: str | None,
    stats: dict[str, int],
    backfill: bool,
) -> None:
    """1 kind をページングで取込む。stats(api_reads / new_signals / rate_limited)を
    ページごとに更新する(例外時も取得済みぶんが呼び出し側に残る)。

    打ち切り条件: (a) 既知 tweet_id に到達(backfill 時は無効)(b) 予算残 < 下限
    (c) next_token なし (d) rate limit 残 0(429 は待たない)。
    差分取得(backfill でない)の 1 ページ目は最小ページで要求する(GH#174)。
    """
    url = API_BASE + KIND_ENDPOINTS[kind].format(me_id=me_id)
    # ブックマークは items にも登録する(GH#99)。帰属先 sources 行はページ処理前に確保
    bookmark_source_id = ensure_bookmark_source(conn) if kind == "bookmark" else None
    # backfill は「kind ごと直近 N 件」なので since_id を送らない(送るとカーソルより
    # 古い過去分を補えない。GH#231)。カーソルの後退は保存の側で防ぐ
    since_id = _load_cursor(conn, kind) if kind == "self_rt" and not backfill else None
    max_wrapper_id = 0  # self_rt のカーソル素材(自分のタイムライン上の最大 tweet id)
    next_token: str | None = None
    while True:
        remaining = budget - stats["api_reads"]
        if remaining < X_SIGNALS_MIN_PAGE:
            break
        page_size = min(PAGE_MAX, remaining)
        if not backfill and next_token is None:
            # 差分取得の 1 ページ目は最小ページにする(GH#174)。読取は既知の判定より先に
            # ページ全件ぶん計上されるため、残予算の全量を要求すると、既知が上限以上
            # 並ぶ日に読み直しだけで予算を使い切り後続 kind が毎日 skip される。
            # 打ち切り条件(既知到達など)に当たらず次のページがあるときは、2 ページ目
            # から残予算と PAGE_MAX の小さい方まで広げる
            page_size = X_SIGNALS_MIN_PAGE
        params: dict[str, str | int] = {
            "max_results": page_size,
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
            if any("id" not in t for t in tweets):
                # _to_signal_row と同じ扱い(KeyError で kind 隔離を抜けさせない)
                raise XSignalsError("応答に id の無いツイートが含まれる")
            max_wrapper_id = max(max_wrapper_id, *(int(t["id"]) for t in tweets))
        users, ref_tweets = _index_includes(body)
        hit_known = False
        page_new = 0  # new_signals は COMMIT 成功後に計上(rollback 分を数えない)
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
                if kind == "bookmark":
                    # 新規シグナルに加え、既知だが item 未紐付けの行(item 化導入前の
                    # 既存シグナルを backfill 再読取した場合)も遡って item 化する。
                    # 深掘りキューへの自動投入は新規取込のみ(backfill = 過去分は
                    # リプライ指定時だけ。GH#104 / 設計 06 §3)
                    _link_bookmark_item(
                        conn, bookmark_source_id, tweet, users, enqueue=not backfill
                    )
                if inserted:
                    page_new += 1
                elif not backfill and kind != "self_rt":
                    # 既知に到達 = これより古い側は取込済み。self_rt には適用しない:
                    # tweet_id は RT 元 id のため、解除→再RT が「既知」に化けて
                    # ページングを止め、未取得の古い RT を恒久欠落させる(境界は
                    # since_id カーソルのみ。SF82-08)
                    hit_known = True
            conn.execute("COMMIT")
        except BaseException:
            _rollback_page(conn)
            raise
        stats["new_signals"] += page_new
        if hit_known and not backfill:
            break
        next_token = body.get("meta", {}).get("next_token")
        if not next_token:
            break
        if rate_limit_exhausted(response):
            break
    if kind == "self_rt" and max_wrapper_id:
        # ホームTL と同じカーソル規約: 打ち切り時も今回の最大 id へ進める(古い側は
        # 追わない)。進めないと、通常投稿が多い日に同じ先頭ページを毎日再課金する。
        # since_id を送らない backfill の最大 id は既存のカーソルより小さいことがある
        # (最新の投稿の削除など)ため、既存の値から後ろへは戻さない(GH#231)。比較は
        # 保存時の値と 1 文の中で数値で行う(取得中に朝バッチなど別の実行がカーソルを
        # 進めても戻さない。id は桁数が変わるため文字列では比べない)
        conn.execute(
            "INSERT INTO x_signal_cursors (kind, since_id) VALUES ('self_rt', ?) "
            "ON CONFLICT(kind) DO UPDATE SET since_id = excluded.since_id "
            "WHERE CAST(excluded.since_id AS INTEGER) "
            "> CAST(x_signal_cursors.since_id AS INTEGER)",
            (str(max_wrapper_id),),
        )


def _rollback_page(conn: sqlite3.Connection) -> None:
    """ページの保存の失敗の後始末。元の例外を後始末の例外で置き換えない(GH#330)。

    容量不足・I/O エラーなどでは SQLite がトランザクションを自動でロールバック済みのことが
    あり、そこで ROLLBACK を送ると別の OperationalError になる。トランザクションが残って
    いるときだけ戻し、ROLLBACK 自体の失敗も抑えて、呼び出し側に元の例外を送出させる。
    """
    if conn.in_transaction:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")


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
    if "id" not in tweet:
        # 想定外の応答形は黙って握らず kind の失敗に(ページはロールバックされる)
        raise XSignalsError("応答に id の無いツイートが含まれる")
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


# --- ブックマークの item 化(GH#99) ---


def ensure_bookmark_source(conn: sqlite3.Connection) -> int:
    """ブックマーク item の帰属先 sources 行を保証して id を返す。

    enabled=0 で登録する(collect の走査対象外。item 化は本モジュールが x_signals 段で
    行うため、collect の「type → Connector 1:1」契約に bookmark を持ち込まない)。
    """
    conn.execute(
        "INSERT OR IGNORE INTO sources (type, name, config, enabled) "
        """VALUES ('x', ?, '{"mode": "bookmark"}', 0)""",
        (BOOKMARK_SOURCE_NAME,),
    )
    return conn.execute(
        "SELECT id FROM sources WHERE name = ?", (BOOKMARK_SOURCE_NAME,)
    ).fetchone()["id"]


def _link_bookmark_item(
    conn: sqlite3.Connection,
    source_id: int,
    tweet: dict,
    users: dict[str, str],
    *,
    enqueue: bool = False,
) -> None:
    """シグナル行に item を紐付ける(未紐付けのときだけ item 化する = 冪等)。

    enqueue=True のとき、新規に紐付けた item を深掘りキューへ自動投入する
    (🔖 = 「後でしっかり見たい」の意思表示。GH#104 / 設計 06 §3)。紐付け済みの
    行は再投入しない(キュー判断は item 化時に一度だけ)。
    """
    row = conn.execute(
        "SELECT item_id FROM x_signals WHERE kind = 'bookmark' AND tweet_id = ?",
        (str(tweet["id"]),),
    ).fetchone()
    if row is None or row["item_id"] is not None:
        return  # 行が無い(INSERT 直後なので想定外)/ 紐付け済み
    item_id = _itemize_bookmark(conn, source_id, tweet, users)
    conn.execute(
        "UPDATE x_signals SET item_id = ? WHERE kind = 'bookmark' AND tweet_id = ?",
        (item_id, str(tweet["id"])),
    )
    _reactivate_skipped_item(conn, item_id)
    if enqueue:
        enqueue_bookmark(conn, item_id)


def _reactivate_skipped_item(conn: sqlite3.Connection, item_id: int) -> None:
    """ブックマークに適用しない規則で skip 済みの合流先を new に戻す(GH#176)。

    前日以前に stale / x_noise / daily_cap で skipped になった既存 item へ合流すると、
    事前選別(new だけが対象)にも保存節(analyzed / digested だけが対象)にも載らない。
    new に戻し、以後の事前選別でブックマークとして扱わせる(設計 02: ブックマークには
    blocklist だけを適用する)。blocklist(明示 NG)による skip は戻さない。
    """
    conn.execute(
        "UPDATE items SET status = 'new', skip_reason = NULL "
        "WHERE id = ? AND status = 'skipped' "
        "AND skip_reason IN ('stale', 'x_noise', 'daily_cap')",
        (item_id,),
    )


def _itemize_bookmark(
    conn: sqlite3.Connection, source_id: int, tweet: dict, users: dict[str, str]
) -> int:
    """ブックマークを保存アイテムとして items へ登録し、live な item id を返す。

    - 外部リンク付き: 先頭の外部リンクを canonical に。本文はリンク先記事が本体のため
      raw_content は持たず extract 段に取得させる(ツイート文は x_signals.text_excerpt
      に TTL キャッシュ済み)
    - リンクなし: ツイート URL を canonical に、全文(note_tweet 優先)を raw_content に
    - canonical / (source, external_id) の既存行・dedup 親がある場合はそこへ合流する
    """
    tweet_url = f"https://x.com/i/web/status/{tweet['id']}"  # normalize_url が status id で統一
    link = _first_external_link(tweet)
    url = link or tweet_url
    raw_content = None if link else tweet_full_text(tweet)
    try:
        canonical = normalize_url(url)
    except UrlError:
        # expanded_url が不正形でも保存は成立させる(ツイート URL にフォールバック)
        url = tweet_url
        raw_content = tweet_full_text(tweet)
        canonical = normalize_url(url)

    normalized_text = normalize_text_for_hash(None, raw_content)
    payload: dict[str, object] = {"meta": {"bookmark": True}}
    if raw_content:
        payload["raw_content"] = raw_content[:RAW_CONTENT_MAX_CHARS]
    author_id = tweet.get("author_id")
    outcome = insert_item_with_dedup(
        conn,
        source_id=source_id,
        external_id=str(tweet["id"]),
        url=url,
        canonical_url=canonical,
        title=None,  # tweet に title はない(表示は analyze の title_ja が担う)
        author=users.get(author_id),
        published_at=_iso_created_at(tweet.get("created_at")),
        raw_payload=json.dumps(payload, ensure_ascii=False),
        content_hash=content_hash(normalized_text),
        dedup_eligible=is_dedup_eligible(normalized_text),
    )
    item_id = outcome.item_id
    if item_id is None:
        # ignored = UNIQUE 既存(canonical_url か (source_id, external_id))へ合流
        existing = conn.execute(
            "SELECT id FROM items WHERE canonical_url = ?", (canonical,)
        ).fetchone() or conn.execute(
            "SELECT id FROM items WHERE source_id = ? AND external_id = ?",
            (source_id, str(tweet["id"])),
        ).fetchone()
        if existing is None:  # UNIQUE 衝突源は上の2つのみ(構造上到達しない)
            raise XSignalsError(f"bookmark item の既存行が引けない: {canonical}")
        item_id = existing["id"]
    return _resolve_live_item(conn, item_id)


def _first_external_link(tweet: dict) -> str | None:
    """entities.urls(note_tweet 側含む)から最初の外部リンクを返す(無ければ None)。"""
    urls: list[dict] = []
    entities = tweet.get("entities")
    if isinstance(entities, dict):
        urls += [u for u in entities.get("urls", []) if isinstance(u, dict)]
    note = tweet.get("note_tweet")
    if isinstance(note, dict) and isinstance(note.get("entities"), dict):
        urls += [u for u in note["entities"].get("urls", []) if isinstance(u, dict)]
    for entry in urls:
        expanded = entry.get("expanded_url") or entry.get("url")
        if not expanded:
            continue
        try:
            host = (urlsplit(expanded).hostname or "").lower()
        except ValueError:
            # 括弧の不整合な netloc などで分解できないリンクは候補から外す(外部リンクが
            # 残らなければ投稿の URL へ戻る)。ページの保存と kind 単位の隔離を抜けさせない
            # (GH#329。GH#186 と同じ根)
            continue
        if host in _INTERNAL_LINK_HOSTS or host.endswith(".twimg.com"):
            continue
        return expanded
    return None


def _resolve_live_item(conn: sqlite3.Connection, item_id: int) -> int:
    """duplicate 化された item は dedup 親(live な item)に解決する(2層 dedup は1段)。"""
    row = conn.execute(
        "SELECT dup_of_item_id FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    if row is not None and row["dup_of_item_id"] is not None:
        return row["dup_of_item_id"]
    return item_id


def _iso_created_at(value: str | None) -> str | None:
    """X の created_at("…T09:08:43.000Z")を items.published_at の形式に揃える。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None  # 不正な日付は published_at だけ落とす(item 化は止めない)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
