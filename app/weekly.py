"""週の仮説の問い(GH#440・設計 03 §4.2・04 §4)。

その週の行動から仮説を立て、本人が「そう」「ちがう」で答えられる問いにして、日曜の夜に送る。夜の
アンケートの 2 問(巡回した?・読むのにかかった時間)に代わる問いで、答えは次の週の材料にする。

- 作るのは日曜の朝バッチ(weekly 段)。その日(JST)の問いがあれば作らない(同じ週に 2 回は作らない)
- 材料は、直近 WINDOW_DAYS 日の行動。記事への反応(👍・📌・✅・👎)・感想・X の 🔖・❤️・🔁・読んだ・
  不要・出来事の調査の依頼と、コードで数えた週の集計(先週との比べ、未読の数、反応の時刻、夜の
  アンケートの答え、今の重点、ソース)。前の週までの問いと答え・補足も渡し、「ちがう」と答えた仮説を
  繰り返させない
- 根拠の件数はコードが数える。LLM には材料の id(s1・k1)だけを返させ、一覧に無い id は捨てる。根拠の
  無い仮説も捨てる。巡回についての仮説が無ければ、決まった文の問いを足す(毎晩の質問 1 の代わり)
- 材料の本文・話題の名前・前の週の問いは外部の本文に由来するので、prompt では nonce の囲みの中の
  data として渡す。関心プロファイルは本人が書いた file なので、囲みの外に置く
- 付加機能: LLM が失敗した週は作らず(ログには件数と失敗の種類だけ)、朝バッチは止めない
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, get_args

from pydantic import BaseModel

from app.analysis.llm import LLMEngine, LLMError, complete_with_retry
from app.config import Config
from app.jst import JST
from app.prompting import fill_template, sanitize

PROMPT_VERSION = "weekly-v001"
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / f"{PROMPT_VERSION}.md"

WEEKDAY = 6  # datetime.weekday() の日曜。作る朝と送る夜
WINDOW_DAYS = 7
MIN_SIGNALS = 5  # 材料がこれより少ない週は作らない(根拠の薄い仮説しか立たない)
MAX_PER_KIND = 40  # 1 種類の材料の上限(新しい側から。X のいいねで材料が埋まらないように)
MAX_SIGNAL_TEXT = 160
MAX_NOTE = 200
MAX_QUESTION_CHARS = 120  # 長すぎる問い(prompt は 100 字以内を指示する)は捨てる
PREVIOUS_WEEKS = 4  # 前の週までの問いを渡す範囲(週)
MAX_PREVIOUS = 24
EXAMPLES = 2  # 送るときに添える根拠の見出しの例の数
EXAMPLE_CHARS = 40

Kind = Literal["interest_shift", "fatigue", "habit", "source", "focus", "patrol", "other"]
KINDS: tuple[str, ...] = get_args(Kind)
# 巡回についての仮説が無い週に足す問い(毎晩の質問 1「自分から巡回した?」の代わり)
PATROL_FALLBACK = "今週も、ダイジェストのほかに、自分から情報を探しに巡回した日が多かった?"

ANSWER_CODES = {"y": "yes", "n": "no", "m": "unsure"}
ANSWER_LABELS = {"yes": "⭕ そう", "no": "❌ ちがう", "unsure": "🤷 どちらとも"}

# 材料の種類と、送るときの根拠の印(今の重点の表示と同じ印。Telegram の 🔖 は 📌 で X の 🔖 と分ける)
SIGNAL_LABELS = {
    "tg_useful": ("👍", "記事への 👍 価値あり"),
    "tg_dig": ("📌", "記事への 🔖 深掘りしたい"),
    "tg_known": ("✅", "記事への ✅ 既知"),
    "tg_noise": ("👎", "記事への 👎 不要"),
    "note": ("📝", "記事への感想"),
    "read": ("📖", "読んだにした記事"),
    "dismissed": ("🚫", "不要にした記事"),
    "x_bookmark": ("🔖", "X のブックマーク"),
    "x_like": ("❤️", "X のいいね"),
    "x_self_rt": ("🔁", "X のリポスト"),
    "investigation": ("🔎", "出来事の調査の依頼"),
}
_REACTION_KINDS = {"useful": "tg_useful", "dig": "tg_dig", "known": "tg_known", "noise": "tg_noise"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class HypothesisDraft(BaseModel):
    kind: Kind
    question: str
    evidence: list[str]  # 材料の id("s3")と集計の id("k1")


class WeeklyDraft(BaseModel):
    hypotheses: list[HypothesisDraft]


@dataclass(frozen=True)
class Material:
    key: str  # prompt の中の id(材料は s1…、集計は k1…)
    kind: str  # SIGNAL_LABELS の種類か "stat"
    text: str  # prompt に入れる本文(材料は外部由来。集計はコードの文だが、話題の名前を含みうる)
    title: str | None = None  # 送るときの例に使う見出し(外部由来)
    at: str = ""  # 材料の時刻(UTC の 'YYYY-MM-DDTHH:MM:SSZ')


@dataclass(frozen=True)
class Question:
    position: int
    kind: str
    question: str
    evidence: list[dict] = field(default_factory=list)  # [{"kind": ..., "title": ...}]


@dataclass
class WeeklyRunResult:
    week_date: str
    skipped: str | None = None  # 作らなかった理由(not_sunday・exists・few_signals)
    signals: int = 0
    questions: int = 0
    dropped: int = 0  # 捨てた仮説(根拠が無い・長すぎる・重複・上限超え)
    llm_failed: bool = False


def run_weekly(
    conn: sqlite3.Connection,
    config: Config,
    engine: LLMEngine,
    logger: logging.Logger,
    *,
    run_id: str | None = None,
    now: datetime | None = None,
    store: bool = True,
) -> tuple[WeeklyRunResult, list[Question]]:
    """日曜なら、その週の仮説を作って台帳に残す。store=False は作るだけ(intel weekly --preview)。"""
    now = now or datetime.now(UTC)
    week_date = now.astimezone(JST).strftime("%Y-%m-%d")
    result = WeeklyRunResult(week_date=week_date)
    extra = {"run_id": run_id, "stage": "weekly"}
    if store and now.astimezone(JST).weekday() != WEEKDAY:
        result.skipped = "not_sunday"
        return result, []
    if store and has_questions(conn, week_date):
        result.skipped = "exists"
        logger.info("今週の仮説は作り済み", extra=extra)
        return result, []
    signals = collect_signals(conn, now)
    result.signals = len(signals)
    if len(signals) < MIN_SIGNALS:
        result.skipped = "few_signals"
        logger.info(
            "材料が少ないので今週の仮説は作らない",
            extra={**extra, "extra": {"signals": len(signals)}},
        )
        return result, []
    materials = signals + collect_stats(conn, config, now)
    nonce = secrets.token_hex(4)
    prompt = build_prompt(config, materials, previous_questions(conn, week_date), nonce)
    try:
        draft = complete_with_retry(engine, prompt, WeeklyDraft)
    except LLMError as e:
        result.llm_failed = True
        logger.warning(f"今週の仮説を作れなかった: {type(e).__name__}", extra=extra)
        return result, []
    questions, result.dropped = select_questions(draft, materials, config.weekly.max_questions)
    result.questions = len(questions)
    if store:
        save_questions(conn, week_date, questions, run_id=run_id)
    logger.info(
        "今週の仮説を作った",
        extra={
            **extra,
            "extra": {
                "signals": result.signals,
                "questions": result.questions,
                "dropped": result.dropped,
            },
        },
    )
    return result, questions


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _one_line(value: str) -> str:
    return " ".join(_CONTROL.sub(" ", value).split())


_LATEST_TITLE = (
    "coalesce((SELECT a.title_ja FROM analyses a WHERE a.item_id = {item} "
    "ORDER BY a.analyzed_at DESC, a.id DESC LIMIT 1), "
    "(SELECT title FROM items WHERE id = {item}))"
)


def collect_signals(conn: sqlite3.Connection, now: datetime) -> list[Material]:
    """直近 WINDOW_DAYS 日の行動(新しい側から、種類ごとに MAX_PER_KIND 件まで)。

    記事への反応は、記事ごとにその週の最新の 1 つ(押し直しは最新を採る)。読んだは、その週に
    読んだにして、週の終わり(now)の時点でも読んだのままの記事。最新は週の終わりより前の行から
    選ぶ(集める間に受信デーモンが now の後の行を書いても、週の材料を変えない)。本文の無い材料
    (X の本文の保存期間が過ぎたなど)は渡さない。
    """
    start, end = _iso(now - timedelta(days=WINDOW_DAYS)), _iso(now)
    found: list[tuple[str, str, str, str | None]] = []  # (kind, at, text, title)
    for row in conn.execute(
        f"""
        SELECT s.item_id, s.value, s.created_at, {_LATEST_TITLE.format(item="s.item_id")} AS title,
               src.name AS source_name,
               (SELECT a.category FROM analyses a WHERE a.item_id = s.item_id
                ORDER BY a.analyzed_at DESC, a.id DESC LIMIT 1) AS category
        FROM signals s
        JOIN items i ON i.id = s.item_id
        JOIN sources src ON src.id = i.source_id
        WHERE s.kind = 'reaction' AND s.created_at >= ? AND s.created_at < ?
          AND s.id = (SELECT id FROM signals s2 WHERE s2.kind = 'reaction'
                      AND s2.item_id = s.item_id AND s2.created_at < ?
                      ORDER BY s2.created_at DESC, s2.id DESC LIMIT 1)
        """,
        (start, end, end),
    ):
        if row["title"]:
            text = f"{row['title']}(ソース: {row['source_name']} / {row['category'] or '分類なし'})"
            found.append((_REACTION_KINDS[row["value"]], row["created_at"], text, row["title"]))
    for row in conn.execute(
        f"""
        SELECT n.note, n.created_at, {_LATEST_TITLE.format(item="n.item_id")} AS title
        FROM item_notes n WHERE n.created_at >= ? AND n.created_at < ?
        """,
        (start, end),
    ):
        note = row["note"][:MAX_NOTE]
        text = f"記事: {row['title']} / 感想: {note}" if row["title"] else f"感想: {note}"
        found.append(("note", row["created_at"], text, row["title"]))
    for row in conn.execute(
        f"""
        SELECT x.kind, x.observed_at, x.text_excerpt,
               CASE WHEN x.item_id IS NOT NULL THEN {_LATEST_TITLE.format(item="x.item_id")} END
                 AS title
        FROM x_signals x WHERE x.observed_at >= ? AND x.observed_at < ?
        """,
        (start, end),
    ):
        text = row["text_excerpt"] or row["title"]
        if text:
            found.append((f"x_{row['kind']}", row["observed_at"], text, row["title"] or text))
    for row in conn.execute(
        f"""
        SELECT f.created_at, {_LATEST_TITLE.format(item="f.item_id")} AS title
        FROM signals f
        WHERE f.kind = 'followup_dig' AND f.value = 'yes' AND f.created_at >= ? AND f.created_at < ?
          AND f.id = (SELECT id FROM signals f2 WHERE f2.kind = 'followup_dig'
                      AND f2.item_id = f.item_id AND f2.created_at < ?
                      ORDER BY f2.created_at DESC, f2.id DESC LIMIT 1)
        """,
        (start, end, end),
    ):
        if row["title"]:
            found.append(("read", row["created_at"], row["title"], row["title"]))
    for row in conn.execute(
        f"""
        SELECT d.dismissed_at, {_LATEST_TITLE.format(item="d.item_id")} AS title
        FROM dig_dismissals d WHERE d.dismissed_at >= ? AND d.dismissed_at < ?
        """,
        (start, end),
    ):
        if row["title"]:
            found.append(("dismissed", row["dismissed_at"], row["title"], row["title"]))
    for row in conn.execute(
        "SELECT label, summary, requested_at FROM news_investigations "
        "WHERE requested_at >= ? AND requested_at < ?",
        (start, end),
    ):
        found.append((
            "investigation", row["requested_at"], f"{row['label']}: {row['summary']}", row["label"]
        ))
    found.sort(key=lambda s: (s[1], s[0]), reverse=True)  # 新しい側から
    per_kind: Counter[str] = Counter()
    kept = []
    for kind, at, text, title in found:
        if per_kind[kind] >= MAX_PER_KIND:
            continue
        per_kind[kind] += 1
        kept.append((kind, at, text, title))
    return [
        Material(key=f"s{index}", kind=kind, text=text, title=title, at=at)
        for index, (kind, at, text, title) in enumerate(kept, start=1)
    ]


def collect_stats(conn: sqlite3.Connection, config: Config, now: datetime) -> list[Material]:
    """コードで数えた週の集計(k1…)。仮説の数字はここから取らせる(LLM に数えさせない)。"""
    this_start, end = _iso(now - timedelta(days=WINDOW_DAYS)), _iso(now)
    last_start = _iso(now - timedelta(days=2 * WINDOW_DAYS))
    lines: list[str] = []

    def reactions(start: str, stop: str) -> Counter[str]:
        return Counter(
            row["value"]
            for row in conn.execute(
                """
                SELECT s.value FROM signals s
                WHERE s.kind = 'reaction' AND s.created_at >= ? AND s.created_at < ?
                  AND s.id = (SELECT id FROM signals s2 WHERE s2.kind = 'reaction'
                              AND s2.item_id = s.item_id AND s2.created_at < ?
                              ORDER BY s2.created_at DESC, s2.id DESC LIMIT 1)
                """,
                (start, stop, stop),
            )
        )

    def reaction_text(counts: Counter[str]) -> str:
        return "・".join(
            f"{SIGNAL_LABELS[_REACTION_KINDS[value]][0]} {counts[value]}"
            for value in ("useful", "dig", "known", "noise")
        )

    # 配信した記事 = 記事のメッセージを送れた記事(台帳 digest_messages の記事の行。--force の置換で
    # 外れた前の世代も、送れていれば数える)。digest を作っただけで送れなかった記事は数えない
    delivered = conn.execute(
        "SELECT count(DISTINCT item_id) FROM digest_messages "
        "WHERE item_id IS NOT NULL AND sent_at >= ? AND sent_at < ?",
        (this_start, end),
    ).fetchone()[0]
    lines.append(
        f"配信した記事 {delivered} 件への反応(記事ごとの最新): 今週 "
        f"{reaction_text(reactions(this_start, end))}(先週 "
        f"{reaction_text(reactions(last_start, this_start))})"
    )

    def count(sql: str, start: str, stop: str) -> int:
        return conn.execute(sql, (start, stop)).fetchone()[0]

    notes_sql = "SELECT count(*) FROM item_notes WHERE created_at >= ? AND created_at < ?"
    lines.append(
        f"記事への感想のリプライ: 今週 {count(notes_sql, this_start, end)} 件"
        f"(先週 {count(notes_sql, last_start, this_start)} 件)"
    )

    def x_counts(start: str, stop: str) -> str:
        counts = Counter(
            row["kind"]
            for row in conn.execute(
                "SELECT kind FROM x_signals WHERE observed_at >= ? AND observed_at < ?",
                (start, stop),
            )
        )
        return "・".join(
            f"{SIGNAL_LABELS['x_' + kind][0]} {counts[kind]}"
            for kind in ("bookmark", "like", "self_rt")
        )

    lines.append(f"X: 今週 {x_counts(this_start, end)}(先週 {x_counts(last_start, this_start)})")

    from app.delivery.surveys import pending_dig_items  # 夜の一覧と同じ条件で数える

    marked = count(
        "SELECT count(DISTINCT item_id) FROM ("
        "  SELECT item_id, created_at FROM signals WHERE kind = 'reaction' AND value = 'dig'"
        "  UNION ALL SELECT item_id, observed_at FROM x_signals"
        "  WHERE kind = 'bookmark' AND item_id IS NOT NULL"
        ") WHERE created_at >= ? AND created_at < ?",
        this_start,
        end,
    )
    consumed = count(
        "SELECT count(DISTINCT item_id) FROM ("
        "  SELECT item_id, created_at FROM signals WHERE kind = 'followup_dig' AND value = 'yes'"
        "  UNION ALL SELECT item_id, dismissed_at FROM dig_dismissals"
        ") WHERE created_at >= ? AND created_at < ?",
        this_start,
        end,
    )
    lines.append(
        f"夜の一覧の未読の記事は今 {len(pending_dig_items(conn))} 件。今週 📌 か 🔖 を付けた記事 "
        f"{marked} 件、読んだ・不要にした記事 {consumed} 件"
    )

    hours: Counter[str] = Counter()
    for row in conn.execute(
        "SELECT created_at FROM signals WHERE kind = 'reaction' AND created_at >= ? "
        "AND created_at < ? UNION ALL SELECT created_at FROM item_notes "
        "WHERE created_at >= ? AND created_at < ?",
        (this_start, end, this_start, end),
    ):
        hour = (
            datetime.strptime(row["created_at"], "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=UTC)
            .astimezone(JST)
            .hour
        )
        hours["朝(5〜10 時)" if 5 <= hour < 10 else "昼(10〜17 時)" if 10 <= hour < 17
              else "夜(17〜24 時)" if hour >= 17 else "深夜(0〜5 時)"] += 1
    if hours:
        lines.append(
            "反応と感想を書いた時刻(JST): "
            + "・".join(f"{name} {hours[name]}" for name in
                       ("朝(5〜10 時)", "昼(10〜17 時)", "夜(17〜24 時)", "深夜(0〜5 時)"))
        )

    days = [
        (now - timedelta(days=offset)).astimezone(JST).strftime("%Y-%m-%d")
        for offset in range(1, WINDOW_DAYS + 1)
    ]
    answers = dict(
        conn.execute(
            f"""
            SELECT s.signal_date, s.value FROM signals s
            WHERE s.kind = 'survey_patrol' AND s.signal_date IN ({",".join("?" * len(days))})
              AND s.id = (SELECT id FROM signals s2 WHERE s2.kind = 'survey_patrol'
                          AND s2.signal_date = s.signal_date ORDER BY s2.created_at DESC, s2.id DESC
                          LIMIT 1)
            """,
            days,
        ).fetchall()
    )
    yes = sum(1 for value in answers.values() if value == "yes")
    no = sum(1 for value in answers.values() if value == "no")
    lines.append(
        f"夜の質問「今日、自分から情報を探しに巡回した?」の答え(直近 {WINDOW_DAYS} 日): "
        f"はい {yes} 日・いいえ {no} 日・未回答 {WINDOW_DAYS - yes - no} 日"
    )

    if config.focus.enabled:
        topics = conn.execute(
            "SELECT name, status, activated_at, deactivated_at, dismissed_at FROM focus_topics "
            "WHERE status = 'active' OR activated_at >= ? OR deactivated_at >= ? "
            "OR dismissed_at >= ? ORDER BY id",
            (this_start, this_start, this_start),
        ).fetchall()
        active = [_one_line(t["name"]) for t in topics if t["status"] == "active"]
        entered = [_one_line(t["name"]) for t in topics if (t["activated_at"] or "") >= this_start]
        left = [
            _one_line(t["name"]) for t in topics
            if (t["deactivated_at"] or "") >= this_start and t["status"] != "active"
        ]
        removed = [_one_line(t["name"]) for t in topics if (t["dismissed_at"] or "") >= this_start]
        def names(values: list[str]) -> str:
            return "・".join(values) or "なし"

        lines.append(
            f"今の重点: {names(active)}(今週入った: {names(entered)} / 外れた: {names(left)} / "
            f"🎯 外すを押した: {names(removed)})"
        )

    by_source = conn.execute(
        """
        SELECT src.name AS source_name,
               sum(s.value IN ('useful', 'dig')) AS good, sum(s.value = 'noise') AS bad
        FROM signals s
        JOIN items i ON i.id = s.item_id
        JOIN sources src ON src.id = i.source_id
        WHERE s.kind = 'reaction' AND s.created_at >= ? AND s.created_at < ?
          AND s.id = (SELECT id FROM signals s2 WHERE s2.kind = 'reaction'
                      AND s2.item_id = s.item_id AND s2.created_at < ?
                      ORDER BY s2.created_at DESC, s2.id DESC LIMIT 1)
        GROUP BY src.name
        """,
        (this_start, end, end),
    ).fetchall()
    good = sorted((r for r in by_source if r["good"]), key=lambda r: (-r["good"], r["source_name"]))
    bad = sorted((r for r in by_source if r["bad"]), key=lambda r: (-r["bad"], r["source_name"]))
    lines.append(
        "👍・📌 の多いソース: "
        + ("・".join(f"{r['source_name']} {r['good']}" for r in good[:5]) or "なし")
        + " / 👎 の多いソース: "
        + ("・".join(f"{r['source_name']} {r['bad']}" for r in bad[:5]) or "なし")
    )
    return [
        Material(key=f"k{index}", kind="stat", text=line)
        for index, line in enumerate(lines, start=1)
    ]


def previous_questions(conn: sqlite3.Connection, week_date: str) -> list[str]:
    """前の週まで(PREVIOUS_WEEKS 週)の問いと、最新の答え・補足(新しい週から MAX_PREVIOUS 件)。"""
    since = (datetime.strptime(week_date, "%Y-%m-%d") - timedelta(weeks=PREVIOUS_WEEKS)).strftime(
        "%Y-%m-%d"
    )
    rows = conn.execute(
        """
        SELECT q.id, q.week_date, q.question,
               (SELECT a.value FROM weekly_answers a WHERE a.question_id = q.id
                ORDER BY a.created_at DESC, a.id DESC LIMIT 1) AS answer
        FROM weekly_questions q
        WHERE q.week_date < ? AND q.week_date >= ?
        ORDER BY q.week_date DESC, q.position
        LIMIT ?
        """,
        (week_date, since, MAX_PREVIOUS),
    ).fetchall()
    lines = []
    for row in rows:
        notes = [
            _one_line(note["note"])[:MAX_NOTE]
            for note in conn.execute(
                "SELECT note FROM weekly_question_notes WHERE question_id = ? ORDER BY id",
                (row["id"],),
            )
        ]
        answer = ANSWER_LABELS.get(row["answer"] or "", "未回答")
        line = f"{row['week_date']} [{answer}] {row['question']}"
        if notes:
            line += " / 補足: " + " / ".join(notes)
        lines.append(line)
    return lines


def build_prompt(
    config: Config, materials: list[Material], previous: list[str], nonce: str
) -> str:
    from app.analysis.profile import ProfileError, load_interest_profile

    template = PROMPT_PATH.read_text(encoding="utf-8")
    try:
        # 本人が書いた関心プロファイル(trusted)。囲みの外に置く。無ければ無しで作る
        profile = load_interest_profile(config.profile_path).body.strip() or "(なし)"
    except ProfileError:
        profile = "(なし)"
    signal_lines = "\n".join(
        f"[{m.key}] ({SIGNAL_LABELS[m.kind][1]}・{m.at[:10]}) "
        + sanitize(m.text, MAX_SIGNAL_TEXT, nonce)
        for m in materials
        if m.kind != "stat"
    )
    stat_lines = "\n".join(
        f"[{m.key}] {sanitize(m.text, 400, nonce)}" for m in materials if m.kind == "stat"
    )
    previous_lines = "\n".join(sanitize(line, 400, nonce) for line in previous) or "(まだ無い)"
    mapping = {
        "profile": profile,
        "stats": stat_lines,
        "signals": signal_lines,
        "previous": previous_lines,
        "max_questions": str(config.weekly.max_questions),
        "schema": json.dumps(WeeklyDraft.model_json_schema(), ensure_ascii=False),
        "nonce": nonce,
    }
    return fill_template(template, mapping)


def select_questions(
    draft: WeeklyDraft, materials: list[Material], max_questions: int
) -> tuple[list[Question], int]:
    """LLM の仮説から送る問いを選ぶ(選んだ問い, 捨てた数)。

    根拠の id は材料と集計の一覧にあるものだけを残し、根拠の無い仮説・長すぎる問い・同じ問いは
    捨てる。巡回についての仮説は 1 つだけにし、無ければ PATROL_FALLBACK を最後に足す(上限の中で。
    上限に達していれば最後の仮説と入れ替える)。
    """
    by_key = {m.key: m for m in materials}
    selected: list[tuple[str, str, list[dict]]] = []
    seen: set[str] = set()
    dropped = 0
    has_patrol = False
    for hypothesis in draft.hypotheses:
        question = _one_line(hypothesis.question)
        keys = [key for key in dict.fromkeys(hypothesis.evidence) if key in by_key]
        if (
            not question
            or len(question) > MAX_QUESTION_CHARS
            or not keys
            or question in seen
            or (hypothesis.kind == "patrol" and has_patrol)
            or len(selected) >= max_questions
        ):
            dropped += 1
            continue
        seen.add(question)
        has_patrol = has_patrol or hypothesis.kind == "patrol"
        evidence = [
            {"kind": by_key[key].kind, "title": by_key[key].title}
            if by_key[key].kind != "stat"
            else {"kind": "stat"}
            for key in keys
        ]
        selected.append((hypothesis.kind, question, evidence))
    if not has_patrol:
        if len(selected) >= max_questions:
            selected.pop()
            dropped += 1
        selected.append(("patrol", PATROL_FALLBACK, []))
    questions = [
        Question(position=position, kind=kind, question=question, evidence=evidence)
        for position, (kind, question, evidence) in enumerate(selected, start=1)
    ]
    return questions, dropped


def has_questions(conn: sqlite3.Connection, week_date: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM weekly_questions WHERE week_date = ? LIMIT 1", (week_date,)
        ).fetchone()
        is not None
    )


def save_questions(
    conn: sqlite3.Connection, week_date: str, questions: list[Question], *, run_id: str | None
) -> None:
    """その週の問いを 1 つのトランザクションで残す。同じ週の行が先にあれば何もしない
    (重なった実行)。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not has_questions(conn, week_date):
            conn.executemany(
                "INSERT INTO weekly_questions (week_date, position, kind, question, "
                "evidence_json, run_id, prompt_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        week_date, q.position, q.kind, q.question,
                        json.dumps(q.evidence, ensure_ascii=False), run_id, PROMPT_VERSION,
                    )
                    for q in questions
                ],
            )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def evidence_line(evidence_json: str) -> str | None:
    """送るときの根拠の 1 行(`根拠: 📌 3・👍 2(例: 「…」「…」)`)。根拠が無ければ None。

    件数は台帳に写した根拠の種類から数える。集計だけが根拠なら「週の集計」と出す。
    """
    try:
        evidence = json.loads(evidence_json)
    except ValueError:
        return None
    if not isinstance(evidence, list):
        return None
    kinds = Counter(
        e["kind"] for e in evidence if isinstance(e, dict) and e.get("kind") in SIGNAL_LABELS
    )
    has_stat = any(isinstance(e, dict) and e.get("kind") == "stat" for e in evidence)
    parts = [f"{SIGNAL_LABELS[kind][0]} {n}" for kind, n in kinds.most_common()]
    if has_stat:
        parts.append("週の集計")
    if not parts:
        return None
    titles: list[str] = []
    seen_titles: set[str] = set()
    for e in evidence:
        title = e.get("title") if isinstance(e, dict) else None
        # 同じ記事を感想と 🔖 の両方で根拠にしたときは、例を 1 つにする
        if (
            isinstance(title, str)
            and _one_line(title)
            and len(titles) < EXAMPLES
            and _one_line(title) not in seen_titles
        ):
            short = _one_line(title)
            seen_titles.add(short)
            if len(short) > EXAMPLE_CHARS:
                short = short[: EXAMPLE_CHARS - 1] + "…"
            titles.append(short)
    line = "根拠: " + "・".join(parts)
    if titles:
        line += "(例: " + "".join(f"「{t}」" for t in titles) + ")"
    return line


def latest_answer(conn: sqlite3.Connection, question_id: int) -> str | None:
    row = conn.execute(
        "SELECT value FROM weekly_answers WHERE question_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (question_id,),
    ).fetchone()
    return row["value"] if row else None


def render_week(conn: sqlite3.Connection, week_date: str | None = None) -> str:
    """intel weekly の表示: 最新の週(または week_date の週)の問いと答え・補足。"""
    if week_date is None:
        row = conn.execute("SELECT max(week_date) AS week_date FROM weekly_questions").fetchone()
        week_date = row["week_date"] if row else None
    if not week_date:
        return "週の仮説はまだありません"
    lines = [f"週の仮説({week_date})"]
    for q in conn.execute(
        "SELECT * FROM weekly_questions WHERE week_date = ? ORDER BY position", (week_date,)
    ):
        answer = ANSWER_LABELS.get(latest_answer(conn, q["id"]) or "", "未回答")
        sent = "送信済み" if q["message_id"] is not None else "未送信"
        lines.append(f"{q['position']}. [{q['kind']}] {_one_line(q['question'])}({answer}・{sent})")
        evidence = evidence_line(q["evidence_json"])
        if evidence:
            lines.append(f"   {evidence}")
        for note in conn.execute(
            "SELECT note FROM weekly_question_notes WHERE question_id = ? ORDER BY id", (q["id"],)
        ):
            lines.append(f"   補足: {_one_line(note['note'])}")
    return "\n".join(lines)


def render_preview(questions: list[Question]) -> str:
    """intel weekly --preview の表示(台帳に残さずに作った問い)。"""
    if not questions:
        return "仮説を作れませんでした"
    lines = []
    for q in questions:
        lines.append(f"{q.position}. [{q.kind}] {q.question}")
        evidence = evidence_line(json.dumps(q.evidence, ensure_ascii=False))
        if evidence:
            lines.append(f"   {evidence}")
    return "\n".join(lines)
