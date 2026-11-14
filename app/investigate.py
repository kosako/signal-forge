"""出来事の調査(GH#415。設計 06 §9)。

朝のダイジェストの「📰 今日の IT ニュース」の出来事 1 つを、報じた記事をまとめて材料にして調べる。
何が起きたか・経緯と原因・影響・報じ方の違い・X の反応・まだ分からないこと をカードとノートにする。

- 材料は手元の記事(出来事を報じた items)に、GDELT で探した報道(GH#415 の 2 本目)と X の投稿
  (3 本目)を足す(ADR 0006)。検索の語は LLM が作り、外へ出す前にコードが演算子と使えない文字を
  外す。検索と本文の取得はコードが行い、どれが失敗しても手元の記事だけで続ける。X の読み取りには
  この機能の月の上限があり、台帳(investigation_x_reads)で数える
- 深掘り(記事 1 本が単位。dig_queue・dig_results)とは別の台帳(news_investigations)に持つ。同じ日に
  2 回掘らない規則やノートの名前が記事に結び付くので、代表の記事のふつうの深掘りを邪魔しないため
- 作るのは `intel dig-now` の中(app.deepdive が、今すぐの深掘りの依頼の後に呼ぶ)。ノートの公開と
  カードの送信は、深掘りと同じ流れ(vault の時間の上限・公開のロック・送り直し)に乗せる
- LLM には道具を持たせない。材料(DB の抽出本文)は nonce のフェンスで囲んでプロンプトに渡す
- 主張の番号([1])は材料にあるものだけを残し、X の反応は X の投稿の材料を根拠にしたものだけを残す
- 材料のリンクは items の正規 URL だけ(LLM の出力の URL は使わない)
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.analysis.llm import LLMEngine, LLMError, complete_with_retry
from app.config import Config
from app.connectors.gdelt import GdeltArticle, GdeltError, search_articles
from app.connectors.google_news import GoogleNewsError, NewsHeadline, search_headlines
from app.connectors.x_search import END_TIME_LAG, XPostsError, XPostsResult
from app.delivery.telegram import escape_and_truncate, escape_attr
from app.export.neutralize import neutralize_markdown
from app.export.obsidian import FORBIDDEN_IN_SLUG, render_frontmatter
from app.extraction.extractor import fetch_article_text
from app.http import HttpClient
from app.models import NewsEntry, link_host, parse_news_json
from app.prompting import fill_template, load_writing_guide, sanitize, sanitize_body

PROMPT_VERSION = "investigate-v001"
QUERY_PROMPT_VERSION = "investigate-query-v001"  # 検索の語を作る(GH#415 の 2 本目)
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# 知らせ(固定の文言だけ。外部由来の見出しを入れない。今すぐの深掘りの知らせと同じ考え)
FAILED_NOTICE = "⚠️ {date} の今日の IT ニュースの {index} 件目の調査を作れませんでした"
NO_MATERIAL_NOTICE = (
    "⚠️ {date} の今日の IT ニュースの {index} 件目は、本文のある材料が無いため調査できませんでした"
)

# X の投稿の host(材料の種別を決める。trend のリンクの並べ方と同じ)
_POST_HOSTS = frozenset({"x.com", "twitter.com", "mobile.twitter.com"})
_POST_KINDS = frozenset({"post", "searched_post"})  # X の反応の根拠にできる材料
_KIND_LABELS = {
    "article": "記事",
    "post": "X の投稿",
    "searched_post": "X の投稿(検索)",  # X の反応の検索で読んだ投稿(GH#415 の 3 本目)
    "news": "報道(検索)",
    "headline": "報道の見出し(検索)",  # Google ニュースの予備。本文は無い
}
_ENGINE_LABELS = {"gdelt": "GDELT", "google_news": "Google ニュース"}

# GDELT の検索の語(外へ出す前の検査)。英数字と一部の記号だけを残し、演算子と短い語を外す
_NEWS_QUERY_MAX = 100
_NEWS_QUERY_TOKENS_MAX = 6
_NEWS_TOKEN = re.compile(r'"[^"]*"|\S+')
_NEWS_DISALLOWED = re.compile(r"[^A-Za-z0-9 .&'+\-]")
_NEWS_OPERATORS = frozenset({"AND", "OR", "NOT"})
_NEWS_MIN_WORD = 3  # GDELT は 3 文字未満の語を受け付けない
_NEWS_WINDOW_BEFORE = timedelta(days=2)  # 検索の範囲(digest の日の 2 日前から)
# Google ニュース(予備)の検索の語。日本語の語を拾うので文字の種類は広く、演算子と記号を外す
_JA_QUERY_MAX = 60
_JA_QUERY_TOKENS_MAX = 4
_JA_MIN_WORD = 2
# X の検索の語(3 本目)。語は AND で結ばれるので少なめにする。演算子はコードが付ける除外句だけ
_X_QUERY_MAX = 60
_X_QUERY_TOKENS_MAX = 3
# search/recent が読めるのは直近 7 日。要求までの時間と時計のずれに余裕を取る
_X_MAX_AGE = timedelta(days=7) - timedelta(minutes=10)
_X_TITLE_MAX = 60  # 投稿の材料の題(本文の先頭。カードと材料の一覧で使う)

# カードの各欄の上限(UTF-16 の code unit。escape_and_truncate で切る)。どれも LLM の出力か外部由来で
# 長さを検証していない。上限いっぱいでも 1 通が Telegram の 4096 字に収まる
CARD_TITLE_MAX = 80
CARD_LABEL_MAX = 60
CARD_WHAT_MAX = 400
CARD_BACKGROUND_MAX = 450
CARD_IMPACT_MAX = 400
CARD_COVERAGE_MAX = 450
CARD_STANCE_MAX = 30
CARD_REACTION_MAX = 200
CARD_UNKNOWN_MAX = 120
CARD_MATERIAL_HOST_MAX = 40
CARD_MATERIAL_TITLE_MAX = 40
CARD_MATERIALS_MAX = 10
REACTIONS_MAX = 3
REACTION_SOURCES_MAX = 5  # 論調 1 つに付ける根拠の投稿の番号の上限(カードの長さの歯止め)
# 検索で読んだ X の投稿は、カードでは番号のリンクを 1 行に並べる(題が本文の先頭で、行ごとに並べると
# 長い)。主張に使った投稿だけで、X の反応の番号の上限と同じ数まで
CARD_POSTS_MAX = 15
UNKNOWNS_MAX = 3

# 主張の番号。`[1]`・`[1, 3]`・`[1、3]` を読み、材料にある番号だけを `[1][3]` の形で残す
_CITATION = re.compile(r"\[(\d{1,3}(?:\s*[,、]\s*\d{1,3})*)\]")
_CITATION_SPLIT = re.compile(r"\s*[,、]\s*")


class NoMaterialError(Exception):
    """本文のある材料が無い(見出しだけでは調べられない)。"""


class SearchPlan(BaseModel):
    """検索の語(prompts/investigate-query-v001.md)。

    news_query は GDELT(英語)、news_query_ja は予備の Google ニュース(日本語)、x_query は X の反応
    (GH#415 の 3 本目)で使う。
    """

    news_query: str = Field(max_length=500)
    news_query_ja: str = Field(default="", max_length=500)
    x_query: str = Field(default="", max_length=500)


SearchEngine = Literal["gdelt", "google_news"]


@dataclass(frozen=True)
class SearchOutcome:
    """報道の検索の結果(カードとノートに出す)。"""

    status: Literal["ok", "none", "failed", "off"]  # 足した / 見つからない / 失敗 / 検索しない
    query: str | None  # 最後に送った語(送っていなければ None)
    local: int  # 手元の材料の数
    found: int  # 検索で足した報道の数
    engine: SearchEngine | None = None  # 足した報道の出どころ(足していなければ None)


XStatus = Literal["ok", "none", "failed", "capped", "expired", "off"]
# X の検索(`app.connectors.x_search.search_posts` を session に結び付けたもの)。
# 引数は (語, *, start, end, max_results)。合成点(app.run)が作って渡す
XRead = Callable[..., XPostsResult]


@dataclass(frozen=True)
class XOutcome:
    """X の反応の検索の結果(GH#415 の 3 本目。カードとノートに出す)。

    status: 読んだ / 見つからない(語が残らないときも)/ 失敗 / 今月の上限で読まない /
    digest の日が検索できる直近 7 日の外 / 読まない(設定・キーが無い)。
    """

    status: XStatus
    query: str | None = None  # 送った語(送っていなければ None)
    found: int = 0  # 材料に足した投稿の数
    cap_usd: float = 0.0  # 上限の額(capped の表示に使う)


class InvestigationReaction(BaseModel):
    stance: str = Field(max_length=200)
    summary: str = Field(max_length=2000)
    sources: list[int] = Field(default_factory=list)


class InvestigationDraft(BaseModel):
    """LLM の出力(prompts/investigate-v001.md)。上限は暴走の歯止めで、表示の上限は別に切る。"""

    title_ja: str = Field(max_length=500)
    what_happened: str = Field(max_length=4000)
    background: str = Field(max_length=4000)
    impact: str = Field(max_length=4000)
    coverage: str = Field(max_length=4000)
    reactions: list[InvestigationReaction] = Field(default_factory=list, max_length=10)
    unknowns: list[str] = Field(default_factory=list, max_length=10)


@dataclass(frozen=True)
class InvestigationRequest:
    id: int
    digest_id: int
    digest_date: str
    event_index: int
    label: str
    summary: str
    item_ids: tuple[int, ...]
    started_at: str | None


@dataclass(frozen=True)
class Material:
    number: int  # プロンプトとカードで使う番号(1 から)
    item_id: int | None  # 手元の記事の items.id(検索で足した報道は None)
    kind: Literal["article", "post", "news", "headline", "searched_post"]
    title: str
    host: str
    url: str  # 手元の記事は items.canonical_url、検索の報道は GDELT の URL を正規化したもの
    published_at: str | None
    body: str
    # 手元の記事の原題(items.title)。title は分析の日本語の見出しを先にするので、検索した報道との
    # 題の重なり(転載)を見るときに両方を使う
    original_title: str = ""


@dataclass(frozen=True)
class GeneratedInvestigation:
    note_name: str
    note_markdown: str
    card_text: str


RequestOutcome = Literal["accepted", "requested", "running", "unsent", "done", "no_sources"]


# --- 依頼の台帳 ---


def event_of_digest(
    conn: sqlite3.Connection, digest_date: str, event_index: int
) -> tuple[int, NewsEntry] | None:
    """その日の digest の出来事(番号は 1 から)。digest・出来事が無ければ None。"""
    row = conn.execute(
        "SELECT id, trend_json FROM digests WHERE digest_date = ?", (digest_date,)
    ).fetchone()
    if row is None:
        return None
    entries = parse_news_json(row["trend_json"]) or []
    if not 1 <= event_index <= len(entries):
        return None
    return row["id"], entries[event_index - 1]


def request_investigation(
    conn: sqlite3.Connection, digest_id: int, event_index: int, entry: NewsEntry
) -> RequestOutcome:
    """出来事の調査を依頼する。同じ digest の同じ見出しの出来事は 1 回だけ作る。

    戻り値は accepted(受け付けた)・requested / running(受け付け済みで、待っている / 作っている)・
    unsent(作り終えたが、公開か送信がまだ)・done(カードを送った)・no_sources(材料の記事が無い)。
    失敗した依頼は、もう一度押せば受け付け直す。unsent は作り直さず、台帳から公開・送信を進める。
    1 つずつの文で書くので、呼び出し側のトランザクションの中でも外でも使える。
    """
    if not entry.source_ids:
        return "no_sources"
    inserted = conn.execute(
        "INSERT INTO news_investigations (digest_id, event_index, label, summary, item_ids) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT (digest_id, label) DO NOTHING",
        (digest_id, event_index, entry.label, entry.summary, json.dumps(entry.source_ids)),
    ).rowcount
    if inserted:
        return "accepted"
    row = conn.execute(
        "SELECT id, status, message_id FROM news_investigations"
        " WHERE digest_id = ? AND label = ?",
        (digest_id, entry.label),
    ).fetchone()
    if row["status"] == "failed":
        retried = conn.execute(
            "UPDATE news_investigations SET status = 'requested', "
            "requested_at = strftime('%Y-%m-%dT%H:%M:%SZ','now'), started_at = NULL, "
            "finished_at = NULL, failure = NULL WHERE id = ? AND status = 'failed'",
            (row["id"],),
        ).rowcount
        return "accepted" if retried else "requested"
    if row["status"] == "done" and row["message_id"] is None:
        return "unsent"
    return row["status"]


def has_investigation_requests(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM news_investigations WHERE status = 'requested' LIMIT 1"
        ).fetchone()
        is not None
    )


def has_unsent_investigations(conn: sqlite3.Connection) -> bool:
    """作り終えて、公開か送信がまだの調査があるか(中断した分の再開の合図)。"""
    return (
        conn.execute(
            "SELECT 1 FROM news_investigations WHERE status = 'done' AND message_id IS NULL "
            "LIMIT 1"
        ).fetchone()
        is not None
    )


_REQUEST_SELECT = (
    "SELECT n.id, n.digest_id, d.digest_date, n.event_index, n.label, n.summary, n.item_ids, "
    "n.started_at FROM news_investigations n JOIN digests d ON d.id = n.digest_id "
)


def _request_from_row(row: sqlite3.Row) -> InvestigationRequest:
    return InvestigationRequest(
        id=row["id"],
        digest_id=row["digest_id"],
        digest_date=row["digest_date"],
        event_index=row["event_index"],
        label=row["label"],
        summary=row["summary"],
        item_ids=tuple(json.loads(row["item_ids"])),
        started_at=row["started_at"],
    )


def interrupted_investigations(conn: sqlite3.Connection) -> list[InvestigationRequest]:
    """作り始めた印の残った依頼(`<DB>.dig-now.lock` の中で呼ぶ = 前の実行が途中で止まった)。"""
    rows = conn.execute(_REQUEST_SELECT + "WHERE n.status = 'running' ORDER BY n.id").fetchall()
    return [_request_from_row(row) for row in rows]


def claim_investigation(conn: sqlite3.Connection) -> InvestigationRequest | None:
    """受け付けた順に 1 件を取り出し、作り始めた印を付ける(compare-and-set)。無ければ None。"""
    while True:
        row = conn.execute(
            _REQUEST_SELECT + "WHERE n.status = 'requested' ORDER BY n.requested_at, n.id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        claimed = conn.execute(
            "UPDATE news_investigations SET status = 'running', "
            "started_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
            "WHERE id = ? AND status = 'requested'",
            (row["id"],),
        ).rowcount
        if claimed:
            fresh = conn.execute(_REQUEST_SELECT + "WHERE n.id = ?", (row["id"],)).fetchone()
            return _request_from_row(fresh)


def close_failed(conn: sqlite3.Connection, investigation_id: int, failure: str) -> None:
    conn.execute(
        "UPDATE news_investigations SET status = 'failed', failure = ?, "
        "finished_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ? AND status = 'running'",
        (failure[:200], investigation_id),
    )


def record_generated(
    conn: sqlite3.Connection, investigation_id: int, generated: GeneratedInvestigation
) -> None:
    """作ったノートとカードを台帳に先に記帳する。

    公開・送信は台帳から行う(深掘りの write-ahead と同じ)。
    """
    conn.execute(
        "UPDATE news_investigations SET status = 'done', note_name = ?, note_markdown = ?, "
        "card_text = ?, finished_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
        "WHERE id = ? AND status = 'running'",
        (
            generated.note_name,
            generated.note_markdown,
            generated.card_text,
            investigation_id,
        ),
    )


def x_month_cost(conn: sqlite3.Connection, now: datetime) -> float:
    """この機能の X の読み取りの、その月(UTC の暦月)の費用の見積もりの合計(上限の判定に使う)。

    前の月の予約の行(pending)もこの月に数える。予約の後に実行が止まった行は、送ったのが月をまたいだ
    後かもしれないため(上限を超えない側に数える。doctor #9 は予約した月だけに数える)。
    """
    month_start = now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month = month_start.strftime("%Y-%m")
    previous = (month_start - timedelta(days=1)).strftime("%Y-%m")
    return conn.execute(
        "SELECT coalesce(sum(estimated_cost_usd), 0) FROM investigation_x_reads "
        "WHERE searched_at LIKE ? || '%' OR (status = 'pending' AND searched_at LIKE ? || '%')",
        (month, previous),
    ).fetchone()[0]


def reserve_x_read(
    conn: sqlite3.Connection,
    investigation_id: int,
    *,
    searched_at: datetime,
    query: str,
    api_reads: int,
    unit_price_usd: float,
) -> int:
    """X の検索を送る前に、費用の台帳へ上限の件数で予約の行(pending)を書く。行の id を返す。

    送った後に実行が止まっても、予約の行が上限の件数のまま月の合計に数えられる(上限を超えない)。
    応答の後に settle_x_read で実際の数に直す。
    """
    return conn.execute(
        "INSERT INTO investigation_x_reads (investigation_id, searched_at, query, status, "
        "api_reads, estimated_cost_usd) VALUES (?, ?, ?, 'pending', ?, ?)",
        (
            investigation_id,
            searched_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            query,
            api_reads,
            round(api_reads * unit_price_usd, 6),
        ),
    ).lastrowid


def settle_x_read(
    conn: sqlite3.Connection,
    read_id: int,
    *,
    status: Literal["ok", "failed"],
    api_reads: int,
    unit_price_usd: float,
    settled_at: datetime,
) -> None:
    """予約の行を、応答の結果と課金されうる読取の数に直す(失敗も行を残す)。

    時刻は応答を受けた時刻に直す(予約と送信の間に月が変わったら、送った後の月に数える)。
    """
    conn.execute(
        "UPDATE investigation_x_reads SET status = ?, api_reads = ?, estimated_cost_usd = ?, "
        "searched_at = ? WHERE id = ? AND status = 'pending'",
        (
            status,
            api_reads,
            round(api_reads * unit_price_usd, 6),
            settled_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            read_id,
        ),
    )


# --- 材料 ---


def gather_materials(
    conn: sqlite3.Connection, request: InvestigationRequest
) -> list[Material]:
    """手元の材料(出来事を報じた記事)。並びは trend の LLM が並べた順(報道・公式が先)。

    消えた item は外す。本文は抽出本文(documents)で、無ければ空(題だけの材料)。
    """
    if not request.item_ids:
        return []
    placeholders = ",".join("?" * len(request.item_ids))
    rows = conn.execute(
        f"""
        SELECT items.id, items.canonical_url, items.title, items.published_at,
               (SELECT a.title_ja FROM analyses AS a WHERE a.item_id = items.id
                ORDER BY a.analyzed_at DESC, a.id DESC LIMIT 1) AS title_ja,
               documents.extracted_text
        FROM items LEFT JOIN documents ON documents.item_id = items.id
        WHERE items.id IN ({placeholders})
        """,
        request.item_ids,
    ).fetchall()
    by_id = {row["id"]: row for row in rows}
    materials: list[Material] = []
    for item_id in request.item_ids:
        row = by_id.get(item_id)
        if row is None:
            continue
        host = link_host(row["canonical_url"])
        materials.append(
            Material(
                number=len(materials) + 1,
                item_id=item_id,
                kind="post" if host in _POST_HOSTS else "article",
                title=(row["title_ja"] or row["title"] or "").strip(),
                original_title=(row["title"] or "").strip(),
                host=host,
                url=row["canonical_url"],
                published_at=row["published_at"],
                body=row["extracted_text"] or "",
            )
        )
    return materials


# --- 検索の語(GH#415 の 2 本目) ---


def plan_search(
    config: Config,
    engine_factory: Callable[[str], LLMEngine],
    request: InvestigationRequest,
    materials: list[Material],
    *,
    x: bool,
) -> SearchPlan | None:
    """報道と X の検索の語を LLM(effort low)に作らせる。作れなければ None。

    x = X を読める(設定とキーがある)か。報道も X も検索しないなら LLM を呼ばずに None を返す
    (このとき search_news・search_x は off を返す)。
    """
    if config.investigate.news_articles <= 0 and not (x and _x_enabled(config)):
        return None
    try:
        return complete_with_retry(
            engine_factory("low"),
            build_query_prompt(request, materials),
            SearchPlan,
            timeout_s=config.dig.timeout_s,
        )
    except LLMError:
        return None


# --- 報道の検索(GDELT。GH#415 の 2 本目) ---


def search_news(
    config: Config,
    http: HttpClient,
    request: InvestigationRequest,
    materials: list[Material],
    plan: SearchPlan | None,
    *,
    now: datetime | None = None,
    clock: Callable[[], float] = time.monotonic,
    search: Callable[..., list[GdeltArticle]] = search_articles,
    headlines: Callable[..., list[NewsHeadline]] = search_headlines,
    fetch: Callable[[HttpClient, str], str | None] = fetch_article_text,
    logger: logging.Logger | None = None,
) -> tuple[list[Material], SearchOutcome]:
    """同じ出来事の報道を探し、手元の材料の後ろに番号を続けて返す。

    まず GDELT で探して本文を取る。GDELT が断ったか、使える報道が無ければ、予備として Google
    ニュースの RSS の検索で見出しと媒体名を拾う(本文は無い。ADR 0006)。検索の語は plan_search が
    LLM に作らせたもので(None = 作れなかった)、外へ出す前に検査する。どれも使えなければ材料を
    足さない(調査は手元の記事だけで続ける)。
    配信記事の重なりと、手元の材料・足した報道と同じ host は外し、`[investigate].news_articles`
    本まで足す。GDELT の本文は `[investigate].news_fetch_budget_s` の間だけ取り、取れなかった
    報道は題だけの材料にする。

    検索と本文の取得は付け足しの材料なので、想定外の例外(壊れたリダイレクトの URL など)も、その
    検索・その記事の失敗として扱い、手元の材料での調査を止めない(例外の型だけをログに残す)。
    """
    log = logger or logging.getLogger(__name__)

    def attempt(
        what: str, call: Callable[[], object], expected: tuple[type[Exception], ...]
    ) -> object | None:
        try:
            return call()
        except expected:
            return None
        except Exception as e:  # 外部の応答の壊れ方は数えきれない。調査は手元の材料で続ける
            log.warning(f"出来事の調査の{what}で想定外の失敗: {type(e).__name__}")
            return None

    local = len(materials)
    limit = config.investigate.news_articles
    if limit <= 0:
        return [], SearchOutcome("off", None, local, 0)
    if plan is None:
        return [], SearchOutcome("failed", None, local, 0)
    now = now or datetime.now(UTC)
    hosts = {m.host for m in materials}
    # 手元の材料の題も入れる(手元の記事の転載が別の host から来ても、検索の枠を使わない)
    titles = {
        key
        for m in materials
        for key in (_title_key(m.title), _title_key(m.original_title))
        if key
    }
    searched = False  # どれかの検索が(見つからなくても)答えた
    sent: str | None = None

    def fresh(host: str, title: str) -> bool:
        key = _title_key(title)
        if not host or host in hosts or not key or key in titles:  # 配信記事の重なりを 1 本に
            return False
        hosts.add(host)
        titles.add(key)
        return True

    query = clean_news_query(plan.news_query)
    if query:
        sent = query
        start, end = search_window(request.digest_date, now)
        articles = attempt(
            "報道の検索", lambda: search(http, query, start=start, end=end), (GdeltError,)
        )
        if articles is not None:
            searched = True
            deadline = clock() + config.investigate.news_fetch_budget_s
            found: list[Material] = []
            for article in articles:
                if len(found) >= limit:
                    break
                host = link_host(article.url)
                if not fresh(host, article.title):
                    continue
                body = (
                    attempt(
                        # 取得の想定内の失敗は fetch が None で返す。それ以外(壊れたリダイレクトの
                        # ValueError など)は型をログに残して題だけの材料にする
                        "報道の本文の取得", lambda url=article.url: fetch(http, url), ()
                    )
                    if clock() < deadline
                    else None
                )
                found.append(
                    Material(
                        number=local + len(found) + 1,
                        item_id=None,
                        kind="news",
                        title=article.title,
                        host=host,
                        url=article.url,
                        published_at=_seen_at(article.seen_at),
                        body=body or "",
                    )
                )
            if found:
                return found, SearchOutcome("ok", query, local, len(found), "gdelt")

    query_ja = clean_ja_query(plan.news_query_ja)
    if query_ja:
        sent = query_ja
        # Google ニュースの期間は日付だけで、before はその日を含まない。digest の日の前後 2 日にする
        # (今の時刻に寄せると、当日の報道が外れる。UTC と JST の日付のずれも吸収する)
        day = datetime.strptime(request.digest_date, "%Y-%m-%d").date()
        results = attempt(
            "見出しの検索",
            lambda: headlines(
                http, query_ja, after=day - _NEWS_WINDOW_BEFORE, before=day + _NEWS_WINDOW_BEFORE
            ),
            (GoogleNewsError,),
        )
        if results is not None:
            searched = True
            found = []
            for headline in results:
                if len(found) >= limit:
                    break
                host = headline.outlet_host or link_host(headline.url)
                if not fresh(host, headline.title):
                    continue
                found.append(
                    Material(
                        number=local + len(found) + 1,
                        item_id=None,
                        kind="headline",
                        title=headline.title,
                        host=host,
                        url=headline.url,
                        published_at=headline.published_at,
                        body="",
                    )
                )
            if found:
                return found, SearchOutcome("ok", query_ja, local, len(found), "google_news")
    return [], SearchOutcome("none" if searched else "failed", sent, local, 0)


# --- X の反応(GH#415 の 3 本目) ---


def search_x(
    conn: sqlite3.Connection,
    config: Config,
    read: XRead | None,
    request: InvestigationRequest,
    materials: list[Material],
    plan: SearchPlan | None,
    *,
    now: datetime | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    logger: logging.Logger | None = None,
) -> tuple[list[Material], XOutcome]:
    """X の投稿を `[investigate].x_posts` 件まで読み、materials の後ろに番号を続けて返す。

    read は X の検索(`app.connectors.x_search.search_posts` を session に結び付けたもの。None = X の
    設定かキーが無い)。この機能の月の上限(`[investigate].x_monthly_cap_usd`)は、1 回分を足すと
    超えるなら読まない。送る前に上限の件数で台帳に予約の行を書き、応答の後に課金されうる読取の数に
    直す(失敗も残す。数が分からなければ上限の件数のまま)。手元の材料と同じ投稿と同じ文の投稿は
    外す。どの失敗でも手元の材料での調査を止めない。
    """
    log = logger or logging.getLogger(__name__)
    if read is None or not _x_enabled(config):
        return [], XOutcome("off")
    if plan is None:
        return [], XOutcome("failed")
    query = clean_x_query(plan.x_query)
    if not query:
        return [], XOutcome("none")
    now = now or clock()
    window = x_window(request.digest_date, now)
    if window is None:
        return [], XOutcome("expired")
    posts_max = config.investigate.x_posts
    unit = config.x.unit_price_usd
    cap = config.investigate.x_monthly_cap_usd
    if x_month_cost(conn, now) + posts_max * unit > cap + 1e-9:
        return [], XOutcome("capped", cap_usd=cap)
    start, end = window
    read_id = reserve_x_read(
        conn, request.id, searched_at=now, query=query, api_reads=posts_max, unit_price_usd=unit
    )

    def record(status: Literal["ok", "failed"], reads: int) -> None:
        settle_x_read(
            conn, read_id, status=status, api_reads=reads, unit_price_usd=unit,
            settled_at=clock(),
        )

    try:
        result = read(query, start=start, end=end, max_results=posts_max)
    except XPostsError as e:
        record("failed", e.api_reads)
        return [], XOutcome("failed", query)
    except Exception as e:  # 外部の応答の壊れ方は数えきれない。調査は手元の材料で続ける
        log.warning(f"出来事の調査の X の検索で想定外の失敗: {type(e).__name__}")
        record("failed", posts_max)  # 課金されたかが分からないので、上限の件数を数える
        return [], XOutcome("failed", query)
    record("ok", result.api_reads)
    seen = {m.url for m in materials if m.kind in _POST_KINDS}
    texts = {_title_key(m.body) for m in materials if m.kind in _POST_KINDS} - {""}
    found: list[Material] = []
    for post in result.posts:
        text_key = _title_key(post.text)
        # 手元の材料と同じ投稿と、同じ文の投稿(写し・bot の繰り返し)は 1 つにする
        if post.url in seen or (text_key and text_key in texts):
            continue
        seen.add(post.url)
        texts.add(text_key)
        found.append(
            Material(
                number=len(materials) + len(found) + 1,
                item_id=None,
                kind="searched_post",
                title=_one_line(post.text)[:_X_TITLE_MAX],
                host="x.com",
                url=post.url,
                published_at=post.created_at,
                body=post.text,
            )
        )
    return found, XOutcome("ok" if found else "none", query, len(found))


def _x_enabled(config: Config) -> bool:
    return config.investigate.x_posts > 0 and config.investigate.x_monthly_cap_usd > 0


def x_window(digest_date: str, now: datetime) -> tuple[datetime, datetime] | None:
    """X の検索の範囲(UTC)。報道と同じ範囲を、search/recent が読める直近 7 日に狭める。

    終わりは要求の時刻の 30 秒前まで(search/recent は 10 秒以上前を求める)。範囲が残らなければ
    None(digest の日が古い)。
    """
    start, end = search_window(digest_date, now)
    start = max(start, now - _X_MAX_AGE)
    end = min(end, now - END_TIME_LAG)
    if end - start < timedelta(minutes=1):
        return None
    return start, end


def build_query_prompt(request: InvestigationRequest, materials: list[Material]) -> str:
    nonce = secrets.token_hex(4)
    template = (_PROMPTS_DIR / f"{QUERY_PROMPT_VERSION}.md").read_text(encoding="utf-8")
    lines = [
        f"見出し: {sanitize(request.label, 100, nonce)}",
        f"要約: {sanitize(request.summary, 300, nonce)}",
        *(f"記事の題: {sanitize(m.title, 200, nonce)}" for m in materials if m.title),
    ]
    return fill_template(
        template,
        {
            "event": "\n".join(lines),
            "schema": json.dumps(SearchPlan.model_json_schema(), ensure_ascii=False),
            "nonce": nonce,
        },
    )


def clean_news_query(text: str) -> str:
    """GDELT に送る語を検査する。英数字と一部の記号だけを残し、演算子・3 文字未満の語を外す。

    `"..."` の語句はそのまま 1 つの語にする(閉じていない引用符は外す)。演算子の形の語(コロンを
    含む・`-` で始まる)は丸ごと捨てる(文字を外して別の語として送らない)。語の数と長さにも上限を
    付ける。何も残らなければ空文字(検索しない)。
    """
    flat = " ".join(text.split())
    if flat.count('"') % 2:
        flat = flat.replace('"', "")
    tokens: list[str] = []
    for token in _NEWS_TOKEN.findall(flat):
        if token.startswith('"'):
            phrase = " ".join(_NEWS_DISALLOWED.sub(" ", token[1:-1]).split())
            if len(phrase) >= _NEWS_MIN_WORD:
                tokens.append(f'"{phrase}"')
        elif ":" in token or token.startswith("-"):
            continue  # 演算子の形の語(sourcelang:・domain:・除外の -)は丸ごと捨てる
        else:
            word = _NEWS_DISALLOWED.sub("", token).strip(".&'+-")
            if len(word) >= _NEWS_MIN_WORD and word.upper() not in _NEWS_OPERATORS:
                tokens.append(word)
    query = ""
    for token in tokens[:_NEWS_QUERY_TOKENS_MAX]:
        candidate = f"{query} {token}".strip()
        if len(candidate) > _NEWS_QUERY_MAX:
            break
        query = candidate
    return query


def clean_ja_query(text: str) -> str:
    """Google ニュース(予備)に送る語を検査する。文字と数字(日本語を含む)と一部の記号だけを残す。

    演算子(AND・OR・NOT)と演算子の形の語(コロンを含む・`-` で始まる)は丸ごと捨てる。期間の
    after: / before: は送るときにコードが付ける。2 文字未満の語は捨て、4 語・60 字まで。
    """
    return _clean_words(text, _JA_QUERY_TOKENS_MAX, _JA_QUERY_MAX)


def clean_x_query(text: str) -> str:
    """X に送る語を検査する(GH#415 の 3 本目)。規則は clean_ja_query と同じで、3 語・60 字まで。

    X の演算子(`from:`・`lang:`・`-除外`・OR・括弧・引用符)は残さない。`#`・`@` は外して語にする。
    除外句(RT・リプライを除く)は送るときにコードが付ける。
    """
    return _clean_words(text, _X_QUERY_TOKENS_MAX, _X_QUERY_MAX)


def _clean_words(text: str, tokens_max: int, chars_max: int) -> str:
    flat = " ".join(text.replace('"', " ").split())
    query = ""
    for token in flat.split(" ")[: tokens_max * 2]:
        if ":" in token or token.startswith("-"):
            continue
        word = "".join(ch for ch in token if ch.isalnum() or ch in ".&+・ー")
        # 記号を外した後の語でも演算子を検査する(`(OR)` が `OR` として残らない)
        if len(word) < _JA_MIN_WORD or word.upper() in _NEWS_OPERATORS:
            continue
        candidate = f"{query} {word}".strip()
        if len(candidate) > chars_max or candidate.count(" ") >= tokens_max:
            break
        query = candidate
    return query


def search_window(digest_date: str, now: datetime) -> tuple[datetime, datetime]:
    """検索の範囲(UTC)。digest の日の 2 日前の 0 時から、翌日の終わりか今のうち早い方まで。"""
    day = datetime.strptime(digest_date, "%Y-%m-%d").replace(tzinfo=UTC)
    end = min(day + timedelta(days=2) - timedelta(seconds=1), now)
    return day - _NEWS_WINDOW_BEFORE, end


def _title_key(title: str) -> str:
    """配信記事の重なりを見分ける題の鍵(小文字の英数字だけの先頭 60 字)。"""
    return "".join(ch for ch in title.lower() if ch.isalnum())[:60]


def _seen_at(value: str) -> str | None:
    """GDELT の seendate(YYYYMMDDTHHMMSSZ)を ISO 8601 の UTC にする。読めなければ None。"""
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


# --- 生成 ---


def generate_investigation(
    config: Config,
    engine_factory: Callable[[str], LLMEngine],
    request: InvestigationRequest,
    materials: list[Material],
    *,
    search: SearchOutcome | None = None,
    x: XOutcome | None = None,
    now: datetime | None = None,
) -> GeneratedInvestigation:
    """材料から調査のカードとノートを作る。本文のある材料が無ければ NoMaterialError。

    search は報道の検索の結果、x は X の反応の検索の結果(カードの末尾とノートの frontmatter に
    出す)。None は検索しない扱い。
    """
    if sum(len(m.body.strip()) for m in materials) < config.dig.min_body_chars:
        raise NoMaterialError("本文のある材料が無い")
    engine = engine_factory(config.dig.reasoning_effort)
    prompt = build_prompt(config, request, materials)
    draft = complete_with_retry(
        engine, prompt, InvestigationDraft, timeout_s=config.dig.timeout_s
    )
    draft = clean_draft(draft, materials)
    created = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    name = note_name(request)
    search = search or SearchOutcome("off", None, len(materials), 0)
    x = x or XOutcome("off")
    return GeneratedInvestigation(
        note_name=name,
        note_markdown=render_note(request, draft, materials, created, search, x),
        card_text=render_card(request, draft, materials, search, x),
    )


def build_prompt(
    config: Config, request: InvestigationRequest, materials: list[Material]
) -> str:
    nonce = secrets.token_hex(4)
    template = (_PROMPTS_DIR / f"{PROMPT_VERSION}.md").read_text(encoding="utf-8")
    return fill_template(
        template,
        {
            "writing_guide": load_writing_guide(),  # 信頼される側(囲みの外。GH#381)
            "label": sanitize(request.label, 100, nonce),
            "summary": sanitize(request.summary, 300, nonce),
            "materials": "\n---\n".join(
                _material_block(m, config.investigate.source_max_chars, nonce) for m in materials
            ),
            "schema": json.dumps(InvestigationDraft.model_json_schema(), ensure_ascii=False),
            "nonce": nonce,
        },
    )


def _material_block(material: Material, limit: int, nonce: str) -> str:
    head = " | ".join(
        [
            f"[{material.number}] 種別: {_KIND_LABELS[material.kind]}",
            f"媒体: {sanitize(material.host, 100, nonce) or '(不明)'}",
            f"題: {sanitize(material.title, 200, nonce) or '(なし)'}",
            f"日時: {sanitize(material.published_at, 30, nonce) or '(不明)'}",
        ]
    )
    body = material.body
    # 切り詰めてから除去する(深掘りの本文と同じ順。除去で範囲がずれない)
    if len(body) > limit:
        text = sanitize_body(body[:limit], nonce) + "\n(…以降は長さ上限で切り詰め)"
    else:
        text = sanitize_body(body, nonce) or "(本文なし)"
    return f"{head}\n本文:\n{text}"


def clean_draft(draft: InvestigationDraft, materials: list[Material]) -> InvestigationDraft:
    """材料に無い番号を外し、X の反応は X の投稿を根拠にしたものだけを残す。"""
    valid = {m.number for m in materials}
    posts = {m.number for m in materials if m.kind in _POST_KINDS}
    reactions = []
    for reaction in draft.reactions:
        # カードとノートに出すのは先頭の REACTION_SOURCES_MAX 件だけなので、そこで切る
        # (カードの材料のリンクを、出さない番号に使わない)
        sources = [n for n in dict.fromkeys(reaction.sources) if n in posts][:REACTION_SOURCES_MAX]
        # 根拠の番号は sources が正(カードとノートはそこから付ける)。論調と要約の中の番号は外し、
        # 材料に無い番号を出さず、重ねもしない
        stance = _without_citations(reaction.stance)
        summary = _without_citations(reaction.summary)
        if sources and stance and summary:
            reactions.append(
                InvestigationReaction(stance=stance, summary=summary, sources=sources)
            )
    return InvestigationDraft(
        title_ja=_without_citations(draft.title_ja),
        what_happened=_clean_citations(draft.what_happened, valid),
        background=_clean_citations(draft.background, valid),
        impact=_clean_citations(draft.impact, valid),
        coverage=_clean_citations(draft.coverage, valid),
        reactions=reactions[:REACTIONS_MAX],
        unknowns=[u for u in map(_without_citations, draft.unknowns) if u][:UNKNOWNS_MAX],
    )


def _without_citations(text: str) -> str:
    """番号を求めていない欄(題・論調・要約・まだ分からないこと)。1 行にし、LLM が書いた番号を外す
    (材料に無い番号を出さない。X の反応の番号は sources から付ける)。"""
    return _CITATION.sub("", _one_line(text)).strip()


def _one_line(text: str) -> str:
    """見出しや箇条書きの 1 行に入れる欄。改行を空白にする(ノートの構造を壊さない)。"""
    return " ".join(text.split())


def _clean_citations(text: str, valid: set[int]) -> str:
    def keep(match: re.Match[str]) -> str:
        numbers = [int(n) for n in _CITATION_SPLIT.split(match.group(1))]
        return "".join(f"[{n}]" for n in dict.fromkeys(numbers) if n in valid)

    return _CITATION.sub(keep, text).strip()


def cited_numbers(draft: InvestigationDraft) -> set[int]:
    texts = [draft.what_happened, draft.background, draft.impact, draft.coverage]
    numbers = {
        int(n)
        for text in texts
        for match in _CITATION.finditer(text)
        for n in _CITATION_SPLIT.split(match.group(1))
    }
    return numbers | {n for reaction in draft.reactions for n in reaction.sources}


# --- カードとノート ---


def note_name(request: InvestigationRequest) -> str:
    """`{YYYYMMDD}-調査{id}-{見出しの slug}`。

    台帳の id を入れて、同じ日の別の出来事と重ならない。
    """
    slug = FORBIDDEN_IN_SLUG.sub("-", request.label).strip("-")[:40] or "untitled"
    return f"{request.digest_date.replace('-', '')}-調査{request.id}-{slug}"


def _card_materials(
    draft: InvestigationDraft, materials: list[Material]
) -> tuple[list[Material], list[Material]]:
    """カードに並べる材料。主張に使った材料だけ(1 つも無ければ記事の材料)、上限まで。

    (行ごとに並べる材料, 番号のリンクを 1 行に並べる検索の投稿)を返す。上限で切るときは、X の反応の
    番号(カードに必ず出る。3 個 × 5 件 = 投稿の行の上限)を先に取り、残りの枠に本文の番号を入れる。
    並びは番号の順。
    """
    cited = cited_numbers(draft)
    reacted = {n for r in draft.reactions[:REACTIONS_MAX] for n in r.sources[:REACTION_SOURCES_MAX]}
    chosen = [m for m in materials if m.number in cited]
    if not chosen:
        return [m for m in materials if m.kind == "article"][:CARD_MATERIALS_MAX], []

    def pick(group: list[Material], limit: int) -> list[Material]:
        first = sorted(group, key=lambda m: (m.number not in reacted, m.number))[:limit]
        return sorted(first, key=lambda m: m.number)

    return (
        pick([m for m in chosen if m.kind != "searched_post"], CARD_MATERIALS_MAX),
        pick([m for m in chosen if m.kind == "searched_post"], CARD_POSTS_MAX),
    )


def render_card(
    request: InvestigationRequest,
    draft: InvestigationDraft,
    materials: list[Material],
    search: SearchOutcome | None = None,
    x: XOutcome | None = None,
) -> str:
    """Telegram のカード(parse_mode=HTML)。LLM の出力と外部由来の値は escape して上限で切る。"""
    lines = [
        f"🔎 調査「{escape_and_truncate(draft.title_ja or request.label, CARD_TITLE_MAX)}」",
        f"{request.digest_date} の今日の IT ニュース {request.event_index} 件目"
        f"「{escape_and_truncate(request.label, CARD_LABEL_MAX)}」から",
    ]
    sections = [
        ("何が起きたか", draft.what_happened, CARD_WHAT_MAX),
        ("経緯と原因", draft.background, CARD_BACKGROUND_MAX),
        ("影響", draft.impact, CARD_IMPACT_MAX),
        ("報じ方の違い", draft.coverage, CARD_COVERAGE_MAX),
    ]
    for heading, text, limit in sections:
        if text:
            lines += ["", f"<b>{heading}</b>", escape_and_truncate(text, limit)]
    if draft.reactions:
        lines += ["", "<b>X の反応</b>"]
        lines += [
            f"・【{escape_and_truncate(r.stance, CARD_STANCE_MAX)}】"
            f"{escape_and_truncate(r.summary, CARD_REACTION_MAX)}{_numbers(r.sources)}"
            for r in draft.reactions[:REACTIONS_MAX]
        ]
    if draft.unknowns:
        lines += ["", "<b>まだ分からないこと</b>"]
        lines += [
            f"・{escape_and_truncate(u, CARD_UNKNOWN_MAX)}" for u in draft.unknowns[:UNKNOWNS_MAX]
        ]
    rows, posts = _card_materials(draft, materials)
    if rows or posts:
        lines += ["", "<b>材料</b>"]
        lines += [
            f'[{m.number}] <a href="{escape_attr(m.url)}">'
            f"{escape_and_truncate(m.host or '記事', CARD_MATERIAL_HOST_MAX)}</a> "
            f"{escape_and_truncate(m.title or '(題なし)', CARD_MATERIAL_TITLE_MAX)}"
            for m in rows
        ]
    if posts:
        lines.append(
            "X の投稿 "
            + " ".join(f'<a href="{escape_attr(m.url)}">[{m.number}]</a>' for m in posts)
        )
    if search is not None:
        lines += ["", search_summary(search, x)]
    return "\n".join(lines)


def search_summary(search: SearchOutcome, x: XOutcome | None = None) -> str:
    """材料の内訳の 1 行(固定の文言と数だけ。外部由来の文字を入れない)。

    足した材料を「・」で並べ、検索の失敗と、X を読まなかった理由を括弧に添える。検索で 1 件も
    足せず理由があるときは、手元の材料だけで調べたことも書く。
    """
    x = x or XOutcome("off")
    parts = [f"手元 {search.local} 件"]
    notes: list[str] = []
    if search.status in ("ok", "none"):
        if search.engine == "google_news":
            parts.append(f"検索した報道の見出し {search.found} 件(Google ニュース)")
        else:
            parts.append(f"検索した報道 {search.found} 件")
    elif search.status == "failed":
        notes.append("報道の検索に失敗した")
    if x.status in ("ok", "none"):
        parts.append(f"X の投稿 {x.found} 件")
    elif x.status == "failed":
        notes.append("X の検索に失敗した")
    elif x.status == "capped":
        notes.append(f"X はこの機能の今月の上限(${x.cap_usd:g})に達したので読まなかった")
    elif x.status == "expired":
        notes.append("X は検索できる直近 7 日の外なので読まなかった")
    line = "材料: " + "・".join(parts)
    if notes:
        if search.found == 0 and x.found == 0:
            notes.append("手元の材料だけで調べた")
        line += f"({'。'.join(notes)})"
    return line


def render_note(
    request: InvestigationRequest,
    draft: InvestigationDraft,
    materials: list[Material],
    created: str,
    search: SearchOutcome | None = None,
    x: XOutcome | None = None,
) -> str:
    """解説ノート(kind: investigation)。本文は公開の直前にも外部の埋め込みと生の HTML を直す。"""
    frontmatter = {
        "kind": "investigation",
        "title": draft.title_ja or request.label,
        "event": request.label,
        "digest_date": request.digest_date,
        "event_index": request.event_index,
        "created": created,
        "materials": len(materials),
    }
    if search is not None:
        frontmatter["search"] = search.status
        frontmatter["search_engine"] = _ENGINE_LABELS.get(search.engine or "", "")
        frontmatter["search_query"] = search.query or ""
        frontmatter["materials_search"] = search.found
    if x is not None:
        frontmatter["x_search"] = x.status
        frontmatter["x_query"] = x.query or ""
        frontmatter["materials_x"] = x.found
    body = [
        f"# {draft.title_ja or request.label}",
        "",
        f"{request.digest_date} の今日の IT ニュース {request.event_index} 件目"
        f"「{request.label}」の調査。",
    ]
    for heading, text in (
        ("何が起きたか", draft.what_happened),
        ("経緯と原因", draft.background),
        ("影響", draft.impact),
        ("報じ方の違い", draft.coverage),
    ):
        if text:
            body += ["", f"## {heading}", text]
    if draft.reactions:
        body += ["", "## X の反応"]
        body += [f"- 【{r.stance}】{r.summary}{_numbers(r.sources)}" for r in draft.reactions]
    if draft.unknowns:
        body += ["", "## まだ分からないこと"]
        body += [f"- {u}" for u in draft.unknowns]
    if search is not None:
        body += ["", search_summary(search, x)]
    if materials:
        body += ["", "## 材料"]
        body += [
            f"{m.number}. {_md_link(m.title or m.url, m.url)}"
            f"({m.host or '不明'}・{_KIND_LABELS[m.kind]})"
            for m in materials
        ]
    # LLM の本文と外部由来の題は untrusted。本文の側をまとめて直す(GH#377。深掘りのノートと同じ)
    return render_frontmatter(frontmatter) + neutralize_markdown(
        "\n" + "\n".join(body) + "\n\n---\n\n(この下は手書きメモ領域)\n"
    )


def _numbers(sources: list[int]) -> str:
    return "".join(f"[{n}]" for n in sources[:REACTION_SOURCES_MAX])


def _md_link(title: str, url: str) -> str:
    """Markdown のリンクを安全に組む(外部由来の題の []・URL の () で構造を壊さない)。"""
    safe_title = title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    safe_url = url.replace("(", "%28").replace(")", "%29").replace(" ", "%20")
    return f"[{safe_title}]({safe_url})"
