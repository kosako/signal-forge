"""ランキング式 v1(設計 03 §6)。LLM 出力後に決定的に合成する。

score = clamp(relevance × novelty係数 − negativeペナルティ − 鮮度減点 − 重複ペナルティ, 0, 100)

- 算出した score は digest 直前に analyses.total_score へ書き戻す(生産者はここのみ)
- 選抜: score 降順で最大 digest.max_items 件。min_score 未満は載せない。
  同一ソース上限 max_per_source 件(超過は次点繰上げ)
- 今の重点(GH#433 の 4 本目): 今の重点に当たる記事(app/focus_match.py が選ぶ)のうち、点の高い
  `[focus].bonus_items` 件の点に `[focus].score_bonus` を足す。min_score は足す前の点で見る
  (重点でも質の下限は守る)。並びは足した後の点で、同点(100 で揃ったときなど)は足す前の点の順
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import Config

# 重点に当たるかを LLM に聞く候補の上限(点の高い順。GH#433)
FOCUS_POOL_MAX = 60


@dataclass(frozen=True)
class RankedItem:
    item_id: int
    analysis_id: int
    source_id: int
    source_label: str
    title: str | None
    canonical_url: str
    score: float
    summary: str
    category: str
    recommendation_reason: str
    implication: str
    degraded: bool
    # 日本語見出し(analyze-v002)。None = v001 世代 / degraded(表示は原文 title に fallback)
    title_ja: str | None = None
    # 本文の抽出に失敗し、見出しだけで分析した(analyses.extraction_failed。GH#240)。表示用
    extraction_failed: bool = False
    # 今の重点に当たる記事なら、その話題の id(GH#433)。点を足したかどうかは問わない
    focus_topic_id: int | None = None


def rank_for_digest(
    conn: sqlite3.Connection, config: Config, *, focus_items: Mapping[int, int] | None = None
) -> list[RankedItem]:
    """status='analyzed' の items をスコアリングし、配信候補(選抜済み)を返す。

    total_score の書き戻しは候補全件に対して行う(選抜外too。週次レビューの分析材料)。
    focus_items は今の重点に当たる記事(item_id → 話題の id。GH#433)。点の高い順に
    `[focus].bonus_items` 件の点に `[focus].score_bonus` を足す(total_score も足した点)。
    """
    focus_items = dict(focus_items or {})
    scored = _apply_focus_bonus(_scored(conn, config), config, focus_items)

    for row, _base, score in scored:
        conn.execute(
            "UPDATE analyses SET total_score = ? WHERE id = ?",
            (_clamp(score), row["analysis_id"]),
        )

    return _select(scored, config, focus_items)


def focus_pool(
    conn: sqlite3.Connection, config: Config, *, include_item_ids: Collection[int] = ()
) -> list[sqlite3.Row]:
    """今の重点に当たるかを LLM に聞く候補(GH#433)。点の高い順に FOCUS_POOL_MAX 件まで。

    点を足すと上位に届きうる記事だけを聞く: 足す前の点が min_score 以上で、点の順に選んだときの
    最後の記事の点から score_bonus を引いた点以上。上位に入る記事も聞く(足す件数の上限
    `bonus_items` は点の高い重点の記事から使うため)。degraded は聞かない。include_item_ids は
    候補に戻してから順位を付け直す記事(`--force` で置き換える digest の記事。GH#433)。
    """
    scored = _scored(conn, config, include_item_ids)
    selected = _select([(row, score, score) for row, score in scored], config, {})
    floor = config.ranking.min_score
    if len(selected) >= config.digest.max_items:
        floor = max(floor, selected[-1].score - config.focus.score_bonus)
    pool = [(row, score) for row, score in scored if not row["degraded"] and _clamp(score) >= floor]
    pool.sort(key=lambda pair: (-pair[1], pair[0]["item_id"]))
    return [row for row, _score in pool[:FOCUS_POOL_MAX]]


def _scored(
    conn: sqlite3.Connection, config: Config, include_item_ids: Collection[int] = ()
) -> list[tuple[sqlite3.Row, float]]:
    """候補と、足す前の生の点(新しさ・否定・鮮度・重複を当てた後。clamp 前)。"""
    rows = digest_candidates(conn, include_item_ids=include_item_ids)
    # 生スコアで合成し、clamp は全ペナルティ適用後に1回だけ(設計 03 §6 の式どおり。
    # 途中 clamp すると novelty 係数 >1.0 等の許容設定で結果がズレる)
    scored = [(row, _base_score(row, config)) for row in rows]
    return _apply_topic_dup_penalty(scored, config)


def digest_candidates(
    conn: sqlite3.Connection, *, include_item_ids: Collection[int] = ()
) -> list[sqlite3.Row]:
    """digest の候補(status='analyzed' の items)を最新の分析と一緒に item_id 順で返す。

    通常のランキングと LLM 全滅(degraded)の判定は同じ集合を見る必要があるので、候補の条件は
    ここにだけ置く(GH#112)。全滅の判定はこのうち当日に分析されたものだけを見る
    (analyzed_at で絞るのは builder 側。GH#175)。include_item_ids は status を問わず候補に
    入れる記事(`--force` で analyzed に戻す前の、置き換える digest の記事。GH#433 の focus_pool)。
    """
    include = [int(item_id) for item_id in include_item_ids]
    include_sql = f"OR items.id IN ({','.join('?' * len(include))})" if include else ""
    return conn.execute(
        f"""
        SELECT items.id AS item_id, items.source_id, items.title, items.canonical_url,
               items.published_at,
               sources.type AS source_type, sources.type || ':' || sources.name AS source_label,
               analyses.id AS analysis_id, analyses.relevance_score, analyses.novelty,
               analyses.negative_labels, analyses.topic, analyses.title_ja, analyses.summary,
               analyses.category, analyses.recommendation_reason, analyses.implication,
               analyses.degraded, analyses.extraction_failed, analyses.analyzed_at
        FROM items
        JOIN sources ON sources.id = items.source_id
        JOIN analyses ON analyses.id = (
            SELECT id FROM analyses WHERE item_id = items.id
            ORDER BY analyzed_at DESC, id DESC LIMIT 1
        )
        WHERE (items.status = 'analyzed' {include_sql})
          -- ブックマーク紐付けのある item は候補に入れない(ランキングにも全滅の判定にも。
          -- must-include の別枠。判定は x_signals の紐付け = 既存 item への合流(RSS 等と
          -- canonical 重複)でも属性が失われない。GH#99)
          AND NOT EXISTS (
            SELECT 1 FROM x_signals
            WHERE x_signals.kind = 'bookmark' AND x_signals.item_id = items.id
          )
        ORDER BY items.id
        """,
        include,
    ).fetchall()


def _base_score(row: sqlite3.Row, config: Config) -> float:
    r = config.ranking
    novelty_factor = {
        "new": r.novelty_new,
        "update": r.novelty_update,
        "known": r.novelty_known,
    }[row["novelty"]]
    negative_count = len(json.loads(row["negative_labels"]))
    return (
        row["relevance_score"] * novelty_factor
        - negative_count * r.negative_penalty
        - _freshness_penalty(row, config)
    )


def _freshness_penalty(row: sqlite3.Row, config: Config) -> float:
    """24h以内=0 / 24〜48h / 48〜72h。manual は常に 0。

    published_at 不明(NULL)も 0(判定不能に減点しない — stage1 の方針と同じ)。
    """
    if row["source_type"] == "manual" or row["published_at"] is None:
        return 0.0
    published = datetime.strptime(row["published_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    age_hours = (datetime.now(UTC) - published).total_seconds() / 3600
    if age_hours < 24:
        return 0.0
    if age_hours < 48:
        return config.ranking.freshness_penalty_24_48
    return config.ranking.freshness_penalty_48_72


def _apply_topic_dup_penalty(
    scored: list[tuple[sqlite3.Row, float]], config: Config
) -> list[tuple[sqlite3.Row, float]]:
    """当日バッチ内で topic 一致(完全一致・前後空白除去)の2件目以降に −penalty。

    最高 score の1件を残す(除外はしない)。同点は item_id 昇順で先勝ち(決定性)。
    """
    by_topic: dict[str, list[int]] = {}
    for index, (row, _score) in enumerate(scored):
        topic = (row["topic"] or "").strip()
        if topic:
            by_topic.setdefault(topic, []).append(index)

    result = list(scored)
    for indexes in by_topic.values():
        if len(indexes) < 2:
            continue
        best = max(indexes, key=lambda i: (result[i][1], -result[i][0]["item_id"]))
        for i in indexes:
            if i != best:
                row, score = result[i]
                result[i] = (row, score - config.ranking.topic_dup_penalty)
    return result


def _apply_focus_bonus(
    scored: list[tuple[sqlite3.Row, float]], config: Config, focus_items: Mapping[int, int]
) -> list[tuple[sqlite3.Row, float, float]]:
    """(row, 足す前の点, 足した後の点)。重点の記事のうち、足す前の点が min_score 以上で点の高い
    bonus_items 件に score_bonus を足す(同点は item_id 昇順)。"""
    focus = sorted(
        (
            (row, score)
            for row, score in scored
            if row["item_id"] in focus_items and _clamp(score) >= config.ranking.min_score
        ),
        key=lambda pair: (-pair[1], pair[0]["item_id"]),
    )
    boosted = {row["item_id"] for row, _score in focus[: config.focus.bonus_items]}
    bonus = config.focus.score_bonus
    return [
        (row, score, score + bonus if row["item_id"] in boosted else score)
        for row, score in scored
    ]


def _select(
    scored: list[tuple[sqlite3.Row, float, float]],
    config: Config,
    focus_items: Mapping[int, int],
) -> list[RankedItem]:
    # min_score は足す前の点で見る(重点の点を足しても質の下限は守る。GH#433)
    eligible = [
        (row, _clamp(final), _clamp(base))
        for row, base, final in scored
        if _clamp(base) >= config.ranking.min_score
    ]
    # score 降順、同点は足す前の点(clamp 後)の順、なお同点は item_id 昇順(決定性)。足さない記事は
    # 足す前と後が同じなので、今までの順(score が同じなら item_id 順)のまま
    eligible.sort(key=lambda t: (-t[1], -t[2], t[0]["item_id"]))

    selected: list[RankedItem] = []
    per_source: dict[int, int] = {}
    for row, score, _base in eligible:
        if len(selected) >= config.digest.max_items:
            break
        if per_source.get(row["source_id"], 0) >= config.ranking.max_per_source:
            continue  # 次点繰上げ(ソース上限超過はスキップして次へ)
        per_source[row["source_id"]] = per_source.get(row["source_id"], 0) + 1
        selected.append(
            RankedItem(
                item_id=row["item_id"],
                analysis_id=row["analysis_id"],
                source_id=row["source_id"],
                source_label=row["source_label"],
                title=row["title"],
                canonical_url=row["canonical_url"],
                score=score,
                summary=row["summary"],
                category=row["category"],
                recommendation_reason=row["recommendation_reason"],
                implication=row["implication"],
                degraded=bool(row["degraded"]),
                title_ja=row["title_ja"],
                extraction_failed=bool(row["extraction_failed"]),
                focus_topic_id=focus_items.get(row["item_id"]),
            )
        )
    return selected


def _clamp(score: float) -> float:
    return max(0.0, min(100.0, score))
