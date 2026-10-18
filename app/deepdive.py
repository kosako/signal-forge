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
  リプライ指定なら段の失敗として loud に報告する(明示依頼を黙って放置しない)

障害の局所化: item 単位で隔離し、1 件の失敗で他の件・他の段を止めない。失敗が
あれば段としては失敗にする(#74 と同じ「進めるものは進め、止まったことは通知」)。
中断耐性は dig_results(migration 010)への write-ahead で作る: LLM 生成物
(ノート本文・カード文)を先に台帳へ記帳し、ファイル公開(published_at)→
カード送信(message_id)を台帳から再開する。どこで中断しても再実行は同一生成物の
続きだけを行い、ノートとカードの生成元が食い違わない。既存ノートは上書きしない
(_publish_note。公開済みノートに書かれた take を守る)。
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from app.analysis.analyzer import sanitize, sanitize_body
from app.analysis.llm import LLMEngine, complete_with_retry
from app.analysis.profile import load_interest_profile
from app.config import Config
from app.delivery.telegram import TelegramApi, escape_and_truncate, escape_text
from app.export.obsidian import FORBIDDEN_IN_SLUG, atomic_write, render_frontmatter

JST = timezone(timedelta(hours=9))

PROMPT_VERSION = "dig-v001"
TRANSLATE_PROMPT_VERSION = "dig-translate-v001"
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

DEEPDIVE_FOLDER = "DeepDive"  # vault の folder 配下(書込先はここに限定。ADR 0004)

# 自動選出(観点なし)の調査ブリーフ(設計 06 §4)
AUTO_BRIEF = "私の文脈(関心プロファイル)との接点を軸に、主張と根拠の強さを検証しながら読む"
_REQUESTED_NONE = "(観点指定なし:ブックマーク自動キューからの深掘り)"

# 対訳は 1 チャンクずつ生成する(1 呼び出しの出力を有界に保つ。段落境界で分割)
_TRANSLATE_CHUNK_CHARS = 6_000
# 対訳の effort は low 固定(翻訳は推論負荷が低く、時間コストだけが増える)
_TRANSLATE_EFFORT = "low"

# カード各欄の上限(合計が Telegram 4096 字を構造的に超えないための予算)
_CARD_TITLE_MAX = 200
_CARD_STANCE_MAX = 300
_CARD_FOR_YOU_MAX = 1500
_CARD_FOR_WORLD_MAX = 1200

# ツイート URL(リンクなし 🔖 の canonical)。本文非保持ポリシー(GH#2)の対象判定
_TWEET_HOSTS = frozenset({"x.com", "twitter.com", "mobile.twitter.com"})


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
) -> None:
    """調査段の本体。生成(台帳へ write-ahead)→ 未公開ノート書出 → 未送信カード送信。

    3 段はそれぞれ台帳(dig_results)から再開可能で、どこで中断しても再実行は
    同一生成物の続きだけを行う(LLM 再呼び出し・生成物の食い違いを起こさない)。
    """
    day = today or datetime.now(JST).strftime("%Y-%m-%d")
    remaining = config.dig.daily_cap - _digs_on(conn, day)
    failures: list[str] = []

    if remaining > 0:
        selected, skipped_requested = _select_candidates(conn, config, remaining, day)
        failures.extend(
            f"item {c.item_id}: リプライ指定だが本文の抽出材料がない(パスするか手動対応)"
            for c in skipped_requested
        )
        for candidate in selected:
            try:
                _dig_one(conn, config, engine_factory, candidate, day, logger, run_id)
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
    failures.extend(_publish_pending_notes(conn, config, logger, run_id))

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
               a.title_ja, a.summary
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
        )
        if candidate.body and len(candidate.body) >= config.dig.min_body_chars:
            selected.append(candidate)
        elif candidate.requested:
            skipped_requested.append(candidate)
        # 材料不足の自動選出は対象外のまま(キューに残る。docstring 参照)
    return selected, skipped_requested


def _dig_one(
    conn: sqlite3.Connection,
    config: Config,
    engine_factory: Callable[[str], LLMEngine],
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
    slug = FORBIDDEN_IN_SLUG.sub("-", candidate.title or "untitled").strip("-")[:40]
    note_name = f"{date_prefix}-{candidate.item_id}-{slug}-深掘り"

    # 原文/対訳ノート: ツイート本文のみのアイテムは作らない(X 本文非保持 = GH#2。
    # Web 記事・gist は私的利用の範囲で全文保全・対訳可。設計 06 §5.2)
    is_tweet_body = _is_tweet_url(candidate.canonical_url)
    source_note_name: str | None = None
    source_note_body: str | None = None
    if not is_tweet_body:
        if candidate.language == "ja":
            source_note_name = f"{date_prefix}-{candidate.item_id}-原文-{slug}"
            source_note_body = _source_note_ja(candidate, note_name)
        else:
            source_note_name = f"{date_prefix}-{candidate.item_id}-対訳-{slug}"
            source_note_body = _source_note_translated(
                config, engine_factory, candidate, note_name
            )

    draft = _generate_draft(config, engine_factory, candidate, source_note_name)
    note_body = _explainer_note(candidate, draft, source_note_name)
    card_text = _card_text(config, candidate, draft, note_name, source_note_name)

    # 生成は分オーダーだが、記帳は短く(poller と DB を共有するため)。
    # 接続は autocommit のため、台帳とキューの原子性は明示トランザクションで作る
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO dig_results
              (item_id, run_id, note_name, source_note_name, angle, card_text,
               note_markdown, source_note_markdown)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
) -> list[str]:
    """未公開ノート(過去の中断分を含む)を台帳の本文から vault へ書き出す。

    公開後は本文列を NULL 化して台帳を軽く保つ(恒久保存はノート側 = ADR 0004)。
    """
    failures: list[str] = []
    rows = conn.execute(
        "SELECT id, item_id, note_name, source_note_name, note_markdown,"
        " source_note_markdown FROM dig_results WHERE published_at IS NULL ORDER BY id"
    ).fetchall()
    for row in rows:
        try:
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
    return failures


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
        "SELECT id, item_id, card_text FROM dig_results "
        "WHERE message_id IS NULL AND published_at IS NOT NULL ORDER BY id"
    ).fetchall()
    for row in rows:
        try:
            message_id = api.send_message(config.telegram.owner_chat_id, row["card_text"])
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
) -> DigDraft:
    engine = engine_factory(config.dig.reasoning_effort)
    prompt = build_dig_prompt(config, candidate, source_note_name)
    return complete_with_retry(engine, prompt, DigDraft, timeout_s=config.dig.timeout_s)


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

    meta_lines = [
        f"- title: {sanitize(candidate.title, 300, nonce) or '(なし)'}",
        f"- author: {sanitize(candidate.author, 100, nonce) or '(不明)'}",
        f"- published_at: {sanitize(candidate.published_at, 30, nonce) or '(不明)'}",
        f"- url: {sanitize(candidate.canonical_url, 500, nonce)}",
    ]
    if candidate.summary:
        meta_lines.append(f"- 既存分析の要約: {sanitize(candidate.summary, 300, nonce)}")

    body = sanitize_body(candidate.body or "", nonce)
    if len(body) > config.dig.body_max_chars:
        body = body[: config.dig.body_max_chars] + "\n(…以降は長さ上限で切り詰め)"

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

    return _fill_template(
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


def _fill_template(template: str, mapping: dict[str, str]) -> str:
    """単一パス置換(analyzer と同じ規約。入力由来の {{nonce}} 等を再展開しない)。"""
    return re.sub(r"\{\{(\w+)\}\}", lambda m: mapping[m.group(1)], template)


def _translate_body(
    config: Config, engine_factory: Callable[[str], LLMEngine], body: str
) -> str:
    """本文を段落境界でチャンク化し、対訳 markdown を順に生成して連結する。"""
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
        prompt = _fill_template(
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
    """空行(段落)境界で貪欲にまとめる。単一段落が上限超過ならそのまま 1 チャンク。"""
    paragraphs = re.split(r"\n\s*\n", text)
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if current and len(current) + len(paragraph) + 2 > chunk_chars:
            chunks.append(current)
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
    if current.strip():
        chunks.append(current)
    return chunks or [text]


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
    """
    if path.exists():
        logger.warning(
            f"既存の DeepDive ノートを保持(再書出は破棄): {path.name}",
            extra={"run_id": run_id, "stage": "dig"},
        )
        return
    atomic_write(path, content)


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
    candidate: DigCandidate, draft: DigDraft, source_note_name: str | None
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
    body = draft.note_markdown.strip()
    return (
        render_frontmatter(frontmatter)
        + "\n"
        + body
        + "\n\n---\n\n(この下は手書きメモ・take 領域)\n"
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
        "fetched": datetime.now(UTC).astimezone(JST).strftime("%Y-%m-%d"),
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
        "fetched": datetime.now(UTC).astimezone(JST).strftime("%Y-%m-%d"),
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
    if config.dig.answer_daily_cap > 0:  # 質問への返事(GH#126)
        guide = "💬 リプライで質問すると答えます。感想・結論は take として記録します"
    else:
        guide = "💬 リプライが take として記録されます"
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


def _is_tweet_url(url: str) -> bool:
    # canonical_url の正規化(01 §4)は www を剥がさないため、ここで剥がしてから
    # 判定する(www.x.com がポリシー判定をすり抜けて本文が恒久保存される穴。Codex P1)
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    return host in _TWEET_HOSTS
