"""X 興味シグナルの週次インサイト(S1c = GH#58 / ADR 0001)。

読取専用の可視化(常連著者・頻出テーマ)。検索スロット・フォロー・ランキングへの
自動反映はしない(チューニングは S2 = #59 の「提案 → 人が承認」ゲート)。

- スコア = **種類の重み × 直近の重み**(ADR 0001)。直近の重みは指数減衰
  (半減期 `[x_signals].half_life_days`)で「移動平均」を実装する(古い興味ほど
  軽く。固定プロファイルでなく「今これが知りたい」を重く見る)
- **頻出テーマ**は digest 経由で分析済みのツイート(items を external_id で突合)の
  `analyses.topic` 集計。テーマは分析(LLM)を経たものしか存在しないため、未分析の
  シグナルは「直近シグナル」の本文抜粋(TTL 内のみ残存)をテーマ判断の材料として
  並べる(週次レビューで人間が読む)
- 週次レビュー(plan §6)の一項目として `intel x-insights` で表示する
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import Config

KIND_LABELS = {"bookmark": "🔖", "like": "❤️", "self_rt": "🔁"}
AUTHOR_LIMIT = 10
TOPIC_LIMIT = 10
RECENT_LIMIT = 15
EXCERPT_DISPLAY = 60


@dataclass(frozen=True)
class AuthorInsight:
    author: str  # @username(無ければ author_id)
    score: float
    counts: dict[str, int]  # kind → 件数
    last_observed: str  # 'YYYY-MM-DD'


@dataclass(frozen=True)
class TopicInsight:
    topic: str
    score: float
    signals: int


def kind_weight(config: Config, kind: str) -> float:
    return {
        "bookmark": config.x_signals.weight_bookmark,
        "like": config.x_signals.weight_like,
        "self_rt": config.x_signals.weight_self_rt,
    }[kind]


def _recency_weight(config: Config, observed_at: str, now: datetime) -> float:
    """直近の重み: 半減期 half_life_days の指数減衰(移動平均の実装。ADR 0001)。"""
    observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    age_days = max(0.0, (now - observed).total_seconds() / 86400)
    return 0.5 ** (age_days / config.x_signals.half_life_days)


def author_insights(
    conn: sqlite3.Connection,
    config: Config,
    *,
    now: datetime | None = None,
    limit: int = AUTHOR_LIMIT,
) -> list[AuthorInsight]:
    """常連著者(重み付きスコア降順)。著者不明のシグナルは集計から除く。"""
    now = now or datetime.now(UTC)
    rows = conn.execute(
        "SELECT kind, author_id, author_username, observed_at FROM x_signals "
        "WHERE author_id IS NOT NULL"
    ).fetchall()
    by_author: dict[str, dict] = {}
    for row in rows:
        entry = by_author.setdefault(
            row["author_id"],
            {"username": None, "score": 0.0, "counts": {}, "last": ""},
        )
        if row["author_username"]:
            entry["username"] = row["author_username"]
        entry["score"] += kind_weight(config, row["kind"]) * _recency_weight(
            config, row["observed_at"], now
        )
        entry["counts"][row["kind"]] = entry["counts"].get(row["kind"], 0) + 1
        entry["last"] = max(entry["last"], row["observed_at"])
    insights = [
        AuthorInsight(
            author=f"@{e['username']}" if e["username"] else author_id,
            score=e["score"],
            counts=e["counts"],
            last_observed=e["last"][:10],
        )
        for author_id, e in by_author.items()
    ]
    return sorted(insights, key=lambda i: i.score, reverse=True)[:limit]


def topic_insights(
    conn: sqlite3.Connection,
    config: Config,
    *,
    now: datetime | None = None,
    limit: int = TOPIC_LIMIT,
) -> list[TopicInsight]:
    """頻出テーマ: 分析済みツイート(items を external_id で突合)の topic 集計。"""
    now = now or datetime.now(UTC)
    rows = conn.execute(
        """
        SELECT s.kind, s.observed_at, a.topic
        FROM x_signals s
        JOIN sources src ON src.type = 'x'
        JOIN items i ON i.source_id = src.id AND i.external_id = s.tweet_id
        JOIN analyses a ON a.id = (
            SELECT id FROM analyses WHERE item_id = i.id
            ORDER BY analyzed_at DESC, id DESC LIMIT 1
        )
        WHERE a.topic IS NOT NULL AND a.degraded = 0
        """
    ).fetchall()
    by_topic: dict[str, dict] = {}
    for row in rows:
        entry = by_topic.setdefault(row["topic"], {"score": 0.0, "signals": 0})
        entry["score"] += kind_weight(config, row["kind"]) * _recency_weight(
            config, row["observed_at"], now
        )
        entry["signals"] += 1
    insights = [
        TopicInsight(topic=topic, score=e["score"], signals=e["signals"])
        for topic, e in by_topic.items()
    ]
    return sorted(insights, key=lambda i: i.score, reverse=True)[:limit]


def recent_signals(conn: sqlite3.Connection, *, limit: int = RECENT_LIMIT) -> list[sqlite3.Row]:
    """直近シグナル(テーマ判断の材料。本文抜粋は TTL 内のみ残存)。"""
    return conn.execute(
        "SELECT kind, author_username, text_excerpt, observed_at FROM x_signals "
        "ORDER BY observed_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def render_insights(
    conn: sqlite3.Connection, config: Config, *, now: datetime | None = None
) -> str:
    """週次レビュー用のプレーンテキスト(読取専用。提案はしない = S2 の領分)。"""
    now = now or datetime.now(UTC)
    total = conn.execute("SELECT count(*) FROM x_signals").fetchone()[0]
    if total == 0:
        return "x_signals にシグナルなし(取込は runbook §10 / intel x-signals)"
    weights = (
        f"🔖{config.x_signals.weight_bookmark:g} / ❤️{config.x_signals.weight_like:g} / "
        f"🔁{config.x_signals.weight_self_rt:g}・半減期 {config.x_signals.half_life_days:g}日"
    )
    lines = [f"X 興味シグナル インサイト(全 {total} 件・重み {weights})", ""]
    lines.append("■ 常連著者(スコア = 種類の重み × 直近の重み)")
    for insight in author_insights(conn, config, now=now):
        counts = " ".join(
            f"{KIND_LABELS[k]}{n}" for k, n in sorted(insight.counts.items())
        )
        lines.append(
            f"  {insight.score:6.1f}  {insight.author}({counts}・直近 {insight.last_observed})"
        )
    topics = topic_insights(conn, config, now=now)
    lines.append("")
    lines.append("■ 頻出テーマ(digest 経由で分析済みのシグナルのみ)")
    if topics:
        lines.extend(
            f"  {t.score:6.1f}  {t.topic}({t.signals} 件)" for t in topics
        )
    else:
        lines.append("  (分析済みシグナルなし。下の直近シグナルを材料に)")
    lines.append("")
    lines.append("■ 直近シグナル(テーマ判断の材料。本文は TTL 内のみ)")
    for row in recent_signals(conn):
        author = f"@{row['author_username']}" if row["author_username"] else "(不明)"
        excerpt = (row["text_excerpt"] or "(本文 TTL 切れ)")[:EXCERPT_DISPLAY]
        lines.append(
            f"  {row['observed_at'][:10]} {KIND_LABELS[row['kind']]} {author}: {excerpt}"
        )
    return "\n".join(lines)
