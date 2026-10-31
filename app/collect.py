"""intel collect — 全ソースの取り込み(設計 02 §1 の冪等性契約の実装点)。

- コネクタは RawItem を返すだけ。正規化(u1)・content_hash(h1)・2層 dedup・
  カーソル保存はここで一元化する
- カーソル更新は「そのソースの全 INSERT 成功後、同一トランザクション」
  (途中クラッシュ → カーソル未更新 → 次回重複取得 → UNIQUE で吸収)
- 外部 I/O(短縮 URL の解決)は書き込みのトランザクションの前に済ませる(GH#179)
- ソース単位のエラー隔離: 1ソースの失敗で他ソースを止めない
- 件数と時間の上限(GH#340): RSS は 1 ソース 1 回に取り込む新しい記事を [collect].rss_max_items
  まで(新しい側を残し、カーソルを進めずに次の実行で残りを取り直す)。保存しても無視される記事
  (取り込み済み・ほかのソースと同じ URL)は数えない。段全体は [collect].time_budget_s まで。
  次のソースと、次の短縮 URL の解決に進む前に確かめる。解決した結果は short_url_resolutions に
  残し、次の実行では解決し直さない(除いた記事の解決で毎回の時間を使い切らない)。ソースの途中で達したら、解決した分を
  保存してカーソルを進めず、残りのソースとともに次の実行へ回す。ソースは前回の取得が古い順に
  回す(打ち切りで回れなかったソースが次の実行で先になる)
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.config import CollectConfig
from app.connectors.base import Connector, FatalError, FetchStats, RawItem, RetryableError
from app.db import insert_item_with_dedup, record_budget_cut
from app.models import RAW_CONTENT_MAX_CHARS, Source
from app.normalize import (
    SHORT_URL_HOSTS,
    UrlError,
    content_hash,
    is_dedup_eligible,
    normalize_text_for_hash,
    normalize_url,
)

_NOW_UTC = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"


@dataclass(frozen=True)
class _Prepared:
    """保存の前の準備(短縮 URL の解決を済ませた記事と、上限で次の実行へ回した数)。"""

    items: list[tuple[RawItem, str | None]]  # (記事, canonical_url。正規化できなければ None)
    known: int = 0  # 保存しても無視されるので除いた記事(取り込み済み・ほかのソースと同じ URL)
    capped: int = 0  # RSS の件数の上限で次の実行へ回した記事
    timed_out: bool = False  # 段の時間の上限に達し、解決していない記事を次の実行へ回した


def collect_all(
    conn: sqlite3.Connection,
    connectors: dict[str, Connector],
    logger: logging.Logger,
    *,
    run_id: str | None = None,
    resolve_short: Callable[[str], str] | None = None,
    limits: CollectConfig | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """有効なソースを順に取り込む。limits は件数と時間の上限(None = 既定の [collect])。"""
    limits = limits or CollectConfig()
    # 前回の取得(collect_runs の最後の行)が古い順。取得したことの無いソースが先、同じなら id 順。
    # 時間の上限で回れなかったソースが次の実行で先になる(毎回 id 順だと後ろのソースが飢える)
    rows = conn.execute(
        """
        SELECT * FROM sources WHERE enabled = 1 AND type <> 'manual'
        ORDER BY (SELECT max(id) FROM collect_runs WHERE collect_runs.source_id = sources.id), id
        """
    ).fetchall()
    deadline = clock() + limits.time_budget_s

    def cut(deferred: int) -> None:
        logger.warning(
            f"collect の段が時間の上限({limits.time_budget_s} 秒)に達した。"
            f"残りの {deferred} ソースは次の実行へ回す",
            extra={"run_id": run_id, "stage": "collect", "extra": {"deferred": deferred}},
        )
        record_budget_cut(conn, "collect_time", deferred=deferred, run_id=run_id)

    for index, row in enumerate(rows):
        if clock() >= deadline:
            cut(len(rows) - index)
            return
        source = Source.from_row(row)
        connector = connectors.get(source.type)
        if connector is None:
            logger.warning(
                "コネクタ未実装のためスキップ",
                extra={"run_id": run_id, "stage": "collect", "source_id": source.id},
            )
            continue
        timed_out = _collect_source(
            conn,
            source,
            connector,
            logger,
            run_id=run_id,
            resolve_short=resolve_short,
            rss_max_items=limits.rss_max_items,
            deadline=deadline,
            clock=clock,
        )
        if timed_out:  # ソースの途中で達した。そのソースの残りも次の実行へ回す
            cut(len(rows) - index)
            return


def _collect_source(
    conn: sqlite3.Connection,
    source: Source,
    connector: Connector,
    logger: logging.Logger,
    *,
    run_id: str | None,
    resolve_short: Callable[[str], str] | None,
    rss_max_items: int,
    deadline: float,
    clock: Callable[[], float],
) -> bool:
    """1 ソースを取り込む。段の時間の上限にソースの途中で達したら True(残りは次の実行へ)。"""
    log_ctx = {"run_id": run_id, "stage": "collect", "source_id": source.id}
    run_row_id = conn.execute(
        "INSERT INTO collect_runs (source_id) VALUES (?)", (source.id,)
    ).lastrowid

    fetched: FetchStats | None = None  # fetch が返したあとの失敗(保存の失敗)で台帳に残す読取
    try:
        cursor = json.loads(source.cursor) if source.cursor else {}
        result = connector.fetch(source, cursor)
        fetched = result.stats
        partial_error = (
            None if result.partial_error is None else _safe_error_text(result.partial_error)
        )
        if source.type == "rss":
            prepared = _prepare_rss(
                conn, source, result.items, rss_max_items, resolve_short, deadline, clock
            )
        else:
            prepared = _prepare(conn, result.items, resolve_short, deadline, clock)
        next_cursor = result.next_cursor
        if prepared.capped or prepared.timed_out:
            # 進めると 304(X は since_id)で残りを取り直せない。今のカーソルのまま次の実行で
            # 読み直し、保存したぶんは既知として除いて残りを取る(重複は UNIQUE で吸収。設計 02 §1)
            next_cursor = cursor
        counts = _store_result(
            conn,
            source,
            prepared.items,
            next_cursor,
            result.moved_url,
            partial_error=partial_error,
            budget_exhausted=result.budget_exhausted,
            skipped_slots=result.skipped_slots,
        )
    except Exception as e:
        # 分類済み(Retryable/Fatal)以外の想定外例外もソース単位で隔離する
        # (1本の壊れたフィード・コネクタのバグで他ソースの collect を止めない。
        # 設計 00 §6 / 02 §1 の「他ソースは必ず継続」の保証)
        _record_failure(conn, source, run_row_id, e, fetched)
        log = logger.warning if isinstance(e, RetryableError | FatalError) else logger.error
        log(
            f"collect 失敗。ソースをスキップ: {_safe_error_text(e)}",
            extra=log_ctx,
            exc_info=not isinstance(e, RetryableError | FatalError),
        )
        return False

    counts["ignored"] += prepared.known  # 保存の前に除いた(INSERT OR IGNORE と同じ結果)
    if prepared.capped:
        logger.warning(
            f"RSS の 1 回の上限({rss_max_items} 件)を超えた。新しい側を取り込み、"
            f"残りの {prepared.capped} 件は次の実行へ回す",
            extra={**log_ctx, "extra": {"deferred": prepared.capped}},
        )
        record_budget_cut(
            conn, "rss_items", deferred=prepared.capped, run_id=run_id, source_id=source.id
        )

    # 部分取得のあとの失敗も run は error で残す。読み取りの費用は掛かっているので件数・費用は
    # 記録する(doctor #9 の当月の集計に入れる。GH#183)
    conn.execute(
        f"""
        UPDATE collect_runs
        SET finished_at = {_NOW_UTC}, status = ?, error = ?, items_fetched = ?, api_reads = ?,
            estimated_cost_usd = ?
        WHERE id = ?
        """,
        (
            "ok" if partial_error is None else "error",
            partial_error,
            result.stats.items_fetched,
            result.stats.api_reads,
            result.stats.estimated_cost_usd,  # X は api_reads×単価、RSS は 0.0(設計 02 §2)
            run_row_id,
        ),
    )
    extra = {
        **log_ctx,
        "extra": {
            "http_status": result.stats.http_status,
            "duration_ms": result.stats.duration_ms,
            **counts,
        },
    }
    if result.budget_exhausted:
        logger.info("当日の読取の枠が残っていないため読まず。ソースの状態は変えない", extra=extra)
    elif partial_error is None and result.skipped_slots:
        logger.info(
            "collect 完了。要求しなかったスロットがあるため、ソースの成否の状態は変えない",
            extra=extra,
        )
    elif partial_error is None:
        logger.info("collect 完了", extra=extra)
    else:
        logger.warning(
            f"collect は部分取得のあと失敗。取得ぶんは保存し失敗として記録: {partial_error}",
            extra=extra,
        )
    return prepared.timed_out


def _store_result(
    conn: sqlite3.Connection,
    source: Source,
    prepared: list[tuple[RawItem, str | None]],
    next_cursor: dict,
    moved_url: str | None,
    *,
    partial_error: str | None,
    budget_exhausted: bool = False,
    skipped_slots: bool = False,
) -> dict[str, int]:
    """準備した全 RawItem の INSERT とカーソル保存を同一トランザクションで行う。

    prepared は (記事, canonical_url) の列。短縮 URL の解決(HTTP の待ち・再試行)は準備の段で
    トランザクションの前に済ませてある(GH#179)。書き込みロックを持ったまま待つと、並行する
    受信デーモンの記帳が busy_timeout を超えて失敗しうるため。

    moved_url は今回の取得の恒久移転の先(無ければ NULL に戻す。GH#150)。
    partial_error は部分取得のあとの失敗の文(GH#183)。あれば items とカーソルは保存しつつ、
    ソースは失敗として数える(連続失敗の警告に載せる。成功の時刻と移転の記録は変えない)。
    budget_exhausted は X の当日の読取の枠が残っておらず要求しなかった実行(GH#178)。カーソルだけを
    保存し、成功の時刻・直近のエラー・連続失敗数は変えない(復旧を確かめていないので、失敗の続きを
    成功で消さない)。skipped_slots は X 検索で要求しなかったスロットを残した正常な終わり(GH#331)。
    items とカーソルを保存し、ソースの成否の状態は budget_exhausted と同じく変えない(要求しなかった
    スロットの失敗の続きを、ほかのスロットの成功で消さない)。
    """
    counts = {"inserted": 0, "ignored": 0, "duplicate": 0, "error": 0}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for raw, canonical in prepared:
            counts[_store_item(conn, source, raw, canonical)] += 1
        if partial_error is None and (budget_exhausted or skipped_slots):
            conn.execute(
                "UPDATE sources SET cursor = ? WHERE id = ?", (json.dumps(next_cursor), source.id)
            )
        elif partial_error is None:
            conn.execute(
                f"""
                UPDATE sources
                SET cursor = ?, last_success_at = {_NOW_UTC}, consecutive_failures = 0,
                    last_error = NULL, moved_url = ?
                WHERE id = ?
                """,
                (json.dumps(next_cursor), moved_url, source.id),
            )
        else:
            conn.execute(
                f"""
                UPDATE sources
                SET cursor = ?, last_error_at = {_NOW_UTC}, last_error = ?,
                    consecutive_failures = consecutive_failures + 1
                WHERE id = ?
                """,
                (json.dumps(next_cursor), partial_error, source.id),
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return counts


def _prepare(
    conn: sqlite3.Connection,
    items: list[RawItem],
    resolve_short: Callable[[str], str] | None,
    deadline: float,
    clock: Callable[[], float],
) -> _Prepared:
    """記事を並びのまま正規化する。短縮 URL の解決の前に段の時間の上限を確かめる。

    上限に達したら、解決した記事までを返して残りを次の実行へ回す(処理中の 1 件は切らない。
    1 件の解決は共通 HTTP クライアントの 60 秒まで。設計 05 §8)。解決が UrlError 以外で失敗した
    ときは、今までどおりソースの失敗になる(何も保存せず、カーソルも進めない)。
    """
    prepared: list[tuple[RawItem, str | None]] = []
    for raw in items:
        if not _needs_resolution(raw, resolve_short):
            prepared.append((raw, _canonical_url(raw, resolve_short)))
            continue
        canonical = _resolved_before(conn, raw)
        if canonical is None:
            if clock() >= deadline:
                return _Prepared(prepared, timed_out=True)
            canonical = _resolve(conn, raw, resolve_short)
        prepared.append((raw, canonical))
    return _Prepared(prepared)


def _prepare_rss(
    conn: sqlite3.Connection,
    source: Source,
    items: list[RawItem],
    limit: int,
    resolve_short: Callable[[str], str] | None,
    deadline: float,
    clock: Callable[[], float],
) -> _Prepared:
    """RSS の記事を、保存すると新しい行になるものだけ limit 件まで、新しい側から準備する。

    保存しても無視される記事(取り込み済み・ほかのソースと同じ URL)は除き、上限にも数えない。
    数えると、上限を超えるフィードで同じ記事が毎回の枠を埋め、残りがいつまでも入らない。
    既知かは短縮 URL の解決の前に安く確かめる: guid(UNIQUE(source_id, external_id))、短縮 URL
    でない記事は正規化した URL(UNIQUE(canonical_url))、短縮 URL の記事はこのソースが前に
    保存した元の URL と、前に解決した結果(short_url_resolutions)。短縮 URL を解決した後も、
    ほかのソースと同じ URL なら除く(解決の結果を残すので、次の実行では解決し直さずに除く)。
    公開日時の新しい順に見る(フィードの並びは新しい順とは限らない。日時の無い entry は古い側と
    し、その中はフィードの並び)。保存はフィードの並びに戻す。
    """
    # 安定ソートなので、同じ日時・日時の無い entry どうしはフィードの並びのまま
    ordered = sorted(
        enumerate(items),
        key=lambda pair: (pair[1].published_at is not None, pair[1].published_at or 0),
        reverse=True,
    )
    kept: list[tuple[int, RawItem, str | None]] = []
    known = 0
    timed_out = False
    position = 0  # 次に見る ordered の位置。打ち切ったら、そこから後ろを次の実行へ回す
    while position < len(ordered) and len(kept) < limit:
        index, raw = ordered[position]
        if _known_without_resolution(conn, source, raw, resolve_short):
            known += 1
            position += 1
            continue
        if _needs_resolution(raw, resolve_short):
            canonical = _resolved_before(conn, raw)
            if canonical is None:
                if clock() >= deadline:
                    timed_out = True
                    break
                canonical = _resolve(conn, raw, resolve_short)
            if _url_taken(conn, canonical or raw.url):  # 解決したらほかのソースと同じ URL
                known += 1
                position += 1
                continue
        else:
            canonical = _canonical_url(raw, None)
        kept.append((index, raw, canonical))
        position += 1
    # 打ち切った後ろのうち、安く既知と分かるものは回した数に入れない(次の実行でも除く)
    rest = [raw for _, raw in ordered[position:]]
    deferred = sum(
        not _known_without_resolution(conn, source, raw, resolve_short) for raw in rest
    )
    kept.sort(key=lambda entry: entry[0])
    return _Prepared(
        [(raw, canonical) for _, raw, canonical in kept],
        known=known + len(rest) - deferred,
        capped=0 if timed_out else deferred,
        timed_out=timed_out and deferred > 0,
    )


def _needs_resolution(raw: RawItem, resolve_short: Callable[[str], str] | None) -> bool:
    """正規化に短縮 URL の解決(HTTP)が要る記事か(u1 手順1。設計 01 §4)。"""
    if resolve_short is None:
        return False
    try:
        return urlsplit(raw.url.strip()).hostname in SHORT_URL_HOSTS
    except ValueError:
        return False  # 分解できない URL は解決せずに error の行になる


def _known_without_resolution(
    conn: sqlite3.Connection,
    source: Source,
    raw: RawItem,
    resolve_short: Callable[[str], str] | None,
) -> bool:
    """短縮 URL を解決せずに、保存しても無視されると分かる記事か(取り込み済みなど)。"""
    if raw.external_id is not None and conn.execute(
        "SELECT 1 FROM items WHERE source_id = ? AND external_id = ?",
        (source.id, raw.external_id),
    ).fetchone():
        return True
    if _needs_resolution(raw, resolve_short):
        # 解決先は HTTP が要るので、このソースが前に保存した元の URL と、前に解決した結果で見る
        if conn.execute(
            "SELECT 1 FROM items WHERE source_id = ? AND url = ?", (source.id, raw.url)
        ).fetchone():
            return True
        canonical = _resolved_before(conn, raw)
        return canonical is not None and _url_taken(conn, canonical)
    return _url_taken(conn, _canonical_url(raw, None) or raw.url)


def _resolved_before(conn: sqlite3.Connection, raw: RawItem) -> str | None:
    """前の実行で解決した短縮 URL の canonical_url(short_url_resolutions。無ければ None)。"""
    row = conn.execute(
        "SELECT canonical_url FROM short_url_resolutions WHERE url = ?", (raw.url.strip(),)
    ).fetchone()
    return None if row is None else row["canonical_url"]


def _resolve(
    conn: sqlite3.Connection, raw: RawItem, resolve_short: Callable[[str], str] | None
) -> str | None:
    """短縮 URL を解決して正規化し、結果を残す(次の実行で解決し直さない。GH#340)。

    残すのは解決できた結果だけ。UrlError(解決先が分解できない等)は None で、記事は error の行
    になるので、次の実行は元の URL で取り込み済みと分かる。通信の失敗(Retryable / Fatal)は
    残さずに送出し、今までどおりソースの失敗になる(次の実行で解決し直す)。
    書き込みは 1 行の INSERT で、書き込みのトランザクションを HTTP の待ちの間は持たない(GH#179)。
    """
    canonical = _canonical_url(raw, resolve_short)
    if canonical is not None:
        conn.execute(
            "INSERT OR REPLACE INTO short_url_resolutions (url, canonical_url) VALUES (?, ?)",
            (raw.url.strip(), canonical),
        )
    return canonical


def _url_taken(conn: sqlite3.Connection, canonical: str) -> bool:
    """canonical_url がすでにある(UNIQUE なので保存しても無視される)。

    正規化できない URL は error の行が生の URL を canonical_url に使う(_store_item)。
    """
    return conn.execute(
        "SELECT 1 FROM items WHERE canonical_url = ?", (canonical,)
    ).fetchone() is not None


def _canonical_url(raw: RawItem, resolve_short: Callable[[str], str] | None) -> str | None:
    """u1 の canonical_url(短縮 URL の解決を含む)。正規化できない URL は None。"""
    try:
        return normalize_url(raw.url, resolve_short=resolve_short)
    except UrlError:
        return None


def _store_item(
    conn: sqlite3.Connection,
    source: Source,
    raw: RawItem,
    canonical: str | None,
) -> str:
    if canonical is None:
        # scheme 不正等はアイテムごと reject して status='error' で記録(設計 01 §4 手順0)
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO items
              (source_id, external_id, url, canonical_url, title, author, published_at,
               content_hash, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, '', 'error')
            """,
            (
                source.id,
                raw.external_id,
                raw.url,
                raw.url,  # 正規化不能のため生 URL を UNIQUE キーに使う
                raw.title,
                raw.author,
                _iso(raw),
            ),
        )
        return "error" if cursor.rowcount else "ignored"

    normalized_text = normalize_text_for_hash(raw.title, raw.raw_content)
    external_id = raw.external_id
    if external_id is None and source.type == "rss":
        # RSS で guid が無い場合は正規化 URL の hash にフォールバック(設計 02 §3)
        external_id = "urlhash:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    outcome = insert_item_with_dedup(
        conn,
        source_id=source.id,
        external_id=external_id,
        url=raw.url,
        canonical_url=canonical,
        title=raw.title,
        author=raw.author,
        published_at=_iso(raw),
        raw_payload=_raw_payload(raw),
        content_hash=content_hash(normalized_text),
        dedup_eligible=is_dedup_eligible(normalized_text),
    )
    return outcome.outcome


def _raw_payload(raw: RawItem) -> str | None:
    """items.raw_payload の JSON 形(設計 01 §6)。無ければ NULL。"""
    payload: dict[str, object] = {}
    if raw.raw_content:
        payload["raw_content"] = raw.raw_content[:RAW_CONTENT_MAX_CHARS]
    if raw.meta:
        payload["meta"] = raw.meta
    return json.dumps(payload, ensure_ascii=False) if payload else None


def _iso(raw: RawItem) -> str | None:
    if raw.published_at is None:
        return None
    return raw.published_at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_error_text(error: Exception) -> str:
    """例外の __str__ 自体が壊れていても隔離を崩さない(分類外例外も扱うため)。"""
    try:
        return f"{type(error).__name__}: {error}"
    except Exception:
        return type(error).__name__


def _record_failure(
    conn: sqlite3.Connection,
    source: Source,
    run_row_id: int,
    error: Exception,
    fetched: FetchStats | None = None,
) -> None:
    """ソースの失敗を記録する。取得ぶんは保存せず、カーソルも据え置く。

    失敗の行にも、それまでに読んだ課金対象の件数と費用を残す(GH#178)。X の日次の読取上限は
    台帳の当日の合計から残りを出すので、漏れると同じ日の再実行が上限を超えて読む(doctor #9 の
    当月の費用も過少になる)。fetched = fetch は返したが保存で失敗したときの取得の統計。
    X の部分取得のあとの認証の失敗は、それまでの読取数と費用を FatalError に添えて来る。
    """
    message = _safe_error_text(error)
    if fetched is not None:
        api_reads, cost = fetched.api_reads, fetched.estimated_cost_usd
    elif isinstance(error, FatalError):
        api_reads, cost = error.api_reads, error.estimated_cost_usd
    else:
        api_reads, cost = 0, 0.0
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            f"""
            UPDATE collect_runs
            SET finished_at = {_NOW_UTC}, status = 'error', error = ?, api_reads = ?,
                estimated_cost_usd = ?
            WHERE id = ?
            """,
            (message, api_reads, cost, run_row_id),
        )
        conn.execute(
            f"""
            UPDATE sources
            SET last_error_at = {_NOW_UTC}, last_error = ?,
                consecutive_failures = consecutive_failures + 1
            WHERE id = ?
            """,
            (message, source.id),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
