import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.analysis.llm import LLMError
from app.analysis.trend import (
    MAX_EVENTS,
    MAX_INPUT_ITEMS,
    MAX_LINKS,
    MAX_SUMMARY,
    TrendSummary,
    TrendTheme,
    synthesize_trend,
)
from app.db import apply_migrations, connect
from app.models import NEWS_HEADING, link_host, link_label, parse_news_json

logger = logging.getLogger("test-trend")


def _now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed-a')")  # id=2
    yield conn
    conn.close()


def add_analyzed(
    conn, url, *, topic, title_ja, degraded=0, relevance=80, status="analyzed", analyzed_at=None
) -> int:
    item_id = conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, title, content_hash, status) "
        "VALUES (2, ?, ?, ?, 'sha256:x', ?)",
        (url, url, f"t {url}", status),
    ).lastrowid
    conn.execute(
        "INSERT INTO analyses (item_id, title_ja, summary, category, topic, relevance_score, "
        "novelty, negative_labels, recommendation_reason, implication, degraded, engine, model, "
        "prompt_version, input_hash, analyzed_at) "
        "VALUES (?, ?, '要約', '課題直結', ?, ?, 'new', '[]', '理由', '示唆', ?, 'codex-cli', "
        "'m', 'analyze-v001', ?, ?)",
        (item_id, title_ja, topic, relevance, degraded, f"sha256:{url}", analyzed_at or _now_utc()),
    )
    return item_id


class FakeEngine:
    name = "fake"

    def __init__(self, result_or_exc):
        self._r = result_or_exc

    def complete_structured(self, prompt, schema, timeout_s=120):
        if isinstance(self._r, Exception):
            raise self._r
        return self._r


def seed_six(conn) -> list[int]:
    return [
        add_analyzed(
            conn, f"https://s{i}.example/a{i}", topic=f"topic{i % 2}", title_ja=f"見出し{i}"
        )
        for i in range(6)
    ]


def test_section_keeps_grounded_events_with_links(conn) -> None:
    ids = seed_six(conn)
    result = TrendSummary(
        themes=[
            TrendTheme(label="AI セキュリティ", summary="権限と秘匿が話題。", item_ids=ids[:2]),
            TrendTheme(label="エージェント設計", summary="設計論が複数。", item_ids=ids[2:4]),
        ]
    )
    section = synthesize_trend(conn, FakeEngine(result), logger)
    assert section is not None
    assert [event.label for event in section.events] == ["AI セキュリティ", "エージェント設計"]
    assert [link.item_id for link in section.events[0].links] == ids[:2]
    assert section.events[0].links[0].url == "https://s0.example/a0"  # items の正規 URL
    text = section.plain_text()
    assert text.startswith(NEWS_HEADING)  # プレーンテキスト(markup・リンクなし)
    assert "・AI セキュリティ — 権限と秘匿が話題。" in text
    assert "・エージェント設計 — 設計論が複数。" in text
    assert "**" not in text and "##" not in text and "http" not in text


def test_summary_truncated_to_bound(conn) -> None:
    ids = seed_six(conn)
    long_summary = "あ" * 300
    result = TrendSummary(
        themes=[TrendTheme(label="長文", summary=long_summary, item_ids=[ids[0]])]
    )
    summary = synthesize_trend(conn, FakeEngine(result), logger).events[0].summary
    assert summary.endswith("…")  # 切り詰められた
    assert len(summary) == MAX_SUMMARY  # ヘッダ 1 通が肥大しない


def test_prompt_compliant_summary_not_truncated(conn) -> None:
    # prompt(v003)の指示上限 120 字を守った要約は文中で切らない(GH#78)。
    # MAX_SUMMARY はあくまで指示無視時の安全網
    ids = seed_six(conn)
    compliant_summary = "い" * 119 + "。"
    result = TrendSummary(
        themes=[TrendTheme(label="遵守", summary=compliant_summary, item_ids=[ids[0]])]
    )
    section = synthesize_trend(conn, FakeEngine(result), logger)
    assert section.events[0].summary == compliant_summary  # 全文がそのまま載る


def test_too_few_items_returns_none(conn) -> None:
    add_analyzed(conn, "only", topic="t", title_ja="1件だけ")
    result = TrendSummary(themes=[TrendTheme(label="x", summary="y", item_ids=[1])])
    assert synthesize_trend(conn, FakeEngine(result), logger) is None


def test_llm_failure_returns_none(conn) -> None:
    seed_six(conn)
    section = synthesize_trend(conn, FakeEngine(LLMError("boom")), logger)
    assert section is None  # 付加機能なので節省略・例外は投げない


def test_ungrounded_hallucinated_and_empty_dropped(conn) -> None:
    ids = seed_six(conn)
    result = TrendSummary(
        themes=[
            TrendTheme(label="実在", summary="接地あり。", item_ids=[ids[0]]),
            TrendTheme(label="幻", summary="存在しない id のみ。", item_ids=[999999]),
            TrendTheme(label="", summary="ラベル空。", item_ids=[ids[1]]),
            TrendTheme(label="要約空", summary="   ", item_ids=[ids[2]]),
        ]
    )
    section = synthesize_trend(conn, FakeEngine(result), logger)
    assert [event.label for event in section.events] == ["実在"]  # 幻・空は捨てる
    assert "幻" not in section.plain_text()


def test_all_events_ungrounded_returns_none(conn) -> None:
    seed_six(conn)
    result = TrendSummary(themes=[TrendTheme(label="幻", summary="無い id。", item_ids=[999999])])
    assert synthesize_trend(conn, FakeEngine(result), logger) is None  # 節ごと省く


def test_degraded_items_excluded(conn) -> None:
    for i in range(6):
        add_analyzed(conn, f"d{i}", topic="t", title_ja=f"d{i}", degraded=1)
    result = TrendSummary(themes=[TrendTheme(label="x", summary="y", item_ids=[1])])
    assert synthesize_trend(conn, FakeEngine(result), logger) is None


def test_digested_items_included(conn) -> None:
    # --force 再生成時、当日 digest 済み(status='digested')の分析も拾う(status で絞らない)
    ids = [
        add_analyzed(
            conn, f"https://g{i}.example/", topic="t", title_ja=f"g{i}", status="digested"
        )
        for i in range(6)
    ]
    themes = [TrendTheme(label="採用", summary="digested 済み。", item_ids=ids)]
    section = synthesize_trend(conn, FakeEngine(TrendSummary(themes=themes)), logger)
    assert section is not None and "・採用 — digested 済み。" in section.plain_text()


def test_prior_day_analyses_excluded(conn) -> None:
    # 前日(JST)以前に分析されたものは当日トレンドに混ざらない(analyzed_at の下限)
    for i in range(6):
        add_analyzed(
            conn, f"old{i}", topic="t", title_ja=f"old{i}", analyzed_at="2020-01-01T00:00:00Z"
        )
    result = TrendSummary(themes=[TrendTheme(label="x", summary="y", item_ids=[1])])
    assert synthesize_trend(conn, FakeEngine(result), logger) is None


def test_trend_candidates_exclude_bookmark_items(conn) -> None:
    # ブックマークは過去の任意時点の内容のため「今日のトレンド」に混ぜない(GH#99)
    from app.analysis.trend import _fetch_today_items

    conn.execute(
        """INSERT INTO sources (type, name, config, enabled)
           VALUES ('x', 'x:bookmark', '{"mode": "bookmark"}', 0)"""
    )
    bm_source = conn.execute(
        "SELECT id FROM sources WHERE name = 'x:bookmark'"
    ).fetchone()["id"]
    add_analyzed(conn, "https://a/normal", topic="t1", title_ja="通常")
    bm_item = conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, content_hash, status) "
        "VALUES (?, 'https://a/bm', 'https://a/bm', 'sha256:b', 'analyzed')",
        (bm_source,),
    ).lastrowid
    conn.execute(
        "INSERT INTO analyses (item_id, title_ja, summary, category, topic, relevance_score, "
        "novelty, negative_labels, recommendation_reason, implication, degraded, engine, model, "
        "prompt_version, input_hash) "
        "VALUES (?, 'BM', '要約', '課題直結', 't1', 90, 'new', '[]', '理由', '示唆', 0, "
        "'codex-cli', 'm', 'analyze-v001', 'sha256:bm')",
        (bm_item,),
    )
    conn.execute(
        "INSERT INTO x_signals (kind, tweet_id, item_id) VALUES ('bookmark', 't1', ?)",
        (bm_item,),
    )
    ids = [row["item_id"] for row in _fetch_today_items(conn)]
    assert bm_item not in ids


class PromptEngine(FakeEngine):
    def __init__(self, result):
        super().__init__(result)
        self.prompts: list[str] = []

    def complete_structured(self, prompt, schema, timeout_s=120):
        self.prompts.append(prompt)
        return super().complete_structured(prompt, schema, timeout_s)


def test_prompt_passes_every_item_of_a_full_day_with_relevance(conn) -> None:
    # GH#413: 1 日の分析(日次上限 150)をすべて渡す。関心の低い大きいニュースを relevance の
    # 順で削らない。relevance は出来事の大きさが同じくらいのときに使わせる自前の数値
    ids = [
        add_analyzed(
            conn, f"https://n{i}.example/", topic="t", title_ja=f"記事{i}", relevance=i % 100
        )
        for i in range(160)
    ]
    engine = PromptEngine(TrendSummary(themes=[TrendTheme(label="x", summary="y", item_ids=ids)]))
    synthesize_trend(conn, engine, logger)
    prompt = engine.prompts[0]
    assert MAX_INPUT_ITEMS > 160
    assert all(f"[id={item_id}]" in prompt for item_id in ids)
    line = (
        f"[id={ids[0]}] 記事0 | topic: t | category: 課題直結 | source: rss:feed-a | relevance: 0"
    )
    assert line in prompt


def test_links_prefer_reports_over_x_posts_and_one_per_host(conn) -> None:
    # 報道の記事・公式の発表を先に、X の投稿を後ろに。同じ host は 1 本、最大 MAX_LINKS 本
    post = add_analyzed(conn, "https://x.com/i/web/status/1", topic="t", title_ja="投稿")
    twitter = add_analyzed(conn, "https://twitter.com/a/status/2", topic="t", title_ja="投稿2")
    news_a = add_analyzed(conn, "https://www.news-a.example/1", topic="t", title_ja="報道 A")
    news_a2 = add_analyzed(conn, "https://news-a.example/2", topic="t", title_ja="報道 A の続報")
    blog = add_analyzed(conn, "https://blog.example/p", topic="t", title_ja="解説")
    official = add_analyzed(conn, "https://status.example/", topic="t", title_ja="公式")
    order = [post, twitter, news_a, news_a2, blog, official, 999999, news_a]
    result = TrendSummary(themes=[TrendTheme(label="障害", summary="続いた。", item_ids=order)])
    links = synthesize_trend(conn, FakeEngine(result), logger).events[0].links
    assert [link.item_id for link in links] == [news_a, blog, official]  # LLM の順を保つ
    assert len(links) == MAX_LINKS


def test_x_post_is_linked_when_it_is_the_only_report(conn) -> None:
    seed_six(conn)
    post = add_analyzed(conn, "https://x.com/i/web/status/9", topic="t", title_ja="投稿だけ")
    result = TrendSummary(themes=[TrendTheme(label="話題", summary="投稿で。", item_ids=[post])])
    links = synthesize_trend(conn, FakeEngine(result), logger).events[0].links
    assert [link.url for link in links] == ["https://x.com/i/web/status/9"]


def test_events_capped(conn) -> None:
    ids = seed_six(conn)
    themes = [TrendTheme(label=f"出来事{i}", summary="要約。", item_ids=[ids[0]]) for i in range(9)]
    section = synthesize_trend(conn, FakeEngine(TrendSummary(themes=themes)), logger)
    assert len(section.events) == MAX_EVENTS


def test_json_round_trip_keeps_events_and_link_ids(conn) -> None:
    ids = seed_six(conn)
    result = TrendSummary(
        themes=[
            TrendTheme(label="A<&>", summary="要約 \"1\"。", item_ids=ids[:3]),
            TrendTheme(label="B", summary="要約 2。", item_ids=[ids[3]]),
        ]
    )
    section = synthesize_trend(conn, FakeEngine(result), logger)
    assert parse_news_json(section.to_json()) == [
        ("A<&>", "要約 \"1\"。", ids[:3], ids[:3]),
        ("B", "要約 2。", [ids[3]], [ids[3]]),
    ]
    assert "http" not in section.to_json()  # URL は持たず、配信のときに items から引く


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "not json",
        "[]",
        '{"events": {}}',
        '{"events": []}',
        '{"events": [1]}',
        '{"events": [{"label": "a", "summary": "b"}]}',
        '{"events": [{"label": "a", "summary": 2, "item_ids": []}]}',
        '{"events": [{"label": "a", "summary": "b", "item_ids": [true]}]}',
        '{"events": [{"label": "a", "summary": "b", "item_ids": ["1"]}]}',
    ],
)
def test_unreadable_news_json_is_none(text) -> None:
    assert parse_news_json(text) is None  # 呼び出し側は trend_markdown の文に戻す


def test_link_label_is_short_host() -> None:
    assert link_host("https://WWW.Example.COM/a?b=1") == "example.com"
    assert link_host("not a url") == ""
    assert link_host("http://[::1") == ""  # 壊れた URL でも落ちない
    assert link_label("https://forest.watch.impress.co.jp/docs/1") == "forest.watch.impress.co.jp"
    long_host = "h" * 60 + ".example"
    assert link_label(f"https://{long_host}/") == "h" * 39 + "…"
    assert link_label("not a url") == "記事"


def test_events_keep_every_grounded_report_as_sources(conn) -> None:
    # GH#415: リンクは 3 本までだが、出来事を報じた記事は材料として全部残す(上限 8。出来事の調査)
    from app.models import NEWS_MAX_SOURCES

    ids = [
        add_analyzed(conn, f"https://r{i}.example/", topic="t", title_ja=f"報道{i}")
        for i in range(10)
    ]
    order = [ids[0], 999999, ids[1], ids[0], *ids[2:]]
    result = TrendSummary(themes=[TrendTheme(label="出来事", summary="要約。", item_ids=order)])
    event = synthesize_trend(conn, FakeEngine(result), logger).events[0]
    assert len(event.links) == MAX_LINKS
    assert event.sources == tuple(ids[:NEWS_MAX_SOURCES])  # 幻の id と重複を外し、順を保つ
    entry = parse_news_json(synthesize_trend(conn, FakeEngine(result), logger).to_json())[0]
    assert entry.source_ids == ids[:NEWS_MAX_SOURCES]
