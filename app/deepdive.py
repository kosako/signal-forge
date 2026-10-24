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
- 優先順位: ①リプライ指定(requested_at 新しい順)②自動キューの新しい順
- 材料不足(本文抽出なし・短すぎ)は、自動選出なら skip(未消化一覧には残る)、
  リプライ指定なら段の失敗として loud に報告する(明示依頼を黙って放置しない)。報告は 1 回に
  して指定を外す(本文を取り直す経路は無く、毎朝の失敗を続けない。GH#197)
- 同じ台帳を使う実行(朝バッチの dig 段と `intel dig`)は 1 つずつ。候補の選定から公開・送信までを
  ロックで囲み、後から来た方は待たずに何もしない(朝バッチを止めない。GH#196)

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
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
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
from app.delivery.telegram import (
    TelegramApi,
    dig_card_keyboard,
    escape_and_truncate,
    escape_text,
)
from app.export.obsidian import FORBIDDEN_IN_SLUG, atomic_write, render_frontmatter
from app.jst import JST
from app.prompting import fill_template, sanitize, sanitize_body
from app.vault_guard import VAULT_TIMEOUT_S, VaultGuard, VaultStalled

PROMPT_VERSION = "dig-v001"
TRANSLATE_PROMPT_VERSION = "dig-translate-v001"
REVIEW_PROMPT_VERSION = "dig-review-v001"  # 別系統の AI のレビュー(GH#153)
REVISE_PROMPT_VERSION = "dig-revise-v001"  # 指摘を受けて書き手が 1 回直す
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

DEEPDIVE_FOLDER = "DeepDive"  # vault の folder 配下(書込先はここに限定。ADR 0004)

# ノートの公開を実行どうしで 1 つに絞るロック(台帳の DB と同じ置き場。GH#234)。
# 別の実行が握っていたら、この時間だけ待って諦める(ふだんの公開は数ミリ秒で終わる)
_PUBLISH_LOCK_NAME = "dig-publish.lock"
_PUBLISH_LOCK_WAIT_S = VAULT_TIMEOUT_S
# 調査段の実行を 1 つに絞るロック(GH#196)。DB の隣の `<DB>.dig.lock`(migrate.lock・
# deliver.lock と同じ名付け)。公開のロックはこの中で取る(入れ子の順はこの 1 通り)
_DIG_LOCK_SUFFIX = ".dig.lock"

# 自動選出(観点なし)の調査ブリーフ(設計 06 §4)
AUTO_BRIEF = "私の文脈(関心プロファイル)との接点を軸に、主張と根拠の強さを検証しながら読む"
_REQUESTED_NONE = "(観点指定なし:ブックマーク自動キューからの深掘り)"

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

# カード各欄の上限(合計が Telegram 4096 字を構造的に超えないための予算)
_CARD_TITLE_MAX = 200
_CARD_STANCE_MAX = 300
_CARD_FOR_YOU_MAX = 1500
_CARD_FOR_WORLD_MAX = 1200

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
    """調査段の失敗(材料不足の明示指定・全件失敗など)。"""


class DigDraft(BaseModel):
    """解説ノート + カードの構造化出力(prompts/dig-v001.md)。"""

    title_ja: str = Field(min_length=1, max_length=_CARD_TITLE_MAX)
    writer_stance: str = Field(min_length=1)
    for_you: str = Field(min_length=1)
    for_world: str = Field(min_length=1)
    note_markdown: str = Field(min_length=1)


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


@dataclass(frozen=True)
class DigCandidate:
    item_id: int
    angle: str | None
    requested: bool  # リプライ指定(requested_at あり)
    # done 化の compare-and-set 用スナップショット(updated_at は秒解像度のため、
    # 同一秒内の更新も検出できるよう status 以外の実値も比較する。Codex P2)
    queue_updated_at: str
    queue_requested_at: str | None
    title: str | None
    author: str | None
    published_at: str | None
    canonical_url: str
    body: str | None  # documents.extracted_text
    language: str | None
    title_ja: str | None  # 既存分析(あれば)
    summary: str | None
    # 同じ item の前回の解説ノート(調べ直し = done からの再指定。GH#137)。新しいノートからリンクする
    previous_note_name: str | None = None
    # 本文を取得・抽出した JST の日付(documents.extracted_at)。原文・対訳ノートの fetched に
    # 書く(深掘りした日ではない。GH#239)
    fetched_date: str | None = None


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
    """
    with _dig_lock(conn) as acquired:
        if not acquired:
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
    day = today or datetime.now(JST).strftime("%Y-%m-%d")
    remaining = config.dig.daily_cap - _digs_on(conn, day)
    failures: list[str] = []

    if remaining > 0:
        selected, skipped_requested = _select_candidates(conn, config, remaining, day)
        for candidate in skipped_requested:
            dropped = _drop_request(conn, candidate, logger, run_id)
            failures.append(
                f"item {candidate.item_id}: リプライ指定だが本文の抽出材料がない"
                + (
                    "(指定を外した。次の朝からは知らせない)"
                    if dropped
                    else "(パスするか手動対応)"
                )
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

    if failures:
        raise DigError(f"調査段で {len(failures)} 件失敗: " + " / ".join(failures))


def _digs_on(conn: sqlite3.Connection, day: str) -> int:
    """その JST 暦日に実行済みの件数(created_at は UTC 保存)。"""
    return conn.execute(
        "SELECT count(*) AS n FROM dig_results WHERE date(created_at, '+9 hours') = ?",
        (day,),
    ).fetchone()["n"]


def has_material(body: str | None, config: Config) -> bool:
    """調査段の材料が足りるか(documents の抽出本文が min_body_chars 字以上)。

    朝の選定と、深掘りカードへの調べ直しの受付(dig_answer。GH#197)が同じ条件を使う。
    """
    return bool(body) and len(body) >= config.dig.min_body_chars


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

    戻り値 = (選定, 材料不足のリプライ指定)。材料不足の自動選出は枠を消費せず
    静かに残す(未消化一覧に見えている。ここで毎朝 ERROR を出すとノイズになる)。
    同日に実行済みの item は選ばない(done 直後の再指定を同日中に再実行すると
    ノート名が衝突し、カードが古いノートを指しうる。Codex P2。翌朝に掘られる =
    「夜選ぶ → 朝届く」のリズム通り)。
    """
    rows = conn.execute(
        """
        SELECT q.item_id, q.angle, q.requested_at, q.updated_at,
               i.title, i.author, i.published_at, i.canonical_url,
               d.extracted_text, d.language,
               date(d.extracted_at, '+9 hours') AS fetched_date,
               a.title_ja, a.summary,
               (SELECT r.note_name FROM dig_results r
                WHERE r.item_id = q.item_id AND r.published_at IS NOT NULL
                ORDER BY r.id DESC LIMIT 1) AS previous_note_name
        FROM dig_queue q
        JOIN items i ON i.id = q.item_id
        LEFT JOIN documents d ON d.item_id = q.item_id
        LEFT JOIN analyses a ON a.id = (
            SELECT max(id) FROM analyses
            WHERE item_id = q.item_id AND degraded = 0
        )
        WHERE q.status = 'queued'
          AND NOT EXISTS (
            SELECT 1 FROM dig_results r
            WHERE r.item_id = q.item_id AND date(r.created_at, '+9 hours') = ?
          )
        ORDER BY (q.requested_at IS NULL), q.requested_at DESC, q.enqueued_at DESC
        """,
        (day,),
    ).fetchall()
    selected: list[DigCandidate] = []
    skipped_requested: list[DigCandidate] = []
    for row in rows:
        if len(selected) >= limit:
            break
        candidate = DigCandidate(
            item_id=row["item_id"],
            angle=row["angle"],
            requested=row["requested_at"] is not None,
            queue_updated_at=row["updated_at"],
            queue_requested_at=row["requested_at"],
            title=row["title"],
            author=row["author"],
            published_at=row["published_at"],
            canonical_url=row["canonical_url"],
            body=row["extracted_text"],
            language=row["language"],
            title_ja=row["title_ja"],
            summary=row["summary"],
            previous_note_name=row["previous_note_name"],
            fetched_date=row["fetched_date"],
        )
        if has_material(candidate.body, config):
            selected.append(candidate)
        elif candidate.requested:
            skipped_requested.append(candidate)
        # 材料不足の自動選出は対象外のまま(キューに残る。docstring 参照)
    return selected, skipped_requested


def _drop_request(
    conn: sqlite3.Connection,
    candidate: DigCandidate,
    logger: logging.Logger,
    run_id: str | None,
) -> bool:
    """材料不足のリプライ指定を、知らせた後に自動キューの扱いへ戻す(GH#197)。外せたら True。

    requested_at を外すだけで、status(queued)・観点・source・受付の順序の印
    (backlog_op_question_id。本人の未消化一覧への操作ではない)は変えない。本文を取り直す
    経路は無いので、指定のまま残すと毎朝同じ失敗を知らせ続ける。材料不足の自動選出と同じく
    静かに残り、本人が指定し直せばまた 1 回知らせる。選定の後に poller が更新していたら
    (再指定・パス)、本人の操作を優先して変えない(done 化と同じ compare-and-set)。
    DB の失敗は item 単位に閉じ(ERROR を残して False)、他の件の深掘りと公開・送信を止めない。
    """
    try:
        return (
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
        logger.error(
            f"材料不足の指定を外せなかった item={candidate.item_id}: {type(e).__name__}: {e}",
            extra={"run_id": run_id, "stage": "dig", "item_id": candidate.item_id},
        )
        return False


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
               review_status, review_findings, review_cost_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        # compare-and-set: 生成中(分オーダー)に poller がこの行を更新していたら
        # (再指定 = queued + 新観点 / パス)、done で上書きせずユーザー操作を優先する
        # (新しい依頼は次回実行で掘られる。Codex P1)。生成物の公開・送信は行う。
        # updated_at は秒解像度で同一秒内の更新を見逃すため、status / angle /
        # requested_at の実値も比較する(全て同値の更新は意味的に no-op。Codex P2)
        done = conn.execute(
            "UPDATE dig_queue SET status = 'done', "
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
    if not rows:
        return failures
    lock = _PublishLock.acquire(conn)
    if lock is None:
        logger.error(
            "別の実行のノートの公開が終わっていない(vault が応答しないまま止まっている可能性)"
            f"ので、この実行では公開しない(未公開の {len(rows)} 件は次の実行へ。runbook §11)",
            extra={"run_id": run_id, "stage": "dig"},
        )
        failures.append(f"ノート公開: 別の実行の公開が終わっていない(未公開 {len(rows)} 件)")
        return failures

    def publish(row: sqlite3.Row) -> None:
        if not lock.begin_write():  # 見切られた後に動き出した。ロックは放されたので書かない
            return
        try:
            _publish_row_notes(config, row, logger, run_id)
        finally:
            lock.end_write()  # 見切られた後に終わったなら、ここで放す

    try:
        for index, row in enumerate(rows):
            try:
                vault.call(lambda row=row: publish(row))
            except VaultStalled as e:
                unpublished = len(rows) - index
                logger.error(
                    "vault が応答しないので、ノートの公開をこの実行では止める"
                    f"(未公開の {unpublished} 件は本文を台帳に残し、次の実行で公開する。"
                    f"runbook §11): {e}",
                    extra={"run_id": run_id, "stage": "dig", "item_id": row["item_id"]},
                )
                failures.append(
                    f"item {row['item_id']}: ノート公開 VaultStalled"
                    f"(未公開 {unpublished} 件は次の実行へ)"
                )
                break
            except Exception as e:
                logger.error(
                    f"DeepDive ノート公開失敗 item={row['item_id']}: {type(e).__name__}",
                    extra={"run_id": run_id, "stage": "dig", "item_id": row["item_id"]},
                    exc_info=True,
                )
                failures.append(f"item {row['item_id']}: ノート公開 {type(e).__name__}")
                continue
            conn.execute(
                "UPDATE dig_results SET published_at = strftime('%Y-%m-%dT%H:%M:%SZ','now'),"
                " note_markdown = NULL, source_note_markdown = NULL WHERE id = ?",
                (row["id"],),
            )
    finally:
        # 見切ったスレッドがまだ書いているなら、それが終わるまで(またはプロセスの終了まで)握る
        lock.leave()
    return failures


def _publish_row_notes(
    config: Config, row: sqlite3.Row, logger: logging.Logger, run_id: str | None
) -> None:
    """台帳の 1 行のノート(解説・原文/対訳)を書き出す(vault-io のスレッドで動く。DB に触れない)。"""
    deepdive_dir = _deepdive_dir(config)
    if row["note_markdown"] is not None:  # NULL = 旧経路の公開済み残骸は再書出不能
        _publish_note(
            deepdive_dir / f"{row['note_name']}.md",
            row["note_markdown"],
            logger,
            run_id,
        )
    if row["source_note_name"] is not None and row["source_note_markdown"] is not None:
        _publish_note(
            deepdive_dir / f"{row['source_note_name']}.md",
            row["source_note_markdown"],
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


@contextlib.contextmanager
def _dig_lock(conn: sqlite3.Connection) -> Iterator[bool]:
    """調査段の実行を、同じ台帳を使う実行どうし(朝バッチの dig 段と `intel dig`)で 1 つに絞る
    (GH#196)。取れたら True、別の実行が握っていれば待たずに False。

    SQLite のロックは 1 トランザクションの間しか続かず、LLM の待ち(分オーダー)をまたいで
    書き込みのトランザクションを持つと poller の書き込みを止めるので、DB の隣の専用ファイルに
    flock を取る(db._migration_lock と同じ考え方)。公開のロック(_PublishLock)はこの中でだけ
    取る。入れ子の順はこの 1 通りで、こちらは待たないので、見切ったスレッドが公開のロックを
    握ったまま残っていてもデッドロックしない(公開のロックの待ちには上限がある)。メモリ上の
    DB は別の実行と台帳を共有しない。
    """
    db_file = _db_file(conn)
    if db_file is None:
        yield True
        return
    lock_path = db_file.with_name(db_file.name + _DIG_LOCK_SUFFIX)
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            acquired = False
        yield acquired
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
        # 🔗 元記事 / 📝 要約 / 📖 詳しく(GH#141。スマホの Telegram で完結させる)
        keyboard = dig_card_keyboard(
            row["id"], row["canonical_url"], with_requests=config.dig.answer_daily_cap > 0
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

    return fill_template(
        template,
        {
            "brief": brief,
            "interest_profile": profile,
            "meta": "\n".join(meta_lines),
            "body": body,
            "source_note_link": source_note_link,
            "quote_policy": quote_policy,
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
    """解説の生成とレビューに渡す本文(同じ切り詰め。書き手と査読者が同じ範囲を読む)。"""
    body = sanitize_body(candidate.body or "", nonce)
    if len(body) > config.dig.body_max_chars:
        body = body[: config.dig.body_max_chars] + "\n(…以降は長さ上限で切り詰め)"
    return body


# --- 別系統の AI のレビュー(GH#153 / 設計 06 §4) ---


class _CostMeter:
    """呼び出しごとの usage(LLMUsage)を合算する。やり直しの分も課金されるため。"""

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
    path: Path, content: str, logger: logging.Logger, run_id: str | None
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
    """
    if not atomic_write(path, content, precondition=lambda: not path.exists()):
        logger.warning(
            f"既存の DeepDive ノートを保持(再書出は破棄): {path.name}",
            extra={"run_id": run_id, "stage": "dig"},
        )


def _deepdive_dir(config: Config) -> Path:
    if config.obsidian.vault_path is None:
        raise DigError("[obsidian].vault_path が未設定(宿題 H4)。DeepDive ノートを書けない")
    base = (
        Path(config.obsidian.vault_path).expanduser()
        / config.obsidian.folder
        / DEEPDIVE_FOLDER
    )
    base.mkdir(parents=True, exist_ok=True)
    return base


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
        "requested": candidate.angle or _REQUESTED_NONE,
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
    body = draft.note_markdown.strip()
    review_block = f"\n\n{_review_callout(review)}" if review is not None else "\n"
    return (
        render_frontmatter(frontmatter)
        + "\n"
        + body
        + review_block
        + "\n---\n\n(この下は手書きメモ・take 領域)\n"
    )


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
    return (
        render_frontmatter(frontmatter)
        + f"\n> [!info] 抽出テキストの保全コピーです。解説は [[{explainer_note_name}]] へ。\n\n"
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
    return (
        render_frontmatter(frontmatter)
        + f"\n> [!info] 全訳(私的利用)。解説と検証は [[{explainer_note_name}]] へ。"
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
    """概要カード v3(設計 06 §5.3)。1 通・分割しない。parse_mode=HTML 前提で escape。"""
    title = escape_and_truncate(draft.title_ja, _CARD_TITLE_MAX)
    original = ""
    if candidate.title and candidate.title.strip() and candidate.title != draft.title_ja:
        original = f"({escape_and_truncate(candidate.title, _CARD_TITLE_MAX)})"
    if candidate.angle:
        label = f"観点「{escape_and_truncate(candidate.angle, 300)}」"
    elif candidate.requested:
        label = "リプライ指定・観点なし"
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
        f"{note_line}\n"
        f"{guide}"
    )


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
