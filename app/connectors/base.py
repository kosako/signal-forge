"""Connector 共通インターフェース(設計 02 §1)。

fetch は読み取り専用・副作用なし(DB に触らない)。items への書き込み・URL 正規化・
重複排除・カーソル保存はすべて `intel collect`(app/collect.py)が行う。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from app.models import Source


class RetryableError(Exception):
    """接続失敗・timeout・5xx・429(リトライ尽き)。そのソースを当日スキップ。"""


class FatalError(Exception):
    """401/403・410 Gone・設定不備。リトライせずスキップ、doctor 警告対象。

    api_reads / estimated_cost_usd は、送出までに読んだ課金対象の件数と費用(既定 0)。
    X の部分取得のあとの認証の失敗で添え、collect が失敗の記録(collect_runs)に残す
    (日次の読取上限の当日の使用量と月次の費用を過少にしない。GH#178)。
    """

    def __init__(
        self, *args: object, api_reads: int = 0, estimated_cost_usd: float = 0.0
    ) -> None:
        super().__init__(*args)
        self.api_reads = api_reads
        self.estimated_cost_usd = estimated_cost_usd


@dataclass(frozen=True)
class RawItem:
    external_id: str | None  # ソース内の自然キー(tweet id / RSS guid)。無ければ None
    url: str  # 正規化前 URL。正規化は collect 側で一元化
    title: str | None
    author: str | None
    published_at: datetime | None  # UTC
    raw_content: str | None  # ソースが本文を同梱する場合のみ(RSS content 等)
    meta: dict = field(default_factory=dict)  # ソース固有情報(RT 種別など)


@dataclass(frozen=True)
class FetchStats:
    items_fetched: int
    api_reads: int  # X の課金対象読み取り件数(collect_runs へ記録)
    estimated_cost_usd: float  # api_reads × 単価。課金のないソース(RSS)は 0.0
    http_status: int | None  # ログのみ(DB には持たない。設計 02 §1)
    duration_ms: int


@dataclass(frozen=True)
class FetchResult:
    items: list[RawItem]
    next_cursor: dict  # sources.cursor に JSON 保存する不透明な辞書
    stats: FetchStats
    # 恒久移転(301/308)を経由したときの移転先(RSS。sources.moved_url へ。設計 02 §3・GH#150)
    moved_url: str | None = None
    # 部分取得のあとの失敗(X 検索の後ろのスロットの 400、X のホームTL の 2 ページ目以降の 5xx、
    # 通信のリトライ尽きなど。GH#183・GH#182)。collect は items と next_cursor を保存しつつ、
    # ソースの失敗として記録する。None = 正常な終わり
    partial_error: Exception | None = None
    # X の当日の読取の枠が残っておらず、読取の要求を 1 回もしなかった(GH#178)。collect は
    # 復旧を確かめていないので、ソースの成否の状態(成功の時刻・直近のエラー・連続失敗数)を変えない
    budget_exhausted: bool = False
    # X 検索で、要求しなかったスロットを残したまま正常に終わった(当日の残り枠の割り当てが下限に
    # 届かない・rate limit 残 0 の打ち切り。GH#331)。要求しなかったスロットの成否は確かめていない
    # ので、collect は budget_exhausted と同じくソースの成否の状態を変えない(後ろのスロットの
    # 失敗の続きを、先のスロットの成功で消さない)。取得ぶんとカーソルは保存する
    skipped_slots: bool = False


class Connector(Protocol):
    source_type: str  # "x" | "rss"

    def fetch(self, source_row: Source, cursor: dict) -> FetchResult: ...
