"""分析の確かめ(GH#423・設計 03 §3.6)。

Codex(主のエンジン)が書いた分析を、別系統の AI(Claude API)が同じ材料と照らして確かめる。
直すときは、直した分析を analyses に新しい行として足す。item の最新の分析になるので、ランキング・
digest・トレンド・書き出しは今までどおり最新の行を読むだけで、直した値を使う。Codex の行は
書き換えずに残し、確かめた結果(ok / fixed・変わった項目・理由)は analysis_reviews に、
呼び出しごとの費用は analysis_review_calls に残す(migration 038)。

- 対象: その日(JST の暦日)に Codex が書き、まだ確かめていない、item の最新の分析(degraded と、予備の
  Claude が書いた分析は除く。予備と確かめが同じ系統になるため。GH#180 と同じ考え)。items が
  analyzed(digest 前)のものだけ。
- 束は分析と同じ大きさ(`[analysis].batch_size`)。1 束 1 回の呼び出しで、試し直さない(費用を有界に
  保つ。失敗した束の分析は Codex の値のまま配信し、同じ日の次の実行で確かめ直す)。
- 月の上限(`[analysis_review].monthly_cap_usd`。UTC の暦月)に達したら、その月は確かめない。
- 外部由来の入力は分析と同じくサニタイズして nonce で囲む。最初の分析も外部の記事に由来する LLM の
  出力なので、同じく囲む。出力は Pydantic で検証し、入力の item_id にだけ写す。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.analysis.analyzer import (
    MAX_TITLE,
    ItemAnalysis,
    _item_block,
    _load_rows,
    _recent_titles,
)
from app.analysis.llm import (
    ANTHROPIC_API_KEY_ENV,
    AnthropicApiEngine,
    LLMError,
    LLMUnavailableError,
)
from app.config import Config
from app.jst import jst_day_bounds_utc, today_jst
from app.prompting import fill_template, sanitize, sanitize_body

PROMPT_VERSION = "analyze-review-v001"
PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / f"{PROMPT_VERSION}.md"
# 確かめで足した analyses の行の prompt_version の頭。日次上限の使用量(stage1.analyzed_on)から外す印
REVIEW_PROMPT_PREFIX = "analyze-review-"

# 確かめで直しうる項目(ItemAnalysis の名前)と、analyses の列の対応
_FIELD_COLUMNS = {
    "title_ja": "title_ja",
    "summary": "summary",
    "relevance": "relevance_score",
    "category": "category",
    "negative": "negative_labels",
    "novelty": "novelty",
    "topic": "topic",
    "reason": "recommendation_reason",
    "implication": "implication",
}
# 失敗の種類(app/analysis/llm.py の AnthropicApiEngine が上げるメッセージの書き出し)。台帳とログには
# この種類だけを書き、続く本文(応答の位置・種別の詳細など)は書かない。先に一致したものを使う
_FAILURE_KINDS = (
    "タイムアウト",
    "Anthropic API への通信失敗",
    "Anthropic API の応答が途中で終わった",
    "Anthropic API の応答が JSON でない",
    "Anthropic API の応答がスキーマに合わない",
    "Anthropic API の応答を検証できない",
    "Anthropic API の応答",
)
_STATUS_FAILURE = re.compile(r"Anthropic API (\d{3})")


class ReviewResult(BaseModel):
    item_id: int
    verdict: Literal["ok", "fixed"]
    analysis: ItemAnalysis | None = None  # fixed のときの直した分析(全項目)
    note: str = Field(default="", max_length=150)


class ReviewBatch(BaseModel):
    results: list[ReviewResult]


@dataclass
class ReviewCounts:
    reviewed: int = 0  # 確かめた(ok + fixed)
    fixed: int = 0
    failed: int = 0  # 呼び出しが失敗した束の件数(Codex の値のまま)
    missing: int = 0  # 呼び出しは成功したが結果が無かった件数(Codex の値のまま)
    skipped: int = 0  # 月の上限で確かめなかった件数
    fields: dict[str, int] = field(default_factory=dict)  # 直した項目ごとの件数

    def as_log(self) -> dict[str, object]:
        return {
            "reviewed": self.reviewed,
            "fixed": self.fixed,
            "failed": self.failed,
            "missing": self.missing,
            "skipped": self.skipped,
            "fields": self.fields,
        }


def review_cost_this_month(conn: sqlite3.Connection) -> float:
    """今の UTC の暦月に確かめで使った額の見積もり(doctor #9 の当月と同じ月)。"""
    month_prefix = datetime.now(UTC).strftime("%Y-%m")
    return conn.execute(
        "SELECT coalesce(sum(cost_usd), 0) AS cost FROM analysis_review_calls "
        "WHERE created_at LIKE ? || '%'",
        (month_prefix,),
    ).fetchone()["cost"]


# その日(analyzed_at の範囲)に Codex が書き、まだ確かめていない、item の最新の分析。
# 確かめの対象(pending_reviews)と、確かめずに残った件数の知らせ(unreviewed_count)が共有する
_UNREVIEWED_WHERE = """
    a.analyzed_at >= ? AND a.analyzed_at < ?
    AND a.degraded = 0
    AND a.engine = 'codex-cli'
    AND NOT EXISTS (SELECT 1 FROM analysis_reviews AS r WHERE r.analysis_id = a.id)
    AND a.id = (
      SELECT id FROM analyses WHERE item_id = a.item_id
      ORDER BY analyzed_at DESC, id DESC LIMIT 1
    )
"""


def pending_reviews(conn: sqlite3.Connection, day: str) -> list[sqlite3.Row]:
    """JST の暦日 day に Codex が書き、まだ確かめていない分析(item の最新のもの)を id 順に。"""
    start, end = jst_day_bounds_utc(day)
    return conn.execute(
        "SELECT a.* FROM analyses AS a JOIN items ON items.id = a.item_id"
        f" WHERE {_UNREVIEWED_WHERE} AND items.status = 'analyzed' ORDER BY a.id",
        (start, end),
    ).fetchall()


def unreviewed_count(conn: sqlite3.Connection, day: str) -> int:
    """JST の暦日 day に Codex が書き、確かめないまま item の最新の分析になっている件数。

    digest に載った後の item も、digest の選外の分析も数える(確かめを経ずに digest に載った分を
    数え落とさないため)。digest に載った数や、配信できたかは表さない。
    """
    start, end = jst_day_bounds_utc(day)
    return conn.execute(
        f"SELECT count(*) AS n FROM analyses AS a WHERE {_UNREVIEWED_WHERE}", (start, end)
    ).fetchone()["n"]


def run_review_notice(
    conn: sqlite3.Connection,
    config: Config,
    env: Mapping[str, str],
    run_id: str,
    day: str,
) -> str | None:
    """朝バッチ 1 回の後の、確かめなかった分析の知らせ(無効か、全件を確かめたなら None)。

    確かめなくても分析は Codex の値のまま使えるので、段の失敗にはしない。それでも、確かめていない
    分析が残った日は知らせる(黙って Codex だけの判定に戻らない)。文言は固定の語と数と、
    台帳の失敗の種類(_failure_kind の固定の語)だけ。
    """
    settings = config.analysis_review
    if not settings.enabled:
        return None
    left = unreviewed_count(conn, day)
    if left == 0:
        return None
    lines = [f"⚠️ 今日の分析のうち {left} 件は、Claude が確かめていません(Codex の値のまま)"]
    failures = conn.execute(
        "SELECT error_kind, count(*) AS calls FROM analysis_review_calls"
        " WHERE run_id = ? AND ok = 0 GROUP BY error_kind ORDER BY min(id)",
        (run_id,),
    ).fetchall()
    if failures:
        kinds = "・".join(f"{row['error_kind']} {row['calls']} 回" for row in failures)
        lines.append(f"・確かめの呼び出しの失敗: {kinds}")
    spent = review_cost_this_month(conn)
    if spent >= settings.monthly_cap_usd:
        lines.append(
            f"・今月の確かめの費用 ${spent:.2f} が上限 ${settings.monthly_cap_usd:.2f} に達した"
        )
    if not env.get(ANTHROPIC_API_KEY_ENV, "").strip():
        lines.append(f"・.env に {ANTHROPIC_API_KEY_ENV} が無い")
    lines.append("morning.log の「分析の確かめ」の WARNING を見てください(runbook §17)")
    return "\n".join(lines)


def review_analyses(
    conn: sqlite3.Connection,
    config: Config,
    engine: AnthropicApiEngine,
    profile_body: str,
    logger: logging.Logger,
    *,
    run_id: str | None = None,
    day: str | None = None,
) -> ReviewCounts:
    """その日の確かめていない分析を確かめ、直したものを analyses に足す。件数を返す。"""
    settings = config.analysis_review
    counts = ReviewCounts()
    rows = pending_reviews(conn, day or today_jst())
    if not rows:
        return counts
    recent = _recent_titles(conn)
    batch_size = config.analysis.batch_size
    extra = {"run_id": run_id, "stage": "analyze"}
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        if review_cost_this_month(conn) >= settings.monthly_cap_usd:
            counts.skipped += len(rows) - start
            logger.warning(
                f"分析の確かめの月の上限(${settings.monthly_cap_usd:.2f})に達したので、"
                f"残り {len(rows) - start} 件は確かめない",
                extra=extra,
            )
            break
        try:
            _review_batch(conn, config, engine, profile_body, recent, batch, counts, run_id)
        except LLMUnavailableError as e:
            # 環境障害(キーの不正・認証の失敗など)は続けても同じなので、残りを確かめない
            counts.failed += len(rows) - start
            logger.warning(
                f"分析の確かめを止めた({_failure_kind(e)})。残り {len(rows) - start} 件は "
                "Codex の分析のまま",
                extra=extra,
            )
            break
        except LLMError as e:
            counts.failed += len(batch)
            logger.warning(
                f"分析の確かめの 1 束が失敗({_failure_kind(e)})。"
                f"{len(batch)} 件は Codex の分析のまま",
                extra=extra,
            )
    logger.info("分析の確かめ 完了", extra={**extra, "extra": counts.as_log()})
    return counts


def _review_batch(
    conn: sqlite3.Connection,
    config: Config,
    engine: AnthropicApiEngine,
    profile_body: str,
    recent_titles: list[str],
    batch: list[sqlite3.Row],
    counts: ReviewCounts,
    run_id: str | None,
) -> None:
    """1 束を確かめて結果を残す。呼び出しの失敗は台帳に残してから LLMError を送出する。"""
    item_rows = {row["id"]: row for row in _load_rows(conn, [a["item_id"] for a in batch])}
    prompt = build_review_prompt(config, profile_body, recent_titles, batch, item_rows)
    try:
        result = engine.complete_structured(
            prompt, ReviewBatch, timeout_s=config.analysis_review.timeout_s
        )
    except LLMError as e:
        _record_call(conn, engine, run_id, item_count=len(batch), error=e)
        raise
    call_id = _record_call(conn, engine, run_id, item_count=len(batch), error=None)

    by_item = {r.item_id: r for r in result.results}
    prompt_hash = "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for analysis_row in batch:
            item_id = analysis_row["item_id"]
            reviewed = by_item.get(item_id)
            if reviewed is None or (reviewed.verdict == "fixed" and reviewed.analysis is None):
                # 結果が無い・直すと答えたのに直した分析が無い = 確かめた記録を残さない(直すべきと
                # いう判断を ok として失わない。同じ日の次の実行で確かめ直す)
                counts.missing += 1
                continue
            if not _still_latest(conn, analysis_row):
                # 待つ間に別の分析が足された・digest 済み = その分析はもう配信に使わない
                continue
            changed: list[str] = []
            fixed: ItemAnalysis | None = None
            if reviewed.verdict == "fixed" and reviewed.analysis is not None:  # 上で None は除いた
                fixed = reviewed.analysis  # 行は確かめた分析の item に足す(中の item_id は使わない)
                changed = changed_fields(analysis_row, fixed)
            if fixed is not None and changed:
                fixed_id = _insert_fixed(conn, analysis_row, fixed, engine, prompt_hash, run_id)
                _insert_review(conn, analysis_row, call_id, "fixed", fixed_id, changed, reviewed)
                counts.fixed += 1
                for name in changed:
                    counts.fields[name] = counts.fields.get(name, 0) + 1
            else:
                # ok、または fixed でも値が変わっていない(analysis が無いものも含む)
                _insert_review(conn, analysis_row, call_id, "ok", None, [], reviewed)
            counts.reviewed += 1
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def build_review_prompt(
    config: Config,
    profile_body: str,
    recent_titles: list[str],
    batch: list[sqlite3.Row],
    item_rows: dict[int, sqlite3.Row],
    *,
    nonce: str | None = None,
) -> str:
    """確かめの prompt。アイテムのブロックは分析と同じ形で、続けて最初の分析を同じ nonce で囲む。"""
    nonce = nonce or secrets.token_hex(4)
    template = PROMPT_PATH.read_text(encoding="utf-8")
    titles = "\n".join(sanitize(t, MAX_TITLE, nonce) for t in recent_titles) or "(なし)"
    blocks = []
    for analysis_row in batch:
        row = item_rows[analysis_row["item_id"]]
        first = json.dumps(_as_item_analysis(analysis_row), ensure_ascii=False)
        blocks.append(
            _item_block(row, nonce, config.analysis.body_max_chars)
            + f'\n<first_analysis id="{row["id"]}">\n'
            f"<<<DATA-{nonce}\n"
            f"{sanitize_body(first, nonce)}\n"
            f"DATA-{nonce}>>>\n"
            "</first_analysis>"
        )
    mapping = {
        "interest_profile": profile_body,
        "recent_titles": titles,
        "items": "\n".join(blocks),
        "schema": json.dumps(ReviewBatch.model_json_schema(), ensure_ascii=False),
        "nonce": nonce,
    }
    return fill_template(template, mapping)


def changed_fields(analysis_row: sqlite3.Row, fixed: ItemAnalysis) -> list[str]:
    """最初の分析(analyses の行)と直した分析で、値が変わった項目の名前(ItemAnalysis の順)。"""
    first = _as_item_analysis(analysis_row)
    changed = []
    for name in _FIELD_COLUMNS:
        before = first[name]
        after = getattr(fixed, name)
        if name == "negative":
            before, after = sorted(before), sorted(after)
        if before != after:
            changed.append(name)
    return changed


def _as_item_analysis(analysis_row: sqlite3.Row) -> dict[str, object]:
    """analyses の行を ItemAnalysis の形(prompt に渡す最初の分析・比べる値)にする。"""
    relevance = analysis_row["relevance_score"]
    return {
        "item_id": analysis_row["item_id"],
        "title_ja": analysis_row["title_ja"] or "",
        "summary": analysis_row["summary"],
        "relevance": int(relevance) if float(relevance).is_integer() else relevance,
        "category": analysis_row["category"],
        "negative": json.loads(analysis_row["negative_labels"]),
        "novelty": analysis_row["novelty"],
        "topic": analysis_row["topic"] or "",
        "reason": analysis_row["recommendation_reason"],
        "implication": analysis_row["implication"],
    }


def _still_latest(conn: sqlite3.Connection, analysis_row: sqlite3.Row) -> bool:
    """確かめた分析が今も item の最新の分析で、item が digest 前(analyzed)のままか。"""
    row = conn.execute(
        """
        SELECT 1 FROM items
        WHERE items.id = ? AND items.status = 'analyzed'
          AND (SELECT id FROM analyses WHERE item_id = items.id
               ORDER BY analyzed_at DESC, id DESC LIMIT 1) = ?
        """,
        (analysis_row["item_id"], analysis_row["id"]),
    ).fetchone()
    return row is not None


def _insert_fixed(
    conn: sqlite3.Connection,
    analysis_row: sqlite3.Row,
    fixed: ItemAnalysis,
    engine: AnthropicApiEngine,
    prompt_hash: str,
    run_id: str | None,
) -> int:
    """直した分析を analyses に足す。費用は台帳(analysis_review_calls)に数えるので、ここは 0。"""
    cursor = conn.execute(
        """
        INSERT INTO analyses
          (item_id, run_id, title_ja, summary, category, topic, relevance_score, novelty,
           negative_labels, recommendation_reason, implication, degraded, engine, model,
           prompt_version, input_hash, extraction_failed, source_language)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
        """,
        (
            analysis_row["item_id"],
            run_id,
            fixed.title_ja,
            fixed.summary,
            fixed.category,
            fixed.topic,
            fixed.relevance,
            fixed.novelty,
            json.dumps(fixed.negative, ensure_ascii=False),
            fixed.reason,
            fixed.implication,
            engine.name,
            str(engine.model),
            PROMPT_VERSION,
            prompt_hash,
            analysis_row["extraction_failed"],
            # 元の記事の言語は入力から決めた値で、直した中身に依らない(GH#439)
            analysis_row["source_language"],
        ),
    )
    return int(cursor.lastrowid)


def _insert_review(
    conn: sqlite3.Connection,
    analysis_row: sqlite3.Row,
    call_id: int,
    verdict: str,
    fixed_analysis_id: int | None,
    changed: list[str],
    reviewed: ReviewResult,
) -> None:
    conn.execute(
        """
        INSERT INTO analysis_reviews
          (analysis_id, item_id, call_id, verdict, fixed_analysis_id, changed_fields, note)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            analysis_row["id"],
            analysis_row["item_id"],
            call_id,
            verdict,
            fixed_analysis_id,
            json.dumps(changed),
            reviewed.note if verdict == "fixed" else "",
        ),
    )


def _record_call(
    conn: sqlite3.Connection,
    engine: AnthropicApiEngine,
    run_id: str | None,
    *,
    item_count: int,
    error: LLMError | None,
) -> int:
    """1 呼び出しを台帳に残す。額は usage の実額。usage を受け取れず費用が分からない呼び出しは、
    トークン数を空にして課金されうる最大の額を積む(月の上限の判定に効かせる。GH#241)。"""
    usage = getattr(engine, "last_usage", None)
    if usage is not None:
        cost = usage.cost_usd
    else:
        bound = getattr(engine, "last_cost_bound_usd", None)
        cost = bound if bound is not None else 0.0
    cursor = conn.execute(
        """
        INSERT INTO analysis_review_calls
          (run_id, model, ok, error_kind, item_count, input_tokens, output_tokens, cost_usd)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            str(engine.model),
            int(error is None),
            None if error is None else _failure_kind(error),
            item_count,
            usage.input_tokens if usage else None,
            usage.output_tokens if usage else None,
            cost,
        ),
    )
    return int(cursor.lastrowid)


def _failure_kind(error: LLMError) -> str:
    """失敗の種類(本文を含まない)。"""
    message = str(error)
    if isinstance(error, LLMUnavailableError) and message.startswith(".env に"):
        return "API キーが無い"
    for kind in _FAILURE_KINDS:
        if message.startswith(kind):
            return kind
    status = _STATUS_FAILURE.match(message)
    if status:
        return f"Anthropic API {status.group(1)}"
    return "その他の失敗"
