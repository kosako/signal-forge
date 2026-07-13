"""分析 Stage 2: LLM 本分析(設計 03 §3)。

- プロンプトは prompts/analyze-v002.md(全文は設計 03 §3.1 の正本)。
  プレースホルダ: interest_profile / recent_titles / items / schema / nonce
- 外部由来フィールドは全て(メタデータ含む)サニタイズしてから注入し、body と
  recent_titles は実行ごとの乱数 nonce でフェンスする
- バッチ(既定15件)→ AnalysisBatch 検証 → allowlist 外破棄 → 欠落は単発再分析1回 →
  なお欠落・チャンク失敗は degraded 記録(黙って止まらない。設計 00 §6)
- 例外(GH#74): 環境障害(LLMUnavailableError)と全滅(analyzed=0)は degraded で
  握らず段の失敗として raise する(silent full-degradation 防止。失敗通知 = 設計 05 §4)
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import sqlite3
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.analysis.llm import LLMEngine, LLMError, LLMUnavailableError, complete_with_retry
from app.config import Config

PROMPT_VERSION = "analyze-v002"
PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / f"{PROMPT_VERSION}.md"

RECENT_DAYS = 14

# サニタイズの長さ上限(設計 03 §3.2)
MAX_TITLE = 300
MAX_AUTHOR = 100
MAX_SOURCE = 100

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class ItemAnalysis(BaseModel):
    item_id: int  # 入力バッチの id 集合に含まれること(allowlist は写像側で検証)
    title_ja: str = Field(max_length=300)  # 日本語見出し(v002。タイトル無しアイテムは本文から生成)
    summary: str = Field(max_length=300)
    relevance: int = Field(ge=0, le=100)
    category: Literal["課題直結", "ゲームチェンジャー", "組織AI活用", "その他"]
    negative: list[Literal["tips", "hype", "宣伝", "その他"]] = []
    novelty: Literal["new", "update", "known"]
    topic: str = Field(max_length=30)
    reason: str = Field(max_length=150)
    implication: str = Field(max_length=150)


class AnalysisBatch(BaseModel):
    results: list[ItemAnalysis]


class FullDegradationError(RuntimeError):
    """1 run の分析が全滅(analyzed=0・degraded>0)。systemic 障害の兆候(GH#74)。

    degraded 行は書き込み済みのため劣化配信は成立するが、「まともに分析できていない朝」を
    黙って正常系に見せないよう、run は失敗として失敗通知(設計 05 §4)に載せる。
    """


def analyze_selected(
    conn: sqlite3.Connection,
    config: Config,
    engine: LLMEngine,
    profile_body: str,
    selected_ids: list[int],
    logger: logging.Logger,
    *,
    run_id: str | None = None,
) -> dict[str, int]:
    """選抜済み items を分析し analyses へ写像する。{analyzed, degraded} 件数を返す。"""
    counts = {"analyzed": 0, "degraded": 0}
    if not selected_ids:
        return counts
    recent = _recent_titles(conn)
    batch_size = config.analysis.batch_size
    for start in range(0, len(selected_ids), batch_size):
        chunk_ids = selected_ids[start : start + batch_size]
        chunk_counts = _analyze_chunk(
            conn, config, engine, profile_body, recent, chunk_ids, logger, run_id
        )
        counts["analyzed"] += chunk_counts["analyzed"]
        counts["degraded"] += chunk_counts["degraded"]
    if counts["analyzed"] == 0 and counts["degraded"] > 0:
        # 原因を問わない全滅検知(種別分岐をすり抜ける systemic 障害の安全網。GH#74)
        raise FullDegradationError(
            f"分析が全滅({counts['degraded']} 件すべて degraded)。環境障害を疑うこと"
        )
    logger.info("本分析 完了", extra={"run_id": run_id, "stage": "analyze", "extra": counts})
    return counts


def _analyze_chunk(
    conn: sqlite3.Connection,
    config: Config,
    engine: LLMEngine,
    profile_body: str,
    recent_titles: list[str],
    chunk_ids: list[int],
    logger: logging.Logger,
    run_id: str | None,
) -> dict[str, int]:
    rows = _load_rows(conn, chunk_ids)
    prompt = build_prompt(config, profile_body, recent_titles, rows)
    chunk_hash = _prompt_hash(prompt)

    results: dict[int, ItemAnalysis] = {}
    hashes: dict[int, str] = {}  # 実際に LLM へ渡した入力の hash(item ごと。設計 01 §3)
    chunk_failed = False
    try:
        batch = complete_with_retry(engine, prompt, AnalysisBatch)
        for result in batch.results:
            if result.item_id in chunk_ids:  # allowlist 外の item_id は破棄
                results[result.item_id] = result
                hashes[result.item_id] = chunk_hash
    except LLMUnavailableError:
        # 環境障害(codex 不在・認証不在)は degraded で握ると全件が黙って劣化する
        # (#72)。段の失敗として伝播させ失敗通知に載せる(GH#74)
        raise
    except LLMError as e:
        # チャンク失敗(リトライ済み)はチャンクごと degraded 直行(設計 03 §2。
        # 単発再分析へ流すと呼び出しが 2+2N に増える)
        chunk_failed = True
        logger.warning(
            f"チャンク分析失敗(チャンクごと degraded 直行): {e}",
            extra={"run_id": run_id, "stage": "analyze"},
        )

    if not chunk_failed:
        # スキーマ上は成功したが欠落した item のみ単発再分析(1回)
        for row in rows:
            if row["id"] in results:
                continue
            analysis, single_hash = _reanalyze_single(
                config, engine, profile_body, recent_titles, row, logger, run_id
            )
            # degraded になっても「実際に渡した入力」= 単発プロンプトの hash を記録する
            hashes[row["id"]] = single_hash
            if analysis is not None:
                results[row["id"]] = analysis

    # 分析結果の挿入と items の状態遷移を同一トランザクションで確定する
    # (別々だと途中クラッシュで status='new' のまま analyses だけ残り、再実行時に
    #  nonce 違いの input_hash で重複挿入される)
    counts = {"analyzed": 0, "degraded": 0}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row in rows:
            analysis = results.get(row["id"])
            if analysis is not None:
                _insert_analysis(
                    conn, row, analysis, engine, hashes[row["id"]], run_id, degraded=False
                )
                counts["analyzed"] += 1
            else:
                _insert_degraded(conn, row, engine, hashes.get(row["id"], chunk_hash), run_id)
                counts["degraded"] += 1
            conn.execute("UPDATE items SET status = 'analyzed' WHERE id = ?", (row["id"],))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return counts


def _prompt_hash(prompt: str) -> str:
    return "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _reanalyze_single(
    config: Config,
    engine: LLMEngine,
    profile_body: str,
    recent_titles: list[str],
    row: sqlite3.Row,
    logger: logging.Logger,
    run_id: str | None,
) -> tuple[ItemAnalysis | None, str]:
    """(分析結果 or None, 単発プロンプトの hash)。hash は失敗時も provenance 用に返す。"""
    prompt = build_prompt(config, profile_body, recent_titles, [row])
    prompt_hash = _prompt_hash(prompt)
    try:
        batch = complete_with_retry(engine, prompt, AnalysisBatch)
    except LLMUnavailableError:
        raise  # 環境障害は degraded にしない(_analyze_chunk と同方針。GH#74)
    except LLMError as e:
        logger.warning(
            f"単発再分析も失敗(degraded 記録): {e}",
            extra={"run_id": run_id, "stage": "analyze", "item_id": row["id"]},
        )
        return None, prompt_hash
    for result in batch.results:
        if result.item_id == row["id"]:
            return result, prompt_hash
    return None, prompt_hash


# --- プロンプト構築(設計 03 §3.1〜3.2) ---


def build_prompt(
    config: Config,
    profile_body: str,
    recent_titles: list[str],
    rows: list[sqlite3.Row],
    *,
    nonce: str | None = None,
) -> str:
    nonce = nonce or secrets.token_hex(4)  # hex 8 桁・実行ごと
    template = PROMPT_PATH.read_text(encoding="utf-8")
    titles = "\n".join(sanitize(t, MAX_TITLE, nonce) for t in recent_titles) or "(なし)"
    items = "\n".join(_item_block(row, nonce, config.analysis.body_max_chars) for row in rows)
    mapping = {
        "interest_profile": profile_body,
        "recent_titles": titles,
        "items": items,
        "schema": json.dumps(AnalysisBatch.model_json_schema(), ensure_ascii=False),
        "nonce": nonce,
    }
    # 単一パス置換: テンプレート上のプレースホルダだけを置き換え、置換後の値は再走査
    # しない(逐次 .replace() だと入力由来の {{nonce}} 等が展開され nonce が漏れる)
    return re.sub(r"\{\{(\w+)\}\}", lambda m: mapping[m.group(1)], template)


def sanitize(value: str | None, max_length: int, nonce: str) -> str:
    """改行・制御文字の除去(1行強制)→ nonce 除去 → 長さ上限(設計 03 §3.2)。"""
    if not value:
        return ""
    flattened = _CONTROL_CHARS.sub(" ", value.replace("\n", " ").replace("\r", " "))
    return flattened.replace(nonce, "")[:max_length]


def _item_block(row: sqlite3.Row, nonce: str, body_max_chars: int) -> str:
    body = sanitize_body(row["extracted_text"] or "", nonce)[:body_max_chars]
    meta = " / ".join(
        [
            f"source: {sanitize(row['source_label'], MAX_SOURCE, nonce)}",
            f"author: {sanitize(row['author'], MAX_AUTHOR, nonce)}",
            f"published_at: {sanitize(row['published_at'], 30, nonce)}",
        ]
    )
    return (
        f'<item id="{row["id"]}">\n'
        f"title: {sanitize(row['title'], MAX_TITLE, nonce)}\n"
        f"{meta}\n"
        f"extraction: {row['extraction_status'] or 'failed'}\n"
        "body:\n"
        f"<<<DATA-{nonce}\n"
        f"{body}\n"
        f"DATA-{nonce}>>>\n"
        "</item>"
    )


def sanitize_body(value: str, nonce: str) -> str:
    """body は改行を保持し、制御文字(改行以外)・nonce・フェンス境界を除去する。"""
    cleaned = re.sub(r"[\x00-\x09\x0b-\x1f\x7f]", " ", value)
    cleaned = cleaned.replace(nonce, "")
    # フェンス境界の偽装文字列も除去(nonce 除去で実質無効化されるが、生の境界形も落とす)
    return cleaned.replace("<<<DATA-", "").replace("DATA-", "")


def _recent_titles(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT items.title FROM digests, json_each(digests.item_ids) AS je
        JOIN items ON items.id = je.value
        WHERE digests.digest_date >= date('now', '+9 hours', ?)  -- digest_date は JST 暦日
          AND items.title IS NOT NULL
        ORDER BY items.title
        """,
        (f"-{RECENT_DAYS} days",),
    ).fetchall()
    return [row["title"] for row in rows]


def _load_rows(conn: sqlite3.Connection, ids: list[int]) -> list[sqlite3.Row]:
    placeholders = ",".join("?" * len(ids))
    return conn.execute(
        f"""
        SELECT items.*, documents.extracted_text, documents.extraction_status,
               sources.type || ':' || sources.name AS source_label
        FROM items
        JOIN sources ON sources.id = items.source_id
        LEFT JOIN documents ON documents.item_id = items.id
        WHERE items.id IN ({placeholders})
        ORDER BY items.id
        """,
        ids,
    ).fetchall()


# --- analyses への写像(設計 03 §3.3 / 01 §3) ---


def _insert_analysis(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    analysis: ItemAnalysis,
    engine: LLMEngine,
    input_hash: str,
    run_id: str | None,
    *,
    degraded: bool,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO analyses
          (item_id, run_id, title_ja, summary, category, topic, relevance_score, novelty,
           negative_labels, recommendation_reason, implication, degraded, engine, model,
           prompt_version, input_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["id"],
            run_id,
            analysis.title_ja,
            analysis.summary,
            analysis.category,
            analysis.topic,
            analysis.relevance,
            analysis.novelty,
            json.dumps(analysis.negative, ensure_ascii=False),
            analysis.reason,
            analysis.implication,
            int(degraded),
            engine.name,
            getattr(engine, "model", "engine-default"),
            PROMPT_VERSION,
            input_hash,
        ),
    )


def _insert_degraded(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    engine: LLMEngine,
    input_hash: str,
    run_id: str | None,
) -> None:
    """LLM 全滅時のタイトルのみ記録(relevance=0・summary=タイトル・topic NULL。設計 03 §3.3)。"""
    conn.execute(
        """
        INSERT OR IGNORE INTO analyses
          (item_id, run_id, summary, category, topic, relevance_score, novelty,
           negative_labels, recommendation_reason, implication, degraded, engine, model,
           prompt_version, input_hash)
        VALUES (?, ?, ?, 'その他', NULL, 0, 'new', '[]', ?, ?, 1, ?, ?, ?, ?)
        """,
        (
            row["id"],
            run_id,
            (row["title"] or "(タイトルなし)")[:300],
            "LLM 分析失敗のため degraded 記録",
            "分析なし(タイトルのみ)",
            engine.name,
            getattr(engine, "model", "engine-default"),
            PROMPT_VERSION,
            input_hash,
        ),
    )
