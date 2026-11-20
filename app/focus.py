"""今の重点(GH#433・設計 03 §4.1)。

直近の興味シグナルを LLM で話題に振り分け、話題ごとの重みで「今の重点」を決める。ADR 0001 の
「種類の重み × 直近の重み」の移動平均を、話題の単位で作る。

- シグナルは、X の 🔖・❤️・🔁(x_signals)、Telegram の 👍・🔖(signals の reaction)、カードへの感想と
  リプライ(item_notes)、🔎 出来事の調査の依頼(news_investigations)。⚡ 今すぐの深掘りは、処理の後に
  依頼の記録が残らないので数えない
- 1 つのシグナルは 1 回だけ振り分け、focus_evidence に残す(どの話題にも当てはまらなかったものも)。
  話題の名前が日ごとに揺れないよう、前回までの話題の一覧を LLM に渡し、同じ対象はその話題に振らせる
- 重み = 記事・投稿ごとに max(種類の重み × 0.5^(経過日数 / 半減期)) を足したもの。1 本の記事への
  👍・🔖・感想の重なりは一番強い 1 つだけを数え、複数の記事・投稿にまたがる話題を重点にする。毎回
  focus_evidence から計算するので、新しいシグナルが無い日も、減衰で重点から外れる
- 台帳には、シグナルが向いた先を寄せる前の形で残す(X のシグナルは投稿、反応と感想は記事)。どの
  記事・投稿として数えるかは、重みの計算のたびに _subject_sql で決める。後から投稿の記事ができた・
  ブックマークが別の記事に合流したときも、保存済みの証拠を今の対応で数え直すため
- 重点に入るのは enter_score 以上、外れるのは exit_score 未満(出入りのばたつきを防ぐ)。数は
  max_active まで。満ちているときは、重みの高い候補が来ても入れ替えない(外れるのを待つ)
- 話題の名前と検索の語は、外部の本文から LLM が作った文。prompt には nonce の囲みの中の data として
  渡し、表示では 1 行に潰す。外へ送る前の検査は、使う側(検索)が持つ
- 付加機能: LLM が失敗しても重みの更新(減衰)は進め、朝バッチを止めない。ログには件数と失敗の種類
  だけを書く(話題の名前は外部の本文に由来するので書かない)
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from pydantic import BaseModel

from app.analysis.llm import LLMEngine, LLMError, complete_with_retry
from app.config import Config
from app.models import FOCUS_MAX_TOPICS, FocusEntry
from app.prompting import fill_template, sanitize

PROMPT_VERSION = "focus-v001"
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / f"{PROMPT_VERSION}.md"

MAX_NAME = 30  # 話題の名前の上限(prompt は 20 字以内を指示する。超えた分は切る)
MAX_TERMS = 3
MAX_TERM = 40  # 検索の語 1 つの上限(prompt は 30 字以内を指示する。超えた語は捨てる)
MAX_SIGNAL_TEXT = 240  # 材料 1 件の本文の上限(prompt に入れる分)
MAX_NOTE = 200  # 感想の本文の上限(記事の見出しと並べる分)
# prompt に渡す前回までの話題の上限と、渡す話題の新しさ(最後の証拠が window_days の何倍以内か)。
# 古い話題は渡さないが、同じ名前で作り直されたら名前の一致で元の行に戻す
MAX_PROMPT_TOPICS = 30
PROMPT_TOPIC_WINDOW_FACTOR = 4
CANDIDATE_DISPLAY = 10
MATCH_EXAMPLES = 3  # 重点に当たる記事を選ぶ prompt に添える、話題の証拠の例の数(GH#433 の 4 本目)
MIN_DISPLAY_SCORE = 0.1

KIND_LABELS = {
    "x_bookmark": "X のブックマーク",
    "x_like": "X のいいね",
    "x_self_rt": "X のリポスト",
    "tg_useful": "記事への 👍 価値あり",
    "tg_dig": "記事への 🔖 深掘りしたい",
    "note": "記事への感想",
    "investigation": "出来事の調査の依頼",
}
KIND_ICONS = {
    "x_bookmark": "🔖",
    "x_like": "❤️",
    "x_self_rt": "🔁",
    "tg_useful": "👍",
    "tg_dig": "📌",
    "note": "📝",
    "investigation": "🔎",
}
_TOPIC_REF = re.compile(r"t(\d+)")
_SIGNAL_REF = re.compile(r"s\d+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# 検索の語から外す文字(演算子と引用。外へ送る前の検査は使う側が別に持つ)
_TERM_STRIP = re.compile(r"[\"'()\[\]{}]")
_TERM_OPERATORS = frozenset({"OR", "AND", "NOT"})
_LATIN = re.compile(r"[A-Za-z]")


class FocusTopicDraft(BaseModel):
    topic: str  # 前回までの話題の id("t12")か "new"
    name: str
    terms: list[str]
    signals: list[str]  # 材料の id("s3")


class FocusAssignments(BaseModel):
    topics: list[FocusTopicDraft]


@dataclass(frozen=True)
class FocusSignal:
    key: str  # prompt の中の id("s1")
    kind: str  # focus_evidence.signal_kind
    ref: int  # 元の行の id
    # シグナルが向いた先('tweet:<id>'・'item:12'・'investigation:3')。寄せる前の形で、重みの計算の
    # たびに同じ記事・投稿へ寄せる
    subject: str
    observed_at: str  # 元のシグナルの時刻(UTC の 'YYYY-MM-DDTHH:MM:SSZ')
    text: str  # 材料の本文(外部由来。prompt に入れるときに sanitize する)


@dataclass(frozen=True)
class TopicScore:
    topic_id: int
    name: str
    terms: tuple[str, ...]
    status: str
    score: float
    counts: dict[str, int]  # 種類ごとのシグナルの数
    subjects: int  # 記事・投稿の数
    last_observed: str | None


class FocusTopicRef(NamedTuple):
    """重点に当たる記事を選ぶ prompt に渡す今の重点の 1 つ(GH#433 の 4 本目・app/focus_match.py)。

    examples は話題の証拠になった投稿・記事の見出し(新しい順)。名前だけでは話題の意味が広く
    読まれるので、例で絞る。name は外部の本文から LLM が作った文、examples は外部の本文なので、
    prompt では nonce の囲みの中に入れる。
    """

    topic_id: int
    name: str
    examples: tuple[str, ...] = ()


@dataclass
class FocusRunResult:
    signals: int = 0  # 振り分けに渡したシグナルの数
    assigned: int = 0  # 話題に振ったシグナルの数
    new_topics: int = 0
    llm_failed: bool = False
    activated: list[int] = field(default_factory=list)  # 重点に入った話題の id
    deactivated: list[int] = field(default_factory=list)  # 重点から外れた話題の id


def run_focus(
    conn: sqlite3.Connection,
    config: Config,
    engine: LLMEngine,
    logger: logging.Logger,
    *,
    run_id: str | None = None,
    now: datetime | None = None,
) -> FocusRunResult:
    """新しいシグナルを話題に振り分け、重みで今の重点を更新する。"""
    now = now or datetime.now(UTC)
    result = FocusRunResult()
    signals = collect_signals(conn, config, now)
    result.signals = len(signals)
    extra = {"run_id": run_id, "stage": "focus"}
    if signals:
        topics = prompt_topics(conn, config, now)
        nonce = secrets.token_hex(4)
        prompt = build_prompt(config, topics, signals, nonce)
        try:
            drafts = complete_with_retry(engine, prompt, FocusAssignments)
        except LLMError as e:
            # 付加機能なので段を止めない。振り分けなかったシグナルは次の実行で振り分ける
            result.llm_failed = True
            logger.warning(
                f"今の重点の振り分けに失敗(重みの更新だけ進める): {type(e).__name__}",
                extra=extra,
            )
        else:
            allowed = {t.topic_id for t in topics}
            result.assigned, result.new_topics = apply_assignments(
                conn, signals, drafts, allowed, run_id=run_id
            )
    result.activated, result.deactivated = refresh_status(conn, config, now)
    logger.info(
        "今の重点を更新",
        extra={
            **extra,
            "extra": {
                "signals": result.signals,
                "assigned": result.assigned,
                "new_topics": result.new_topics,
                "activated": len(result.activated),
                "deactivated": len(result.deactivated),
                "llm_failed": result.llm_failed,
            },
        },
    )
    return result


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tweet_item_sql(tweet: str) -> str:
    """投稿(tweet id の SQL 式)を記事(items.id)に寄せる SQL 式。寄せる先が無ければ NULL。

    ブックマークが記事に合流した投稿は、その記事(x_signals の item_id。外部リンクの先の RSS の記事の
    こともある)に寄せる。無ければ、X の投稿として取り込んだ記事に寄せる。
    """
    return (
        "coalesce("
        f"(SELECT x2.item_id FROM x_signals x2 WHERE x2.tweet_id = {tweet} "
        "AND x2.item_id IS NOT NULL ORDER BY x2.id LIMIT 1), "
        "(SELECT i2.id FROM items i2 JOIN sources so2 ON so2.id = i2.source_id AND so2.type = 'x' "
        f"WHERE i2.external_id = {tweet} ORDER BY i2.id LIMIT 1))"
    )


def _subject_sql(subject: str) -> str:
    """台帳の subject(SQL 式)を、数えるときの記事・投稿に寄せる SQL 式。

    投稿('tweet:<id>')は _tweet_item_sql で記事に寄せる。X の投稿として取り込んだ記事('item:<id>' で
    external_id が投稿の id)も、元の投稿を通して同じ先に寄せる(その投稿のブックマークが別の記事に
    合流していれば、その記事)。寄せる先が無ければ subject のまま。
    """
    tweet_of_item = (
        "(SELECT i3.external_id FROM items i3 JOIN sources so3 ON so3.id = i3.source_id "
        f"AND so3.type = 'x' WHERE i3.id = CAST(substr({subject}, 6) AS INTEGER))"
    )
    return (
        f"CASE WHEN {subject} LIKE 'tweet:%' "
        f"THEN coalesce('item:' || {_tweet_item_sql(f'substr({subject}, 7)')}, {subject}) "
        f"WHEN {subject} LIKE 'item:%' AND {tweet_of_item} IS NOT NULL "
        f"THEN coalesce('item:' || {_tweet_item_sql(tweet_of_item)}, {subject}) "
        f"ELSE {subject} END"
    )


def collect_signals(conn: sqlite3.Connection, config: Config, now: datetime) -> list[FocusSignal]:
    """まだ振り分けていない、window_days 以内のシグナル(新しい側から max_signals 件)。

    材料の本文が無いもの(X の本文の保存期間が過ぎ、分析も無いなど)は渡さない。振り分けの記録も
    残さないので、本文が無いまま window_days を過ぎたら数えない。
    """
    cutoff = _iso(now - timedelta(days=config.focus.window_days))
    found: list[tuple[str, int, str, str, str]] = []  # (kind, ref, subject, observed_at, text)
    latest_title = (
        "(SELECT a.title_ja FROM analyses a WHERE a.item_id = {item} AND a.degraded = 0 "
        "ORDER BY a.analyzed_at DESC, a.id DESC LIMIT 1)"
    )
    for row in conn.execute(
        f"""
        SELECT r.*, {latest_title.format(item="r.item_id")} AS title_ja,
               (SELECT title FROM items WHERE id = r.item_id) AS item_title
        FROM (
          SELECT s.id, s.kind, s.tweet_id, s.observed_at, s.text_excerpt,
                 -- 本文の抜粋が消えていたら、同じ投稿の記事の見出しを使う
                 coalesce(s.item_id, {_tweet_item_sql("s.tweet_id")}) AS item_id
          FROM x_signals s
          WHERE s.observed_at >= ?
            AND NOT EXISTS (
              SELECT 1 FROM focus_evidence e
              WHERE e.signal_kind = 'x_' || s.kind AND e.signal_ref = s.id
            )
        ) r
        """,
        (cutoff,),
    ):
        text = row["text_excerpt"] or row["title_ja"] or row["item_title"]
        if text:
            # 台帳には投稿のまま残す(記事への寄せ方は重みの計算のたびに決める)
            subject = f"tweet:{row['tweet_id']}"
            found.append((f"x_{row['kind']}", row["id"], subject, row["observed_at"], text))
    for row in conn.execute(
        """
        SELECT g.id, g.item_id, g.value, g.created_at, a.title_ja, a.topic, i.title
        FROM signals g
        JOIN items i ON i.id = g.item_id
        -- 分析が degraded の記事も元の見出しで配信するので、その記事への反応も元の見出しで数える
        LEFT JOIN analyses a ON a.id = (
            SELECT id FROM analyses WHERE item_id = g.item_id AND degraded = 0
            ORDER BY analyzed_at DESC, id DESC LIMIT 1
        )
        WHERE g.kind = 'reaction' AND g.value IN ('useful', 'dig') AND g.created_at >= ?
          AND NOT EXISTS (
            SELECT 1 FROM focus_evidence e
            WHERE e.signal_kind = CASE g.value WHEN 'useful' THEN 'tg_useful' ELSE 'tg_dig' END
              AND e.signal_ref = g.id
          )
        """,
        (cutoff,),
    ):
        title = row["title_ja"] or row["title"]
        if title:
            kind = "tg_useful" if row["value"] == "useful" else "tg_dig"
            text = title + (f"(topic: {row['topic']})" if row["topic"] else "")
            found.append((kind, row["id"], f"item:{row['item_id']}", row["created_at"], text))
    for row in conn.execute(
        f"""
        SELECT n.id, n.item_id, n.created_at, n.note,
               {latest_title.format(item="n.item_id")} AS title_ja, i.title
        FROM item_notes n
        JOIN items i ON i.id = n.item_id
        WHERE n.created_at >= ?
          AND NOT EXISTS (
            SELECT 1 FROM focus_evidence e WHERE e.signal_kind = 'note' AND e.signal_ref = n.id
          )
        """,
        (cutoff,),
    ):
        title = row["title_ja"] or row["title"]
        note = row["note"][:MAX_NOTE]
        text = f"記事: {title} / 感想: {note}" if title else f"感想: {note}"
        found.append(("note", row["id"], f"item:{row['item_id']}", row["created_at"], text))
    for row in conn.execute(
        """
        SELECT v.id, v.requested_at, v.label, v.summary
        FROM news_investigations v
        WHERE v.requested_at >= ?
          AND NOT EXISTS (
            SELECT 1 FROM focus_evidence e
            WHERE e.signal_kind = 'investigation' AND e.signal_ref = v.id
          )
        """,
        (cutoff,),
    ):
        found.append((
            "investigation", row["id"], f"investigation:{row['id']}", row["requested_at"],
            f"{row['label']}: {row['summary']}",
        ))
    found.sort(key=lambda s: (s[3], s[0], s[1]), reverse=True)  # 新しい側から
    return [
        FocusSignal(
            key=f"s{index}", kind=kind, ref=ref, subject=subject, observed_at=observed_at,
            text=text,
        )
        for index, (kind, ref, subject, observed_at, text) in enumerate(
            found[: config.focus.max_signals], start=1
        )
    ]


def kind_weight(config: Config, kind: str) -> float:
    return {
        "x_bookmark": config.x_signals.weight_bookmark,
        "x_like": config.x_signals.weight_like,
        "x_self_rt": config.x_signals.weight_self_rt,
        "tg_useful": config.focus.weight_tg_useful,
        "tg_dig": config.focus.weight_tg_dig,
        "note": config.focus.weight_note,
        "investigation": config.focus.weight_investigation,
    }[kind]


def _decay(config: Config, observed_at: str, now: datetime) -> float:
    observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    age_days = max(0.0, (now - observed).total_seconds() / 86400)
    return 0.5 ** (age_days / config.focus.half_life_days)


def topic_scores(conn: sqlite3.Connection, config: Config, now: datetime) -> list[TopicScore]:
    """話題ごとの重み(重みの高い順。同じ重みは id の順)。

    記事・投稿(subject)ごとに、一番強いシグナル(種類の重み × 新しさの減衰)1 つだけを数えて足す。
    """
    topics: dict[int, dict] = {}
    for row in conn.execute(
        f"""
        SELECT t.id, t.name, t.terms, t.status, e.signal_kind, e.observed_at,
               -- 振り分けた後に投稿の記事ができた・ブックマークが合流したときも、今の対応で寄せる
               {_subject_sql("e.subject")} AS subject
        FROM focus_topics t
        LEFT JOIN focus_evidence e ON e.topic_id = t.id
        ORDER BY t.id, e.id
        """
    ):
        entry = topics.setdefault(
            row["id"],
            {
                "name": row["name"],
                "terms": tuple(json.loads(row["terms"])),
                "status": row["status"],
                "subjects": {},
                "counts": {},
                "last": None,
            },
        )
        if row["signal_kind"] is None:
            continue
        weight = kind_weight(config, row["signal_kind"]) * _decay(config, row["observed_at"], now)
        entry["subjects"][row["subject"]] = max(entry["subjects"].get(row["subject"], 0.0), weight)
        entry["counts"][row["signal_kind"]] = entry["counts"].get(row["signal_kind"], 0) + 1
        if entry["last"] is None or row["observed_at"] > entry["last"]:
            entry["last"] = row["observed_at"]
    scores = [
        TopicScore(
            topic_id=topic_id,
            name=e["name"],
            terms=e["terms"],
            status=e["status"],
            score=sum(e["subjects"].values()),
            counts=e["counts"],
            subjects=len(e["subjects"]),
            last_observed=e["last"],
        )
        for topic_id, e in topics.items()
    ]
    return sorted(scores, key=lambda s: (-s.score, s.topic_id))


def prompt_topics(conn: sqlite3.Connection, config: Config, now: datetime) -> list[TopicScore]:
    """LLM に渡す前回までの話題。重点と、最後の証拠が新しい話題(外した話題も含む)。

    外した話題も渡すのは、同じ対象のシグナルを別の名前の新しい話題にさせないため。
    """
    horizon = _iso(now - timedelta(days=config.focus.window_days * PROMPT_TOPIC_WINDOW_FACTOR))
    recent = [
        s
        for s in topic_scores(conn, config, now)
        if s.status == "active" or (s.last_observed is not None and s.last_observed >= horizon)
    ]
    return recent[:MAX_PROMPT_TOPICS]


def _one_line(value: str) -> str:
    return " ".join(_CONTROL.sub(" ", value).split())


def build_prompt(
    config: Config, topics: list[TopicScore], signals: list[FocusSignal], nonce: str
) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    # いつも集めている範囲は、ユーザーが書いた config の検索の語(trusted)。囲みの外に置く
    baseline = "\n".join(f"- {_one_line(slot)}" for slot in config.x_search.slots) or "(なし)"
    topic_lines = (
        "\n".join(
            f"[t{t.topic_id}] {sanitize(t.name, MAX_NAME, nonce)}"
            + (
                " | 検索の語: " + ", ".join(sanitize(term, MAX_TERM, nonce) for term in t.terms)
                if t.terms
                else ""
            )
            for t in topics
        )
        or "(まだ無い)"
    )
    signal_lines = "\n".join(
        f"[{s.key}] ({KIND_LABELS[s.kind]}・{s.observed_at[:10]}) "
        + sanitize(s.text, MAX_SIGNAL_TEXT, nonce)
        for s in signals
    )
    mapping = {
        "baseline": baseline,
        "topics": topic_lines,
        "signals": signal_lines,
        "schema": json.dumps(FocusAssignments.model_json_schema(), ensure_ascii=False),
        "nonce": nonce,
    }
    return fill_template(template, mapping)


def clean_name(value: str) -> str:
    return _one_line(value)[:MAX_NAME].strip()


def clean_terms(values: list[str]) -> list[str]:
    """検索の語を 1 行にし、演算子と引用を外す(最大 MAX_TERMS 個。長すぎる語は捨てる)。"""
    terms: list[str] = []
    seen: set[str] = set()
    for value in values:
        words = _TERM_STRIP.sub(" ", _one_line(value)).split()
        words = [w.lstrip("-") for w in words if w not in _TERM_OPERATORS]
        term = " ".join(w for w in words if w)
        if not term or len(term) > MAX_TERM or term.casefold() in seen:
            continue
        seen.add(term.casefold())
        terms.append(term)
        if len(terms) >= MAX_TERMS:
            break
    return terms


def apply_assignments(
    conn: sqlite3.Connection,
    signals: list[FocusSignal],
    drafts: FocusAssignments,
    allowed_topics: set[int],
    *,
    run_id: str | None = None,
) -> tuple[int, int]:
    """LLM の振り分けを台帳に書く。(話題に振ったシグナルの数, 新しい話題の数)を返す。

    材料の id は渡した一覧のものだけを受け、1 つの材料は最初に振られた話題にだけ数える。話題の id は
    prompt に渡した一覧のものだけを受け、それ以外(幻の id・"new")は名前で探して、無ければ作る。
    振られなかった材料は、話題なしとして残す(次の実行で振り分け直さない)。

    書き込みのロックを取ってから、まだ振り分けていない材料だけに絞る。LLM を待つ間に別の実行
    (朝バッチと `intel focus --update`)が同じ材料を振り分けていたら、先の振り分けを残し、こちらでは
    数えず、その材料のための話題も作らない。
    """
    used: set[str] = set()
    assigned = 0
    new_topics = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        pending = [s for s in signals if not _assigned(conn, s)]
        by_key = {s.key: s for s in pending}
        names = {
            row["name"].casefold(): row["id"]
            for row in conn.execute("SELECT id, name FROM focus_topics")
        }
        for draft in drafts.topics:
            keys = [
                key
                for key in dict.fromkeys(k.strip() for k in draft.signals)
                if _SIGNAL_REF.fullmatch(key) and key in by_key and key not in used
            ]
            if not keys:
                continue
            topic_id = _existing_ref(draft.topic, allowed_topics)
            terms = clean_terms(draft.terms)
            if topic_id is None:
                name = clean_name(draft.name)
                if not name:
                    continue
                topic_id = names.get(name.casefold())
                if topic_id is None:
                    topic_id = conn.execute(
                        "INSERT INTO focus_topics (name, terms) VALUES (?, ?)",
                        (name, json.dumps(terms, ensure_ascii=False)),
                    ).lastrowid
                    names[name.casefold()] = topic_id
                    new_topics += 1
            if terms:
                # 検索の語は、まだ無い話題にだけ入れる(日ごとに語が揺れないように)
                conn.execute(
                    "UPDATE focus_topics SET terms = ? WHERE id = ? AND terms = '[]'",
                    (json.dumps(terms, ensure_ascii=False), topic_id),
                )
            for key in keys:
                _insert_evidence(conn, by_key[key], topic_id, run_id)
                used.add(key)
                assigned += 1
        for signal in pending:
            if signal.key not in used:
                _insert_evidence(conn, signal, None, run_id)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return assigned, new_topics


def _assigned(conn: sqlite3.Connection, signal: FocusSignal) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM focus_evidence WHERE signal_kind = ? AND signal_ref = ?",
            (signal.kind, signal.ref),
        ).fetchone()
        is not None
    )


def _existing_ref(ref: str, allowed: set[int]) -> int | None:
    match = _TOPIC_REF.fullmatch(ref.strip())
    if match is None:
        return None
    topic_id = int(match.group(1))
    return topic_id if topic_id in allowed else None


def _insert_evidence(
    conn: sqlite3.Connection, signal: FocusSignal, topic_id: int | None, run_id: str | None
) -> None:
    # 書き込みのロックの中で振り分けていないと確かめた材料だけを書く(UNIQUE の衝突は起きない)
    conn.execute(
        "INSERT INTO focus_evidence "
        "(signal_kind, signal_ref, topic_id, subject, observed_at, run_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (signal.kind, signal.ref, topic_id, signal.subject, signal.observed_at, run_id),
    )


def refresh_status(
    conn: sqlite3.Connection, config: Config, now: datetime
) -> tuple[list[int], list[int]]:
    """重みで重点の出入りを決める。(入った話題の id, 外れた話題の id)を返す。

    重点は exit_score を下回ると外れ、候補は enter_score 以上で入る(重みの高い順に max_active
    まで)。
    外した話題(dismissed)は動かさない。
    """
    focus = config.focus
    stamp = _iso(now)
    activated: list[int] = []
    deactivated: list[int] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        scores = topic_scores(conn, config, now)
        active = [s for s in scores if s.status == "active"]
        keep = [s for s in active if s.score >= focus.exit_score][: focus.max_active]
        keep_ids = {s.topic_id for s in keep}
        for s in active:
            if s.topic_id not in keep_ids:
                deactivated.append(s.topic_id)
        free = focus.max_active - len(keep)
        for s in scores:
            if free <= 0:
                break
            if s.status == "candidate" and s.score >= focus.enter_score:
                activated.append(s.topic_id)
                free -= 1
        for topic_id in deactivated:
            conn.execute(
                "UPDATE focus_topics SET status = 'candidate', deactivated_at = ? WHERE id = ?",
                (stamp, topic_id),
            )
        for topic_id in activated:
            conn.execute(
                "UPDATE focus_topics SET status = 'active', activated_at = ? WHERE id = ?",
                (stamp, topic_id),
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return activated, deactivated


def dismiss_topic(conn: sqlite3.Connection, topic_id: int, now: datetime) -> str:
    """人が話題を外す(GH#433 の 3 本目。ヘッダの「🎯 外す」)。

    呼び出し側のトランザクションの中で使う。外した話題は重みが上がっても重点に戻さない
    (refresh_status が動かさない)。重点だったときは、外れた時刻も残す。返すのは "dismissed"
    (外した)・"already"(外し済み)・"missing"(話題が無い)。
    """
    row = conn.execute("SELECT status FROM focus_topics WHERE id = ?", (topic_id,)).fetchone()
    if row is None:
        return "missing"
    if row["status"] == "dismissed":
        return "already"
    stamp = _iso(now)
    conn.execute(
        "UPDATE focus_topics SET status = 'dismissed', dismissed_at = ?, "
        "deactivated_at = CASE WHEN status = 'active' THEN ? ELSE deactivated_at END "
        "WHERE id = ?",
        (stamp, stamp, topic_id),
    )
    return "dismissed"


def restore_topic(conn: sqlite3.Connection, topic_id: int) -> str:
    """外した話題を戻す(「↩️ 戻す」)。候補に戻し、次の refresh_status から重みで出入りを決める。

    返すのは "restored"(戻した)・"already"(外していない)・"missing"(話題が無い)。
    """
    row = conn.execute("SELECT status FROM focus_topics WHERE id = ?", (topic_id,)).fetchone()
    if row is None:
        return "missing"
    if row["status"] != "dismissed":
        return "already"
    conn.execute(
        "UPDATE focus_topics SET status = 'candidate', dismissed_at = NULL WHERE id = ?",
        (topic_id,),
    )
    return "restored"


def active_focus(
    conn: sqlite3.Connection, config: Config, *, now: datetime | None = None
) -> list[FocusEntry]:
    """今の重点(重みの高い順に FOCUS_MAX_TOPICS 個まで)。digest 段が digests.focus_json に写す。"""
    return [
        FocusEntry(topic_id=s.topic_id, name=s.name, subjects=s.subjects)
        for s in _active_scores(conn, config, now or datetime.now(UTC))
    ]


def search_terms(
    conn: sqlite3.Connection, config: Config, *, now: datetime | None = None
) -> list[str]:
    """今の重点の検索の語のうち、英字を含むもの(GH#433 の 5 本目。HN の検索に送る)。

    話題は active_focus と同じもの・同じ順(重みの高い順)で、話題ごとの語の順に並べる。同じ語は
    1 つにする。語は保存の前に clean_terms を通しているが、外へ送る前にもう一度通す(台帳の値を
    そのまま外へ出さない)。英字の無い語(日本語だけの語)は、英語の投稿の検索に当たらないので送らない。
    """
    terms: list[str] = []
    seen: set[str] = set()
    for score in _active_scores(conn, config, now or datetime.now(UTC)):
        for term in clean_terms([t for t in score.terms if isinstance(t, str)]):
            if _LATIN.search(term) and term.casefold() not in seen:
                seen.add(term.casefold())
                terms.append(term)
    return terms


def _active_scores(conn: sqlite3.Connection, config: Config, now: datetime) -> list[TopicScore]:
    return [s for s in topic_scores(conn, config, now) if s.status == "active"][:FOCUS_MAX_TOPICS]


def focus_topic_refs(conn: sqlite3.Connection, entries: list[FocusEntry]) -> list[FocusTopicRef]:
    """重点に当たる記事を選ぶ prompt に渡す今の重点(GH#433 の 4 本目)。

    digest に出す今の重点(active_focus)と同じ話題に、証拠の例を添える。検索の語は渡さない
    (投稿の一言から作った語が、話題を広く読ませた。11-20 の本番の写しの比較)。
    """
    return [
        FocusTopicRef(topic_id=e.topic_id, name=e.name, examples=topic_examples(conn, e.topic_id))
        for e in entries
    ]


def topic_examples(
    conn: sqlite3.Connection, topic_id: int, limit: int = MATCH_EXAMPLES
) -> tuple[str, ...]:
    """話題の証拠になった投稿・記事の見出し(新しい順に limit 個まで。同じ文は 1 つ)。

    記事・投稿は重みの計算と同じく寄せた先で 1 つに数える。記事は分析の日本語の見出し(degraded を
    除く)か元の見出し、記事の無い投稿は本文の抜粋、調査は出来事の名前。本文の無いものは飛ばす。
    """
    examples: list[str] = []
    for row in conn.execute(
        f"""
        SELECT {_subject_sql("e.subject")} AS subject, max(e.observed_at) AS last
        FROM focus_evidence e WHERE e.topic_id = ?
        GROUP BY 1 ORDER BY last DESC, subject
        """,
        (topic_id,),
    ):
        text = _subject_text(conn, row["subject"])
        if text and text not in examples:
            examples.append(text)
        if len(examples) >= limit:
            break
    return tuple(examples)


def _subject_text(conn: sqlite3.Connection, subject: str) -> str | None:
    kind, _, ref = subject.partition(":")
    if kind == "item" and ref.isdigit():
        row = conn.execute(
            """
            SELECT coalesce(
                     nullif((SELECT a.title_ja FROM analyses a
                             WHERE a.item_id = items.id AND a.degraded = 0
                             ORDER BY a.analyzed_at DESC, a.id DESC LIMIT 1), ''),
                     nullif(items.title, '')) AS text
            FROM items WHERE items.id = ?
            """,
            (int(ref),),
        ).fetchone()
    elif kind == "tweet":
        row = conn.execute(
            "SELECT text_excerpt AS text FROM x_signals WHERE tweet_id = ? "
            "AND coalesce(text_excerpt, '') != '' ORDER BY id LIMIT 1",
            (ref,),
        ).fetchone()
    elif kind == "investigation" and ref.isdigit():
        row = conn.execute(
            "SELECT label AS text FROM news_investigations WHERE id = ?", (int(ref),)
        ).fetchone()
    else:
        return None
    return _one_line(row["text"]) if row is not None and row["text"] else None


def render_focus(conn: sqlite3.Connection, config: Config, *, now: datetime | None = None) -> str:
    """`intel focus` の表示(読み取りだけ)。"""
    now = now or datetime.now(UTC)
    focus = config.focus
    scores = topic_scores(conn, config, now)
    lines = [
        f"今の重点(半減期 {focus.half_life_days:g} 日・{focus.enter_score:g} 以上で入り、"
        f"{focus.exit_score:g} 未満で外れる・最大 {focus.max_active} 個"
        + ("" if focus.enabled else "・[focus].enabled = false で朝バッチは更新しない")
        + ")",
        "",
        "■ 今の重点",
    ]
    active = [s for s in scores if s.status == "active"]
    lines.extend(_topic_lines(active) or ["  (なし)"])
    candidates = [
        s for s in scores if s.status == "candidate" and s.score >= MIN_DISPLAY_SCORE
    ][:CANDIDATE_DISPLAY]
    lines.append("")
    lines.append(f"■ 候補(重みの高い順に {CANDIDATE_DISPLAY} 個まで)")
    lines.extend(_topic_lines(candidates) or ["  (なし)"])
    dismissed = [s for s in scores if s.status == "dismissed"]
    if dismissed:
        lines.append("")
        lines.append("■ 外した話題")
        lines.extend(_topic_lines(dismissed))
    total, unassigned = conn.execute(
        "SELECT count(*), coalesce(sum(topic_id IS NULL), 0) FROM focus_evidence"
    ).fetchone()
    lines.append("")
    lines.append(
        f"振り分けたシグナル {total} 件(どの話題にも当てはまらなかったもの {unassigned} 件)"
    )
    return "\n".join(lines)


def _topic_lines(scores: list[TopicScore]) -> list[str]:
    lines: list[str] = []
    for s in scores:
        counts = " ".join(
            f"{KIND_ICONS[kind]}{n}" for kind, n in sorted(s.counts.items(), key=lambda kv: kv[0])
        )
        last = f"・直近 {s.last_observed[:10]}" if s.last_observed else ""
        sources = f"・記事と投稿 {s.subjects} 件" if s.subjects else ""
        lines.append(
            f"  {s.score:5.1f}  {_one_line(s.name)}({counts or '証拠なし'}{sources}{last})"
        )
        if s.terms:
            lines.append("         検索の語: " + " / ".join(_one_line(t) for t in s.terms))
    return lines
