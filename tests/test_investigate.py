"""出来事の調査(GH#415。設計 06 §9)。"""

import json
import logging
import sqlite3
from dataclasses import replace
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
    InvestigationDraft,
    InvestigationReaction,
    NoMaterialError,
    SearchOutcome,
    SearchPlan,
    build_prompt,
    claim_investigation,
    clean_draft,
    clean_ja_query,
    clean_news_query,
    event_of_digest,
    gather_materials,
    generate_investigation,
    has_unsent_investigations,
    render_card,
    render_note,
    request_investigation,
    search_news,
    search_window,
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
    from datetime import UTC, datetime

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
        on, factory(DraftEngine()), object(), claimed, materials,
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
        on, factory(DraftEngine()), object(), claimed, materials,
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
        on, factory(DraftEngine()), object(), claimed, materials,
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
        on, factory(DraftEngine()), object(), claimed, materials,
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
        on, factory(DraftEngine()), object(), claimed, materials,
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
        on, factory(DraftEngine()), object(), claimed, materials,
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
        on, factory(DraftEngine(plan=plan)), object(), claimed, materials,
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
        on, factory(DraftEngine()), object(), claimed, materials,
        search=lambda *a, **k: [gdelt("https://a.example/1", "A")], fetch=fetch,
    )
    assert [(m.host, m.body) for m in found] == [("a.example", "")]  # 題だけの材料で続ける
    assert outcome.status == "ok"

    def broken(*args, **kwargs):
        raise ValueError("壊れた応答")

    found, outcome = search_news(
        on, factory(DraftEngine()), object(), claimed, materials,
        search=broken, headlines=broken,
    )
    assert found == [] and outcome.status == "failed"


def test_search_off_does_not_call_llm(conn, config) -> None:
    on, claimed, materials = search_inputs(conn, config, news_articles=0)
    engine = DraftEngine()
    found, outcome = search_news(
        on, factory(engine), object(), claimed, materials,
        search=lambda *a, **k: pytest.fail("検索しない"),
    )
    assert (found, outcome, engine.plans) == ([], SearchOutcome("off", None, 3, 0), [])


def test_query_prompt_fences_event_and_titles(conn, config) -> None:
    on, claimed, materials = search_inputs(conn, config)
    engine = DraftEngine()
    search_news(
        on, factory(engine), object(), claimed, materials,
        search=lambda *a, **k: [], headlines=lambda *a, **k: [],
    )
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
    assert "報道の検索に失敗したので、手元の材料だけで調べた" in render_card(
        claimed, draft, materials, failed
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
