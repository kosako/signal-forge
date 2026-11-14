"""出来事の調査(GH#415。設計 06 §9)。

朝のダイジェストの「📰 今日の IT ニュース」の出来事 1 つを、報じた記事をまとめて材料にして調べる。
何が起きたか・経緯と原因・影響・報じ方の違い・X の反応・まだ分からないこと をカードとノートにする。

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
import re
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.analysis.llm import LLMEngine, complete_with_retry
from app.config import Config
from app.delivery.telegram import escape_and_truncate, escape_attr
from app.export.neutralize import neutralize_markdown
from app.export.obsidian import FORBIDDEN_IN_SLUG, render_frontmatter
from app.models import NewsEntry, link_host, parse_news_json
from app.prompting import fill_template, load_writing_guide, sanitize, sanitize_body

PROMPT_VERSION = "investigate-v001"
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# 知らせ(固定の文言だけ。外部由来の見出しを入れない。今すぐの深掘りの知らせと同じ考え)
FAILED_NOTICE = "⚠️ {date} の今日の IT ニュースの {index} 件目の調査を作れませんでした"
NO_MATERIAL_NOTICE = (
    "⚠️ {date} の今日の IT ニュースの {index} 件目は、本文のある材料が無いため調査できませんでした"
)

# X の投稿の host(材料の種別を決める。trend のリンクの並べ方と同じ)
_POST_HOSTS = frozenset({"x.com", "twitter.com", "mobile.twitter.com"})
_KIND_LABELS = {"article": "記事", "post": "X の投稿"}

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
UNKNOWNS_MAX = 3

# 主張の番号。`[1]`・`[1, 3]`・`[1、3]` を読み、材料にある番号だけを `[1][3]` の形で残す
_CITATION = re.compile(r"\[(\d{1,3}(?:\s*[,、]\s*\d{1,3})*)\]")
_CITATION_SPLIT = re.compile(r"\s*[,、]\s*")


class NoMaterialError(Exception):
    """本文のある材料が無い(見出しだけでは調べられない)。"""


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
    item_id: int
    kind: Literal["article", "post"]
    title: str
    host: str
    url: str  # items.canonical_url
    published_at: str | None
    body: str


@dataclass(frozen=True)
class GeneratedInvestigation:
    note_name: str
    note_markdown: str
    card_text: str


RequestOutcome = Literal["accepted", "requested", "running", "done", "no_sources"]


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
    done(作り終えた)・no_sources(材料の記事が無い)。失敗した依頼は、もう一度押せば受け付け直す。
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
        "SELECT id, status FROM news_investigations WHERE digest_id = ? AND label = ?",
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
                host=host,
                url=row["canonical_url"],
                published_at=row["published_at"],
                body=row["extracted_text"] or "",
            )
        )
    return materials


# --- 生成 ---


def generate_investigation(
    config: Config,
    engine_factory: Callable[[str], LLMEngine],
    request: InvestigationRequest,
    materials: list[Material],
    *,
    now: datetime | None = None,
) -> GeneratedInvestigation:
    """材料から調査のカードとノートを作る。本文のある材料が無ければ NoMaterialError。"""
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
    return GeneratedInvestigation(
        note_name=name,
        note_markdown=render_note(request, draft, materials, created),
        card_text=render_card(request, draft, materials),
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
    posts = {m.number for m in materials if m.kind == "post"}
    reactions = []
    for reaction in draft.reactions:
        sources = [n for n in dict.fromkeys(reaction.sources) if n in posts]
        stance = _one_line(reaction.stance)
        # 根拠の番号は sources が正(カードとノートはそこから付ける。要約の中の番号は外して重ねない)
        summary = _CITATION.sub("", _one_line(reaction.summary)).strip()
        if sources and stance and summary:
            reactions.append(
                InvestigationReaction(stance=stance, summary=summary, sources=sources)
            )
    return InvestigationDraft(
        title_ja=_one_line(draft.title_ja),
        what_happened=_clean_citations(draft.what_happened, valid),
        background=_clean_citations(draft.background, valid),
        impact=_clean_citations(draft.impact, valid),
        coverage=_clean_citations(draft.coverage, valid),
        reactions=reactions[:REACTIONS_MAX],
        unknowns=[_one_line(u) for u in draft.unknowns if u.strip()][:UNKNOWNS_MAX],
    )


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


def _card_materials(draft: InvestigationDraft, materials: list[Material]) -> list[Material]:
    """カードに並べる材料。主張に使った材料だけ(1 つも無ければ記事の材料)、上限まで。"""
    cited = cited_numbers(draft)
    chosen = [m for m in materials if m.number in cited] or [
        m for m in materials if m.kind == "article"
    ]
    return chosen[:CARD_MATERIALS_MAX]


def render_card(
    request: InvestigationRequest, draft: InvestigationDraft, materials: list[Material]
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
    card_materials = _card_materials(draft, materials)
    if card_materials:
        lines += ["", "<b>材料</b>"]
        lines += [
            f'[{m.number}] <a href="{escape_attr(m.url)}">'
            f"{escape_and_truncate(m.host or '記事', CARD_MATERIAL_HOST_MAX)}</a> "
            f"{escape_and_truncate(m.title or '(題なし)', CARD_MATERIAL_TITLE_MAX)}"
            for m in card_materials
        ]
    return "\n".join(lines)


def render_note(
    request: InvestigationRequest,
    draft: InvestigationDraft,
    materials: list[Material],
    created: str,
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
