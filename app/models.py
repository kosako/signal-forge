"""テーブル行の型(設計 01 §3 の列と 1:1)。

共通語彙6テーブル(sources/items/documents/analyses/signals/digests)+
運用系5テーブル(collect_runs/telegram_state/digest_messages/item_notes/digest_notes)。
`from_row` が薄いリポジトリ層の共通入口(SELECT 結果 → 型付きオブジェクト)。
列挙値の正本は DDL の CHECK 制約(ここに enum を二重定義しない)。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import MISSING, dataclass, fields
from typing import NamedTuple, Self
from urllib.parse import urlsplit

RAW_CONTENT_MAX_CHARS = 50_000  # items.raw_payload の切り詰め上限(設計 01 §6)

# 本文の抽出に失敗し、見出しとメタ情報だけで作った分析(analyses.extraction_failed)の印(GH#240)。
# digest・Telegram・Items ノートの 3 つの段で共通なので共通の層に置く(設計 00 §3)。degraded(LLM の
# 失敗でタイトルのみ)とは別で、分析がある記事にだけ付ける
EXTRACTION_FAILED_NOTE = "⚠️ 本文を読めていない(見出しだけで分析)"

# 冒頭の「今日の IT ニュース」節(GH#413)。trend の段が作り、digest 段が digests に残し、Telegram と
# Daily ノートがリンク付きで出す。3 つの段で共通なので共通の層に置く(設計 00 §3)
NEWS_HEADING = "📰 今日の IT ニュース"
NEWS_MAX_EVENTS = 6  # 節に載せる出来事の上限
NEWS_MAX_LINKS = 3  # 出来事 1 つに付けるリンクの上限
NEWS_MAX_SOURCES = 8  # 出来事 1 つの材料(報じた記事)として残す id の上限(出来事の調査。GH#415)


@dataclass(frozen=True)
class NewsLink:
    item_id: int
    url: str  # items.canonical_url(DB の正規 URL。LLM の出力ではない)


@dataclass(frozen=True)
class NewsEvent:
    label: str
    summary: str
    links: tuple[NewsLink, ...]
    # 出来事を報じた記事の id(リンクにしない分も含む。出来事の調査の材料。GH#415)
    sources: tuple[int, ...] = ()


class NewsEntry(NamedTuple):
    """digests.trend_json の出来事 1 つ(parse_news_json が返す)。"""

    label: str
    summary: str
    item_ids: list[int]  # リンクにする記事
    source_ids: list[int]  # 出来事を報じた記事(無い前の digest はリンクの記事と同じ)


@dataclass(frozen=True)
class NewsSection:
    """今日の IT ニュースの節。

    digests の trend_markdown(plain_text)と trend_json(to_json)に残す。
    """

    events: tuple[NewsEvent, ...]

    def plain_text(self) -> str:
        """リンクを含まないプレーンテキスト(markup なし)。"""
        lines = [NEWS_HEADING]
        lines.extend(f"・{event.label} — {event.summary}" for event in self.events)
        return "\n".join(lines)

    def to_json(self) -> str:
        """出来事と、リンクにする記事の id。URL は持たず、配信のときに items から引く。"""
        return json.dumps(
            {
                "events": [
                    {
                        "label": event.label,
                        "summary": event.summary,
                        "item_ids": [link.item_id for link in event.links],
                        # 材料の記事。無ければリンクの記事を材料とみなす(読む側と同じ規則)
                        "source_ids": list(event.sources)
                        or [link.item_id for link in event.links],
                    }
                    for event in self.events
                ]
            },
            ensure_ascii=False,
        )


def parse_news_json(text: str | None) -> list[NewsEntry] | None:
    """digests.trend_json を出来事の並びにする。

    空・形が違えば None(呼び出し側は trend_markdown の文に戻す)。source_ids の無い前の
    digest(GH#415 より前)は、リンクの記事を材料の記事とみなす。
    """
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list):
        return None
    parsed: list[NewsEntry] = []
    for event in events:
        if not isinstance(event, dict):
            return None
        label, summary, item_ids = event.get("label"), event.get("summary"), event.get("item_ids")
        source_ids = event.get("source_ids", item_ids)
        if not (
            isinstance(label, str)
            and isinstance(summary, str)
            and _is_id_list(item_ids)
            and _is_id_list(source_ids)
        ):
            return None
        parsed.append(NewsEntry(label, summary, item_ids, source_ids))
    return parsed or None


def _is_id_list(value: object) -> bool:
    # bool を id と取り違えない(bool は int の派生)
    return isinstance(value, list) and all(type(i) is int for i in value)


NEWS_LINK_LABEL_MAX = 40  # リンクの文字(host 名)の上限。host は外部由来で長さを検証していない


def link_label(url: str) -> str:
    """リンクに出す文字。host 名を NEWS_LINK_LABEL_MAX 字で切る(取れなければ「記事」)。"""
    host = link_host(url) or "記事"
    if len(host) > NEWS_LINK_LABEL_MAX:
        host = host[: NEWS_LINK_LABEL_MAX - 1] + "…"
    return host


def link_host(url: str) -> str:
    """リンクの文字にする host 名(小文字・先頭の www. を外す)。取れなければ空文字。"""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:  # 壊れた URL(正規 URL は検証済みだが、表示の段で落とさない)
        return ""
    return host.lower().removeprefix("www.")


@dataclass(frozen=True)
class _Row:
    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        # 既定値のある(後の migration で足した)列は、行に無ければ既定値にする。intel doctor は
        # migration を当てずに読むので、未適用の DB でも読めるように(既定値の無い列は必須のまま)
        keys = set(row.keys())
        return cls(
            **{f.name: row[f.name] for f in fields(cls) if f.name in keys or f.default is MISSING}
        )


@dataclass(frozen=True)
class Source(_Row):
    id: int
    type: str
    name: str
    config: str | None
    cursor: str | None
    enabled: int
    last_success_at: str | None
    last_error_at: str | None
    last_error: str | None
    consecutive_failures: int
    created_at: str
    moved_url: str | None = None  # migration 016(GH#150)


@dataclass(frozen=True)
class Item(_Row):
    id: int
    source_id: int
    external_id: str | None
    url: str
    canonical_url: str
    title: str | None
    author: str | None
    published_at: str | None
    fetched_at: str
    raw_payload: str | None
    content_hash: str
    status: str
    skip_reason: str | None
    dup_of_item_id: int | None


@dataclass(frozen=True)
class Document(_Row):
    id: int
    item_id: int
    extracted_text: str | None
    language: str | None
    extraction_status: str
    extraction_error: str | None
    extracted_at: str
    expires_at: str


@dataclass(frozen=True)
class Analysis(_Row):
    id: int
    item_id: int
    run_id: str | None
    summary: str
    category: str
    topic: str | None
    relevance_score: float
    novelty: str
    negative_labels: str
    total_score: float | None
    recommendation_reason: str
    implication: str
    degraded: int
    engine: str
    model: str
    prompt_version: str
    input_hash: str
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost_usd: float
    analyzed_at: str
    title_ja: str | None  # 002 で追加(ALTER のため実テーブルでは末尾列)
    extraction_failed: int = 0  # migration 021(GH#240。本文なしで分析した = 1)


@dataclass(frozen=True)
class Signal(_Row):
    id: int
    kind: str
    item_id: int | None
    value: str
    signal_date: str
    created_at: str


@dataclass(frozen=True)
class Digest(_Row):
    id: int
    digest_date: str
    run_id: str | None
    generated_at: str
    status: str
    markdown: str
    item_ids: str
    item_count: int
    delivery_status: str
    sent_at: str | None


@dataclass(frozen=True)
class CollectRun(_Row):
    id: int
    source_id: int
    started_at: str
    finished_at: str | None
    status: str
    items_fetched: int
    api_reads: int
    estimated_cost_usd: float
    error: str | None


@dataclass(frozen=True)
class TelegramState(_Row):
    key: str
    value: str
    updated_at: str


@dataclass(frozen=True)
class DigestMessage(_Row):
    id: int
    digest_id: int
    item_id: int | None
    message_id: int
    sent_at: str


@dataclass(frozen=True)
class ItemNote(_Row):
    id: int
    item_id: int
    note: str
    created_at: str
    exported_at: str | None
    chat_id: int | None = None  # migration 019(GH#193。受信したリプライの冪等キー)
    message_id: int | None = None  # 同上


@dataclass(frozen=True)
class DigestNote(_Row):
    id: int
    digest_id: int
    note: str
    created_at: str
    exported_at: str | None
    chat_id: int | None = None  # migration 019(GH#193。受信したリプライの冪等キー)
    message_id: int | None = None  # 同上
