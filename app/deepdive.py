"""朝の調査段 — 深掘りドラフト生成 + DeepDive ノート + 概要カード(GH#105 / 設計 06 §3〜5)。

実行主体 = 使い捨て `codex exec`(設計 06 §4・§8 の実装時決定):
- 調査段は非対話のドラフト生成であり、analyze 段と同じ隔離レール(network 遮断・
  CODEX_HOME deny-read・書込遮断。Issue #54)をそのまま使う。LLM にはツールを一切
  持たせず、材料(DB の抽出本文)はプロンプトで渡し、vault への書込
  (`SignalForge/DeepDive/` 限定)と Telegram 送信は本モジュール(Python)が行う。
  PROPOSAL §フェーズ2境界の安全条件(read-only 基本・書込先限定・自動実行アクション
  禁止・untrusted 本文起点の実行はツール制限)を構造的に満たす
- app-server は非対話バッチでは利点(会話管理・ツール呼び出し)が使われず、常駐化は
  使い捨て CODEX_HOME の前提を壊す(memories 持ち越し = injection 持続経路)ため不採用

実行制御(設計 06 §3):
- 1 日上限 = config `[dig].daily_cap`(JST 暦日。同日再実行は残枠のみ = 冪等)
- 優先順位: ①リプライ指定(requested_at 新しい順)②自動キューの新しい順 ③その日に配信した
  digest の上位(①②で上限に足りない分だけ。GH#349)。③ はキューに積まずに選び、生成物の記帳と
  同じトランザクションで source = 'digest'・status = 'done' の行を足す
- 材料不足(本文抽出なし・短すぎ)は、自動選出なら skip(未消化一覧には残る)、
  リプライ指定なら段の失敗として loud に報告する(明示依頼を黙って放置しない)。報告は 1 回に
  して指定を外す(本文を取り直す経路は無く、毎朝の失敗を続けない。GH#197)。外すのは朝バッチが
  失敗の通知を送れた後(release_material_request。GH#337)
- 同じ台帳を使う実行(朝バッチの dig 段と `intel dig`)は 1 つずつ。候補の選定から公開・送信までを
  ロックで囲み、後から来た方は待たずに何もしない(朝バッチを止めない。GH#196)
- 上限に達していないのに ①〜③ のどれからも選べず、カードも届かない朝は、段の終わりに理由を
  1 通で知らせる(_notify_no_candidates。失敗の通知とは別で、段の失敗にしない。GH#349)
- 今すぐの依頼(GH#388): 未消化一覧のボタンで受け付けた依頼を、poller が起動する
  `intel dig-now`(run_dig_now)が同じ台帳とロックで作る。朝の 1 日の上限には数えない

別系統の AI のレビュー(GH#153。config `[dig].review`): ドラフトを Claude API が元記事と
照らしてレビューし、指摘があれば書き手が 1 回だけ直す(直したあとの再レビューはしない)。
レビューは品質の上乗せなので、失敗しても深掘りは止めない(未レビューのまま届け、ノートと
カードに明示する)。費用は台帳に残し、月(UTC の暦月。doctor #9 の当月と同じ)の上限額に達したら
その月はレビューを省く。Codex が失敗してドラフトを予備の Claude(app/analysis/fallback.py)が
書いた回は、レビュー役と同じ系統で別の AI のレビューにならないので省き、ノートとカードに明示する
(GH#180)。

障害の局所化: item 単位で隔離し、1 件の失敗で他の件・他の段を止めない。失敗が
あれば段としては失敗にする(#74 と同じ「進めるものは進め、止まったことは通知」)。
中断耐性は dig_results(migration 010)への write-ahead で作る: LLM 生成物
(ノート本文・カード文)を先に台帳へ記帳し、ファイル公開(published_at)→
カード送信(message_id)を台帳から再開する。どこで中断しても再実行は同一生成物の
続きだけを行い、ノートとカードの生成元が食い違わない。既存ノートは上書きしない
(_publish_note。公開済みノートに書かれた take を守る)。

ノートの本文(LLM の解説・対訳と、抽出本文の保全コピー)は untrusted なので、外部の画像の
埋め込みをリンクに変え、生の HTML を escape してから台帳に記帳する(開いただけで外部を読みに
行かない。GH#377。app/export/neutralize.py)。公開の直前にも同じ直しをかける(直しより前に記帳
した未公開の本文のため)。frontmatter は YAML シリアライザが作るので対象外。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from app.analysis.llm import (
    DEFAULT_TIMEOUT_S,
    AnthropicApiEngine,
    LLMEngine,
    LLMError,
    complete_with_retry,
)
from app.analysis.profile import load_interest_profile
from app.config import Config
from app.delivery.state import get_state, set_state
from app.delivery.telegram import (
    TelegramApi,
    dig_card_keyboard,
    escape_and_truncate,
    escape_text,
)
from app.dig import (
    DigCandidate,
    claim_now_request,
    close_now_request,
    dug_on,
    has_material,
    has_now_requests,
    has_unsent_cards,
    interrupted_now_requests,
    select_from_queue,
)
from app.dig_take import is_dismissed, is_read
from app.export.neutralize import neutralize_markdown, neutralize_note
from app.export.obsidian import FORBIDDEN_IN_SLUG, atomic_write, render_frontmatter
from app.http import HttpClient
from app.investigate import FAILED_NOTICE as _INVESTIGATE_FAILED
from app.investigate import NO_MATERIAL_NOTICE as _INVESTIGATE_NO_MATERIAL
from app.investigate import (
    NoMaterialError,
    XRead,
    claim_investigation,
    gather_materials,
    generate_investigation,
    has_unsent_investigations,
    interrupted_investigations,
    plan_search,
    record_generated,
    search_news,
    search_x,
)
from app.investigate import close_failed as close_investigation_failed
from app.jst import today_jst
from app.prompting import fill_template, load_writing_guide, sanitize, sanitize_body
from app.vault_dir import VaultDir, VaultPathError, folder_dir
from app.vault_guard import VAULT_TIMEOUT_S, VaultGuard, VaultStalled

# 解説とその直しには書き方の手引き(app.prompting.load_writing_guide)を入れる(GH#381)。手引きは
# 解説のプロンプトの writing_guide に入れ、直しは解説のプロンプトを丸ごと含むので二重には入れない
PROMPT_VERSION = "dig-v003"  # 原文からの引用(quotes)を足した(GH#394)
TRANSLATE_PROMPT_VERSION = "dig-translate-v001"
# レビューには手引きを入れない(GH#381)。元記事との食い違いだけを見る役で、文の書き方を指摘の
# 対象にしない(手引きを渡すと、書き方の好みが事実の指摘に混ざりうる)
# 別系統の AI のレビュー(GH#153)。v002 は引用の一言を指摘しないものに足した(GH#394)
REVIEW_PROMPT_VERSION = "dig-review-v002"
REVISE_PROMPT_VERSION = "dig-revise-v002"  # 指摘を受けて書き手が 1 回直す
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

DEEPDIVE_FOLDER = "DeepDive"  # vault の folder 配下(書込先はここに限定。ADR 0004)

# ノートの公開を実行どうしで 1 つに絞るロック(台帳の DB と同じ置き場。GH#234)。
# 別の実行が握っていたら、この時間だけ待って諦める(ふだんの公開は数ミリ秒で終わる)
_PUBLISH_LOCK_NAME = "dig-publish.lock"
_PUBLISH_LOCK_WAIT_S = VAULT_TIMEOUT_S
# 調査段の実行を 1 つに絞るロック(GH#196)。DB の隣の `<DB>.dig.lock`(migrate.lock・
# deliver.lock と同じ名付け)。公開のロックはこの中で取る(入れ子の順はこの 1 通り)
_DIG_LOCK_SUFFIX = ".dig.lock"
# `intel dig-now` を 1 つに絞るロック(GH#388)。プロセスの間ずっと持つ。この中で、作り始めた印の
# 残った依頼を「前の実行が途中で止まった」と見分ける。dig.lock とは別で、生成の間は dig.lock を
# 持たない(朝バッチの dig 段を止めない)
_DIG_NOW_LOCK_SUFFIX = ".dig-now.lock"
# 朝の dig 段と `intel dig` が、`intel dig-now` の短い取り出し・公開・送信の間の dig.lock を
# 待つ上限と確かめる間隔(GH#388)。dig-now は生成の間は dig.lock を持たないので、ふつうは数秒で取れる
_DIG_NOW_YIELD_WAIT_S = 120.0
_DIG_LOCK_POLL_S = 0.5

# 自動選出(観点なし)の調査ブリーフ(設計 06 §4)
AUTO_BRIEF = "私の文脈(関心プロファイル)との接点を軸に、主張と根拠の強さを検証しながら読む"
_REQUESTED_NONE = "(観点指定なし:ブックマーク自動キューからの深掘り)"
_REQUESTED_DIGEST = "(観点指定なし:今日の digest の上位からの深掘り)"  # 設計 06 §3 の ③
_REQUESTED_NOW = "(観点指定なし:今すぐの依頼からの深掘り)"  # GH#388

# 候補の無い朝の知らせ(設計 06 §3。GH#349)。固定の文言だけで、外部由来の本文を入れない。
# 印は telegram_state の日付キー(送れた日の JST 暦日。失敗の通知の印と同じ流儀)
STATE_NO_CANDIDATE_NOTIFIED = "dig_no_candidate_notified_date"
_NO_CANDIDATE_NONE = (
    "🔎 今朝の深掘りはありません。キューにも今日のダイジェストにも、掘れる候補がありませんでした"
)
_NO_CANDIDATE_NO_DIGEST = (
    "🔎 今朝の深掘りはありません。今日のダイジェストが配信されていないため、"
    "上位から選べませんでした"
)
_NO_CANDIDATE_GUIDE = "今夜の一覧が届いたら、そのリプライで指定できます"

# 対訳は 1 チャンクずつ生成する(1 呼び出しの出力を有界に保つ。段落境界で分割)。
# 出力は原文の再掲(callout の引用記法つき)と訳文の両方を含み、入力のチャンクに訳文の分が
# 足される。どのチャンクもこの上限を超えないようにして出力の量を抑える(超える段落は追加で
# 分ける。GH#237)
_TRANSLATE_CHUNK_CHARS = 6_000
# 上限を超える段落を分ける境界(粗い順。GH#237): 改行 → 文の終わり → 語の切れ目(空白)。
# どれでも上限に収まらない片は文字数で切る。境界の字は前の片の末尾に残す
_OVERSIZED_BOUNDARIES = (
    re.compile(r"\n"),
    re.compile(r"[。．！？]+[」』）)]*|[.!?]+[\"'”’)\]]*\s+"),
    re.compile(r"\s+"),
)
# 対訳の effort は low 固定(翻訳は推論負荷が低く、時間コストだけが増える)
_TRANSLATE_EFFORT = "low"

# カード各欄の上限(合計が Telegram 4096 字を構造的に超えないための予算)。引用(GH#394)の分は
# 2 方面の欄から回した(2026-11 の実物のカードは最大 613 字で、欄の上限は異常な出力を切る安全網)
_CARD_TITLE_MAX = 200
_CARD_STANCE_MAX = 300
_CARD_FOR_YOU_MAX = 1000
_CARD_FOR_WORLD_MAX = 800
_CARD_QUOTE_MAX = 200
_CARD_QUOTE_WHY_MAX = 100

# 原文からの引用(GH#394)。数の上限と、照合を通す長さの下限(短すぎる写しは、どの本文にも
# 含まれやすく、本文にある文の確かめにならない。照合の形の、空白を除いた字数)
QUOTES_MAX = 3
_TWEET_QUOTES_MAX = 1  # X の投稿は必要最小限の抜粋だけ(本文非保持 = GH#2)
_QUOTE_MIN_CHARS = 10
_NOTE_QUOTE_MAX = 400  # ノートに出す 1 件の上限(LLM 出力を有界に保つ)
_NOTE_QUOTE_WHY_MAX = 200
# 写しの照合で同じ字とみなすもの。意味の変わらない違いだけに限る(全角の英数字と記号・全角の
# 空白・曲がった引用符。LLM が写すときに変えることがある)。NFKC は使わない: 上付きの数字(10⁶ →
# 106)や丸数字のように意味の違う字までそろえ、載せる写しの側の誤りを通すため
_QUOTE_MATCH_TRANSLATION = str.maketrans(
    {chr(code): chr(code - 0xFEE0) for code in range(0xFF01, 0xFF5F)}
    | {"\u3000": " ", "‘": "'", "’": "'", "“": '"', "”": '"'}
)
# ノートの `## 原文から` を前に置く見出し(先に見つかったもの。プロンプトの節の構成の 4 と 7)
_NOTE_QUOTES_BEFORE = (re.compile(r"^## 根拠の強さ", re.M), re.compile(r"^## ソース", re.M))
# CommonMark が backslash で escape できる ASCII の記号(!"#$%&'()*+,-./:;<=>?@[\]^_`{|}~)
_MARKDOWN_PUNCTUATION = re.compile(r"([!-/:-@\[-`{-~])")
# 写しの全体を囲む引用符の組(LLM が付けがちな括弧。写しのままで本文に無いときだけ 1 組を外して
# 照合し直す。片側だけを外すと「検索」よりも… の「 のように、本文の字を削った形で載る)
_QUOTE_WRAPPERS = (("「", "」"), ("『", "』"), ("“", "”"), ("‘", "’"), ('"', '"'), ("'", "'"))
# 照合の形で空白を外す、日本語の字の隣(和文の折り返しの改行が空白になったもの)。英単語の間の
# 空白は残す(外すと now here と nowhere のように、意味の違う文が一致する)
_CJK_CHAR = r"[\u3000-\u303f\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff\uff00-\uffef]"
_CJK_CHAR_RE = re.compile(_CJK_CHAR)
_SPACE_NEXT_TO_CJK = re.compile(rf"(?<={_CJK_CHAR}) | (?={_CJK_CHAR})")

# レビューの指摘の上限件数と、ノート・カードに出す各欄の上限(LLM 出力を有界に保つ)
REVIEW_MAX_FINDINGS = 8
_FINDING_LOCATION_MAX = 120
_FINDING_TEXT_MAX = 300
_REVIEW_KIND_LABELS = {
    "unsupported": "元記事に無い",
    "misread": "読み違い",
    "overstated": "言い過ぎ",
    "omission": "抜け",
}
_REVIEW_SEVERITY_MARKS = {"must": "🔴", "should": "🟡"}
# レビュー役のエンジン(run.py / cli.py が create_anthropic_engine で作る Claude API)。ドラフトを
# 同じエンジン(予備の Claude)が書いた回はレビューを省き、この文言を出す(GH#180。固定の文言)
_REVIEWER_ENGINE = AnthropicApiEngine.name
_SAME_FAMILY_NOTICE = "別の AI のレビューなし(下書きが予備の Claude のため)"

# ツイート URL(リンクなし 🔖 の canonical)。本文非保持ポリシー(GH#2)の対象判定
_TWEET_HOSTS = frozenset({"x.com", "twitter.com", "mobile.twitter.com"})

# 本文の言語の判定(GH#173): 日本語の文字(かな・漢字)のうち、かながこの割合に満たなければ
# 日本語にしない(かなは日本語にしか無い。漢字だけの中国語を外す)
_JA_KANA_MIN_SHARE = 0.2
# 引用の括弧(開き → 閉じ)。地の文の言語の根拠にしない部分(GH#173)
_QUOTE_PAIRS = {"「": "」", "『": "』", "“": "”"}
# 開きと閉じが同じ字の二重引用符は、同じ行の中で前から 2 つずつ組にする
_ASCII_QUOTE_RE = re.compile(r'"[^"\n]*"')


class DigError(Exception):
    """調査段の失敗(材料不足の明示指定・全件失敗など)。

    material_requests: 材料不足で掘れなかったリプライ指定(GH#337)。指定を外すのは、朝バッチが
    失敗の通知を送れた後(release_material_request)。段の中で外すと、通知までに止まったときに
    指定だけが消えて本人に届かないため。
    """

    def __init__(
        self, message: str, *, material_requests: Sequence[DigCandidate] = ()
    ) -> None:
        super().__init__(message)
        self.material_requests: tuple[DigCandidate, ...] = tuple(material_requests)


class DigQuote(BaseModel):
    """原文からの引用 1 件(GH#394)。

    欄の形だけを決め、中身の確かめ(本文にあるか・数・長さ)は _verified_quotes が行う。1 件の
    不備で解説のドラフト全体を捨てない(引用は上乗せ)ため、空の値もここでは通す。
    """

    original: str  # 本文の文を一字一句そのまま(本文にあるかを確かめる用)
    ja: str  # 日本語以外の元記事のときの和訳(カードとノートにはこれだけを載せる)。日本語なら空
    why: str  # なぜ気になるか・何の役に立つかの一言


class DigDraft(BaseModel):
    """解説ノート + カードの構造化出力(prompts/dig-v003.md)。"""

    title_ja: str = Field(min_length=1, max_length=_CARD_TITLE_MAX)
    writer_stance: str = Field(min_length=1)
    for_you: str = Field(min_length=1)
    for_world: str = Field(min_length=1)
    note_markdown: str = Field(min_length=1)
    # 欄が無い出力も通す(引用は 0 個でよい。直しの出力が欄ごと落としても、ドラフトは捨てない)
    quotes: list[DigQuote] = Field(default_factory=list)


class TranslationChunk(BaseModel):
    """対訳チャンクの構造化出力(prompts/dig-translate-v001.md)。"""

    markdown: str = Field(min_length=1)


class ReviewFinding(BaseModel):
    """レビューの指摘 1 件(prompts/dig-review-v001.md)。長さはノート・カードに出すときに切る。"""

    kind: Literal["unsupported", "misread", "overstated", "omission"]
    severity: Literal["must", "should"]
    location: str = Field(min_length=1)
    problem: str = Field(min_length=1)
    fix: str = Field(min_length=1)

    @field_validator("kind", "severity", mode="before")
    @classmethod
    def _normalize_case(cls, value: object) -> object:
        # 列挙値の大文字・小文字の揺れで、課金済みのレビュー全体を捨てない
        return value.strip().lower() if isinstance(value, str) else value


class DigReview(BaseModel):
    """レビューの構造化出力。誤りが無ければ findings は空。"""

    findings: list[ReviewFinding]


@dataclass(frozen=True)
class ReviewOutcome:
    """1 件のレビューの結果(dig_results の review_* 列に記帳する)。"""

    status: str  # clean / revised / revise_failed / failed / skipped(migration 017)
    model: str = ""
    findings: tuple[ReviewFinding, ...] = ()
    cost_usd: float = 0.0
    # skipped のうち、ドラフトを書いたエンジンがレビュー役と同じ(予備の Claude)なので省いた回
    # (GH#180)。台帳の状態は月の上限で省いた回と同じ skipped(migration 017 の CHECK のまま)
    same_family: bool = False


def run_dig_stage(
    conn: sqlite3.Connection,
    config: Config,
    engine_factory: Callable[[str], LLMEngine],  # effort → engine
    api: TelegramApi | None,
    logger: logging.Logger,
    *,
    run_id: str | None = None,
    # JST の YYYY-MM-DD(テスト注入用)。差し替わるのは暦日だけで dig_results.created_at は
    # 実時刻で入るため、上限・同日判定を試す test は created_at を注入した日に揃える(GH#119)
    today: str | None = None,
    # 別系統の AI のレビュー役(GH#153)。None = レビューしない(config `[dig].review` が off)
    review_engine_factory: Callable[[], LLMEngine] | None = None,
    # ノートの公開の vault へのアクセスの時間の上限(GH#234。None = 既定の上限。テスト注入用)
    vault: VaultGuard | None = None,
) -> bool:
    """調査段の本体。生成(台帳へ write-ahead)→ 未公開ノート書出 → 未送信カード送信。

    3 段はそれぞれ台帳(dig_results)から再開可能で、どこで中断しても再実行は
    同一生成物の続きだけを行う(LLM 再呼び出し・生成物の食い違いを起こさない)。

    候補の選定から公開・送信までは、同じ台帳を使う実行どうしで 1 つに絞る(GH#196)。
    別の実行が進めている間に来たら、待たずに何もしないで False を返す(段の失敗にはしない)。
    重なると、両方が同じ候補を選んで生成し、1 日上限を超え、カードが重複するため。待たない
    のは、生成が分オーダーで続き、朝バッチの purge・backup を遅らせるため(先の実行が同じ
    台帳から生成・公開・送信を終えるので、何もしなくても取りこぼさない)。
    例外は `intel dig-now`(GH#388)が短く持っているときで、取れるまで待つ(_stage_dig_lock)。
    dig-now は朝の候補を選ばないので、何もしないとその日の朝の深掘りが届かない。上限まで待っても
    取れなければ段の失敗にして知らせる。
    """
    with _stage_dig_lock(conn) as lock:
        if lock == "dig_now":
            # 今すぐの深掘りが dig.lock を放さなかった。何もしないと、その日の朝の深掘りが届かない
            raise DigError(
                "今すぐの深掘り(intel dig-now)が調査段のロックを放さなかったので、深掘りを"
                "行わなかった(intel dig で再実行)"
            )
        if lock == "busy":
            logger.warning(
                "dig: 別の実行(朝バッチか intel dig)が調査段を進めているので、この実行では"
                "何もしない(生成・公開・送信はその実行が行う)",
                extra={"run_id": run_id, "stage": "dig"},
            )
            return False
        _run_dig_stage_locked(
            conn,
            config,
            engine_factory,
            api,
            logger,
            run_id,
            today,
            review_engine_factory,
            vault,
        )
    return True


def _run_dig_stage_locked(
    conn: sqlite3.Connection,
    config: Config,
    engine_factory: Callable[[str], LLMEngine],
    api: TelegramApi | None,
    logger: logging.Logger,
    run_id: str | None,
    today: str | None,
    review_engine_factory: Callable[[], LLMEngine] | None,
    vault: VaultGuard | None,
) -> None:
    """run_dig_stage の本体(_dig_lock の中で呼ぶ)。"""
    # 段の初めの実時計の暦日。候補の無い朝の知らせの印を、送れた日へ進めるのに使う(GH#349)。
    # 注入しないときの day も同じ値にする(別々に読むと、その間に 0 時を越えて食い違う)
    started_on = today_jst()
    day = today or started_on
    remaining = config.dig.daily_cap - _digs_on(conn, day)
    failures: list[str] = []
    material_requests: list[DigCandidate] = []
    selected: list[DigCandidate] = []

    if remaining > 0:
        selected, skipped_requested = _select_candidates(conn, config, remaining, day)
        # 材料不足の指定は、ここでは外さない。失敗の通知を送れた後に朝バッチが外す(GH#337)
        material_requests = skipped_requested
        failures.extend(
            f"item {candidate.item_id}: リプライ指定だが本文の抽出材料がない"
            "(失敗の通知を送った後に指定を外す)"
            for candidate in skipped_requested
        )
        for candidate in selected:
            try:
                _dig_one(
                    conn,
                    config,
                    engine_factory,
                    review_engine_factory,
                    candidate,
                    day,
                    logger,
                    run_id,
                )
            except Exception as e:
                # item 単位の隔離。ただし黙らせない(段の失敗に集約 → 失敗通知)
                logger.error(
                    f"deepdive 生成失敗 item={candidate.item_id}: {type(e).__name__}: {e}",
                    extra={"run_id": run_id, "stage": "dig", "item_id": candidate.item_id},
                    exc_info=True,
                )
                failures.append(f"item {candidate.item_id}: {type(e).__name__}")
    else:
        logger.info(
            "dig: 本日の上限に到達済み(生成なし)",
            extra={"run_id": run_id, "stage": "dig"},
        )

    # 候補の無い朝(GH#349)。中断した分(未公開・未送信の行)を再開する実行は、送れたかに
    # かかわらず知らせない(届いたカードの直後に「深掘りはありません」と言わない。送れなければ
    # 失敗の通知で知らせ、次の実行が送る。設計 06 §3)。再開の前に台帳を見る。今すぐの依頼を
    # 作っている途中(か、これから作る)ときも、その後にカードが届くので知らせない(GH#388)
    no_candidates = (
        api is not None
        and remaining > 0
        and not selected
        and not has_unsent_cards(conn)
        and not has_now_requests(conn)
    )

    # 未公開ノートの書出(過去の中断分を含む)
    failures.extend(
        _publish_pending_notes(conn, config, logger, run_id, vault or VaultGuard())
    )

    # 未送信カード(過去の中断分を含む)の送信。Telegram 未設定なら台帳に残す
    if api is not None:
        failures.extend(_send_pending_cards(conn, config, api, logger, run_id))
    else:
        pending = conn.execute(
            "SELECT count(*) AS n FROM dig_results WHERE message_id IS NULL"
        ).fetchone()["n"]
        if pending:
            logger.warning(
                f"Telegram 未設定のため概要カード {pending} 件は未送信のまま",
                extra={"run_id": run_id, "stage": "dig"},
            )

    # 候補の無い朝の知らせ(GH#349)。材料不足のリプライ指定で段が失敗する朝も、先に送る
    # (失敗の通知とこの知らせの両方が届く。設計 06 §3)
    if no_candidates and api is not None:
        _notify_no_candidates(conn, config, api, logger, run_id, day, started_on)

    if failures:
        raise DigError(
            f"調査段で {len(failures)} 件失敗: " + " / ".join(failures),
            material_requests=material_requests,
        )


def run_dig_now(
    conn: sqlite3.Connection,
    config: Config,
    engine_factory: Callable[[str], LLMEngine],  # effort → engine
    api: TelegramApi | None,
    logger: logging.Logger,
    *,
    run_id: str | None = None,
    review_engine_factory: Callable[[], LLMEngine] | None = None,
    vault: VaultGuard | None = None,
    http: HttpClient | None = None,
    x_read: XRead | None = None,
) -> bool:
    """今すぐの依頼(GH#388。設計 06 §3)を受け付けた順に 1 件ずつ作り、ノートの公開とカードの
    送信まで進める。poller が起動する `intel dig-now` が呼ぶ。依頼を作り切ったら True、別の実行に
    阻まれて依頼を残したら False(poller の見回りが後で起動し直す)。

    http は出来事の調査の報道の検索(None なら作る)、x_read は X の反応の検索(None = X を読まない。
    合成点が X の設定とキーを見て渡す。GH#415)。

    朝の調査段と同じ台帳を使い、作った分は朝の 1 日の上限に数えない(dig_results.on_demand)。
    - 実行どうし: `intel dig-now` は自分のロック(`<DB>.dig-now.lock`)で 1 つにする。調査段の
      ロック(dig.lock)は、依頼を取り出す間と、公開・送信の間だけ持ち、生成の間は持たない。
      朝バッチの dig 段がその間に始まっても止めずに済み、朝の選定は取り出した依頼を選ばない
      (app.dig.select_from_queue)。dig.lock が取れなければ、取り出しはこの実行では止めて
      False を返し、公開・送信は dig.lock を持つ実行(朝バッチなど)か、次の実行に任せる
    - 1 回押して 1 回だけ作る。取り出すときに作り始めた印を付け、印の残った依頼(前の実行が
      途中で止まった)は作り直さずに明朝の深掘りへ回して知らせる(LLM を繰り返し呼ばない)
    - 作れなかった依頼(LLM の失敗など)も、明朝の深掘りの ① へ回して知らせ、段の失敗にする
    - 今日もう掘った item は作らずに閉じる(同じ日に 2 回は掘らない。朝の生成中に押されたときの
      取りこぼしで、カードは朝の分が届いている)。本文が消えていたら閉じて知らせる
    - 1 件ごとに公開と送信まで進め、次の依頼で前のカードを待たせない。中断した分(未公開の
      ノート・未送信のカード。朝の分を含む)は、朝の調査段と同じく台帳から再開する
    """
    with _db_side_lock(conn, _DIG_NOW_LOCK_SUFFIX) as acquired:
        if not acquired:
            logger.info(
                "dig-now: 別の dig-now が実行中なので何もしない(依頼はその実行が作る)",
                extra={"run_id": run_id, "stage": "dig-now"},
            )
            return False
        return _run_dig_now_locked(
            conn, config, engine_factory, api, logger, run_id, review_engine_factory,
            vault or VaultGuard(), http, x_read,
        )


# 今すぐの依頼を作れなかったときの知らせ(GH#388)。固定の文言だけにし、外部由来の本文を入れない
_NOW_FAILED = "⚠️ {item_id} の今すぐの深掘りを作れませんでした。明朝の深掘りに回します"
_NOW_NO_BODY = "⚠️ {item_id} は本文が無いため、今すぐの深掘りを作れませんでした"


def _run_dig_now_locked(
    conn: sqlite3.Connection,
    config: Config,
    engine_factory: Callable[[str], LLMEngine],
    api: TelegramApi | None,
    logger: logging.Logger,
    run_id: str | None,
    review_engine_factory: Callable[[], LLMEngine] | None,
    vault: VaultGuard,
    http: HttpClient | None = None,
    x_read: XRead | None = None,
) -> bool:
    """run_dig_now の本体(`<DB>.dig-now.lock` の中で呼ぶ)。"""
    failures: list[str] = []
    # dig-now のロックの中なので、作り始めた印の残った依頼を作っている実行は無い
    for item_id, started_at in interrupted_now_requests(conn):
        if close_now_request(conn, item_id, started_at, to_morning=True):
            logger.warning(
                f"前の実行が途中で止まった今すぐの依頼を、明朝の深掘りに回した item={item_id}",
                extra={"run_id": run_id, "stage": "dig-now", "item_id": item_id},
            )
            _notify_now(config, api, logger, run_id, _NOW_FAILED.format(item_id=item_id))
            failures.append(f"item {item_id}: 前の実行が途中で止まった(明朝に回した)")
        else:
            # 作っている間に本人がパスした(依頼は取り消し済み)。作り始めた印だけを消す。残すと、
            # 指定し直しても朝の選定がこの行を選ばない
            close_now_request(conn, item_id, started_at, to_morning=False)
    # 前の実行が公開・送信できなかった分(送信の失敗など)を先に進める。依頼はもう閉じているので、
    # 下のループだけでは進まない。出来事の調査(GH#415)の分も同じ流れで進める
    if has_unsent_cards(conn) or has_unsent_investigations(conn):
        failures.extend(_publish_and_send(conn, config, api, logger, run_id, vault))

    completed = True
    while has_now_requests(conn):  # 無ければ dig.lock を取りに行かない(朝の段と競わない)
        with _dig_lock(conn, "dig-now") as acquired:
            if not acquired:
                logger.info(
                    "dig-now: 別の実行が調査段を進めているので、残りの依頼は後で作る"
                    "(poller が起動し直す)",
                    extra={"run_id": run_id, "stage": "dig-now"},
                )
                completed = False
                break
            # 取り出しを dig.lock の中で行う。朝の選定の途中や、朝が選んで作っている item を
            # 取り出さない(朝の dig 段は選定から公開・送信まで dig.lock を持つ)
            day = today_jst()  # 1 件ごとの暦日(ノートの名前と、同じ日に 2 回掘らない判定)
            conn.execute("BEGIN IMMEDIATE")
            try:
                candidate = claim_now_request(conn)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            dug_today = candidate is not None and dug_on(conn, candidate.item_id, day)
        if candidate is None:
            break
        assert candidate.now_started_at is not None  # claim_now_request が付けた印
        log_extra = {"run_id": run_id, "stage": "dig-now", "item_id": candidate.item_id}
        if dug_today:
            close_now_request(
                conn, candidate.item_id, candidate.now_started_at, to_morning=False
            )
            logger.info("今日もう掘った item の今すぐの依頼を、作らずに閉じた", extra=log_extra)
            continue
        if not has_material(candidate.body, config):
            close_now_request(
                conn, candidate.item_id, candidate.now_started_at, to_morning=False
            )
            logger.warning("本文が無いため、今すぐの依頼を作らずに閉じた", extra=log_extra)
            _notify_now(
                config, api, logger, run_id, _NOW_NO_BODY.format(item_id=candidate.item_id)
            )
            continue
        try:
            _dig_one(
                conn, config, engine_factory, review_engine_factory, candidate, day, logger,
                run_id,
            )
        except Exception as e:
            logger.error(
                f"今すぐの深掘りの生成失敗 item={candidate.item_id}: {type(e).__name__}: {e}",
                extra=log_extra,
                exc_info=True,
            )
            failures.append(f"item {candidate.item_id}: {type(e).__name__}")
            if close_now_request(
                conn, candidate.item_id, candidate.now_started_at, to_morning=True
            ):
                _notify_now(
                    config, api, logger, run_id, _NOW_FAILED.format(item_id=candidate.item_id)
                )
            continue
        failures.extend(_publish_and_send(conn, config, api, logger, run_id, vault))

    # 出来事の調査(GH#415)。生成の間は dig.lock を持たない(朝の選定と関わらない)
    failures.extend(
        _run_investigations(
            conn, config, engine_factory, api, logger, run_id, vault, http, x_read
        )
    )

    if api is None and (has_unsent_cards(conn) or has_unsent_investigations(conn)):
        logger.warning(
            "Telegram 未設定のため、概要カードは未送信のまま",
            extra={"run_id": run_id, "stage": "dig-now"},
        )
    if failures:
        raise DigError(f"今すぐの深掘りで {len(failures)} 件失敗: " + " / ".join(failures))
    return completed


def _run_investigations(
    conn: sqlite3.Connection,
    config: Config,
    engine_factory: Callable[[str], LLMEngine],
    api: TelegramApi | None,
    logger: logging.Logger,
    run_id: str | None,
    vault: VaultGuard,
    http: HttpClient | None = None,
    x_read: XRead | None = None,
) -> list[str]:
    """出来事の調査(GH#415。設計 06 §9)の依頼を受け付けた順に作り、1 件ごとに公開と送信まで進める。

    `<DB>.dig-now.lock` の中で呼ぶ。作り始めた印の残った依頼は前の実行が途中で止まったもので、
    作り直さずに失敗として閉じて知らせる(LLM を繰り返し呼ばない。押し直せば受け付け直す)。
    本文のある材料が無い依頼は、作らずに閉じて知らせる。作れなかった依頼(LLM の失敗など)も
    閉じて知らせ、段の失敗にする。ほかの依頼と深掘りは止めない。報道の検索(GDELT と予備の Google
    ニュース)と X の反応の検索に失敗しても、手元の材料だけで作る(カードとノートにその旨が出る)。
    """
    failures: list[str] = []
    for request in interrupted_investigations(conn):
        close_investigation_failed(conn, request.id, "interrupted")
        logger.warning(
            "前の実行が途中で止まった出来事の調査を、作り直さずに閉じた",
            extra={"run_id": run_id, "stage": "dig-now", "extra": {"investigation": request.id}},
        )
        _notify_now(
            config, api, logger, run_id,
            _INVESTIGATE_FAILED.format(date=request.digest_date, index=request.event_index),
        )
        failures.append(f"investigation {request.id}: 前の実行が途中で止まった")
    while True:
        request = claim_investigation(conn)
        if request is None:
            break
        log_extra = {"run_id": run_id, "stage": "dig-now", "extra": {"investigation": request.id}}
        try:
            materials = gather_materials(conn, request)
            if http is None:
                http = HttpClient()
            plan = plan_search(
                config, engine_factory, request, materials, x=x_read is not None
            )
            found, search = search_news(config, http, request, materials, plan, logger=logger)
            materials = materials + found
            posts, x = search_x(conn, config, x_read, request, materials, plan, logger=logger)
            materials = materials + posts
            logger.info(
                f"出来事の調査の検索: 報道 {search.status}(足した報道 {search.found} 件)・"
                f"X {x.status}(足した投稿 {x.found} 件)",
                extra=log_extra,
            )
            generated = generate_investigation(
                config, engine_factory, request, materials, search=search, x=x
            )
        except NoMaterialError:
            close_investigation_failed(conn, request.id, "no_material")
            logger.warning(
                "本文のある材料が無いため、出来事の調査を作らずに閉じた", extra=log_extra
            )
            _notify_now(
                config, api, logger, run_id,
                _INVESTIGATE_NO_MATERIAL.format(
                    date=request.digest_date, index=request.event_index
                ),
            )
            continue
        except Exception as e:
            logger.error(
                f"出来事の調査の生成失敗 investigation={request.id}: {type(e).__name__}: {e}",
                extra=log_extra,
                exc_info=True,
            )
            failures.append(f"investigation {request.id}: {type(e).__name__}")
            close_investigation_failed(conn, request.id, type(e).__name__)
            _notify_now(
                config, api, logger, run_id,
                _INVESTIGATE_FAILED.format(date=request.digest_date, index=request.event_index),
            )
            continue
        record_generated(conn, request.id, generated)
        logger.info(
            f"出来事の調査を作った(材料 {len(materials)} 件)", extra=log_extra
        )
        failures.extend(_publish_and_send(conn, config, api, logger, run_id, vault))
    return failures


def _publish_and_send(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi | None,
    logger: logging.Logger,
    run_id: str | None,
    vault: VaultGuard,
) -> list[str]:
    """台帳の未公開のノートを公開し、未送信のカードを送る(dig.lock の中で。GH#388)。

    dig.lock が取れなければ何もしない。持っている実行(朝バッチの dig 段・`intel dig`)が、
    段の終わりに同じ台帳から公開・送信する。間に合わなかった分は、poller が送れていない
    カード(作った経路を問わない)を見て `intel dig-now` を起動し直す。
    """
    with _dig_lock(conn, "dig-now") as acquired:
        if not acquired:
            logger.info(
                "dig-now: 別の実行が調査段を進めているので、公開と送信はその実行か次の実行に任せる",
                extra={"run_id": run_id, "stage": "dig-now"},
            )
            return []
        failures = _publish_pending_notes(conn, config, logger, run_id, vault)
        if api is not None:
            failures.extend(_send_pending_cards(conn, config, api, logger, run_id))
        return failures


def _notify_now(
    config: Config,
    api: TelegramApi | None,
    logger: logging.Logger,
    run_id: str | None,
    text: str,
) -> None:
    """今すぐの依頼を作れなかったことを知らせる(GH#388)。知らせの失敗はログだけ残す。"""
    if api is None:
        return
    try:
        api.send_message(config.telegram.owner_chat_id, text)
    except Exception as e:
        logger.error(
            f"今すぐの深掘りの知らせを送れなかった: {type(e).__name__}",
            extra={"run_id": run_id, "stage": "dig-now"},
        )


def _digs_on(conn: sqlite3.Connection, day: str) -> int:
    """その JST 暦日に実行済みの件数(created_at は UTC 保存)。

    今すぐの依頼で作った分(on_demand)は数えない。朝の 1 日の上限の外にある(GH#388)。
    """
    return conn.execute(
        "SELECT count(*) AS n FROM dig_results "
        "WHERE date(created_at, '+9 hours') = ? AND on_demand = 0",
        (day,),
    ).fetchone()["n"]


def _notify_no_candidates(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    logger: logging.Logger,
    run_id: str | None,
    day: str,
    started_on: str,
) -> None:
    """上限に達していないのに候補を選べなかった朝に、その理由を 1 通で知らせる(設計 06 §3。GH#349)。

    呼ぶのは、この実行が ①〜③ のどれからも選べず、中断した分の再開も無かったときだけ。
    次のときは送らない。
    - その日に送れた印がある(同じ日の再走・`intel dig`)。印は送れてから記録する(失敗の
      通知と同じ。送れなかった日は、次の実行が知らせ直せる)
    - その日に先の実行がカードを送った(「今朝の深掘りはありません」と食い違う)
    知らせの失敗(DB・送信)は段の失敗にせず、ログだけ残す。
    """
    try:
        if get_state(conn, STATE_NO_CANDIDATE_NOTIFIED) == day:
            return
        sent_today = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM dig_results"
            " WHERE date(sent_at, '+9 hours') = ?) AS found",
            (day,),
        ).fetchone()["found"]
        if sent_today:
            return
        reason = _NO_CANDIDATE_NONE if _digest_delivered(conn, day) else _NO_CANDIDATE_NO_DIGEST
        api.send_message(config.telegram.owner_chat_id, f"{reason}。\n{_NO_CANDIDATE_GUIDE}")
        # 印は送れた時点の暦日にする。段の初めの day のままだと、23:59 に始めた実行が選定・
        # 公開・送信の間に 0 時を越えたとき、前の日の印になり、その朝の実行がもう 1 通送る。
        # day は注入できる(テスト)ので、段の初め(started_on)から送れるまでに実時計が進んだ
        # 日数だけ day を進める
        elapsed = date.fromisoformat(today_jst()) - date.fromisoformat(started_on)
        set_state(
            conn,
            STATE_NO_CANDIDATE_NOTIFIED,
            (date.fromisoformat(day) + elapsed).isoformat(),
        )
    except Exception as e:
        logger.error(
            f"候補の無い朝の知らせを送れなかった: {type(e).__name__}",
            extra={"run_id": run_id, "stage": "dig"},
        )
        return
    logger.info(
        "dig: 候補が無いので、深掘りが無いことを知らせた",
        extra={"run_id": run_id, "stage": "dig"},
    )


def _digest_delivered(conn: sqlite3.Connection, day: str) -> bool:
    """その日の digest を配信したか(③ の選定 _select_from_digest が digest を見る条件と同じ)。"""
    return (
        conn.execute(
            "SELECT 1 FROM digests WHERE digest_date = ? AND delivery_status = 'sent'",
            (day,),
        ).fetchone()
        is not None
    )


def item_has_material(conn: sqlite3.Connection, config: Config, item_id: int) -> bool:
    """その item の今の documents の本文が、調査段の材料に足りるか(GH#197)。

    本文は保存の期間(既定で RSS 30 日・X 7 日)を過ぎると purge で消え、取り直す経路は無い
    (抽出の対象は新着 = status 'new' の item だけ)。調べ直しを受け付けて翌朝の実行を約束する
    前に確かめる。
    """
    row = conn.execute(
        "SELECT extracted_text FROM documents WHERE item_id = ?", (item_id,)
    ).fetchone()
    return has_material(row["extracted_text"] if row is not None else None, config)


def _select_candidates(
    conn: sqlite3.Connection, config: Config, limit: int, day: str
) -> tuple[list[DigCandidate], list[DigCandidate]]:
    """優先順に材料十分な candidate を limit 件選ぶ。

    戻り値 = (選定, 材料不足のリプライ指定)。キュー(①②)の規則は夜の未消化一覧の「明朝の
    予定」と共用する(app.dig.select_from_queue。GH#349)。キューで limit に足りなければ、
    足りない分をその日の digest の上位(③)で埋める(_select_from_digest。GH#349)。
    """
    selected, skipped_requested = select_from_queue(conn, config, limit, day)
    if len(selected) < limit:
        selected.extend(_select_from_digest(conn, config, limit - len(selected), day))
    return selected, skipped_requested


def _select_from_digest(
    conn: sqlite3.Connection, config: Config, limit: int, day: str
) -> list[DigCandidate]:
    """その日に配信した digest の上位から、材料十分な item を limit 件選ぶ(設計 06 §3 の ③)。

    見るのは配信済み(delivery_status = 'sent')の digests 行の item_ids(配信順 = ランキングの
    順)だけで、🔖 保存節(bookmark_item_ids)は見ない(X のブックマークは ② の経路にある)。
    除くのは、キューに行がある item(状態を問わない。queued で ② に選ばれなかったものは本文なし、
    passed はパス、done は掘り済み)、台帳に行がある item(掘り済み)、材料不足の item(枠を
    使わずに黙って次へ進む)。digest が無い・配信していない日は選ばない。選ぶ時点ではキューに
    積まない(行は生成物の記帳と同じトランザクションで足す。_record_digest_pick)。
    """
    rows = conn.execute(
        """
        SELECT i.id AS item_id,
               i.title, i.author, i.published_at, i.canonical_url,
               d.extracted_text, d.language,
               date(d.extracted_at, '+9 hours') AS fetched_date,
               a.title_ja, a.summary
        FROM digests g
        JOIN json_each(g.item_ids) AS listed
        JOIN items i ON i.id = listed.value
        LEFT JOIN documents d ON d.item_id = i.id
        LEFT JOIN analyses a ON a.id = (
            SELECT max(id) FROM analyses
            WHERE item_id = i.id AND degraded = 0
        )
        WHERE g.digest_date = ? AND g.delivery_status = 'sent'
          AND NOT EXISTS (SELECT 1 FROM dig_queue q WHERE q.item_id = i.id)
          AND NOT EXISTS (SELECT 1 FROM dig_results r WHERE r.item_id = i.id)
        ORDER BY listed.key
        """,
        (day,),
    ).fetchall()
    selected: list[DigCandidate] = []
    for row in rows:
        if len(selected) >= limit:
            break
        if not has_material(row["extracted_text"], config):
            continue
        selected.append(
            DigCandidate(
                item_id=row["item_id"],
                angle=None,
                requested=False,
                queue_updated_at=None,
                queue_requested_at=None,
                title=row["title"],
                author=row["author"],
                published_at=row["published_at"],
                canonical_url=row["canonical_url"],
                body=row["extracted_text"],
                language=row["language"],
                title_ja=row["title_ja"],
                summary=row["summary"],
                fetched_date=row["fetched_date"],
                from_digest=True,
            )
        )
    return selected


def release_material_request(
    conn: sqlite3.Connection,
    candidate: DigCandidate,
    logger: logging.Logger,
    run_id: str | None,
) -> bool:
    """材料不足のリプライ指定を、知らせた後に自動キューの扱いへ戻す(GH#197)。外せたら True。

    呼ぶのは朝バッチの失敗の通知を送れた後だけ(GH#337)。調査段の中で外すと、通知までの間
    (他の件の深掘り・公開・送信は分オーダー)に止まったとき、指定だけが消えて本人に届かない。
    送れなかった日(今日は通知済み・Telegram 未設定・送信の失敗)は呼ばず、指定のまま残して
    次の朝にもう一度知らせる。送った後・外す前に止まると 1 回多く知らせうる(知らせ漏れは作らない)。

    requested_at を外すだけで、status(queued)・観点・source・受付の順序の印
    (backlog_op_question_id。本人の未消化一覧への操作ではない)は変えない。本文を取り直す
    経路は無いので、指定のまま残すと毎朝同じ失敗を知らせ続ける。材料不足の自動選出と同じく
    静かに残り、本人が指定し直せばまた 1 回知らせる。選定の後に poller が更新していたら
    (再指定・パス)、本人の操作を優先して変えない(done 化と同じ compare-and-set)。
    DB の失敗は item 単位に閉じ(WARNING を残して False)、他の件の解除を止めない。
    """
    log_extra = {"run_id": run_id, "stage": "dig", "item_id": candidate.item_id}
    try:
        released = (
            conn.execute(
                "UPDATE dig_queue SET requested_at = NULL, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                "WHERE item_id = ? AND status = 'queued' AND updated_at = ? "
                "  AND angle IS ? AND requested_at IS ?",
                (
                    candidate.item_id,
                    candidate.queue_updated_at,
                    candidate.angle,
                    candidate.queue_requested_at,
                ),
            ).rowcount
            == 1
        )
    except sqlite3.Error as e:
        logger.warning(
            f"材料不足の指定を外せなかった item={candidate.item_id}: {type(e).__name__}: {e}"
            "(指定のまま残し、次の朝にもう一度知らせる)",
            extra=log_extra,
        )
        return False
    if released:
        logger.info(
            f"材料不足の指定を外した item={candidate.item_id}(失敗の通知を送った後。"
            "次の朝からは知らせない)",
            extra=log_extra,
        )
    else:
        logger.warning(
            f"材料不足の指定を外さなかった item={candidate.item_id}: 選定の後に本人が操作した"
            "(再指定・パス)。指定が残っていれば次の朝にもう一度知らせる",
            extra=log_extra,
        )
    return released


def _dig_one(
    conn: sqlite3.Connection,
    config: Config,
    engine_factory: Callable[[str], LLMEngine],
    review_engine_factory: Callable[[], LLMEngine] | None,
    candidate: DigCandidate,
    day: str,
    logger: logging.Logger,
    run_id: str | None,
) -> None:
    """1 item の深掘り: LLM 生成 → 生成物を台帳へ write-ahead(短トランザクション)。

    ファイル公開・カード送信はここでは行わない(_publish_pending_notes /
    _send_pending_cards が台帳から実行する)。生成と公開・送信を分けるのは、
    中断後の再実行が**同一生成物**から続きだけを行うため(ノートとカードの
    生成元が食い違わない。Codex P2)。
    """
    date_prefix = day.replace("-", "")
    slug = _note_slug(candidate)
    note_name = f"{date_prefix}-{candidate.item_id}-{slug}-深掘り"

    # 原文/対訳ノート: ツイート本文のみのアイテムは作らない(X 本文非保持 = GH#2。
    # Web 記事・gist は私的利用の範囲で全文保全・対訳可。設計 06 §5.2)
    is_tweet_body = _is_tweet_url(candidate.canonical_url)
    source_note_name: str | None = None
    source_note_body: str | None = None
    if not is_tweet_body:
        if _is_japanese_source(candidate):
            source_note_name = f"{date_prefix}-{candidate.item_id}-原文-{slug}"
            source_note_body = _source_note_ja(candidate, note_name)
        else:
            source_note_name = f"{date_prefix}-{candidate.item_id}-対訳-{slug}"
            source_note_body = _source_note_translated(
                config, engine_factory, candidate, note_name
            )

    draft, draft_engine = _generate_draft(config, engine_factory, candidate, source_note_name)
    # 引用は本文と照合してから査読に渡す(外す引用を査読させない。GH#394)
    draft = _verified_quotes(config, candidate, draft, logger, run_id)
    review: ReviewOutcome | None = None
    if review_engine_factory is not None:
        draft, review = _review_and_revise(
            conn,
            config,
            engine_factory,
            review_engine_factory,
            candidate,
            source_note_name,
            draft,
            draft_engine,
            logger,
            run_id,
        )
        # 直した版は引用も出し直すので、もう一度照合する(直さなかった版には何もしない)
        draft = _verified_quotes(config, candidate, draft, logger, run_id)
    note_body = _explainer_note(candidate, draft, source_note_name, review)
    card_text = _card_text(config, candidate, draft, note_name, source_note_name, review)
    review_findings = None
    if review is not None and review.status in ("clean", "revised", "revise_failed"):
        review_findings = json.dumps(
            [finding.model_dump() for finding in review.findings], ensure_ascii=False
        )

    # 生成は分オーダーだが、記帳は短く(poller と DB を共有するため)。
    # 接続は autocommit のため、台帳とキューの原子性は明示トランザクションで作る
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO dig_results
              (item_id, run_id, note_name, source_note_name, angle, card_text,
               note_markdown, source_note_markdown,
               review_status, review_findings, review_cost_usd, on_demand)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.item_id,
                run_id,
                note_name,
                source_note_name,
                candidate.angle,
                card_text,
                note_body,
                source_note_body,
                review.status if review is not None else None,
                review_findings,
                review.cost_usd if review is not None else 0.0,
                int(candidate.on_demand),
            ),
        )
        if candidate.from_digest:
            done = _record_digest_pick(conn, candidate.item_id)
        else:
            # compare-and-set: 生成中(分オーダー)に poller がこの行を更新していたら
            # (再指定 = queued + 新観点 / パス)、done で上書きせずユーザー操作を優先する
            # (新しい依頼は次回実行で掘られる。Codex P1)。生成物の公開・送信は行う。
            # updated_at は秒解像度で同一秒内の更新を見逃すため、status / angle /
            # requested_at の実値も比較する(全て同値の更新は意味的に no-op。Codex P2)。
            # 今すぐの依頼(GH#388)は掘れたので閉じる。依頼の印は updated_at を進めないので、
            # 朝の生成中に押された依頼もここで閉じる(朝のカードで満たされる)
            done = conn.execute(
                "UPDATE dig_queue SET status = 'done', "
                "now_requested_at = NULL, now_started_at = NULL, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                "WHERE item_id = ? AND status = 'queued' AND updated_at = ? "
                "  AND angle IS ? AND requested_at IS ?",
                (
                    candidate.item_id,
                    candidate.queue_updated_at,
                    candidate.angle,
                    candidate.queue_requested_at,
                ),
            ).rowcount
            if candidate.now_started_at is not None and done == 0:
                # 作っている間に本人が指定し直した(観点など)。キューの状態は本人の操作のまま
                # 残し(朝の生成中と同じ)、この依頼だけを閉じる。作り直さない
                close_now_request(
                    conn, candidate.item_id, candidate.now_started_at, to_morning=False
                )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    if done == 0:
        logger.info(
            f"生成中にキューが更新されたため状態を保持 item={candidate.item_id}",
            extra={"run_id": run_id, "stage": "dig", "item_id": candidate.item_id},
        )
    logger.info(
        f"deepdive 生成 item={candidate.item_id} note={note_name}",
        extra={"run_id": run_id, "stage": "dig", "item_id": candidate.item_id},
    )


def _record_digest_pick(conn: sqlite3.Connection, item_id: int) -> int:
    """digest の上位から掘った item のキューの行を記帳する(設計 06 §3 の ③)。done にしたら 1。

    _dig_one の記帳のトランザクションの中で呼ぶ。行が無ければ source = 'digest'・status = 'done'
    で足す。ON CONFLICT は item_id の UNIQUE だけに効かせる(INSERT OR IGNORE は CHECK の違反まで
    黙って捨てるので使わない)。生成中に poller などが同じ item の行を作っていたら、本人の明示の
    操作(requested_at のある指定・passed)は変えず、自動投入の行(requested_at の無い queued。
    X のブックマークの取り込み)だけを done にする(掘ったばかりの item を翌朝にまた掘らない)。
    source は先にできた行の値のまま(初回投入の経路で以後不変)。どの場合も生成物は記帳する。
    今すぐの依頼のボタンで足した行(requested_at は付かない。GH#388)も done にし、依頼を閉じる
    (このカードで満たされる)。
    """
    inserted = conn.execute(
        "INSERT INTO dig_queue (item_id, status, source) VALUES (?, 'done', 'digest') "
        "ON CONFLICT(item_id) DO NOTHING",
        (item_id,),
    ).rowcount
    if inserted == 1:
        return 1
    return conn.execute(
        "UPDATE dig_queue SET status = 'done', "
        "now_requested_at = NULL, now_started_at = NULL, "
        "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
        "WHERE item_id = ? AND status = 'queued' AND requested_at IS NULL",
        (item_id,),
    ).rowcount


def _publish_pending_notes(
    conn: sqlite3.Connection,
    config: Config,
    logger: logging.Logger,
    run_id: str | None,
    vault: VaultGuard,
) -> list[str]:
    """未公開ノート(過去の中断分を含む)を台帳の本文から vault へ書き出す。

    公開後は本文列を NULL 化して台帳を軽く保つ(恒久保存はノート側 = ADR 0004)。

    vault へのアクセスは 1 件ごとに時間の上限を付ける(GH#234。仕組みは GH#145 と同じ)。
    macOS のプライバシーの確認待ちなどで止まると、カードの送信と朝バッチの後続の段・失敗通知
    まで止まるため。上限を超えたら、その件と残りの件は本文を台帳に持ったまま未公開にして
    先へ進む(公開済みの件のカードは送り、未公開の件は次の実行で公開し直す)。止まった
    スレッドは後から書き終えうるので、それが終わるまで公開のロック(_PublishLock)を握り、
    別の実行に同じノートを公開させない。
    """
    failures: list[str] = []
    rows = conn.execute(
        "SELECT id, item_id, note_name, source_note_name, note_markdown,"
        " source_note_markdown FROM dig_results WHERE published_at IS NULL ORDER BY id"
    ).fetchall()
    # 出来事の調査(GH#415)の未公開のノートも、同じロック・同じ時間の上限で書き出す
    investigations = conn.execute(
        "SELECT id, note_name, note_markdown FROM news_investigations"
        " WHERE status = 'done' AND published_at IS NULL ORDER BY id"
    ).fetchall()
    units = [_dig_note_unit(conn, config, row, logger, run_id) for row in rows] + [
        _investigation_note_unit(conn, config, row, logger, run_id) for row in investigations
    ]
    if not units:
        return failures
    lock = _PublishLock.acquire(conn)
    if lock is None:
        logger.error(
            "別の実行のノートの公開が終わっていない(vault が応答しないまま止まっている可能性)"
            f"ので、この実行では公開しない(未公開の {len(units)} 件は次の実行へ。runbook §11)",
            extra={"run_id": run_id, "stage": "dig"},
        )
        failures.append(f"ノート公開: 別の実行の公開が終わっていない(未公開 {len(units)} 件)")
        return failures

    def publish(unit: _PendingNote) -> None:
        if not lock.begin_write():  # 見切られた後に動き出した。ロックは放されたので書かない
            return
        try:
            unit.write()
        finally:
            lock.end_write()  # 見切られた後に終わったなら、ここで放す

    try:
        for index, unit in enumerate(units):
            try:
                vault.call(lambda unit=unit: publish(unit))
            except VaultStalled as e:
                unpublished = len(units) - index
                logger.error(
                    "vault が応答しないので、ノートの公開をこの実行では止める"
                    f"(未公開の {unpublished} 件は本文を台帳に残し、次の実行で公開する。"
                    f"runbook §11): {e}",
                    extra=unit.log_extra,
                )
                failures.append(
                    f"{unit.name}: ノート公開 VaultStalled"
                    f"(未公開 {unpublished} 件は次の実行へ)"
                )
                break
            except Exception as e:
                logger.error(
                    f"DeepDive ノート公開失敗 {unit.log_name}: {type(e).__name__}",
                    extra=unit.log_extra,
                    exc_info=True,
                )
                failures.append(f"{unit.name}: ノート公開 {type(e).__name__}")
                continue
            unit.mark_published()
    finally:
        # 見切ったスレッドがまだ書いているなら、それが終わるまで(またはプロセスの終了まで)握る
        lock.leave()
    return failures


@dataclass(frozen=True)
class _PendingNote:
    """公開を待つノートの 1 行ぶん(深掘りの dig_results か、出来事の調査の news_investigations)。"""

    name: str  # 失敗の記録の名前(item 5 / investigation 3)
    log_name: str  # ログの名前(item=5 / investigation=3)
    log_extra: dict
    write: Callable[[], None]  # vault-io のスレッドで動く。DB に触れない
    mark_published: Callable[[], None]  # 書けた後に台帳に記帳する(本文の列を NULL にする)


def _dig_note_unit(
    conn: sqlite3.Connection,
    config: Config,
    row: sqlite3.Row,
    logger: logging.Logger,
    run_id: str | None,
) -> _PendingNote:
    def mark() -> None:
        conn.execute(
            "UPDATE dig_results SET published_at = strftime('%Y-%m-%dT%H:%M:%SZ','now'),"
            " note_markdown = NULL, source_note_markdown = NULL WHERE id = ?",
            (row["id"],),
        )

    return _PendingNote(
        name=f"item {row['item_id']}",
        log_name=f"item={row['item_id']}",
        log_extra={"run_id": run_id, "stage": "dig", "item_id": row["item_id"]},
        write=partial(_publish_row_notes, config, row, logger, run_id),
        mark_published=mark,
    )


def _investigation_note_unit(
    conn: sqlite3.Connection,
    config: Config,
    row: sqlite3.Row,
    logger: logging.Logger,
    run_id: str | None,
) -> _PendingNote:
    def write() -> None:
        _publish_note(
            _deepdive_dir(config),
            f"{row['note_name']}.md",
            neutralize_note(row["note_markdown"]),
            logger,
            run_id,
        )

    def mark() -> None:
        conn.execute(
            "UPDATE news_investigations SET published_at = strftime('%Y-%m-%dT%H:%M:%SZ','now'),"
            " note_markdown = NULL WHERE id = ?",
            (row["id"],),
        )

    return _PendingNote(
        name=f"investigation {row['id']}",
        log_name=f"investigation={row['id']}",
        log_extra={"run_id": run_id, "stage": "dig", "extra": {"investigation": row["id"]}},
        write=write,
        mark_published=mark,
    )


def _publish_row_notes(
    config: Config, row: sqlite3.Row, logger: logging.Logger, run_id: str | None
) -> None:
    """台帳の 1 行のノート(解説・原文/対訳)を書き出す(vault-io のスレッドで動く。DB に触れない)。

    本文は書き出す直前にも、外部の埋め込みと生の HTML を直す(GH#377)。この直しより前に
    台帳へ記帳した未公開の本文も、直してから公開するため(組み立てで直した本文は変わらない)。
    """
    deepdive_dir = _deepdive_dir(config)
    if row["note_markdown"] is not None:  # NULL = 旧経路の公開済み残骸は再書出不能
        _publish_note(
            deepdive_dir,
            f"{row['note_name']}.md",
            neutralize_note(row["note_markdown"]),
            logger,
            run_id,
        )
    if row["source_note_name"] is not None and row["source_note_markdown"] is not None:
        _publish_note(
            deepdive_dir,
            f"{row['source_note_name']}.md",
            neutralize_note(row["source_note_markdown"]),
            logger,
            run_id,
        )


class _PublishLock:
    """ノートの公開を、同じ台帳を使う実行どうし(朝バッチと `intel dig`)で 1 つに絞る(GH#234)。

    vault が応答せず見切ったスレッドは中断できず、後から書き終えうる。その間に別の実行が
    同じノートを公開し、本人が take を足したあとで、遅れた書き込みがノートを置き換えないよう、
    見切ったときはそのスレッドが終わるまで握る。flock なのでプロセスが終われば外れる
    (止まったスレッドもプロセスと一緒に消える)。answer.lock(GH#145)と同じ考え方。

    放す役は 1 つの mutex の下で決める: 書き出しのスレッドの終わり(end_write)と呼び出し側の
    離脱(leave)のうち、後になった側が放す。見切りの判定の直後にスレッドが書き終えても、
    どちらも放さずに残ることはない。
    """

    def __init__(self, fd: int | None) -> None:
        self._fd = fd
        self._mutex = threading.Lock()
        self._writing = False  # 書き出しのスレッドが動いている
        self._left = False  # 呼び出し側が公開を終えた(見切った場合を含む)

    @classmethod
    def acquire(cls, conn: sqlite3.Connection) -> _PublishLock | None:
        """取れなければ None(別の実行が _PUBLISH_LOCK_WAIT_S を過ぎても握っている)。"""
        path = _publish_lock_path(conn)
        if path is None:  # ファイルでない DB は、別の実行と台帳を共有しない
            return cls(None)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            deadline = time.monotonic() + _PUBLISH_LOCK_WAIT_S
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return cls(fd)
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.1)
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)
        return None

    def begin_write(self) -> bool:
        """書き出しのスレッドの始まり。呼び出し側が既に離れていれば False(書かない)。"""
        with self._mutex:
            if self._left:
                return False
            self._writing = True
            return True

    def end_write(self) -> None:
        """書き出しのスレッドの終わり。呼び出し側が先に離れていれば、ここで放す。"""
        with self._mutex:
            self._writing = False
            if self._left:
                self._close()

    def leave(self) -> None:
        """呼び出し側の離脱。書き出し中のスレッドがあれば、放すのはその終わり(end_write)に任せる。"""
        with self._mutex:
            self._left = True
            if not self._writing:
                self._close()

    def _close(self) -> None:
        """放す(fd を閉じると flock も外れる。2 回目は何もしない)。mutex の中で呼ぶ。"""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def _publish_lock_path(conn: sqlite3.Connection) -> Path | None:
    """この接続の DB と同じ置き場のロックファイル(設定の値でなく、開いている台帳から決める)。"""
    db_file = _db_file(conn)
    return db_file.with_name(_PUBLISH_LOCK_NAME) if db_file is not None else None


def _db_file(conn: sqlite3.Connection) -> Path | None:
    """この接続が開いている DB のファイル(メモリ上の DB は None)。"""
    for _seq, name, file in conn.execute("PRAGMA database_list").fetchall():
        if name == "main":
            return Path(file) if file else None
    return None


# dig.lock の持ち主の種類(ロックの file に書く。GH#388)
_DigLockHolder = Literal["stage", "dig-now"]


@contextlib.contextmanager
def _dig_lock(
    conn: sqlite3.Connection, holder: _DigLockHolder = "stage"
) -> Iterator[bool]:
    """調査段の実行を、同じ台帳を使う実行どうし(朝バッチの dig 段と `intel dig`)で 1 つに絞る
    (GH#196)。取れたら True、別の実行が握っていれば待たずに False。`intel dig-now` は依頼の
    取り出しと、公開・送信の間だけ持つ(GH#388。run_dig_now)。

    holder は持ち主の種類で、持つ間だけロックの file に書く(放す前に消す)。取れなかった朝の
    dig 段が、持ち主が dig-now か朝の段・`intel dig` かを見分けるのに使う(_stage_dig_lock)。

    SQLite のロックは 1 トランザクションの間しか続かず、LLM の待ち(分オーダー)をまたいで
    書き込みのトランザクションを持つと poller の書き込みを止めるので、DB の隣の専用ファイルに
    flock を取る(db._migration_lock と同じ考え方)。公開のロック(_PublishLock)はこの中でだけ
    取る。入れ子の順はこの 1 通りで、こちらは待たないので、見切ったスレッドが公開のロックを
    握ったまま残っていてもデッドロックしない(公開のロックの待ちには上限がある)。メモリ上の
    DB は別の実行と台帳を共有しない。
    """
    with _db_side_lock(conn, _DIG_LOCK_SUFFIX, holder=holder) as acquired:
        yield acquired


def _dig_lock_holder(conn: sqlite3.Connection) -> str:
    """今 dig.lock の file に書かれた持ち主の種類(空 = 持ち主が書く前・放した後・読めない)。"""
    db_file = _db_file(conn)
    if db_file is None:
        return ""
    try:
        return db_file.with_name(db_file.name + _DIG_LOCK_SUFFIX).read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return ""


@contextlib.contextmanager
def _stage_dig_lock(
    conn: sqlite3.Connection,
    *,
    wait_s: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[Literal["acquired", "busy", "dig_now"]]:
    """朝の dig 段と `intel dig` の dig.lock(GH#196・GH#388)。

    - acquired: 取れた
    - busy: 別の朝の dig 段か `intel dig` が持っている(待たない。今までどおり)
    - dig_now: `intel dig-now` が持ったまま、上限(既定 _DIG_NOW_YIELD_WAIT_S)まで待っても
      取れなかった
    dig-now は `<DB>.dig-now.lock` を持つ間だけ dig.lock を持つ(取るのは dig-now.lock の後、放すのは
    前)。そこで先に dig-now.lock を待たずに取ってみる。取れたら dig-now は動いておらず、持つ間は
    始まらないので、そのまま dig.lock が取れなければ持ち主は朝の dig 段か `intel dig` と決まる
    (確かめる間に dig-now が終わっても取り違えない)。dig.lock を取ったら dig-now.lock はすぐ放す
    (dig-now は朝の段の間に起動しても何もせずに終わる)。dig-now.lock が取れなければ dig-now が
    動いている。そのときの dig.lock の持ち主は、ロックの file に書かれた種類で見分ける。朝の段か
    `intel dig` なら待たない(dig-now の生成中に `intel dig` が持っているとき)。dig-now か、書く前・
    放した後(空)なら取れるまで待つ。dig-now は依頼の取り出しと公開・送信の間だけ持ち、生成の
    間は放すので、ふつうは数秒で取れる。
    """
    deadline = clock() + (_DIG_NOW_YIELD_WAIT_S if wait_s is None else wait_s)
    while True:
        with contextlib.ExitStack() as held:
            guard = contextlib.ExitStack()
            no_dig_now = guard.enter_context(_db_side_lock(conn, _DIG_NOW_LOCK_SUFFIX))
            acquired = held.enter_context(_dig_lock(conn))
            guard.close()
            if acquired:
                yield "acquired"
                return
            if no_dig_now or _dig_lock_holder(conn) == "stage":
                yield "busy"
                return
        if clock() >= deadline:
            yield "dig_now"
            return
        sleep(_DIG_LOCK_POLL_S)


@contextlib.contextmanager
def _db_side_lock(
    conn: sqlite3.Connection, suffix: str, *, holder: str | None = None
) -> Iterator[bool]:
    """DB の隣の `<DB><suffix>` に flock を待たずに取る。取れたら True。メモリ上の DB は True。

    holder があれば、持つ間だけその文字列を file に書き、放す前に消す(持ち主の種類。dig.lock)。
    """
    db_file = _db_file(conn)
    if db_file is None:
        yield True
        return
    lock_path = db_file.with_name(db_file.name + suffix)
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            acquired = False
        if acquired and holder is not None:
            os.ftruncate(fd, 0)
            os.pwrite(fd, holder.encode("ascii"), 0)
        try:
            yield acquired
        finally:
            if acquired and holder is not None:
                os.ftruncate(fd, 0)
    finally:
        os.close(fd)  # close で flock は解放される


def _send_pending_cards(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    logger: logging.Logger,
    run_id: str | None,
) -> list[str]:
    """未送信カードを送り message_id を記帳する(公開済みの行だけ。1 件ごとに永続化)。

    送信成功と記帳の間で中断すると同一カードが再送されうるが、これは digest 配信
    (deliver_digest = 送信 → digest_messages 記帳)と同じ、**設計として許容済みの
    稀な二重送信**(設計 05 §8。2026-07-08 監査で「無配信より二重送信」に倒すと
    確定)。fail-closed(記帳先行)にすると中断時に未達のまま送信済み扱いになり、
    通知の欠落 = 沈黙障害の方が実害が大きい。
    """
    failures: list[str] = []
    rows = conn.execute(
        "SELECT r.id, r.item_id, r.card_text, i.canonical_url FROM dig_results r "
        "JOIN items i ON i.id = r.item_id "
        "WHERE r.message_id IS NULL AND r.published_at IS NOT NULL ORDER BY r.id"
    ).fetchall()
    for row in rows:
        # 🔗 元記事 / 📖 詳しく / 💬 話す(GH#141・GH#407。スマホの Telegram で完結させる)と
        # ✅ 読んだ / 👎 不要(GH#398・GH#403)。掘り直したカードでは、前のカードで付けた
        # 今の状態を出す
        keyboard = dig_card_keyboard(
            row["id"],
            row["canonical_url"],
            with_requests=config.dig.answer_daily_cap > 0,
            read=is_read(conn, row["item_id"]),
            dismissed=is_dismissed(conn, row["item_id"]),
        )
        try:
            message_id = api.send_message(
                config.telegram.owner_chat_id, row["card_text"], reply_markup=keyboard
            )
        except Exception as e:
            logger.error(
                f"概要カード送信失敗 item={row['item_id']}: {type(e).__name__}",
                extra={"run_id": run_id, "stage": "dig", "item_id": row["item_id"]},
            )
            failures.append(f"item {row['item_id']}: カード送信 {type(e).__name__}")
            continue
        # 接続は autocommit(送信 1 件ごとに即永続化 = 中断しても再送は未送信分のみ)
        conn.execute(
            "UPDATE dig_results SET message_id = ?, "
            "sent_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?",
            (message_id, row["id"]),
        )
    failures.extend(_send_pending_investigation_cards(conn, config, api, logger, run_id))
    return failures


def _send_pending_investigation_cards(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    logger: logging.Logger,
    run_id: str | None,
) -> list[str]:
    """出来事の調査(GH#415)の未送信カードを送る(公開済みの行だけ。深掘りのカードと同じ扱い)。

    ボタンは付けない(返事・読んだ・不要はこの Issue の外。材料のリンクはカードの本文にある)。
    """
    failures: list[str] = []
    rows = conn.execute(
        "SELECT id, card_text FROM news_investigations WHERE status = 'done'"
        " AND message_id IS NULL AND published_at IS NOT NULL ORDER BY id"
    ).fetchall()
    for row in rows:
        try:
            message_id = api.send_message(config.telegram.owner_chat_id, row["card_text"])
        except Exception as e:
            logger.error(
                f"出来事の調査のカード送信失敗 investigation={row['id']}: {type(e).__name__}",
                extra={"run_id": run_id, "stage": "dig", "extra": {"investigation": row["id"]}},
            )
            failures.append(f"investigation {row['id']}: カード送信 {type(e).__name__}")
            continue
        conn.execute(
            "UPDATE news_investigations SET message_id = ?, "
            "sent_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?",
            (message_id, row["id"]),
        )
    return failures


# --- LLM 生成 ---


def _generate_draft(
    config: Config,
    engine_factory: Callable[[str], LLMEngine],
    candidate: DigCandidate,
    source_note_name: str | None,
) -> tuple[DigDraft, str]:
    """解説のドラフトと、それを実際に書いたエンジンの name(GH#180)。

    予備(FallbackEngine)の name は最後に答えたエンジンのもので、呼び出しの直後に読めば
    このドラフトを書いたエンジンになる(Codex が失敗した回は予備の Claude。設計 00 §5)。
    """
    engine = engine_factory(config.dig.reasoning_effort)
    prompt = build_dig_prompt(config, candidate, source_note_name)
    draft = complete_with_retry(engine, prompt, DigDraft, timeout_s=config.dig.timeout_s)
    return draft, engine.name


def build_dig_prompt(
    config: Config, candidate: DigCandidate, source_note_name: str | None
) -> str:
    nonce = secrets.token_hex(4)
    template = (_PROMPTS_DIR / f"{PROMPT_VERSION}.md").read_text(encoding="utf-8")
    profile = load_interest_profile(config.profile_path).body

    if candidate.angle:
        brief = (
            f"指定観点: {sanitize(candidate.angle, 500, nonce)}\n"
            "この観点への答えを最優先の問いとして読む。"
        )
    else:
        brief = f"観点指定なし(自動選出)。{AUTO_BRIEF}。"

    meta_lines = _item_meta_lines(candidate, nonce)
    if candidate.summary:
        meta_lines.append(f"- 既存分析の要約: {sanitize(candidate.summary, 300, nonce)}")
    body = _prompt_body(config, candidate, nonce)

    if source_note_name is not None:
        source_note_link = f"原文/対訳ノート `[[{source_note_name}]]` への wikilink"
    else:
        source_note_link = "原典 URL(原文/対訳ノートは無し)"
    if _is_tweet_url(candidate.canonical_url):
        quote_policy = (
            "引用の制約: この本文は X ポストであり、全文の再掲・長い引用をしない"
            "(必要最小限の抜粋のみ。保存ポリシー上の制約)。"
        )
    else:
        quote_policy = "引用は必要な範囲で可(出典を明示)。"
    # 原文からの引用(quotes)の規則(GH#394)。数と長さの上限は _verified_quotes でも守らせる
    if _is_tweet_url(candidate.canonical_url):
        quote_limit = (
            f"この本文は X ポストなので、選ぶのは {_TWEET_QUOTES_MAX} 個まで、ポストの一部の"
            "短い抜粋だけにする(全文や大半を写さない)。"
        )
    else:
        quote_limit = ""
    if _is_japanese_source(candidate):
        quote_ja_rule = "元記事は日本語なので、空文字にする(original をそのまま載せる)。"
    else:
        quote_ja_rule = (
            "original の日本語訳。カードとノートには和訳だけを載せるので、原文を読まなくても"
            "意味が通る自然な日本語にする。訳に推測を足さない。"
        )

    return fill_template(
        template,
        {
            "writing_guide": load_writing_guide(),  # 信頼される側(囲みの外。GH#381)
            "brief": brief,
            "interest_profile": profile,
            "meta": "\n".join(meta_lines),
            "body": body,
            "source_note_link": source_note_link,
            "quote_policy": quote_policy,
            "quote_limit": quote_limit,
            "quote_ja_rule": quote_ja_rule,
            "schema": json.dumps(DigDraft.model_json_schema(), ensure_ascii=False),
            "nonce": nonce,
        },
    )


def _item_meta_lines(candidate: DigCandidate, nonce: str) -> list[str]:
    return [
        f"- title: {sanitize(candidate.title, 300, nonce) or '(なし)'}",
        f"- author: {sanitize(candidate.author, 100, nonce) or '(不明)'}",
        f"- published_at: {sanitize(candidate.published_at, 30, nonce) or '(不明)'}",
        f"- url: {sanitize(candidate.canonical_url, 500, nonce)}",
    ]


def _prompt_body(config: Config, candidate: DigCandidate, nonce: str) -> str:
    """解説の生成とレビューに渡す本文(同じ切り詰め。書き手と査読者が同じ範囲を読む)。

    元の本文を先に切り詰めてから除去する。LLM に渡す範囲が元の本文の先頭 body_max_chars 字に
    決まり、引用の照合(_verified_quotes)が同じ範囲を元の本文で見られる(GH#394。除去してから
    切ると、除去で縮んだ分だけ範囲が元の本文の先へずれ、その量は除去の位置で変わる)。
    """
    body = candidate.body or ""
    limit = config.dig.body_max_chars
    if len(body) > limit:
        return sanitize_body(body[:limit], nonce) + "\n(…以降は長さ上限で切り詰め)"
    return sanitize_body(body, nonce)


# --- 原文からの引用(GH#394) ---


def _verified_quotes(
    config: Config,
    candidate: DigCandidate,
    draft: DigDraft,
    logger: logging.Logger,
    run_id: str | None,
) -> DigDraft:
    """ドラフトの引用を、本文にあるものだけに絞る(LLM が作った文を引用として載せない)。

    照合の相手は元の本文の、LLM に渡した範囲(先頭 body_max_chars 字。_prompt_body。切り詰めの注記は
    含めない)。プロンプトに入れる前の除去(sanitize_body)は掛けない: 除去で消えた字を欠いた写しを、
    元の本文と違う形のまま載せないため(外れる側に倒れる)。範囲の直後の字も見て、上限で切れた英単語の
    途中で終わる写しは採らない。
    意味の変わらない違い(空白・全角の英数字・曲がった引用符)だけを同じとみなす(_quote_match_key)。
    写しのままで本文に無ければ、全体を囲む引用符の 1 組だけを外して試す(_matched_original)。
    外すのは、本文に無い・短すぎる・同じ写しの繰り返し・一言が空・日本語以外で和訳が空の引用。
    X の投稿は 1 個までにし、ポストの半分を超える写しは外す(全文の再掲をしない = GH#2)。
    日本語の元記事では ja を空にそろえる(載せるのは原文。_quote_text)。
    """
    if not draft.quotes:
        return draft
    body = candidate.body or ""
    head = body[: config.dig.body_max_chars]
    haystack = _quote_match_key(head)
    # 範囲の直後の字(上限で英単語が切れていれば、その続き)。範囲が空白で終わるなら語は続かない
    tail = "" if head[-1:].isspace() else body[len(head) : len(head) + 1]
    tail = tail.translate(_QUOTE_MATCH_TRANSLATION).strip()
    is_tweet = _is_tweet_url(candidate.canonical_url)
    japanese = _is_japanese_source(candidate)
    limit = _TWEET_QUOTES_MAX if is_tweet else QUOTES_MAX
    kept: list[DigQuote] = []
    seen: set[str] = set()
    for quote in draft.quotes:
        matched = _matched_original(quote.original, haystack, tail)
        ja = "" if japanese else quote.ja.strip()
        if matched is None or not quote.why.strip() or (not japanese and not ja):
            continue
        original, key = matched
        if key in seen or (is_tweet and len(key) * 2 > len(haystack)):
            continue
        seen.add(key)
        kept.append(DigQuote(original=original, ja=ja, why=quote.why.strip()))
        if len(kept) == limit:
            break
    if len(kept) < len(draft.quotes):
        logger.info(
            f"引用 {len(draft.quotes)} 件のうち {len(kept)} 件を載せる"
            f"(本文に無い・上限を超えたなどを外した) item={candidate.item_id}",
            extra={"run_id": run_id, "stage": "dig", "item_id": candidate.item_id},
        )
    return draft.model_copy(update={"quotes": kept})


def _matched_original(original: str, haystack: str, tail: str = "") -> tuple[str, str] | None:
    """本文にある写しの形と、その照合の形。本文に無ければ None。

    まず写しのまま照合し、無ければ全体を囲む引用符の 1 組だけを外して試す(本文の字は削らない)。
    写しの両端が英単語の途中にかかる一致は採らない(_at_word_boundary)。
    """
    text = original.strip()
    candidates = [text]
    for opening, closing in _QUOTE_WRAPPERS:
        if len(text) > 2 and text.startswith(opening) and text.endswith(closing):
            candidates.append(text[len(opening) : -len(closing)].strip())
            break
    for candidate in candidates:
        key = _quote_match_key(candidate)
        if len(key.replace(" ", "")) >= _QUOTE_MIN_CHARS and _at_word_boundary(
            haystack, key, tail
        ):
            return candidate, key
    return None


def _quote_match_key(text: str) -> str:
    """写しの照合に使う形。全角の英数字と記号・曲がった引用符をそろえ、空白を 1 つにまとめる。

    日本語の字の隣の空白は外す(本文の折り返しの改行や、写すときに入る・消える空白で、本物の
    引用を外さないため)。英単語の間の空白は残す(語の区切りが変わる一致を通さないため)。
    """
    normalized = text.translate(_QUOTE_MATCH_TRANSLATION)
    return _SPACE_NEXT_TO_CJK.sub("", " ".join(normalized.split()))


def _at_word_boundary(haystack: str, needle: str, tail: str = "") -> bool:
    """needle が haystack に、両端で英単語を切らずに現れるか(now が nowhere の頭に一致しない)。

    tail は haystack の直後の元の本文の字(haystack が本文の途中で切ったものなら、その続き)。
    """
    start = haystack.find(needle)
    while start != -1:
        end = start + len(needle)
        before = haystack[start - 1] if start > 0 else ""
        after = haystack[end] if end < len(haystack) else tail
        if not (_in_one_word(before, needle[0]) or _in_one_word(needle[-1], after)):
            return True
        start = haystack.find(needle, start + 1)
    return False


def _in_one_word(left: str, right: str) -> bool:
    """隣り合う 2 字(端なら空)が、1 つの英単語の中で続いているか(日本語の字は語の境目を見ない)。"""
    return all(ch.isalnum() and _CJK_CHAR_RE.match(ch) is None for ch in (left, right))


def _quote_text(quote: DigQuote) -> str:
    """カードとノートに載せる文。日本語以外の元記事は和訳だけ(GH#394 で本人が決めた)。"""
    return quote.ja or quote.original


# --- 別系統の AI のレビュー(GH#153 / 設計 06 §4) ---


class _CostMeter:
    """呼び出しごとの usage(LLMUsage)を合算する。やり直しの分も課金されるため。

    送った後に応答の usage を受け取れず費用が分からない呼び出し(時間切れ・応答の切断など)は、
    エンジンが残す課金されうる最大の額(`last_cost_bound_usd`)を積む。0 円で数えると、月の
    上限の判定が実際の課金より少なめの累計で行われるため(GH#323。予備の台帳と同じ。GH#241)。
    どちらも無い呼び出し(送っていない・エラーの応答・その属性を持たないエンジン)は 0。
    """

    def __init__(self, engine: LLMEngine) -> None:
        self._engine = engine
        self.name = engine.name
        self.model = str(getattr(engine, "model", engine.name))
        self.cost_usd = 0.0

    def complete_structured[M: BaseModel](
        self, prompt: str, schema: type[M], timeout_s: int = DEFAULT_TIMEOUT_S
    ) -> M:
        try:
            return self._engine.complete_structured(prompt, schema, timeout_s)
        finally:
            usage = getattr(self._engine, "last_usage", None)
            if usage is not None:
                self.cost_usd += usage.cost_usd
            else:
                bound = getattr(self._engine, "last_cost_bound_usd", None)
                if bound is not None:
                    self.cost_usd += bound


def _review_and_revise(
    conn: sqlite3.Connection,
    config: Config,
    engine_factory: Callable[[str], LLMEngine],
    review_engine_factory: Callable[[], LLMEngine],
    candidate: DigCandidate,
    source_note_name: str | None,
    draft: DigDraft,
    draft_engine: str,
    logger: logging.Logger,
    run_id: str | None,
) -> tuple[DigDraft, ReviewOutcome]:
    """レビュー → 指摘があれば書き手が 1 回だけ直す。戻り値 = (届けるドラフト, 結果)。

    LLM の失敗(LLMError)はここで握り、直す前のドラフトで先へ進む(レビューは上乗せ)。
    それ以外の例外(実装の誤り)は握らず、item の失敗として段に伝える。

    draft_engine はドラフトを実際に書いたエンジンの name。レビュー役と同じなら、同じ系統の
    点検は別の AI のレビューにならないので、呼ばずに省く(GH#180。費用も掛けない)。主は Codex、
    レビュー役は Claude API なので、同じになるのは Codex が失敗して予備の Claude が書いた回。
    月の上限より先に見る(上限に達した月でも、予備が書いたことを出す)。
    """
    extra = {"run_id": run_id, "stage": "dig", "item_id": candidate.item_id}
    if draft_engine == _REVIEWER_ENGINE:
        logger.warning(
            f"レビューを省略: ドラフトを書いたエンジン({draft_engine})がレビュー役と同じ"
            "(予備の Claude が書いたため、別の AI のレビューにならない)"
            f" item={candidate.item_id}",
            extra=extra,
        )
        return draft, ReviewOutcome("skipped", same_family=True)
    cap = config.dig.review_monthly_cap_usd
    spent = _review_cost_this_month(conn)
    if spent >= cap:
        logger.warning(
            f"レビューを省略: 今月のレビュー費用 ${spent:.2f} が上限 ${cap:.2f} に達している",
            extra=extra,
        )
        return draft, ReviewOutcome("skipped")

    meter: _CostMeter | None = None
    try:
        meter = _CostMeter(review_engine_factory())
        prompt = build_review_prompt(config, candidate, draft)
        review = complete_with_retry(meter, prompt, DigReview, timeout_s=config.dig.timeout_s)
    except LLMError as e:
        logger.warning(
            f"レビュー失敗(未レビューのまま届ける) item={candidate.item_id}: "
            f"{type(e).__name__}: {e}",
            extra=extra,
        )
        if meter is None:
            return draft, ReviewOutcome("failed")
        return draft, ReviewOutcome("failed", meter.model, (), meter.cost_usd)

    findings = tuple(review.findings[:REVIEW_MAX_FINDINGS])
    if len(review.findings) > REVIEW_MAX_FINDINGS:
        logger.warning(
            f"レビューの指摘 {len(review.findings)} 件のうち先頭 {REVIEW_MAX_FINDINGS} 件だけ使う",
            extra=extra,
        )
    if not findings:
        return draft, ReviewOutcome("clean", meter.model, (), meter.cost_usd)
    try:
        revised = _revise_draft(
            config, engine_factory, candidate, source_note_name, draft, findings
        )
    except LLMError as e:
        logger.warning(
            f"指摘を受けた直しに失敗(直す前のまま届ける) item={candidate.item_id}: "
            f"{type(e).__name__}: {e}",
            extra=extra,
        )
        return draft, ReviewOutcome("revise_failed", meter.model, findings, meter.cost_usd)
    logger.info(
        f"レビューの指摘 {len(findings)} 件を受けて 1 回直した item={candidate.item_id}",
        extra=extra,
    )
    return revised, ReviewOutcome("revised", meter.model, findings, meter.cost_usd)


def _review_cost_this_month(conn: sqlite3.Connection) -> float:
    """今の UTC の暦月のレビュー費用の合計。

    月は doctor #9(month_cost_summary)の当月と同じ UTC の暦月にそろえる(上限の判定と
    doctor の表示で同じ額を見る)。JST の朝 9 時前の月初は、まだ前の月として数える。
    """
    month_prefix = datetime.now(UTC).strftime("%Y-%m")
    return conn.execute(
        "SELECT coalesce(sum(review_cost_usd), 0) AS cost FROM dig_results "
        "WHERE created_at LIKE ? || '%'",
        (month_prefix,),
    ).fetchone()["cost"]


def build_review_prompt(config: Config, candidate: DigCandidate, draft: DigDraft) -> str:
    """査読のプロンプト。関心プロファイルと既存分析の要約は渡さない(元記事との照合だけ)。"""
    nonce = secrets.token_hex(4)
    template = (_PROMPTS_DIR / f"{REVIEW_PROMPT_VERSION}.md").read_text(encoding="utf-8")
    draft_text = (
        "## 概要カード\n"
        f"- 見出し: {draft.title_ja}\n"
        f"- 書き手: {draft.writer_stance}\n"
        f"- あなたにとって: {draft.for_you}\n"
        f"- 世の中にとって: {draft.for_world}\n\n"
        f"{_review_quotes(draft)}"
        "## 解説ノート\n"
        f"{draft.note_markdown}"
    )
    return fill_template(
        template,
        {
            "meta": "\n".join(_item_meta_lines(candidate, nonce)),
            "body": _prompt_body(config, candidate, nonce),
            "draft": sanitize_body(draft_text, nonce),
            "schema": json.dumps(DigReview.model_json_schema(), ensure_ascii=False),
            "max_findings": str(REVIEW_MAX_FINDINGS),
            "nonce": nonce,
        },
    )


def _review_quotes(draft: DigDraft) -> str:
    """査読に渡す引用(GH#394)。カードとノートの両方に載るので 1 つの節にまとめる。

    和訳のあるものは原文も添える(載せるのは和訳だけだが、誤訳を元記事と照らして見つけるため)。
    """
    if not draft.quotes:
        return ""
    lines = ["## 原文から(概要カードと解説ノートの両方に載る)"]
    for index, quote in enumerate(draft.quotes, start=1):
        shown = f"和訳: {quote.ja} / 原文: {quote.original}" if quote.ja else quote.original
        lines.append(f"{index}. {shown} / 一言: {quote.why}")
    return "\n".join(lines) + "\n\n"


def _revise_draft(
    config: Config,
    engine_factory: Callable[[str], LLMEngine],
    candidate: DigCandidate,
    source_note_name: str | None,
    draft: DigDraft,
    findings: tuple[ReviewFinding, ...],
) -> DigDraft:
    """書き手(解説と同じエンジン・effort)が、元の指示 + 前回のドラフト + 指摘で直す。"""
    engine = engine_factory(config.dig.reasoning_effort)
    nonce = secrets.token_hex(4)
    template = (_PROMPTS_DIR / f"{REVISE_PROMPT_VERSION}.md").read_text(encoding="utf-8")
    finding_lines = [
        f"{index}. [{finding.severity} / {finding.kind}] "
        f"場所: {sanitize(finding.location, _FINDING_LOCATION_MAX, nonce)} / "
        f"問題: {sanitize(finding.problem, _FINDING_TEXT_MAX, nonce)} / "
        f"直し方: {sanitize(finding.fix, _FINDING_TEXT_MAX, nonce)}"
        for index, finding in enumerate(findings, start=1)
    ]
    prompt = fill_template(
        template,
        {
            "dig_prompt": build_dig_prompt(config, candidate, source_note_name),
            "draft": sanitize_body(
                json.dumps(draft.model_dump(), ensure_ascii=False, indent=2), nonce
            ),
            "findings": "\n".join(finding_lines),
            "nonce": nonce,
        },
    )
    return complete_with_retry(engine, prompt, DigDraft, timeout_s=config.dig.timeout_s)


def _one_line(value: str, max_length: int) -> str:
    """ノート・カードに出す LLM の文を 1 行にして切り詰める。"""
    flattened = " ".join(value.split())
    if len(flattened) > max_length:
        return flattened[: max_length - 1] + "…"
    return flattened


def _review_callout(review: ReviewOutcome) -> str:
    """解説ノートの末尾(take 領域の上)に置くレビューの記録(Obsidian の callout)。"""
    who = f"別 AI のレビュー({review.model})" if review.model else "別 AI のレビュー"
    count = len(review.findings)
    if review.status == "clean":
        return f"> [!note] {who}: 指摘なし\n"
    if review.status == "failed":
        return f"> [!warning] {who}は失敗(未レビューのまま)\n"
    if review.status == "skipped":
        if review.same_family:  # GH#180。失敗と同じ出し方
            return f"> [!warning] {_SAME_FAMILY_NOTICE}\n"
        return "> [!info] 今月のレビュー費用が上限に達したため、レビューなし\n"
    if review.status == "revised":
        head = f"> [!note]- {who}: 指摘 {count} 件 → 書き手が確かめて 1 回直した"
    else:  # revise_failed
        head = f"> [!warning]- {who}: 指摘 {count} 件(直すのに失敗。本文は直す前のまま)"
    lines = [head]
    for index, finding in enumerate(review.findings, start=1):
        lines.append(
            f"> {index}. {_REVIEW_SEVERITY_MARKS[finding.severity]} "
            f"{_REVIEW_KIND_LABELS[finding.kind]} — "
            f"{_one_line(finding.location, _FINDING_LOCATION_MAX)}: "
            f"{_one_line(finding.problem, _FINDING_TEXT_MAX)}"
            f"(直し方: {_one_line(finding.fix, _FINDING_TEXT_MAX)})"
        )
    return "\n".join(lines) + "\n"


def _review_card_line(review: ReviewOutcome) -> str:
    count = len(review.findings)
    if review.status == "clean":
        return "🧪 別 AI のレビュー: 指摘なし"
    if review.status == "revised":
        return f"🧪 別 AI のレビュー: 指摘 {count} 件 → 直した版です"
    if review.status == "revise_failed":
        return f"⚠️ 別 AI のレビュー: 指摘 {count} 件(直せていません。ノート末尾を参照)"
    if review.status == "failed":
        return "⚠️ 別 AI のレビューに失敗(未レビュー)"
    if review.same_family:  # skipped(GH#180。失敗と同じ出し方)
        return f"⚠️ {_SAME_FAMILY_NOTICE}"
    return "🧪 今月のレビュー費用が上限に達したため未レビュー"  # skipped


def _translate_body(
    config: Config, engine_factory: Callable[[str], LLMEngine], body: str
) -> str:
    """本文を段落境界(上限を超える段落は改行・文の境界など)でチャンク化し、対訳 markdown を
    順に生成して連結する。"""
    engine = engine_factory(_TRANSLATE_EFFORT)
    truncated = ""
    if len(body) > config.dig.translation_max_chars:
        body = body[: config.dig.translation_max_chars]
        truncated = "\n\n> [!warning] 長さ上限により以降の対訳は打ち切り(原典 URL を参照)\n"
    chunks = _split_paragraphs(body, _TRANSLATE_CHUNK_CHARS)
    template = (_PROMPTS_DIR / f"{TRANSLATE_PROMPT_VERSION}.md").read_text(encoding="utf-8")
    parts: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        nonce = secrets.token_hex(4)
        prompt = fill_template(
            template,
            {
                "part": f"{index}/{len(chunks)}",
                "chunk": sanitize_body(chunk, nonce),
                "schema": json.dumps(
                    TranslationChunk.model_json_schema(), ensure_ascii=False
                ),
                "nonce": nonce,
            },
        )
        result = complete_with_retry(
            engine, prompt, TranslationChunk, timeout_s=config.dig.timeout_s
        )
        parts.append(result.markdown.strip())
    return "\n\n".join(parts) + truncated


def _split_paragraphs(text: str, chunk_chars: int) -> list[str]:
    """空行(段落)境界で貪欲にまとめる。

    上限を超える段落は前後の段落とまとめず、改行・文の境界などで上限以内に分ける(GH#237。
    抽出本文は空行を持たないことが多く、本文全体が 1 段落になりうる)。
    """
    paragraphs = re.split(r"\n\s*\n", text)
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > chunk_chars:
            if current.strip():
                chunks.append(current)
            current = ""
            parts = _split_oversized(paragraph, chunk_chars)
            chunks.extend(part for part in parts if part.strip())  # 空白だけの片は送らない
            continue
        if current and len(current) + len(paragraph) + 2 > chunk_chars:
            chunks.append(current)
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
    if current.strip():
        chunks.append(current)
    return chunks or [text]


def _split_oversized(text: str, chunk_chars: int, level: int = 0) -> list[str]:
    """上限を超える段落を粗い境界から順に分け、上限以内の片に貪欲にまとめる(GH#237)。

    片をつなぐと元の段落に戻る(境界の字は前の片の末尾に残す)。どの境界でも上限に
    収まらない片は文字数で切る。
    """
    if len(text) <= chunk_chars:
        return [text]
    if level == len(_OVERSIZED_BOUNDARIES):
        return [text[i : i + chunk_chars] for i in range(0, len(text), chunk_chars)]
    cuts = [m.end() for m in _OVERSIZED_BOUNDARIES[level].finditer(text)]
    starts = [0, *cuts]
    ends = [*cuts, len(text)]
    chunks: list[str] = []
    current = ""
    for start, end in zip(starts, ends, strict=True):
        if end == start:  # 末尾が境界の字で終わるときの空の片
            continue
        for part in _split_oversized(text[start:end], chunk_chars, level + 1):
            if current and len(current) + len(part) > chunk_chars:
                chunks.append(current)
                current = part
            else:
                current += part
    if current:
        chunks.append(current)
    return chunks


# --- ノート・カードのレンダリング(書込先は vault/<folder>/DeepDive/ に限定) ---


def _publish_note(
    directory: VaultDir, name: str, content: str, logger: logging.Logger, run_id: str | None
) -> None:
    """既存ノートは上書きしない(Codex P1)。

    公開(ファイル書出)と published_at 記帳の間で中断すると、次回 run が同じ
    ノートを再書出する。公開済みノートにはユーザーが take を書き込みうるため、
    盲目的な上書きは take 領域の破壊になる。atomic_write は部分ファイルを残さない
    ので、既存ファイル = 完全な公開済みノート。それを正として再書出は破棄する
    (write-ahead により内容は同一生成物なので、失われるのは何もない)。

    既存かどうかは置き換え(rename)の直前に確かめる(GH#234)。vault が応答せず上限の時間で
    見切ったスレッドは後から書き終えうるので、その間にできたノートを置き換えないため。
    別の実行の公開との競合は、公開のロック(_PublishLock)で防ぐ。

    書き出し先は vault の実体から 1 段ずつ symlink を辿らずに開いた DeepDive の置き場(GH#204)。
    途中のフォルダが外への symlink なら書かない(VaultPathError でその件の公開の失敗になり、
    本文は台帳に残る)。
    """
    with directory.opened() as dir_fd:
        absent = partial(_note_absent, dir_fd, name)
        if not atomic_write(dir_fd, name, content, precondition=absent):
            logger.warning(
                f"既存の DeepDive ノートを保持(再書出は破棄): {name}",
                extra={"run_id": run_id, "stage": "dig"},
            )


def _note_absent(dir_fd: int, name: str) -> bool:
    """置き換えの直前の確認: 同じ名前のノートが無いか(GH#234)。

    同じ名前が symlink・通常のファイルでなければ、辿らず置き換えずに公開の失敗にする(既存の
    ノートとみなして本文を台帳から消すと、ノートの中身がどこにも残らないため。GH#204)。
    """
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    if not stat.S_ISREG(st.st_mode):
        raise VaultPathError(f"DeepDive の {name} が symlink か通常のファイルでない")
    return False


def _deepdive_dir(config: Config) -> VaultDir:
    if config.obsidian.vault_path is None:
        raise DigError("[obsidian].vault_path が未設定(宿題 H4)。DeepDive ノートを書けない")
    base = folder_dir(config, DEEPDIVE_FOLDER)
    base.ensure()
    return base


def _requested_without_angle(candidate: DigCandidate) -> str:
    """観点の無い深掘りのノートの requested(どの経路で掘ったか)。"""
    if candidate.on_demand:
        return _REQUESTED_NOW
    if candidate.from_digest:
        return _REQUESTED_DIGEST
    return _REQUESTED_NONE


def _explainer_note(
    candidate: DigCandidate,
    draft: DigDraft,
    source_note_name: str | None,
    review: ReviewOutcome | None = None,
) -> str:
    """解説ノート(kind: deepdive)。フォーマットの正はデモ実物(設計 06 §0・§5.1)。"""
    frontmatter: dict = {
        "id": candidate.item_id,
        "kind": "deepdive",
        "title": draft.title_ja,
        "url": candidate.canonical_url,
        "author": candidate.author or "",
        "published": candidate.published_at or "",
        "requested": candidate.angle or _requested_without_angle(candidate),
        "status": "take待ち",
    }
    if not candidate.angle:
        frontmatter["auto_brief"] = AUTO_BRIEF
    if source_note_name is not None:
        frontmatter["related"] = f"[[{source_note_name}]]"
    if candidate.previous_note_name is not None:
        # 調べ直しの結果は前回のノートを書き換えず、新しいノートにして前回へつなぐ(GH#137。
        # 公開済みのノートは本人のもの。AI のドラフトを take の付いたノートに混ぜない = ADR 0003)
        frontmatter["previous"] = f"[[{candidate.previous_note_name}]]"
    if review is not None:
        frontmatter["review"] = review.status  # 別 AI のレビューの結果(GH#153)
    body = _with_quotes_section(draft.note_markdown.strip(), draft)
    review_block = f"\n\n{_review_callout(review)}" if review is not None else "\n"
    # LLM の本文とレビューの指摘は untrusted。本文の側をまとめて直す(GH#377。テンプレートの
    # 部分は直す対象の形を含まないので変わらない)
    return render_frontmatter(frontmatter) + neutralize_markdown(
        "\n" + body + review_block + "\n---\n\n(この下は手書きメモ・take 領域)\n"
    )


def _with_quotes_section(note: str, draft: DigDraft) -> str:
    """解説ノートに `## 原文から` の節を入れる(GH#394)。置くのは骨子の後(根拠の強さの前)。

    LLM の見出しが構成どおりでないときは、ソースの節の前、それも無ければ末尾に置く(節が
    見つからないだけで引用を落とさない)。引用と一言は記号ごと文字として見せる(_markdown_literal)。
    """
    if not draft.quotes:
        return note
    parts = ["## 原文から"]
    for quote in draft.quotes:
        text = _markdown_literal(_one_line(_quote_text(quote), _NOTE_QUOTE_MAX))
        why = _markdown_literal(_one_line(quote.why, _NOTE_QUOTE_WHY_MAX))
        parts.append(f"> {text}\n\n→ {why}")
    section = "\n\n".join(parts)
    for heading in _NOTE_QUOTES_BEFORE:
        match = heading.search(note)
        if match is not None:
            return f"{note[: match.start()]}{section}\n\n{note[match.start() :]}"
    return f"{note}\n\n{section}"


def _markdown_literal(text: str) -> str:
    """ASCII の記号をすべて backslash で escape し、Markdown と Obsidian の記法として読ませない。

    引用は原文の写しなので、原文の `![[…]]`(埋め込み)・`%%…%%`(コメント)・強調・リンクなどを
    そのまま文字として見せる(GH#394)。neutralize_markdown は外への読み込みを止めるだけで、
    記法を文字にはしない。backslash で escape した ASCII の記号は、その字として表示される
    (CommonMark)。
    """
    return _MARKDOWN_PUNCTUATION.sub(r"\\\1", text)


def _source_note_ja(candidate: DigCandidate, explainer_note_name: str) -> str:
    """日本語ソースの原文保全ノート(私的利用の全文コピー。設計 06 §5.2)。"""
    frontmatter = {
        "id": candidate.item_id,
        "kind": "deepdive-source",
        "title": f"原文:{candidate.title or '(タイトルなし)'}",
        "url": candidate.canonical_url,
        "author": candidate.author or "",
        "published": candidate.published_at or "",
        "fetched": candidate.fetched_date or "",  # 本文の取得日(GH#239)
        "note": "私的利用のための保全コピー(抽出テキスト)",
    }
    # 抽出本文は untrusted(GH#377)
    return render_frontmatter(frontmatter) + neutralize_markdown(
        f"\n> [!info] 抽出テキストの保全コピーです。解説は [[{explainer_note_name}]] へ。\n\n"
        + (candidate.body or "")
        + "\n"
    )


def _source_note_translated(
    config: Config,
    engine_factory: Callable[[str], LLMEngine],
    candidate: DigCandidate,
    explainer_note_name: str,
) -> str:
    """非日本語ソースの対訳ノート(日本語地の文 + 原文折りたたみ。設計 06 §5.2)。"""
    translation = _translate_body(config, engine_factory, candidate.body or "")
    frontmatter = {
        "id": candidate.item_id,
        "kind": "deepdive-source",
        "title": f"対訳:{candidate.title or '(タイトルなし)'}",
        "url": candidate.canonical_url,
        "author": candidate.author or "",
        "published": candidate.published_at or "",
        "fetched": candidate.fetched_date or "",  # 本文の取得日(GH#239)
        "note": "私的利用のための全訳。日本語が地の文、原文は節ごとの折りたたみ内",
    }
    # 対訳は LLM の出力で、原文の節も含む(GH#377)
    return render_frontmatter(frontmatter) + neutralize_markdown(
        f"\n> [!info] 全訳(私的利用)。解説と検証は [[{explainer_note_name}]] へ。"
        "原文は各節の折りたたみを開くと読めます。\n\n"
        + translation
        + "\n"
    )


def _card_text(
    config: Config,
    candidate: DigCandidate,
    draft: DigDraft,
    note_name: str,
    source_note_name: str | None,
    review: ReviewOutcome | None = None,
) -> str:
    """概要カード v4(設計 06 §5.3)。1 通・分割しない。parse_mode=HTML 前提で escape。"""
    title = escape_and_truncate(draft.title_ja, _CARD_TITLE_MAX)
    original = ""
    if candidate.title and candidate.title.strip() and candidate.title != draft.title_ja:
        original = f"({escape_and_truncate(candidate.title, _CARD_TITLE_MAX)})"
    if candidate.angle:
        label = f"観点「{escape_and_truncate(candidate.angle, 300)}」"
    elif candidate.on_demand:  # GH#388
        label = "⚡ 今すぐの依頼・観点なし"
    elif candidate.requested:
        label = "リプライ指定・観点なし"
    elif candidate.from_digest:  # 設計 06 §3 の ③(GH#349)
        label = "🤖 自動選出(今日の digest の上位)・観点なし"
    else:
        label = "🤖 自動選出(🔖新着)・観点なし"
    suffix = ""
    if source_note_name is not None:
        suffix = "(対訳あり)" if "対訳" in source_note_name else "(原文ノートあり)"
    # note_name はタイトル由来 slug を含み `&` 等が残る(FORBIDDEN_IN_SLUG の対象外)。
    # parse_mode=HTML で送るため未エスケープだと送信自体が 400 で恒久失敗する(Codex P1)
    note_path = escape_text(f"{config.obsidian.folder}/{DEEPDIVE_FOLDER}/{note_name}.md")
    note_line = f"📄 ノート: {note_path}{suffix}"
    if review is not None:  # 別 AI のレビューの結果(GH#153。固定の文言だけ)
        note_line = f"{_review_card_line(review)}\n{note_line}"
    if config.dig.answer_daily_cap > 0:  # 質問への返事(GH#126)
        guide = "💬 リプライで質問すると答えます。感想・結論は take として記録します"
    else:
        # 返事をしない設定では LLM が分けないので、take としては記録しない(GH#106)
        guide = "💬 リプライは感想として記録されます"
    return (
        f"🔎 深掘り #{candidate.item_id}「{title}」{original}\n"
        f"✍️ 書き手: {escape_and_truncate(draft.writer_stance, _CARD_STANCE_MAX)}\n"
        f"{label}\n"
        "\n"
        "あなたにとって\n"
        f"{escape_and_truncate(draft.for_you, _CARD_FOR_YOU_MAX)}\n"
        "\n"
        "世の中にとって\n"
        f"{escape_and_truncate(draft.for_world, _CARD_FOR_WORLD_MAX)}\n"
        "\n"
        f"{_card_quotes(draft)}"
        f"{note_line}\n"
        f"{guide}"
    )


def _card_quotes(draft: DigDraft) -> str:
    """カードの「原文から」の欄(GH#394)。引用は Telegram の blockquote、一言はその下の 1 行。"""
    if not draft.quotes:
        return ""
    lines = ["原文から"]
    for quote in draft.quotes:
        text = escape_and_truncate(" ".join(_quote_text(quote).split()), _CARD_QUOTE_MAX)
        why = escape_and_truncate(" ".join(quote.why.split()), _CARD_QUOTE_WHY_MAX)
        lines.append(f"<blockquote>{text}</blockquote>")
        lines.append(f"→ {why}")
    return "\n".join(lines) + "\n\n"


def _note_slug(candidate: DigCandidate) -> str:
    # 原文タイトルの無い item(X の投稿)は、分析の日本語の見出しから名前を付ける(GH#167)。
    # ノート名は台帳(dig_results.note_name)に残して引くので、既存のノートとの対応は崩れない
    for text in (candidate.title, candidate.title_ja):
        slug = FORBIDDEN_IN_SLUG.sub("-", text or "").strip("-")[:40]
        if slug:
            return slug
    return "untitled"


def _is_tweet_url(url: str) -> bool:
    # canonical_url の正規化(01 §4)が www.x.com を x.com に寄せるのは GH#232 からで、
    # それより前の行は www つきのまま残るため、ここで剥がしてから判定する(www.x.com が
    # ポリシー判定をすり抜けて本文が恒久保存される穴。Codex P1)
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    return host in _TWEET_HOSTS


def _is_japanese_source(candidate: DigCandidate) -> bool:
    """原文ノート(日本語)か対訳ノート(日本語以外)か(設計 06 §5.2)。

    documents.language は抽出で書いておらず(既存の行もすべて NULL)、NULL を日本語以外と
    扱うと日本語の記事まで対訳(LLM)に回る(GH#173)。値があればそれに従い、無ければ本文から
    判定する。
    """
    if candidate.language is not None:
        return candidate.language == "ja"
    return _looks_japanese(candidate.body or "")


def _looks_japanese(text: str) -> bool:
    """本文が日本語か。引用を除いた地の文で、日本語の語の数がそれ以外の語の数より多ければ日本語。

    語 = 同じ文字種の文字の連なり(ひらがな・カタカナ・漢字は文字種ごとに区切る。それ以外は
    英単語やコードの識別子)。文字の数で比べないのは、日本語は 1 字あたりの情報が多く、英単語や
    コードの多い日本語の記事を取りこぼすため。引用(「」『』“” と同じ行の中の "…")の中は数えない:
    長い日本語の文を引用する英語の記事を、引用の語の数で日本語と誤らないため(日本語の記事は引用を
    除いても地の文が日本語のまま残る)。英語の記事を日本語と誤ると対訳が無くなるので、語の数が同じ
    ものや、コードが本文より多い日本語の記事のように迷うものは日本語以外(対訳)に倒す。
    """
    text = _strip_quotes(text)
    kana_chars = ja_chars = ja_words = other_words = 0
    previous: str | None = None
    for ch in text:
        script = _char_script(ch) if ch.isalpha() else None
        if script is not None and script != previous:
            if script == "other":
                other_words += 1
            else:
                ja_words += 1
        if script in ("hiragana", "katakana"):
            kana_chars += 1
        if script in ("hiragana", "katakana", "kanji"):
            ja_chars += 1
        previous = script
    if kana_chars == 0 or kana_chars < ja_chars * _JA_KANA_MIN_SHARE:
        return False
    return ja_words > other_words


def _strip_quotes(text: str) -> str:
    """引用を空白に置き換える(前後の語がつながらないように空白にする)。

    「」『』“” は 1 回の走査で開きと閉じを対応づける(入れ子は外側ごと除く。本文の長さに比例する
    手間で済ませ、深い入れ子でも全文を何度も走査しない)。対応の取れない閉じは無視し、閉じの無い
    開きは中身ごと残す(閉じ忘れの括弧ひとつで本文の残りを捨てない)。" は同じ行の中だけで組にする。
    """
    out: list[str] = []
    opened: list[tuple[str, int]] = []  # (待っている閉じ, 開きを置いた out の位置)
    for ch in _ASCII_QUOTE_RE.sub(" ", text):
        if opened and ch == opened[-1][0]:
            del out[opened.pop()[1] :]
            out.append(" ")
        else:
            if ch in _QUOTE_PAIRS:
                opened.append((_QUOTE_PAIRS[ch], len(out)))
            out.append(ch)
    return "".join(out)


def _char_script(ch: str) -> str:
    code = ord(ch)
    if 0x3040 <= code <= 0x309F:
        return "hiragana"
    if 0x30A0 <= code <= 0x30FF or 0xFF66 <= code <= 0xFF9F:  # 全角・半角のカタカナ
        return "katakana"
    if code == 0x3005 or 0x3400 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF:  # 々・漢字
        return "kanji"
    return "other"
