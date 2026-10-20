"""テーブル行の型(設計 01 §3 の列と 1:1)。

共通語彙6テーブル(sources/items/documents/analyses/signals/digests)+
運用系5テーブル(collect_runs/telegram_state/digest_messages/item_notes/digest_notes)。
`from_row` が薄いリポジトリ層の共通入口(SELECT 結果 → 型付きオブジェクト)。
列挙値の正本は DDL の CHECK 制約(ここに enum を二重定義しない)。
"""

from __future__ import annotations

import sqlite3
from dataclasses import MISSING, dataclass, fields
from typing import Self

RAW_CONTENT_MAX_CHARS = 50_000  # items.raw_payload の切り詰め上限(設計 01 §6)


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


@dataclass(frozen=True)
class DigestNote(_Row):
    id: int
    digest_id: int
    note: str
    created_at: str
    exported_at: str | None
