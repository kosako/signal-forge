"""出来事の調査(GH#415。設計 06 §9)。"""

import json
import logging
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from app.config import Config, DigConfig, InvestigateConfig, ObsidianConfig, TelegramConfig
from app.db import apply_migrations, connect
from app.deepdive import DigError, run_dig_now
from app.delivery.telegram import visible_length
from app.investigate import (
    CARD_MATERIALS_MAX,
    InvestigationDraft,
    InvestigationReaction,
    NoMaterialError,
    build_prompt,
    claim_investigation,
    clean_draft,
    event_of_digest,
    gather_materials,
    generate_investigation,
    has_unsent_investigations,
    render_card,
    render_note,
    request_investigation,
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


class DraftEngine:
    name = "fake"

    def __init__(self, draft: InvestigationDraft = DRAFT, fail: bool = False):
        self.prompts: list[str] = []
        self.efforts: list[str] = []
        self._draft = draft
        self._fail = fail

    def complete_structured(self, prompt, schema, timeout_s=120):
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
        title_ja="題\n2 行目",
        what_happened="起きた[1, 9]。続き[2、3]。",
        background="原因[7]。",
        impact="影響[2]",
        coverage="違い[1][1]",
        reactions=[
            InvestigationReaction(stance="記事の論調", summary="記事だけ[1]。", sources=[1, 2]),
            InvestigationReaction(stance="不満", summary="声[3][8]。", sources=[3, 3, 9]),
            InvestigationReaction(stance=" ", summary="空", sources=[3]),
        ],
        unknowns=["a", " ", "b", "c", "d"],
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
