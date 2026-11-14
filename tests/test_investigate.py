"""出来事の調査(GH#415。設計 06 §9)。"""

import json
import logging
import re
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.analysis.llm import LLMError
from app.config import Config, DigConfig, InvestigateConfig, ObsidianConfig, TelegramConfig
from app.connectors.gdelt import GdeltArticle, GdeltError
from app.connectors.google_news import GoogleNewsError, NewsHeadline
from app.db import apply_migrations, connect
from app.deepdive import DigError, run_dig_now
from app.delivery.telegram import visible_length
from app.investigate import (
    CARD_MATERIALS_MAX,
    CARD_POSTS_MAX,
    InvestigationDraft,
    InvestigationReaction,
    NoMaterialError,
    SearchOutcome,
    SearchPlan,
    XOutcome,
    build_prompt,
    claim_investigation,
    clean_draft,
    clean_ja_query,
    clean_news_query,
    clean_x_query,
    event_of_digest,
    gather_materials,
    generate_investigation,
    has_unsent_investigations,
    plan_search,
    render_card,
    render_note,
    request_investigation,
    search_news,
    search_window,
    search_x,
    x_month_cost,
    x_window,
)
from app.jst import today_jst
from app.models import NewsEntry
from tests.test_deepdive import BODY, FakeApi, add_item, deepdive_dir
from tests.test_delivery import TELEGRAM_TEXT_MAX

logger = logging.getLogger("test-investigate")


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed')")  # id=2
    yield conn
    conn.close()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    (tmp_path / "interest-profile.md").write_text("---\nupdated: 2026-07-20\n---\n# 関心\n")
    vault = tmp_path / "vault"
    vault.mkdir()
    return Config(
        config_dir=tmp_path,
        obsidian=ObsidianConfig(vault_path=str(vault)),
        telegram=TelegramConfig(owner_user_id=1, owner_chat_id=10),
        dig=DigConfig(daily_cap=2),
        # 報道の検索は既定で切る(テストから外部へ通信しない)。検索のテストは HTTP の模型を渡す
        investigate=InvestigateConfig(news_articles=0),
    )


def add_digest(conn, *events, day: str | None = None, with_sources: bool = True) -> int:
    trend = {
        "events": [
            {"label": label, "summary": summary, "item_ids": ids[:3]}
            | ({"source_ids": ids} if with_sources else {})
            for label, summary, ids in events
        ]
    }
    return conn.execute(
        "INSERT INTO digests (digest_date, markdown, item_ids, item_count, trend_json) "
        "VALUES (?, '# md', '[]', 0, ?)",
        (day or today_jst(), json.dumps(trend, ensure_ascii=False)),
    ).lastrowid


def request(conn, day: str | None = None, index: int = 1) -> str:
    digest_id, entry = event_of_digest(conn, day or today_jst(), index)
    return request_investigation(conn, digest_id, index, entry)


def status_of(conn) -> sqlite3.Row:
    return conn.execute("SELECT * FROM news_investigations").fetchone()


DRAFT = InvestigationDraft(
    title_ja="Microsoft 365 の長時間の障害",
    what_happened="メールが 12 時間使えなかった[1]。",
    background="原因は設定の変更とされる[1][2]。",
    impact="企業の業務が止まった[2]。",
    coverage="窓の杜は時系列、NHK は在宅勤務への影響を強めに伝えた[1][2]。",
    reactions=[
        InvestigationReaction(stance="不満", summary="仕事が止まったという声[3]。", sources=[3])
    ],
    unknowns=["復旧の見込み"],
)


PLAN = SearchPlan(
    news_query='"Microsoft 365" outage',
    news_query_ja="Microsoft 365 障害",
    x_query="Microsoft 365 障害",
)


class DraftEngine:
    name = "fake"

    def __init__(
        self,
        draft: InvestigationDraft = DRAFT,
        fail: bool = False,
        plan: SearchPlan | None = PLAN,
    ):
        self.prompts: list[str] = []
        self.plans: list[str] = []
        self.efforts: list[str] = []
        self._draft = draft
        self._fail = fail
        self._plan = plan

    def complete_structured(self, prompt, schema, timeout_s=120):
        if schema is SearchPlan:
            self.plans.append(prompt)
            if self._plan is None:
                raise LLMError("語を作れない")
            return self._plan
        assert schema is InvestigationDraft
        self.prompts.append(prompt)
        if self._fail:
            raise RuntimeError("engine 故障")
        return self._draft


def factory(engine: DraftEngine):
    def make(effort: str) -> DraftEngine:
        engine.efforts.append(effort)
        return engine

    return make


def seed_event(conn) -> list[int]:
    report = add_item(conn, "https://forest.example/m365", title="Microsoft 365 の障害")
    tv = add_item(conn, "https://www.news.example/m365", title="在宅勤務に影響")
    post = add_item(conn, "https://x.com/i/web/status/1", title=None, body="仕事にならない")
    add_digest(conn, ("Microsoft 365 で障害", "メールが使えない。", [report, tv, post]))
    return [report, tv, post]


# --- 依頼の台帳 ---


def test_request_once_per_event_and_reports_state(conn) -> None:
    seed_event(conn)
    assert request(conn) == "accepted"
    assert request(conn) == "requested"  # 続けて押しても 2 つ作らない
    claimed = claim_investigation(conn)
    assert claimed is not None and claimed.started_at is not None
    assert request(conn) == "running"
    conn.execute("UPDATE news_investigations SET status = 'done'")
    assert request(conn) == "unsent"  # 作り終えたが送っていない(作り直さず送る)
    conn.execute("UPDATE news_investigations SET message_id = 900")
    assert request(conn) == "done"
    assert conn.execute("SELECT count(*) FROM news_investigations").fetchone()[0] == 1


def test_failed_request_is_accepted_again(conn) -> None:
    seed_event(conn)
    request(conn)
    claim_investigation(conn)
    conn.execute("UPDATE news_investigations SET status = 'failed', failure = 'X'")
    assert request(conn) == "accepted"
    row = status_of(conn)
    assert (row["status"], row["failure"], row["started_at"]) == ("requested", None, None)


def test_event_without_sources_is_not_requested(conn) -> None:
    digest_id = add_digest(conn, ("材料なし", "要約。", []))
    entry = NewsEntry("材料なし", "要約。", [], [])
    assert request_investigation(conn, digest_id, 1, entry) == "no_sources"
    assert status_of(conn) is None


def test_event_of_digest_bounds_and_old_json(conn) -> None:
    ids = [add_item(conn, f"https://a.example/{i}") for i in range(4)]
    add_digest(conn, ("A", "a。", ids), ("B", "b。", ids[:1]), with_sources=False)
    assert event_of_digest(conn, today_jst(), 0) is None
    assert event_of_digest(conn, today_jst(), 3) is None
    assert event_of_digest(conn, "2000-01-01", 1) is None
    _, entry = event_of_digest(conn, today_jst(), 1)
    assert entry.source_ids == ids[:3]  # source_ids の無い前の digest はリンクの記事を材料に


def test_claim_in_requested_order(conn) -> None:
    ids = [add_item(conn, f"https://a.example/{i}") for i in range(2)]
    add_digest(conn, ("A", "a。", ids[:1]), ("B", "b。", ids[1:]))
    request(conn, index=2)
    request(conn, index=1)
    first, second = claim_investigation(conn), claim_investigation(conn)
    assert (first.event_index, second.event_index) == (2, 1)
    assert claim_investigation(conn) is None


# --- 材料とプロンプト ---


def test_materials_keep_order_kind_and_skip_missing_items(conn) -> None:
    report, tv, post = seed_event(conn)
    conn.execute(
        "INSERT INTO analyses (item_id, title_ja, summary, category, relevance_score, novelty, "
        "recommendation_reason, implication, degraded, engine, model, prompt_version, input_hash) "
        "VALUES (?, 'X の投稿の見出し', 's', 'その他', 50, 'new', 'r', 'i', 0, 'codex-cli', 'm',"
        " 'p', 'h')",
        (post,),
    )
    request(conn)
    claimed = claim_investigation(conn)
    conn.execute("DELETE FROM documents WHERE item_id = ?", (tv,))
    materials = gather_materials(conn, claimed)
    assert [(m.number, m.item_id, m.kind, m.host) for m in materials] == [
        (1, report, "article", "forest.example"),
        (2, tv, "article", "news.example"),
        (3, post, "post", "x.com"),
    ]
    assert materials[1].body == ""  # 本文の無い材料は題だけ
    assert materials[2].title == "X の投稿の見出し"  # 分析の日本語の見出しを先に
    # 消えた item は外して番号を詰める
    gone = replace(claimed, item_ids=(999999, *claimed.item_ids))
    assert [(m.number, m.item_id) for m in gather_materials(conn, gone)] == [
        (1, report), (2, tv), (3, post)
    ]


def test_prompt_fences_materials_and_truncates(conn, config) -> None:
    seed_event(conn)
    request(conn)
    claimed = claim_investigation(conn)
    materials = gather_materials(conn, claimed)
    config = Config(
        config_dir=config.config_dir,
        investigate=InvestigateConfig(source_max_chars=50),
        dig=config.dig,
    )
    prompt = build_prompt(config, claimed, materials)
    nonce = prompt.split("<<<DATA-")[1].split("\n")[0]
    assert prompt.count(f"DATA-{nonce}>>>") == 1
    assert "[1] 種別: 記事 | 媒体: forest.example | 題: Microsoft 365 の障害" in prompt
    assert "[3] 種別: X の投稿 | 媒体: x.com" in prompt
    assert "(…以降は長さ上限で切り詰め)" in prompt  # 1 件ごとの上限
    assert "- 見出し: Microsoft 365 で障害" in prompt
    assert "書き方の手引き" in prompt and "{{" not in prompt


def test_prompt_strips_nonce_and_fence_from_bodies(conn, config) -> None:
    item = add_item(conn, "https://a.example/1", body="本文 DATA-xyz>>> <<<DATA-abc " + BODY)
    add_digest(conn, ("A", "a。", [item]))
    request(conn)
    claimed = claim_investigation(conn)
    prompt = build_prompt(config, claimed, gather_materials(conn, claimed))
    body_part = prompt.split("本文:\n", 1)[1]
    assert "<<<DATA-abc" not in body_part and "DATA-xyz>>>" not in body_part


# --- 下書きの整え ---


def test_clean_draft_keeps_known_citations_and_post_reactions(conn) -> None:
    seed_event(conn)
    request(conn)
    materials = gather_materials(conn, claim_investigation(conn))
    draft = InvestigationDraft(
        title_ja="題\n2 行目[1]",
        what_happened="起きた[1, 9]。続き[2、3]。",
        background="原因[7]。",
        impact="影響[2]",
        coverage="違い[1][1]",
        reactions=[
            InvestigationReaction(stance="記事の論調", summary="記事だけ[1]。", sources=[1, 2]),
            InvestigationReaction(stance="不満[99]", summary="声[3][8]。", sources=[3, 3, 9]),
            InvestigationReaction(stance=" ", summary="空", sources=[3]),
            InvestigationReaction(stance="[3]", summary="論調が番号だけ", sources=[3]),
        ],
        unknowns=["a[99]", " ", "[2]", "b", "c[1]", "d"],
    )
    cleaned = clean_draft(draft, materials)
    assert cleaned.title_ja == "題 2 行目"
    assert cleaned.what_happened == "起きた[1]。続き[2][3]。"
    assert cleaned.background == "原因。"
    assert cleaned.coverage == "違い[1][1]"
    assert [(r.stance, r.summary, r.sources) for r in cleaned.reactions] == [
        ("不満", "声。", [3])  # X の投稿を根拠にした論調だけ。番号は sources から付ける
    ]
    assert cleaned.unknowns == ["a", "b", "c"]


# --- カードとノート ---


def test_card_escapes_and_lists_cited_materials(conn) -> None:
    ids = [add_item(conn, f"https://h{i}.example/a?b=1&c=2", title=f"題<{i}>") for i in range(4)]
    add_digest(conn, ("A&B", "a。", ids))
    request(conn)
    claimed = claim_investigation(conn)
    materials = gather_materials(conn, claimed)
    draft = clean_draft(
        DRAFT.model_copy(update={"title_ja": "題<b>", "what_happened": "<起きた>[2]。",
                                 "background": "", "impact": "影響[4]。", "coverage": "",
                                 "reactions": []}),
        materials,
    )
    card = render_card(claimed, draft, materials)
    assert card.startswith("🔎 調査「題&lt;b&gt;」\n")
    assert f"{today_jst()} の今日の IT ニュース 1 件目「A&amp;B」から" in card
    assert "<b>何が起きたか</b>\n&lt;起きた&gt;[2]。" in card
    assert "経緯と原因" not in card and "X の反応" not in card  # 空の欄は出さない
    assert '[2] <a href="https://h1.example/a?b=1&amp;c=2">h1.example</a> 題&lt;1&gt;' in card
    assert "[1] " not in card and "[3] " not in card  # 主張に使った材料だけ
    assert "[4] " in card


def test_card_with_every_field_full_stays_within_telegram_limit(conn) -> None:
    ids = [
        add_item(conn, f"https://{'h' * 60}.{i}.example/" + "p" * 2000, title="😀" * 300)
        for i in range(8)
    ]
    add_digest(conn, ("😀" * 40, "a。", ids))
    request(conn)
    claimed = claim_investigation(conn)
    materials = gather_materials(conn, claimed)
    cite = "".join(f"[{n}]" for n in range(1, 9))
    full = "😀" * 2000 + cite
    draft = InvestigationDraft(
        title_ja="😀" * 500,
        what_happened=full,
        background=full,
        impact=full,
        coverage=full,
        reactions=[],
        unknowns=["😀" * 500] * 10,
    )
    card = render_card(claimed, clean_draft(draft, materials), materials)
    assert visible_length(card) <= TELEGRAM_TEXT_MAX
    assert card.count("<a href=") == min(len(materials), CARD_MATERIALS_MAX)

    # X の反応(GH#415 の 3 本目)も上限いっぱいにする。検索した投稿を足して材料の一覧を上限まで
    # 埋め、材料の内訳の行も長い形にする
    news = [
        replace(materials[0], number=9 + i, item_id=None, kind="news", host="n.example")
        for i in range(4)
    ]
    posts = [
        replace(materials[0], number=13 + i, item_id=None, kind="searched_post", host="x.com",
                url=f"https://x.com/i/web/status/{i + 1}", title="😀" * 60)
        for i in range(20)
    ]
    reacted = draft.model_copy(
        update={
            "reactions": [
                InvestigationReaction(
                    stance="😀" * 200,
                    summary="😀" * 2000,
                    sources=list(range(13 + 5 * i, 18 + 5 * i)),
                )
                for i in range(3)
            ]
        }
    )
    # 反応の根拠(15 件)に加えて、本文の欄も検索した投稿を挙げる(1 行に並べるのは 15 件まで)
    reacted = reacted.model_copy(update={"coverage": full + "[9][10][11][12][30][31]"})
    every = materials + news + posts
    breakdown = SearchOutcome("ok", "q", 8, 100, "google_news")
    x = XOutcome("capped", cap_usd=12345.67)
    card = render_card(claimed, clean_draft(reacted, every), every, breakdown, x)
    assert card.count("・【") == 3
    assert visible_length(card) <= TELEGRAM_TEXT_MAX
    # 行ごとの材料は上限まで、検索した投稿は根拠の番号の上限まで 1 行に並べる
    assert card.count("<a href=") == CARD_MATERIALS_MAX + CARD_POSTS_MAX


def test_note_has_sections_materials_and_safe_markdown(conn) -> None:
    report, tv, post = seed_event(conn)
    conn.execute("UPDATE items SET title = 'A [x] (y)' WHERE id = ?", (report,))
    request(conn)
    claimed = claim_investigation(conn)
    materials = gather_materials(conn, claimed)
    draft = clean_draft(
        DRAFT.model_copy(update={"what_happened": "起きた ![x](https://evil.example/a.png) [1]"}),
        materials,
    )
    note = render_note(claimed, draft, materials, "2026-11-14T00:00:00Z")
    assert note.startswith("---\nkind: investigation\n")
    assert "event_index: 1" in note and "materials: 3" in note
    headings = (
        "何が起きたか", "経緯と原因", "影響", "報じ方の違い", "X の反応", "まだ分からないこと"
    )
    for heading in headings:
        assert f"\n## {heading}\n" in note
    assert "- 【不満】仕事が止まったという声。[3]" in note
    assert "1. [A \\[x\\] (y)](https://forest.example/m365)(forest.example・記事)" in note
    assert "3. [" in note and "(x.com・X の投稿)" in note
    # 外部の埋め込みは、開いただけで読みに行かないふつうのリンクに直す(GH#377)
    assert "起きた \\![x](https://evil.example/a.png)" in note


def test_generate_needs_material_with_body(conn, config) -> None:
    item = add_item(conn, "https://a.example/1", body="短い")
    add_digest(conn, ("A", "a。", [item]))
    request(conn)
    claimed = claim_investigation(conn)
    engine = DraftEngine()
    with pytest.raises(NoMaterialError):
        generate_investigation(config, factory(engine), claimed, gather_materials(conn, claimed))
    assert engine.prompts == []  # LLM を呼ばない


def test_generate_uses_dig_effort_and_names_note(conn, config) -> None:
    seed_event(conn)
    request(conn)
    claimed = claim_investigation(conn)
    engine = DraftEngine()
    generated = generate_investigation(
        config, factory(engine), claimed, gather_materials(conn, claimed)
    )
    assert engine.efforts == [config.dig.reasoning_effort]
    assert generated.note_name == (
        f"{today_jst().replace('-', '')}-調査{claimed.id}-Microsoft-365-で障害"
    )
    assert "🔎 調査「Microsoft 365 の長時間の障害」" in generated.card_text
    assert "\n・【不満】仕事が止まったという声。[3]\n" in generated.card_text


# --- intel dig-now の中での生成・公開・送信 ---


def test_dig_now_makes_investigation_note_and_card(conn, config) -> None:
    seed_event(conn)
    request(conn)
    api = FakeApi()

    assert run_dig_now(conn, config, factory(DraftEngine()), api, logger) is True

    row = status_of(conn)
    assert row["status"] == "done" and row["message_id"] == 901
    assert row["published_at"] is not None and row["note_markdown"] is None  # 公開したら本文は NULL
    [(chat_id, card)] = api.sent
    assert chat_id == 10 and card == row["card_text"]
    assert api.markups == [None]  # ボタンは付けない
    note = deepdive_dir(config) / f"{row['note_name']}.md"
    assert "kind: investigation" in note.read_text()
    assert not has_unsent_investigations(conn)


def test_failed_investigation_is_closed_and_notified(conn, config) -> None:
    seed_event(conn)
    request(conn)
    api = FakeApi()
    with pytest.raises(DigError, match="investigation 1: RuntimeError"):
        run_dig_now(conn, config, factory(DraftEngine(fail=True)), api, logger)
    row = status_of(conn)
    assert (row["status"], row["failure"]) == ("failed", "RuntimeError")
    assert api.sent == [
        (10, f"⚠️ {today_jst()} の今日の IT ニュースの 1 件目の調査を作れませんでした")
    ]


def test_investigation_without_material_is_closed_and_notified(conn, config) -> None:
    item = add_item(conn, "https://a.example/1", body=None)
    add_digest(conn, ("A", "a。", [item]))
    request(conn)
    api = FakeApi()
    run_dig_now(conn, config, factory(DraftEngine()), api, logger)  # 段の失敗にはしない
    assert status_of(conn)["failure"] == "no_material"
    [(_, text)] = api.sent
    assert "本文のある材料が無いため調査できませんでした" in text


def test_interrupted_investigation_is_not_redone(conn, config) -> None:
    seed_event(conn)
    request(conn)
    claim_investigation(conn)  # 前の実行が作り始めて止まった
    api = FakeApi()
    engine = DraftEngine()
    with pytest.raises(DigError, match="前の実行が途中で止まった"):
        run_dig_now(conn, config, factory(engine), api, logger)
    assert engine.prompts == []  # 作り直さない
    assert (status_of(conn)["status"], status_of(conn)["failure"]) == ("failed", "interrupted")
    assert "調査を作れませんでした" in api.sent[0][1]
    assert request(conn) == "accepted"  # 押し直せば受け付け直す


def test_unsent_investigation_card_is_sent_by_next_run(conn, config) -> None:
    seed_event(conn)
    request(conn)
    with pytest.raises(DigError, match="investigation 1: カード送信"):
        run_dig_now(conn, config, factory(DraftEngine()), FakeApi(fail=True), logger)
    assert has_unsent_investigations(conn)
    api = FakeApi()
    engine = DraftEngine()
    run_dig_now(conn, config, factory(engine), api, logger)
    assert engine.prompts == []  # 作り直さず、台帳のカードを送る
    assert len(api.sent) == 1 and status_of(conn)["message_id"] == 901


# --- 報道の検索(GDELT。GH#415 の 2 本目) ---


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        ('"Microsoft 365" outage', '"Microsoft 365" outage'),
        ("Microsoft OR Azure AND (outage) -AI sourcelang:japanese", "Microsoft Azure outage"),
        ("Microsoft -outage domain:x.com", "Microsoft"),  # 除外と絞り込みを語として送らない
        ('"Microsoft 365 outage', "Microsoft 365 outage"),  # 閉じていない引用符は外す
        ('"AI" "Gemini 4" 障害 Hydrogen', '"Gemini 4" Hydrogen'),  # 3 文字未満・ASCII 以外
        ("a b c", ""),
        ("one two three four five six seven eight", "one two three four five six"),
        ("x" * 120, ""),  # 1 語で上限を超える
        ("  NVIDIA\n Huang\tresigns ", "NVIDIA Huang resigns"),
    ],
)
def test_news_query_is_checked_before_sending(raw, cleaned) -> None:
    assert clean_news_query(raw) == cleaned


def test_search_window_is_utc_days_around_the_digest() -> None:
    late = datetime(2026, 11, 20, tzinfo=UTC)
    assert search_window("2026-11-14", late) == (
        datetime(2026, 11, 12, tzinfo=UTC),
        datetime(2026, 11, 15, 23, 59, 59, tzinfo=UTC),
    )
    now = datetime(2026, 11, 14, 3, 0, tzinfo=UTC)
    assert search_window("2026-11-14", now)[1] == now  # 今より先は探さない


def gdelt(url: str, title: str) -> GdeltArticle:
    return GdeltArticle(
        url=url, title=title, domain="", language="English", seen_at="20261113T180000Z"
    )


def search_inputs(conn, config, **investigate):
    seed_event(conn)
    request(conn)
    claimed = claim_investigation(conn)
    on = Config(
        config_dir=config.config_dir,
        obsidian=config.obsidian,
        telegram=config.telegram,
        dig=config.dig,
        investigate=InvestigateConfig(**({"news_articles": 3} | investigate)),
    )
    return on, claimed, gather_materials(conn, claimed)


def test_search_adds_distinct_reports_after_local_materials(conn, config) -> None:
    on, claimed, materials = search_inputs(conn, config)
    articles = [
        gdelt("https://forest.example/other", "手元と同じ host"),  # 手元の記事と同じ host
        gdelt("https://a.example/1", "Is Outlook down? Widespread outage"),
        gdelt("https://b.example/1", "Is Outlook down?  Widespread outage!"),  # 同じ配信記事
        gdelt("https://a.example/2", "同じ host の別の記事"),
        gdelt("https://c.example/1", "Microsoft reports progress"),
        gdelt("https://d.example/1", "Outage enters second day"),
        gdelt("https://e.example/1", "上限を超える 4 本目"),
    ]
    queries: list[str] = []

    def search(http, query, *, start, end):
        queries.append(query)
        return articles

    fetched = {"https://a.example/1": "A の本文", "https://d.example/1": None}
    found, outcome = search_news(
        on, object(), claimed, materials, PLAN,
        search=search, fetch=lambda http, url: fetched.get(url, "本文"),
    )
    assert queries == ['"Microsoft 365" outage']
    assert [(m.number, m.kind, m.host, m.item_id, m.body) for m in found] == [
        (4, "news", "a.example", None, "A の本文"),
        (5, "news", "c.example", None, "本文"),
        (6, "news", "d.example", None, ""),  # 本文が取れなければ題だけの材料
    ]
    assert found[0].published_at == "2026-11-13T18:00:00Z"
    assert outcome == SearchOutcome("ok", '"Microsoft 365" outage', 3, 3, "gdelt")


def test_search_skips_reprints_of_local_materials(conn, config) -> None:
    # 手元の記事の転載(別の host・同じ題)は検索の枠を使わない
    on, claimed, materials = search_inputs(conn, config, news_articles=1)
    found, outcome = search_news(
        on, object(), claimed, materials, PLAN,
        search=lambda *a, **k: [
            gdelt("https://reprint.example/1", "Microsoft 365 の障害"),  # 手元の [1] と同じ題
            gdelt("https://other.example/1", "別の報道"),
        ],
        fetch=lambda *a: "本文",
    )
    assert [m.host for m in found] == ["other.example"]


def test_search_skips_reprints_by_original_title_of_translated_local_article(conn, config):
    # 手元の記事に日本語の見出し(analyses.title_ja)が付いていても、英語の原題の転載を外す
    report = add_item(conn, "https://wire.example/m365", title="Microsoft outage drags on")
    conn.execute(
        "INSERT INTO analyses (item_id, title_ja, summary, category, relevance_score, novelty, "
        "recommendation_reason, implication, degraded, engine, model, prompt_version, input_hash) "
        "VALUES (?, 'マイクロソフトの障害が長引く', 's', 'その他', 50, 'new', 'r', 'i', 0, "
        "'codex-cli', 'm', 'p', 'h')",
        (report,),
    )
    add_digest(conn, ("障害", "長引く。", [report]))
    request(conn)
    claimed = claim_investigation(conn)
    materials = gather_materials(conn, claimed)
    assert (materials[0].title, materials[0].original_title) == (
        "マイクロソフトの障害が長引く", "Microsoft outage drags on"
    )
    on = Config(
        config_dir=config.config_dir, dig=config.dig,
        investigate=InvestigateConfig(news_articles=1),
    )
    found, _ = search_news(
        on, object(), claimed, materials, PLAN,
        search=lambda *a, **k: [
            gdelt("https://reprint.example/1", "Microsoft outage drags on"),
            gdelt("https://other.example/1", "Outage enters second day"),
        ],
        fetch=lambda *a: "本文",
    )
    assert [m.host for m in found] == ["other.example"]


def test_search_stops_fetching_after_the_time_budget(conn, config) -> None:
    on, claimed, materials = search_inputs(conn, config, news_fetch_budget_s=10)
    ticks = iter([0, 5, 11, 12])  # 期限の計算・1 本目・2 本目(期限を過ぎた)…
    fetched: list[str] = []

    def fetch(http, url):
        fetched.append(url)
        return "本文"

    found, _ = search_news(
        on, object(), claimed, materials, PLAN,
        search=lambda *a, **k: [gdelt("https://a.example/", "A"), gdelt("https://b.example/", "B")],
        headlines=lambda *a, **k: pytest.fail("GDELT で足りたら予備は使わない"),
        fetch=fetch, clock=lambda: next(ticks),
    )
    assert fetched == ["https://a.example/"]
    assert [m.body for m in found] == ["本文", ""]


def headline(url: str, title: str, outlet_host: str) -> NewsHeadline:
    return NewsHeadline(
        url=url, title=title, outlet="媒体", outlet_host=outlet_host,
        published_at="2026-11-13T20:43:24Z",
    )


def test_google_news_headlines_when_gdelt_fails(conn, config) -> None:
    from datetime import date

    on, claimed, materials = search_inputs(conn, config)
    sent: list[tuple] = []

    def search(http, query, *, start, end):
        raise GdeltError("HTTP 429")

    def headlines(http, query, *, after, before):
        sent.append((query, after, before))
        return [
            headline("https://news.google.com/a/A", "手元と同じ媒体", "forest.example"),
            headline("https://news.google.com/a/B", "マイクロソフト障害、復旧", "nikkei.example"),
            headline("https://news.google.com/a/C", "マイクロソフト障害、復旧", "asahi.example"),
            headline("https://news.google.com/a/D", "テクノロジー依存を痛感", "bbc.example"),
            headline("https://news.google.com/a/E", "媒体の host が無い", ""),
        ]

    found, outcome = search_news(
        on, object(), claimed, materials, PLAN,
        search=search, headlines=headlines, fetch=lambda *a: pytest.fail("見出しの本文は取らない"),
    )
    # Google ニュースの before はその日を含まないので、digest の日の 2 日後まで
    # (当日の報道を外さない)
    day = date.fromisoformat(today_jst())
    assert sent == [
        (
            "Microsoft 365 障害",
            day.fromordinal(day.toordinal() - 2),
            day.fromordinal(day.toordinal() + 2),
        )
    ]
    assert [(m.number, m.kind, m.host, m.url, m.body) for m in found] == [
        (4, "headline", "nikkei.example", "https://news.google.com/a/B", ""),
        (5, "headline", "bbc.example", "https://news.google.com/a/D", ""),
        (6, "headline", "news.google.com", "https://news.google.com/a/E", ""),
    ]
    assert outcome == SearchOutcome("ok", "Microsoft 365 障害", 3, 3, "google_news")


def test_google_news_when_gdelt_finds_nothing_usable(conn, config) -> None:
    on, claimed, materials = search_inputs(conn, config)
    found, outcome = search_news(
        on, object(), claimed, materials, PLAN,
        search=lambda *a, **k: [gdelt("https://forest.example/x", "手元と同じ host")],
        headlines=lambda *a, **k: [headline("https://news.google.com/a", "見出し", "n.example")],
        fetch=lambda *a: "本文",
    )
    assert [m.kind for m in found] == ["headline"] and outcome.engine == "google_news"


@pytest.mark.parametrize(
    ("plan", "gdelt_result", "google_result", "status", "query"),
    [
        (None, [], [], "failed", None),  # 語を作れない(LLM の失敗)
        # 検査で語が残らない
        (SearchPlan(news_query="AI :: OR", news_query_ja="x"), [], [], "failed", None),
        (PLAN, GdeltError("429"), GoogleNewsError("503"), "failed", "Microsoft 365 障害"),
        (PLAN, GdeltError("429"), [], "none", "Microsoft 365 障害"),  # 予備は答えたが見つからない
        (PLAN, [], GoogleNewsError("503"), "none", "Microsoft 365 障害"),  # GDELT は答えた
        # 日本語の語が無ければ予備は使わない
        (SearchPlan(news_query='"Microsoft 365"'), [], [], "none", '"Microsoft 365"'),
    ],
)
def test_search_failures_keep_local_materials_only(
    conn, config, plan, gdelt_result, google_result, status, query
):
    on, claimed, materials = search_inputs(conn, config)

    def answer(result):
        def call(*args, **kwargs):
            if isinstance(result, Exception):
                raise result
            return result

        return call

    found, outcome = search_news(
        on, object(), claimed, materials, plan,
        search=answer(gdelt_result), headlines=answer(google_result),
    )
    assert found == [] and outcome == SearchOutcome(status, query, 3, 0)


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        ("Microsoft 365 障害", "Microsoft 365 障害"),
        ('"マイクロソフト" OR 障害 site:x.com -噂 (復旧)', "マイクロソフト 障害 復旧"),
        ("a 障 365", "365"),  # 2 文字未満の語は捨てる
        # 記号を外した後も演算子を捨てる
        ("Microsoft (OR) 障害 [NOT] 「and」", "Microsoft 障害"),
        ("一 二三 四五 六七 八九 十一", "二三 四五 六七 八九"),  # 4 語まで
        ("あ" * 70, ""),  # 1 語で上限を超える
    ],
)
def test_ja_query_is_checked_before_sending(raw, cleaned) -> None:
    assert clean_ja_query(raw) == cleaned


def test_unexpected_errors_in_search_and_fetch_do_not_stop_investigation(conn, config) -> None:
    # 外部の応答の壊れ方(壊れたリダイレクトの URL の ValueError など)で調査全体を止めない
    on, claimed, materials = search_inputs(conn, config)

    def fetch(http, url):
        raise ValueError("Invalid IPv6 URL")

    found, outcome = search_news(
        on, object(), claimed, materials, PLAN,
        search=lambda *a, **k: [gdelt("https://a.example/1", "A")], fetch=fetch,
    )
    assert [(m.host, m.body) for m in found] == [("a.example", "")]  # 題だけの材料で続ける
    assert outcome.status == "ok"

    def broken(*args, **kwargs):
        raise ValueError("壊れた応答")

    found, outcome = search_news(
        on, object(), claimed, materials, PLAN,
        search=broken, headlines=broken,
    )
    assert found == [] and outcome.status == "failed"


def test_search_off_does_not_call_llm(conn, config) -> None:
    on, claimed, materials = search_inputs(conn, config, news_articles=0)
    engine = DraftEngine()
    # 報道も X も検索しないなら、語を作らない
    assert plan_search(on, factory(engine), claimed, materials, x=False) is None
    off_x = replace(on, investigate=replace(on.investigate, x_posts=0))
    assert plan_search(off_x, factory(engine), claimed, materials, x=True) is None
    found, outcome = search_news(
        on, object(), claimed, materials, PLAN,
        search=lambda *a, **k: pytest.fail("検索しない"),
    )
    assert (found, outcome, engine.plans) == ([], SearchOutcome("off", None, 3, 0), [])
    # X だけを読むときも語を作る
    assert plan_search(on, factory(engine), claimed, materials, x=True) == PLAN


def test_plan_failure_is_none(conn, config) -> None:
    on, claimed, materials = search_inputs(conn, config)
    assert plan_search(on, factory(DraftEngine(plan=None)), claimed, materials, x=True) is None


def test_query_prompt_fences_event_and_titles(conn, config) -> None:
    on, claimed, materials = search_inputs(conn, config)
    engine = DraftEngine()
    plan_search(on, factory(engine), claimed, materials, x=False)
    [prompt] = engine.plans
    nonce = prompt.split("<<<DATA-")[1].split("\n")[0]
    fenced = prompt.split(f"<<<DATA-{nonce}\n")[1].split(f"DATA-{nonce}>>>")[0]
    assert "見出し: Microsoft 365 で障害" in fenced
    assert "記事の題: Microsoft 365 の障害" in fenced
    assert engine.efforts == ["low"]


def test_card_and_note_show_material_breakdown(conn, config) -> None:
    seed_event(conn)
    request(conn)
    claimed = claim_investigation(conn)
    materials = gather_materials(conn, claimed)
    draft = clean_draft(DRAFT, materials)
    ok = SearchOutcome("ok", '"Microsoft 365" outage', 3, 2, "gdelt")
    assert render_card(claimed, draft, materials, ok).endswith(
        "\n\n材料: 手元 3 件・検索した報道 2 件"
    )
    google = SearchOutcome("ok", "Microsoft 365 障害", 3, 4, "google_news")
    assert render_card(claimed, draft, materials, google).endswith(
        "\n\n材料: 手元 3 件・検索した報道の見出し 4 件(Google ニュース)"
    )
    failed = SearchOutcome("failed", None, 3, 0)
    assert render_card(claimed, draft, materials, failed).endswith(
        "\n\n材料: 手元 3 件(報道の検索に失敗した。手元の材料だけで調べた)"
    )
    note = render_note(claimed, draft, materials, "2026-11-14T00:00:00Z", ok)
    assert "search: ok\n" in note and "materials_search: 2\n" in note
    assert "search_engine: GDELT\n" in note
    google_note = render_note(claimed, draft, materials, "2026-11-14T00:00:00Z", google)
    assert "search_engine: Google ニュース\n" in google_note
    assert "search_query: '\"Microsoft 365\" outage'" in note


GDELT_BODY = json.dumps(
    {
        "articles": [
            {"url": "https://wire.example/m365", "title": "Microsoft outage drags on",
             "seendate": "20261113T004500Z"},
            {"url": "https://tech.example/m365", "title": "Microsoft 365 caught a bug",
             "seendate": "20261113T180000Z"},
        ]
    }
)
ARTICLE_HTML = (
    "<html><body><article><p>" + "Exchange Online was down. " * 20 + "</p></article></body></html>"
)


GOOGLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>
<item><title>マイクロソフト365障害、20時間経て広範に復旧 - 日本経済新聞</title>
<link>https://news.google.com/rss/articles/CBMiAAA?oc=5</link>
<pubDate>Fri, 13 Nov 2026 20:43:24 GMT</pubDate>
<source url="https://www.nikkei.com">日本経済新聞</source></item>
</channel></rss>"""


def gdelt_http(status: int = 200, google_status: int = 200):
    from tests.test_extraction import make_http

    def handler(request):
        import httpx

        host = request.headers["host"]
        if host == "api.gdeltproject.org":
            return httpx.Response(status, text=GDELT_BODY)
        if host == "news.google.com":
            return httpx.Response(
                google_status, text=GOOGLE_RSS, headers={"content-type": "application/xml"}
            )
        return httpx.Response(200, text=ARTICLE_HTML, headers={"content-type": "text/html"})

    return make_http(handler)


def test_dig_now_investigation_uses_gdelt_reports(conn, config) -> None:
    seed_event(conn)
    request(conn)
    on = Config(
        config_dir=config.config_dir, obsidian=config.obsidian, telegram=config.telegram,
        dig=config.dig, investigate=InvestigateConfig(news_articles=5),
    )
    engine = DraftEngine()
    api = FakeApi()
    run_dig_now(conn, on, factory(engine), api, logger, http=gdelt_http())

    [prompt] = engine.prompts
    assert "[4] 種別: 報道(検索) | 媒体: wire.example | 題: Microsoft outage drags on" in prompt
    assert "[5] 種別: 報道(検索) | 媒体: tech.example" in prompt
    assert "Exchange Online was down." in prompt  # 検索した報道の本文を取って渡す
    [(_, card)] = api.sent
    assert card.endswith("材料: 手元 3 件・検索した報道 2 件")
    note = (deepdive_dir(on) / f"{status_of(conn)['note_name']}.md").read_text()
    assert "search: ok" in note and "(wire.example・報道(検索))" in note


def test_dig_now_investigation_falls_back_to_google_news(conn, config) -> None:
    seed_event(conn)
    request(conn)
    on = Config(
        config_dir=config.config_dir, obsidian=config.obsidian, telegram=config.telegram,
        dig=config.dig, investigate=InvestigateConfig(news_articles=5),
    )
    engine = DraftEngine(
        draft=DRAFT.model_copy(update={"coverage": "日経は復旧を伝えた[4]。"})
    )
    api = FakeApi()
    run_dig_now(conn, on, factory(engine), api, logger, http=gdelt_http(status=429))
    [prompt] = engine.prompts
    assert "[4] 種別: 報道の見出し(検索) | 媒体: nikkei.com | 題: マイクロソフト365障害" in prompt
    [(_, card)] = api.sent
    assert card.endswith("材料: 手元 3 件・検索した報道の見出し 1 件(Google ニュース)")
    assert '[4] <a href="https://news.google.com/rss/articles/CBMiAAA' in card


def test_dig_now_investigation_survives_broken_redirect_of_a_report(conn, config, caplog) -> None:
    # 記事が壊れた Location を返しても(urljoin の ValueError)、その記事を題だけにして作る
    from tests.test_extraction import make_http

    def handler(request):
        import httpx

        host = request.headers["host"]
        if host == "api.gdeltproject.org":
            return httpx.Response(200, text=GDELT_BODY)
        return httpx.Response(302, headers={"location": "https://["})

    seed_event(conn)
    request(conn)
    on = Config(
        config_dir=config.config_dir, obsidian=config.obsidian, telegram=config.telegram,
        dig=config.dig, investigate=InvestigateConfig(news_articles=5),
    )
    engine = DraftEngine()
    api = FakeApi()
    with caplog.at_level(logging.WARNING, logger=logger.name):
        run_dig_now(conn, on, factory(engine), api, logger, http=make_http(handler))
    assert status_of(conn)["status"] == "done"
    # 想定外の失敗は型だけをログに残す(URL や本文は書かない)
    assert "出来事の調査の報道の本文の取得で想定外の失敗: ValueError" in caplog.text
    assert "https://[" not in caplog.text
    [prompt] = engine.prompts
    assert "[4] 種別: 報道(検索) | 媒体: wire.example" in prompt
    assert api.sent[0][1].endswith("材料: 手元 3 件・検索した報道 2 件")


def test_dig_now_investigation_survives_every_search_failure(conn, config) -> None:
    seed_event(conn)
    request(conn)
    on = Config(
        config_dir=config.config_dir, obsidian=config.obsidian, telegram=config.telegram,
        dig=config.dig, investigate=InvestigateConfig(news_articles=5),
    )
    api = FakeApi()
    http = gdelt_http(status=400, google_status=404)
    run_dig_now(conn, on, factory(DraftEngine()), api, logger, http=http)
    assert status_of(conn)["status"] == "done"
    assert "報道の検索に失敗した" in api.sent[0][1]


# --- X の反応(GH#415 の 3 本目) ---


def x_post(number: int, text: str = "反応", created: str | None = "2026-11-13T08:00:00Z"):
    from app.connectors.x_search import XPost

    return XPost(f"https://x.com/i/web/status/{number}", text, created)


def x_reader(posts=(), *, reads: int | None = None, error: Exception | None = None, sent=None):
    from app.connectors.x_search import XPostsResult

    def read(query, *, start, end, max_results):
        if sent is not None:
            sent.append((query, start, end, max_results))
        if error is not None:
            raise error
        return XPostsResult(list(posts), len(posts) if reads is None else reads)

    return read


def x_ledger(conn) -> list[tuple]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT investigation_id, query, status, api_reads, estimated_cost_usd "
            "FROM investigation_x_reads ORDER BY id"
        )
    ]


def test_x_posts_are_added_after_materials_and_recorded(conn, config) -> None:
    on, claimed, materials = search_inputs(conn, config)
    sent: list[tuple] = []
    posts = [
        x_post(1, "手元と同じ投稿"),  # 手元の材料 [3] と同じ
        x_post(7, "仕事にならない\n朝から"),
        x_post(8, "長い" * 50, created=None),
        x_post(7, "同じ投稿が 2 回"),
        x_post(9, "仕事にならない 朝から!"),  # 同じ文の投稿(写し・bot の繰り返し)
        x_post(10, "仕事にならない"),  # 手元の投稿 [3] と同じ文
    ]
    found, outcome = search_x(
        conn, on, x_reader(posts, reads=8, sent=sent), claimed, materials, PLAN, logger=logger
    )
    [(query, start, end, max_results)] = sent
    assert (query, max_results) == ("Microsoft 365 障害", 20)
    assert start < end
    assert [(m.number, m.kind, m.host, m.item_id, m.url, m.body) for m in found] == [
        (4, "searched_post", "x.com", None, "https://x.com/i/web/status/7",
         "仕事にならない\n朝から"),
        (5, "searched_post", "x.com", None, "https://x.com/i/web/status/8", "長い" * 50),
    ]
    assert found[0].title == "仕事にならない 朝から"  # 本文の先頭を 1 行に
    assert len(found[1].title) == 60 and found[1].published_at is None
    assert outcome == XOutcome("ok", "Microsoft 365 障害", 2)
    # 課金は応答の件数で数える(手元と同じ投稿や、形の悪くて外した投稿も読んでいる)
    assert x_ledger(conn) == [(claimed.id, "Microsoft 365 障害", "ok", 8, 0.032)]


def test_x_posts_feed_reactions_from_searched_posts(conn, config) -> None:
    on, claimed, materials = search_inputs(conn, config)
    found, _ = search_x(conn, on, x_reader([x_post(7)]), claimed, materials, PLAN)
    draft = DRAFT.model_copy(
        update={
            "reactions": [
                InvestigationReaction(stance="不満", summary="声。", sources=[4]),
                InvestigationReaction(stance="記事", summary="記事の中身。", sources=[1]),
            ]
        }
    )
    cleaned = clean_draft(draft, materials + found)
    # 検索した投稿も X の反応の根拠になる(記事の中身は論調にしない)
    assert [(r.stance, r.sources) for r in cleaned.reactions] == [("不満", [4])]


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        ("Microsoft 365 障害", "Microsoft 365 障害"),
        ("from:someone lang:ja #障害 @MSFT365Status", "障害 MSFT365Status"),
        ("Microsoft OR (Outlook) -噂 復旧", "Microsoft Outlook 復旧"),
        ("一二 三四 五六 七八", "一二 三四 五六"),  # 3 語まで(AND で結ばれるので少なめ)
        ('"Microsoft 365" 障害', "Microsoft 365 障害"),  # 引用符は外す
        ("あ" * 70, ""),
    ],
)
def test_x_query_is_checked_before_sending(raw, cleaned) -> None:
    assert clean_x_query(raw) == cleaned


def test_x_window_is_the_news_window_within_the_last_seven_days() -> None:
    now = datetime(2026, 11, 14, 3, 0, tzinfo=UTC)
    assert x_window("2026-11-14", now) == (
        datetime(2026, 11, 12, tzinfo=UTC),
        now - timedelta(seconds=30),  # search/recent は要求の 10 秒以上前までしか読めない
    )
    later = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    start, end = x_window("2026-11-14", later)
    assert start == later - timedelta(days=7) + timedelta(minutes=10)  # 直近 7 日に狭める
    assert end == datetime(2026, 11, 15, 23, 59, 59, tzinfo=UTC)
    assert x_window("2026-11-14", datetime(2026, 11, 23, tzinfo=UTC)) is None  # 古い日


def test_x_is_skipped_when_the_monthly_cap_would_be_exceeded(conn, config) -> None:
    from app.investigate import reserve_x_read, settle_x_read

    on, claimed, materials = search_inputs(conn, config, x_monthly_cap_usd=0.2)
    now = datetime.now(UTC)
    last_month = now.replace(day=1) - timedelta(days=1)

    def read_on(when: datetime, reads: int) -> None:
        read_id = reserve_x_read(
            conn, claimed.id, searched_at=when, query="q", api_reads=reads, unit_price_usd=0.004
        )
        settle_x_read(
            conn, read_id, status="ok", api_reads=reads, unit_price_usd=0.004, settled_at=when
        )

    read_on(last_month, 100)  # 先月の分は数えない
    read_on(now, 30)  # 今月 $0.12。1 回分($0.08)を足すと $0.20 ちょうどで、まだ読める
    assert x_month_cost(conn, now) == pytest.approx(0.12)
    found, outcome = search_x(conn, on, x_reader([x_post(7)]), claimed, materials, PLAN)
    assert outcome.status == "ok" and x_month_cost(conn, now) == pytest.approx(0.124)
    # 次の 1 回分を足すと上限を超えるので、読まない(台帳にも残さない)
    found, outcome = search_x(
        conn, on, x_reader(error=AssertionError("読まない")), claimed, materials, PLAN
    )
    assert (found, outcome) == ([], XOutcome("capped", cap_usd=0.2))
    assert len(x_ledger(conn)) == 3


def test_x_read_is_reserved_before_sending_and_kept_when_interrupted(conn, config) -> None:
    # 送る前に上限の件数で予約の行を書く。送った後に実行が止まっても、上限の件数のまま月の合計に
    # 数えられる(上限を超えない)
    on, claimed, materials = search_inputs(conn, config)
    during: list[list[tuple]] = []

    def read(query, *, start, end, max_results):
        during.append(x_ledger(conn))
        raise KeyboardInterrupt  # 応答の前に実行が止まった

    with pytest.raises(KeyboardInterrupt):
        search_x(conn, on, read, claimed, materials, PLAN)
    assert during == [[(claimed.id, "Microsoft 365 障害", "pending", 20, 0.08)]]
    assert x_ledger(conn) == [(claimed.id, "Microsoft 365 障害", "pending", 20, 0.08)]
    assert x_month_cost(conn, datetime.now(UTC)) == pytest.approx(0.08)


def test_x_read_across_the_month_boundary_counts_in_the_month_it_was_sent(conn, config) -> None:
    # 予約の後に月が変わったら、応答を受けた月に数える。予約のまま止まった行は次の月にも数える
    # (Codex 指摘。送った月の上限から費用を漏らさない)
    on, claimed, materials = search_inputs(conn, config)
    end_of_nov = datetime(2026, 11, 30, 23, 59, 59, tzinfo=UTC)
    dec = datetime(2026, 12, 1, 0, 0, 5, tzinfo=UTC)
    times = iter([end_of_nov, dec])
    on_day = replace(claimed, digest_date="2026-11-30")
    found, outcome = search_x(
        conn, on, x_reader([x_post(7)]), on_day, materials, PLAN, clock=lambda: next(times)
    )
    assert outcome.status == "ok"
    assert conn.execute("SELECT searched_at FROM investigation_x_reads").fetchone()[0] == (
        "2026-12-01T00:00:05Z"
    )
    assert x_month_cost(conn, end_of_nov) == 0
    assert x_month_cost(conn, dec) == pytest.approx(0.004)

    def interrupted(query, *, start, end, max_results):
        raise KeyboardInterrupt

    conn.execute("DELETE FROM investigation_x_reads")
    with pytest.raises(KeyboardInterrupt):
        search_x(conn, on, interrupted, on_day, materials, PLAN, now=end_of_nov)
    assert x_month_cost(conn, end_of_nov) == pytest.approx(0.08)  # 予約した月
    assert x_month_cost(conn, dec) == pytest.approx(0.08)  # 次の月にも数える
    assert x_month_cost(conn, datetime(2027, 1, 1, tzinfo=UTC)) == 0  # 2 か月後には数えない


def test_x_failures_are_recorded_and_keep_local_materials(conn, config, caplog) -> None:
    from app.connectors.x_search import XPostsError

    on, claimed, materials = search_inputs(conn, config)
    found, outcome = search_x(
        conn, on, x_reader(error=XPostsError("HTTP 429")), claimed, materials, PLAN
    )
    assert (found, outcome) == ([], XOutcome("failed", "Microsoft 365 障害"))
    found, outcome = search_x(
        conn, on, x_reader(error=XPostsError("応答が JSON でない", api_reads=20)), claimed,
        materials, PLAN,
    )
    assert outcome.status == "failed"
    with caplog.at_level(logging.WARNING, logger=logger.name):
        found, outcome = search_x(
            conn, on, x_reader(error=ValueError("壊れた https://secret.example")), claimed,
            materials, PLAN, logger=logger,
        )
    assert outcome.status == "failed"
    # 想定外の失敗は型だけをログに残し、課金されたか分からないので上限の件数を数える
    assert "出来事の調査の X の検索で想定外の失敗: ValueError" in caplog.text
    assert "secret.example" not in caplog.text
    assert [(status, reads) for _, _, status, reads, _ in x_ledger(conn)] == [
        ("failed", 0), ("failed", 20), ("failed", 20)
    ]


def test_x_is_off_or_skipped_without_reader_plan_or_words(conn, config) -> None:
    on, claimed, materials = search_inputs(conn, config)
    never = x_reader(error=AssertionError("読まない"))
    assert search_x(conn, on, None, claimed, materials, PLAN) == ([], XOutcome("off"))
    off = replace(on, investigate=replace(on.investigate, x_posts=0))
    assert search_x(conn, off, never, claimed, materials, PLAN) == ([], XOutcome("off"))
    no_cap = replace(on, investigate=replace(on.investigate, x_monthly_cap_usd=0))
    assert search_x(conn, no_cap, never, claimed, materials, PLAN) == ([], XOutcome("off"))
    # 語を作れなかった
    assert search_x(conn, on, never, claimed, materials, None) == ([], XOutcome("failed"))
    # 検査で語が残らない
    plan = SearchPlan(news_query="x", x_query="from:a OR -b")
    assert search_x(conn, on, never, claimed, materials, plan) == ([], XOutcome("none"))
    assert x_ledger(conn) == []


def test_x_is_not_read_for_an_old_digest(conn, config) -> None:
    seed_event(conn)
    conn.execute("UPDATE digests SET digest_date = '2026-01-01'")
    request(conn, day="2026-01-01")
    claimed = claim_investigation(conn)
    on = replace(config, investigate=InvestigateConfig(news_articles=0))
    found, outcome = search_x(
        conn, on, x_reader(error=AssertionError("読まない")), claimed,
        gather_materials(conn, claimed), PLAN, now=datetime(2026, 11, 14, tzinfo=UTC),
    )
    assert (found, outcome) == ([], XOutcome("expired"))


def test_card_links_every_shown_reaction_number_first(conn, config) -> None:
    # 本文が引いた投稿より、X の反応に出す番号のリンクを先に取る(Codex 指摘)。反応の根拠は
    # 出す 5 件で切る
    on, claimed, materials = search_inputs(conn, config)
    found, _ = search_x(
        conn, on, x_reader([x_post(100 + i, f"投稿 {i}") for i in range(20)]), claimed,
        materials, PLAN,
    )
    assert [m.number for m in found] == list(range(4, 24))
    draft = DRAFT.model_copy(
        update={
            "what_happened": "障害が起きた[1][4]。",
            "reactions": [
                InvestigationReaction(stance="a", summary="s", sources=list(range(5, 10)) + [20]),
                InvestigationReaction(stance="b", summary="s", sources=list(range(10, 15))),
                InvestigationReaction(stance="c", summary="s", sources=list(range(15, 20))),
            ],
        }
    )
    cleaned = clean_draft(draft, materials + found)
    assert cleaned.reactions[0].sources == [5, 6, 7, 8, 9]  # 出す 5 件で切る
    card = render_card(claimed, cleaned, materials + found)
    line = next(text for text in card.split("\n") if text.startswith("X の投稿 "))
    linked = [int(n) for n in re.findall(r">\[(\d+)\]</a>", line)]
    assert linked == list(range(5, 20))  # 反応の 15 件で枠が埋まり、本文の [4] は入らない
    assert "[20]" not in card


def test_card_and_note_show_x_breakdown(conn, config) -> None:
    seed_event(conn)
    request(conn)
    claimed = claim_investigation(conn)
    materials = gather_materials(conn, claimed)
    draft = clean_draft(DRAFT, materials)
    google = SearchOutcome("ok", "Microsoft 365 障害", 3, 4, "google_news")
    failed = SearchOutcome("failed", None, 3, 0)
    off = SearchOutcome("off", None, 3, 0)

    def tail(search, x):
        return render_card(claimed, draft, materials, search, x).rsplit("\n", 1)[1]

    assert tail(google, XOutcome("ok", "q", 18)) == (
        "材料: 手元 3 件・検索した報道の見出し 4 件(Google ニュース)・X の投稿 18 件"
    )
    assert tail(off, XOutcome("none", "q", 0)) == "材料: 手元 3 件・X の投稿 0 件"
    assert tail(failed, XOutcome("ok", "q", 5)) == (
        "材料: 手元 3 件・X の投稿 5 件(報道の検索に失敗した)"
    )
    assert tail(google, XOutcome("capped", cap_usd=5.0)) == (
        "材料: 手元 3 件・検索した報道の見出し 4 件(Google ニュース)"
        "(X はこの機能の今月の上限($5)に達したので読まなかった)"
    )
    assert tail(failed, XOutcome("failed", "q")) == (
        "材料: 手元 3 件(報道の検索に失敗した。X の検索に失敗した。手元の材料だけで調べた)"
    )
    assert tail(off, XOutcome("expired")) == (
        "材料: 手元 3 件(X は検索できる直近 7 日の外なので読まなかった。手元の材料だけで調べた)"
    )
    assert tail(off, XOutcome("off")) == "材料: 手元 3 件"
    note = render_note(
        claimed, draft, materials, "2026-11-14T00:00:00Z", google, XOutcome("ok", "障害", 18)
    )
    assert "x_search: ok\n" in note and "materials_x: 18\n" in note
    assert "x_query: 障害\n" in note
    assert "・X の投稿 18 件" in note


X_PLAN_ONLY = InvestigateConfig(news_articles=0)


def test_dig_now_investigation_reads_x_reactions(conn, config) -> None:
    seed_event(conn)
    request(conn)
    on = replace(config, investigate=X_PLAN_ONLY)
    engine = DraftEngine(
        draft=DRAFT.model_copy(
            update={
                "reactions": [
                    InvestigationReaction(stance="不満", summary="仕事が止まった。", sources=[4])
                ]
            }
        )
    )
    api = FakeApi()
    sent: list[tuple] = []
    run_dig_now(
        conn, on, factory(engine), api, logger,
        x_read=x_reader([x_post(7, "朝から Outlook が開かない")], sent=sent),
    )
    assert status_of(conn)["status"] == "done"
    assert engine.efforts[0] == "low" and len(engine.plans) == 1  # 語は X のためにも作る
    [prompt] = engine.prompts
    assert "[4] 種別: X の投稿(検索) | 媒体: x.com | 題: 朝から Outlook が開かない" in prompt
    [(_, card)] = api.sent
    assert "・【不満】仕事が止まった。[4]" in card
    # 検索した投稿は、番号のリンクを 1 行に並べる
    assert '\nX の投稿 <a href="https://x.com/i/web/status/7">[4]</a>\n' in card
    assert '[4] <a href="https://x.com/i/web/status/7">' not in card  # 行ごとには並べない
    assert card.endswith("材料: 手元 3 件・X の投稿 1 件")
    assert [row[2:4] for row in x_ledger(conn)] == [("ok", 1)]
    note = (deepdive_dir(on) / f"{status_of(conn)['note_name']}.md").read_text()
    assert "x_search: ok" in note and "materials_x: 1" in note


def test_dig_now_investigation_without_x_reader_does_not_plan(conn, config) -> None:
    # 報道の検索が切れていて X も読めないなら、語を作らない(今までと同じ)
    seed_event(conn)
    request(conn)
    engine = DraftEngine()
    api = FakeApi()
    run_dig_now(conn, config, factory(engine), api, logger)
    assert engine.plans == [] and api.sent[0][1].endswith("材料: 手元 3 件")
