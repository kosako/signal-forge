import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.analysis.llm import LLMError
from app.config import Config, FocusConfig, XSearchConfig
from app.db import apply_migrations, connect
from app.focus import (
    FocusAssignments,
    FocusTopicDraft,
    build_prompt,
    clean_terms,
    collect_signals,
    prompt_topics,
    render_focus,
    run_focus,
    topic_scores,
)

NOW = datetime(2026, 11, 20, 0, 0, 0, tzinfo=UTC)
logger = logging.getLogger("test-focus")


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    yield conn
    conn.close()


def make_config(tmp_path: Path, **focus) -> Config:
    return Config(config_dir=tmp_path, focus=FocusConfig(enabled=True, **focus))


def iso(days_ago: float = 0, *, now: datetime = NOW) -> str:
    return (now - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_x_signal(conn, kind, tweet_id, *, text=None, days_ago=0.0, item_id=None) -> int:
    return conn.execute(
        "INSERT INTO x_signals (kind, tweet_id, text_excerpt, observed_at, item_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (kind, tweet_id, text, iso(days_ago), item_id),
    ).lastrowid


def add_item(conn, key: str, *, title_ja: str | None = None, topic: str = "t") -> int:
    item_id = conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, title, content_hash) "
        "VALUES (1, ?, ?, ?, ?)",
        (f"https://e/{key}", f"https://e/{key}", f"title {key}", f"sha256:{key}"),
    ).lastrowid
    if title_ja is not None:
        conn.execute(
            "INSERT INTO analyses (item_id, summary, category, topic, relevance_score, "
            "novelty, negative_labels, recommendation_reason, implication, degraded, "
            "engine, model, prompt_version, input_hash, title_ja) "
            "VALUES (?, 's', 'その他', ?, 50, 'new', '[]', 'r', 'i', 0, "
            "'codex-cli', 'm', 'v', 'h', ?)",
            (item_id, topic, title_ja),
        )
    return item_id


class FakeEngine:
    name = "fake"
    model = "fake"

    def __init__(self, respond: Callable[[str], FocusAssignments] | None = None) -> None:
        self.respond = respond
        self.prompts: list[str] = []

    def complete_structured(self, prompt, schema, timeout_s=0):
        self.prompts.append(prompt)
        if self.respond is None:
            raise AssertionError("新しいシグナルが無いので LLM を呼ばない")
        return self.respond(prompt)


class FailingEngine(FakeEngine):
    def complete_structured(self, prompt, schema, timeout_s=0):
        self.prompts.append(prompt)
        raise LLMError("boom")


def drafts(*topics: tuple[str, str, list[str], list[str]]) -> FocusAssignments:
    return FocusAssignments(
        topics=[
            FocusTopicDraft(topic=topic, name=name, terms=terms, signals=signals)
            for topic, name, terms, signals in topics
        ]
    )


def scores_by_name(conn, config, now=NOW) -> dict[str, tuple[float, str]]:
    return {s.name: (s.score, s.status) for s in topic_scores(conn, config, now)}


def test_collect_signals_reads_each_kind_newest_first_within_window(conn, tmp_path) -> None:
    config = make_config(tmp_path, window_days=7)
    bookmarked = add_item(conn, "b", title_ja="ブックマークした記事")
    add_x_signal(conn, "like", "l1", text="いいねした投稿", days_ago=1)
    add_x_signal(conn, "bookmark", "b1", days_ago=2, item_id=bookmarked)  # 本文は分析の見出し
    add_x_signal(conn, "self_rt", "r1", days_ago=3)  # 本文も分析も無い → 渡さない
    add_x_signal(conn, "like", "old", text="古い", days_ago=8)  # window の外
    reacted = add_item(conn, "r", title_ja="反応した記事", topic="MCP")
    for value, days_ago in (("useful", 0.5), ("dig", 1.5), ("known", 0.2), ("noise", 0.3)):
        conn.execute(
            "INSERT INTO signals (kind, item_id, value, signal_date, created_at) "
            "VALUES ('reaction', ?, ?, '2026-11-19', ?)",
            (reacted, value, iso(days_ago)),
        )
    conn.execute(
        "INSERT INTO item_notes (item_id, note, created_at) VALUES (?, '試してみたい', ?)",
        (reacted, iso(4)),
    )
    digest_id = conn.execute(
        "INSERT INTO digests (digest_date, markdown, item_ids, item_count) "
        "VALUES ('2026-11-19', '', '[]', 0)"
    ).lastrowid
    conn.execute(
        "INSERT INTO news_investigations (digest_id, event_index, label, summary, item_ids, "
        "requested_at) VALUES (?, 1, '出来事の見出し', '要約', '[]', ?)",
        (digest_id, iso(5)),
    )

    signals = collect_signals(conn, config, NOW)

    assert [(s.key, s.kind) for s in signals] == [
        ("s1", "tg_useful"),
        ("s2", "x_like"),
        ("s3", "tg_dig"),
        ("s4", "x_bookmark"),
        ("s5", "note"),
        ("s6", "investigation"),
    ]
    texts = {s.kind: s.text for s in signals}
    assert texts["tg_useful"] == "反応した記事(topic: MCP)"
    assert texts["x_bookmark"] == "ブックマークした記事"
    assert texts["note"] == "記事: 反応した記事 / 感想: 試してみたい"
    assert texts["investigation"] == "出来事の見出し: 要約"
    assert len(collect_signals(conn, replace(config, focus=replace(config.focus, max_signals=2)),
                               NOW)) == 2


def test_new_topic_enters_focus_and_fades_after_signals_stop(conn, tmp_path) -> None:
    # ❤️1 + 🔁3 + 🔖3 = 7 ≥ 6 で入り、半減期 7 日で 3 を下回る(約 8.6 日後)と外れる
    config = make_config(tmp_path)
    add_x_signal(conn, "like", "1", text="software factory の話")
    add_x_signal(conn, "self_rt", "2", text="ソフトウェアファクトリーを作った")
    add_x_signal(conn, "bookmark", "3", text="software factories")
    engine = FakeEngine(lambda _p: drafts(
        ("new", "ソフトウェアファクトリー", ['"software factory" OR', "ソフトウェアファクトリー"],
         ["s1", "s2", "s3"]),
    ))

    result = run_focus(conn, config, engine, logger, run_id="R1", now=NOW)

    assert (result.signals, result.assigned, result.new_topics) == (3, 3, 1)
    assert scores_by_name(conn, config) == {
        "ソフトウェアファクトリー": (pytest.approx(7.0), "active")
    }
    topic = conn.execute("SELECT id, terms, activated_at FROM focus_topics").fetchone()
    assert json.loads(topic["terms"]) == ["software factory", "ソフトウェアファクトリー"]
    assert topic["activated_at"] == iso(0)
    assert result.activated == [topic["id"]]

    quiet = FakeEngine()  # 新しいシグナルが無い日は LLM を呼ばない
    later = NOW + timedelta(days=8)
    assert run_focus(conn, config, quiet, logger, now=later).deactivated == []
    assert scores_by_name(conn, config, later)["ソフトウェアファクトリー"][1] == "active"
    later = NOW + timedelta(days=9)
    assert run_focus(conn, config, quiet, logger, now=later).deactivated == [topic["id"]]
    assert scores_by_name(conn, config, later)["ソフトウェアファクトリー"][1] == "candidate"
    assert quiet.prompts == []


def test_signals_on_the_same_article_count_once(conn, tmp_path) -> None:
    # 1 本の記事への 👍・🔖・感想と、その記事として配信した投稿への X の 🔖 は、一番強い 1 つだけを
    # 数える(1 本の記事だけの話題を重点にしない)。別の投稿は別に数える
    config = make_config(tmp_path)
    conn.execute("INSERT INTO sources (type, name) VALUES ('x', 'x:home')")
    x_source = conn.execute("SELECT id FROM sources WHERE type = 'x'").fetchone()[0]
    article = add_item(conn, "a", title_ja="ある記事")
    conn.execute("UPDATE items SET source_id = ?, external_id = '77' WHERE id = ?",
                 (x_source, article))
    for value in ("useful", "dig"):
        conn.execute(
            "INSERT INTO signals (kind, item_id, value, signal_date, created_at) "
            "VALUES ('reaction', ?, ?, '2026-11-20', ?)",
            (article, value, iso(0)),
        )
    conn.execute(
        "INSERT INTO item_notes (item_id, note, created_at) VALUES (?, '良い', ?)",
        (article, iso(0)),
    )
    add_x_signal(conn, "bookmark", "77", text="同じ投稿")  # item_id は無いが同じ投稿の記事がある
    add_x_signal(conn, "like", "88", text="別の投稿")
    signals = collect_signals(conn, config, NOW)
    assert {s.subject for s in signals} == {f"item:{article}", "tweet:77", "tweet:88"}

    run_focus(conn, config, FakeEngine(lambda _p: drafts(
        ("new", "話題", [], [s.key for s in signals]),
    )), logger, now=NOW)

    (score,) = topic_scores(conn, config, NOW)
    assert (score.score, score.subjects) == (pytest.approx(4.0), 2)  # 3(記事)+ 1(別の投稿)
    assert score.counts == {"x_bookmark": 1, "x_like": 1, "tg_useful": 1, "tg_dig": 1, "note": 1}


def test_candidate_below_enter_score_waits(conn, tmp_path) -> None:
    # 外れる値以上でも、入る値に届かない候補は重点に入らない(ばたつきを防ぐ)
    config = make_config(tmp_path)
    add_x_signal(conn, "bookmark", "1", text="a")
    add_x_signal(conn, "like", "2", text="b")
    run_focus(conn, config, FakeEngine(lambda _p: drafts(("new", "話題", [], ["s1", "s2"]))),
              logger, now=NOW)
    assert scores_by_name(conn, config) == {"話題": (pytest.approx(4.0), "candidate")}


def test_existing_topic_is_reused_and_untrusted_ids_are_ignored(conn, tmp_path) -> None:
    config = make_config(tmp_path)
    add_x_signal(conn, "like", "1", text="first", days_ago=1)
    run_focus(conn, config, FakeEngine(lambda _p: drafts(("new", "MCP の認可", [], ["s1"]))),
              logger, now=NOW)
    topic_id = conn.execute("SELECT id FROM focus_topics").fetchone()[0]

    for tweet_id in ("2", "3", "4", "5"):
        add_x_signal(conn, "like", tweet_id, text=f"post {tweet_id}")
    engine = FakeEngine(lambda _p: drafts(
        (f"t{topic_id}", "別の名前", [], ["s1", "s1", "s99"]),  # 重複と幻の id は数えない
        ("t999", "mcp の認可", [], ["s2"]),  # 幻の話題の id → 名前の一致で元の話題に戻す
        ("new", "別の話題", [], ["s1", "s3"]),  # s1 は先の話題に振った
        ("new", "   ", [], ["s4"]),  # 名前が空 → 振らない
    ))
    result = run_focus(conn, config, engine, logger, run_id="R2", now=NOW)

    assert f"[t{topic_id}] MCP の認可" in engine.prompts[0]  # 前回までの話題を渡した
    assert (result.signals, result.assigned, result.new_topics) == (4, 3, 1)
    rows = conn.execute(
        "SELECT t.name, count(e.id) FROM focus_topics t LEFT JOIN focus_evidence e "
        "ON e.topic_id = t.id GROUP BY t.id ORDER BY t.id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [("MCP の認可", 3), ("別の話題", 1)]
    unassigned = conn.execute(
        "SELECT count(*) FROM focus_evidence WHERE topic_id IS NULL"
    ).fetchone()[0]
    assert unassigned == 1  # s4 は話題なしとして残し、次の実行で振り分け直さない
    assert collect_signals(conn, config, NOW) == []


def test_llm_failure_keeps_signals_and_still_decays(conn, tmp_path) -> None:
    config = make_config(tmp_path)
    add_x_signal(conn, "self_rt", "1", text="a")
    add_x_signal(conn, "self_rt", "2", text="b")
    run_focus(conn, config, FakeEngine(lambda _p: drafts(("new", "話題", [], ["s1", "s2"]))),
              logger, now=NOW)
    add_x_signal(conn, "like", "3", text="c", days_ago=-8)  # 後の実行の前日のシグナル

    later = NOW + timedelta(days=9)  # 6 × 0.5^(9/7) ≈ 2.5 < 3
    failing = FailingEngine()
    result = run_focus(conn, config, failing, logger, now=later)

    assert result.llm_failed and len(failing.prompts) == 2  # 同じエンジンで 1 回やり直した
    assert scores_by_name(conn, config, later)["話題"][1] == "candidate"
    assert len(collect_signals(conn, config, later)) == 1  # 振り分けなかったシグナルは次の実行へ


def test_max_active_keeps_the_highest_and_dismissed_stays_out(conn, tmp_path) -> None:
    config = make_config(tmp_path, max_active=2, enter_score=3.0, exit_score=1.0)
    for index in range(1, 5):
        add_x_signal(conn, "bookmark", str(index), text=f"post {index}")
    add_x_signal(conn, "bookmark", "5", text="post 5")
    engine = FakeEngine(lambda _p: drafts(
        ("new", "A", [], ["s1", "s2"]),
        ("new", "B", [], ["s3"]),
        ("new", "C", [], ["s4"]),
        ("new", "D", [], ["s5"]),
    ))
    conn.execute(
        "INSERT INTO focus_topics (name, status, dismissed_at) VALUES ('D', 'dismissed', ?)",
        (iso(1),),
    )

    run_focus(conn, config, engine, logger, now=NOW)

    statuses = {name: status for name, (_score, status) in scores_by_name(conn, config).items()}
    # A(6)と、同じ重みの B・C のうち先に作った B が入る。外した D は重みがあっても戻さない
    assert statuses == {"A": "active", "B": "active", "C": "candidate", "D": "dismissed"}


def test_prompt_fences_untrusted_text_and_lists_baseline(conn, tmp_path) -> None:
    config = replace(
        make_config(tmp_path), x_search=XSearchConfig(slots=('"claude code" OR "codex cli"',))
    )
    add_x_signal(conn, "like", "1", text="nonce-ABCD を含む\n改行つきの投稿")
    conn.execute("INSERT INTO focus_topics (name, terms) VALUES ('既存の話題', '[\"term\"]')")
    topic_id = conn.execute("SELECT id FROM focus_topics").fetchone()[0]
    conn.execute(
        "INSERT INTO focus_evidence (signal_kind, signal_ref, topic_id, subject, observed_at) "
        "VALUES ('x_like', 999, ?, 'tweet:999', ?)",
        (topic_id, iso(2)),
    )

    prompt = build_prompt(
        config, prompt_topics(conn, config, NOW), collect_signals(conn, config, NOW), "ABCD"
    )

    baseline, fenced = prompt.split("<<<DATA-ABCD", 1)
    assert '- "claude code" OR "codex cli"' in baseline  # config の語は囲みの外
    assert f"[t{topic_id}] 既存の話題 | 検索の語: term" in fenced
    assert "[s1] (X のいいね・2026-11-20) nonce- を含む 改行つきの投稿" in fenced
    assert prompt.count("ABCD") == 4  # nonce は囲みの印にだけ出る


def test_prompt_topics_skip_old_topics(conn, tmp_path) -> None:
    config = make_config(tmp_path, window_days=7)
    for name, days_ago in (("新しい", 10), ("古い", 40)):
        topic_id = conn.execute(
            "INSERT INTO focus_topics (name) VALUES (?)", (name,)
        ).lastrowid
        conn.execute(
            "INSERT INTO focus_evidence (signal_kind, signal_ref, topic_id, subject, "
            "observed_at) VALUES ('x_like', ?, ?, 'tweet:1', ?)",
            (topic_id, topic_id, iso(days_ago)),
        )
    assert [t.name for t in prompt_topics(conn, config, NOW)] == ["新しい"]


def test_clean_terms_drops_operators_and_long_terms() -> None:
    assert clean_terms([
        '"software factory" OR "dark factory"',
        "-ソフトウェア工場",
        "x" * 41,
        "Software Factory OR dark factory",  # 大文字小文字だけ違う重複
        "(MCP)",
        "four",
    ]) == ["software factory dark factory", "ソフトウェア工場", "MCP"]


def test_render_focus_lists_active_and_candidates(conn, tmp_path) -> None:
    config = make_config(tmp_path)
    for index in range(1, 4):
        add_x_signal(conn, "self_rt", str(index), text=f"post {index}")
    run_focus(conn, config, FakeEngine(lambda _p: drafts(
        ("new", "重点の話題", ["focus term"], ["s1", "s2"]),
        ("new", "候補の\n話題", [], ["s3"]),
    )), logger, now=NOW)

    text = render_focus(conn, config, now=NOW)

    active, candidates = text.split("■ 候補", 1)
    assert "6.0  重点の話題(🔁2・記事と投稿 2 件・直近 2026-11-20)" in active
    assert "検索の語: focus term" in active
    assert "3.0  候補の 話題(🔁1・記事と投稿 1 件・直近 2026-11-20)" in candidates  # 1 行に潰す
    assert "振り分けたシグナル 3 件(どの話題にも当てはまらなかったもの 0 件)" in text


def test_like_and_bookmark_merged_into_an_article_count_as_one(conn, tmp_path) -> None:
    # Codex F1: ブックマークが RSS の記事に合流した投稿へのいいねも、その記事に寄せる。
    # 先に投稿として振り分けたいいねも、後でブックマークが合流したら、重みの計算で同じ記事に寄せる
    config = make_config(tmp_path)
    article = add_item(conn, "rss", title_ja="リンク先の記事")
    add_x_signal(conn, "like", "55", text="リンク付きの投稿", days_ago=1)
    run_focus(conn, config, FakeEngine(lambda _p: drafts(("new", "話題", [], ["s1"]))),
              logger, now=NOW)
    assert conn.execute("SELECT subject FROM focus_evidence").fetchone()[0] == "tweet:55"

    add_x_signal(conn, "bookmark", "55", text="リンク付きの投稿", item_id=article)
    add_x_signal(conn, "self_rt", "55", text="リンク付きの投稿")
    signals = collect_signals(conn, config, NOW)
    assert {s.subject for s in signals} == {"tweet:55"}  # 台帳には投稿のまま残す
    topic_id = conn.execute("SELECT id FROM focus_topics").fetchone()[0]
    run_focus(conn, config, FakeEngine(lambda _p: drafts(
        (f"t{topic_id}", "話題", [], [s.key for s in signals]),
    )), logger, now=NOW)

    (score,) = topic_scores(conn, config, NOW)
    assert (score.score, score.subjects) == (pytest.approx(3.0), 1)  # 1 本の投稿の一番強い 🔖 だけ


def test_x_signal_without_excerpt_uses_the_resolved_article_title(conn, tmp_path) -> None:
    # Codex F2: 本文の抜粋が消えたいいねも、同じ投稿の記事(X の投稿として取り込んだもの)の見出しで
    # 振り分ける
    config = make_config(tmp_path, window_days=14)
    conn.execute("INSERT INTO sources (type, name) VALUES ('x', 'x:home')")
    tweet_item = add_item(conn, "tw", title_ja="投稿の見出し")
    conn.execute(
        "UPDATE items SET source_id = (SELECT id FROM sources WHERE type = 'x'), "
        "external_id = '66' WHERE id = ?",
        (tweet_item,),
    )
    add_x_signal(conn, "like", "66", days_ago=8)  # 抜粋は保存期間を過ぎて消えた
    (signal,) = collect_signals(conn, config, NOW)
    assert (signal.subject, signal.text) == ("tweet:66", "投稿の見出し")


def test_reaction_on_a_degraded_article_uses_its_original_title(conn, tmp_path) -> None:
    # Codex F3: 分析が degraded の記事も元の見出しで配信するので、その記事への反応も数える
    config = make_config(tmp_path)
    item_id = add_item(conn, "deg")  # 分析なし(degraded と同じく title_ja が無い)
    conn.execute(
        "INSERT INTO analyses (item_id, summary, category, topic, relevance_score, novelty, "
        "negative_labels, recommendation_reason, implication, degraded, engine, model, "
        "prompt_version, input_hash) VALUES (?, 's', 'その他', 't', 0, 'new', '[]', 'r', 'i', 1, "
        "'codex-cli', 'm', 'v', 'h')",
        (item_id,),
    )
    conn.execute(
        "INSERT INTO signals (kind, item_id, value, signal_date, created_at) "
        "VALUES ('reaction', ?, 'dig', '2026-11-20', ?)",
        (item_id, iso(0)),
    )
    (signal,) = collect_signals(conn, config, NOW)
    assert (signal.kind, signal.text) == ("tg_dig", "title deg")


def test_signals_assigned_by_another_run_are_not_counted_again(conn, tmp_path) -> None:
    # Codex F4: LLM を待つ間に別の実行が同じ材料を振り分けていたら、こちらでは数えず、
    # 話題も作らない
    from app.focus import apply_assignments

    add_x_signal(conn, "like", "1", text="a")
    add_x_signal(conn, "like", "2", text="b")
    signals = collect_signals(conn, make_config(tmp_path), NOW)
    first = signals[0]
    conn.execute(
        "INSERT INTO focus_evidence (signal_kind, signal_ref, subject, observed_at) "
        "VALUES (?, ?, ?, ?)",
        (first.kind, first.ref, first.subject, first.observed_at),
    )

    assigned, new_topics = apply_assignments(
        conn, signals,
        drafts(("new", "先に振られた材料だけの話題", [], [first.key]),
               ("new", "残りの話題", [], [signals[1].key])),
        set(),
    )

    assert (assigned, new_topics) == (1, 1)
    names = [r[0] for r in conn.execute("SELECT name FROM focus_topics")]
    assert names == ["残りの話題"]
    assert conn.execute("SELECT count(*) FROM focus_evidence").fetchone()[0] == 2


def test_x_article_evidence_follows_a_later_bookmark_merge(conn, tmp_path) -> None:
    # Codex F1(round 2): X の投稿として取り込んだ記事 A への反応を先に振り分け、後で同じ投稿の
    # ブックマークが RSS の記事 B に合流しても、A と B を別の記事として二重に数えない
    config = make_config(tmp_path)
    conn.execute("INSERT INTO sources (type, name) VALUES ('x', 'x:home')")
    x_article = add_item(conn, "xa", title_ja="X の投稿の記事")
    conn.execute(
        "UPDATE items SET source_id = (SELECT id FROM sources WHERE type = 'x'), "
        "external_id = '99' WHERE id = ?",
        (x_article,),
    )
    conn.execute(
        "INSERT INTO signals (kind, item_id, value, signal_date, created_at) "
        "VALUES ('reaction', ?, 'dig', '2026-11-19', ?)",
        (x_article, iso(1)),
    )
    add_x_signal(conn, "self_rt", "99", text="投稿", days_ago=1)
    run_focus(conn, config, FakeEngine(lambda _p: drafts(("new", "話題", [], ["s1", "s2"]))),
              logger, now=NOW)
    topic_id = conn.execute("SELECT id FROM focus_topics").fetchone()[0]

    rss_article = add_item(conn, "rb", title_ja="リンク先の記事")
    add_x_signal(conn, "bookmark", "99", text="投稿", item_id=rss_article)
    add_x_signal(conn, "like", "100", text="別の投稿")
    signals = collect_signals(conn, config, NOW)
    run_focus(conn, config, FakeEngine(lambda _p: drafts(
        (f"t{topic_id}", "話題", [], [s.key for s in signals]),
    )), logger, now=NOW)

    (score,) = topic_scores(conn, config, NOW)
    # 投稿 99 は 1 つの記事(一番強いのは今日の 🔖 の 3.0)、別の投稿の ❤️ が 1.0
    assert (score.score, score.subjects) == (pytest.approx(4.0), 2)
    assert score.status == "candidate"


def test_active_focus_lists_active_topics_by_weight(conn, tmp_path) -> None:
    # GH#433 の 2 本目: digest 段が写す今の重点は、重点の話題だけを重みの高い順に
    from app.focus import active_focus
    from app.models import FocusEntry, focus_to_json, parse_focus_json

    config = make_config(tmp_path, enter_score=1.0, exit_score=0.5)
    for index in range(1, 5):
        add_x_signal(conn, "bookmark", str(index), text=f"post {index}")
    run_focus(conn, config, FakeEngine(lambda _p: drafts(
        ("new", "B", [], ["s1"]),
        ("new", "A", [], ["s2", "s3"]),
        ("new", "外した", [], ["s4"]),
    )), logger, now=NOW)
    conn.execute(
        "UPDATE focus_topics SET status = 'dismissed', dismissed_at = ? WHERE name = '外した'",
        (iso(0),),
    )

    entries = active_focus(conn, config, now=NOW)

    assert [(e.name, e.subjects) for e in entries] == [("A", 2), ("B", 1)]
    assert parse_focus_json(focus_to_json(entries)) == entries
    for broken in ("", "[]", '{"topics": [{"topic_id": true, "name": "x", "subjects": 1}]}',
                   '{"topics": []}', "not json"):
        assert parse_focus_json(broken) is None
    assert parse_focus_json('{"topics": [{"topic_id": 1, "name": "x", "subjects": 2}]}') == [
        FocusEntry(1, "x", 2)
    ]


def test_dismissed_topic_stays_out_and_restored_topic_returns_by_weight(conn, tmp_path) -> None:
    # GH#433 の 3 本目: 外した話題は重みがあっても重点に戻さず、戻すと次の更新で重みで入る
    from app.focus import dismiss_topic, restore_topic

    config = make_config(tmp_path)
    add_x_signal(conn, "self_rt", "1", text="a")
    add_x_signal(conn, "self_rt", "2", text="b")
    run_focus(conn, config, FakeEngine(lambda _p: drafts(("new", "話題", [], ["s1", "s2"]))),
              logger, now=NOW)
    topic_id = conn.execute("SELECT id FROM focus_topics").fetchone()[0]

    assert dismiss_topic(conn, topic_id, NOW) == "dismissed"
    assert dismiss_topic(conn, topic_id, NOW) == "already"
    row = conn.execute("SELECT status, dismissed_at, deactivated_at FROM focus_topics").fetchone()
    assert (row["status"], row["dismissed_at"], row["deactivated_at"]) == (
        "dismissed", iso(0), iso(0)
    )
    run_focus(conn, config, FakeEngine(), logger, now=NOW)
    assert scores_by_name(conn, config)["話題"][1] == "dismissed"

    assert restore_topic(conn, topic_id) == "restored"
    assert restore_topic(conn, topic_id) == "already"
    assert restore_topic(conn, 999) == dismiss_topic(conn, 999, NOW) == "missing"
    run_focus(conn, config, FakeEngine(), logger, now=NOW)
    assert scores_by_name(conn, config)["話題"][1] == "active"


def test_focus_topic_refs_add_newest_examples_from_evidence(conn, tmp_path) -> None:
    # GH#433 の 4 本目: 当たる記事を選ぶ prompt に渡す重点は、digest に出す重点の話題に、証拠の
    # 見出しを新しい順に 3 つまで添える。記事・投稿は重みと同じく寄せた先で 1 つに数え、本文の
    # 無いものは飛ばす
    from app.focus import FocusTopicRef, active_focus, focus_topic_refs

    config = make_config(tmp_path)
    topic = conn.execute(
        "INSERT INTO focus_topics (name, status, activated_at) VALUES ('話題', 'active', ?)",
        (iso(0),),
    ).lastrowid
    candidate = conn.execute("INSERT INTO focus_topics (name) VALUES ('候補')").lastrowid
    article = add_item(conn, "a", title_ja="記事の見出し")
    unanalyzed = add_item(conn, "u")  # 分析の無い記事は元の見出し
    add_x_signal(conn, "like", "500", text="投稿の\n本文")
    add_x_signal(conn, "bookmark", "600", text="合流した投稿", item_id=article)
    add_x_signal(conn, "like", "800", text="古い投稿")
    add_x_signal(conn, "like", "900", text="投稿の 本文")  # 別の投稿でも同じ文は 1 つ
    evidence = [
        ("tg_dig", 1, "tweet:700", 0.05, topic),  # 本文が無い投稿は飛ばす
        ("x_like", 2, "tweet:500", 0.1, topic),
        ("x_like", 8, "tweet:900", 0.15, topic),
        ("x_bookmark", 3, "tweet:600", 0.2, topic),  # 合流した記事に寄せる
        ("tg_useful", 4, f"item:{article}", 3, topic),  # 同じ記事なので 1 つ
        ("note", 5, f"item:{unanalyzed}", 4, topic),
        ("x_like", 6, "tweet:800", 6, topic),  # 3 つを超えた分
        ("tg_useful", 7, f"item:{article}", 0, candidate),  # 候補の話題は渡さない
    ]
    for kind, ref, subject, days_ago, topic_id in evidence:
        conn.execute(
            "INSERT INTO focus_evidence (signal_kind, signal_ref, topic_id, subject, observed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (kind, ref, topic_id, subject, iso(days_ago)),
        )

    entries = active_focus(conn, config, now=NOW)
    assert [e.topic_id for e in entries] == [topic]  # 候補の話題は渡さない
    assert focus_topic_refs(conn, entries) == [
        FocusTopicRef(topic, "話題", ("投稿の 本文", "記事の見出し", "title u"))
    ]



def test_search_terms_send_latin_terms_of_the_active_topics_in_weight_order(conn, tmp_path) -> None:
    # GH#433 の 5 本目: HN の検索に送る語は、digest に出す重点の話題の語のうち英字を含むもの。
    # 重みの高い話題から順に、話題の中の語の順で並べ、同じ語は 1 つにする。台帳の値も送る前に
    # clean_terms をかけ直す(引用符と演算子を外す)
    from app.focus import search_terms

    config = make_config(tmp_path)

    def topic(name: str, terms: list[str], status: str = "active") -> int:
        return conn.execute(
            "INSERT INTO focus_topics (name, terms, status) VALUES (?, ?, ?)",
            (name, json.dumps(terms, ensure_ascii=False), status),
        ).lastrowid

    light = topic("軽い", ['"quoted" term', "Agent Review", "OR"])
    heavy = topic("重い", ["日本語の語", "Agent Review", "agent review"])
    topic("候補", ["candidate term"], status="candidate")
    for ref, topic_id in ((1, heavy), (2, heavy), (3, light)):
        conn.execute(
            "INSERT INTO focus_evidence (signal_kind, signal_ref, topic_id, subject, observed_at) "
            "VALUES ('tg_dig', ?, ?, ?, ?)",
            (ref, topic_id, f"tweet:{ref}", iso(0)),
        )

    assert search_terms(conn, config, now=NOW) == ["Agent Review", "quoted term"]
