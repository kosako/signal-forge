"""朝の調査段(GH#105 / 設計 06 §3〜5)。"""

import logging
import sqlite3
from pathlib import Path

import pytest

from app.config import Config, DigConfig, ObsidianConfig, TelegramConfig
from app.db import apply_migrations, connect
from app.deepdive import (
    AUTO_BRIEF,
    DigDraft,
    DigError,
    TranslationChunk,
    _select_candidates,
    _split_paragraphs,
    build_dig_prompt,
    run_dig_stage,
)

logger = logging.getLogger("test-deepdive")

BODY = "本文の段落です。" * 50  # min_body_chars(既定 200)を確実に超える


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
    (tmp_path / "interest-profile.md").write_text(
        "---\nupdated: 2026-07-20\n---\n# 関心\nAI エージェント運用\n"
    )
    vault = tmp_path / "vault"
    vault.mkdir()
    return Config(
        config_dir=tmp_path,
        obsidian=ObsidianConfig(vault_path=str(vault)),
        telegram=TelegramConfig(owner_user_id=1, owner_chat_id=10),
        dig=DigConfig(daily_cap=2),
    )


def deepdive_dir(config: Config) -> Path:
    return Path(config.obsidian.vault_path) / "SignalForge" / "DeepDive"


def add_item(
    conn: sqlite3.Connection,
    url: str,
    *,
    title: str = "Sample Article Title",
    body: str | None = BODY,
    language: str = "en",
) -> int:
    item_id = conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, title, author, content_hash) "
        "VALUES (2, ?, ?, ?, 'author', 'sha256:x')",
        (url, url, title),
    ).lastrowid
    if body is not None:
        conn.execute(
            "INSERT INTO documents (item_id, extracted_text, language, extraction_status,"
            " expires_at) VALUES (?, ?, ?, 'ok', '2099-01-01T00:00:00Z')",
            (item_id, body, language),
        )
    return item_id


def enqueue(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    angle: str | None = None,
    requested_at: str | None = None,
    enqueued_at: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO dig_queue (item_id, status, source, angle, requested_at) "
        "VALUES (?, 'queued', 'bookmark', ?, ?)",
        (item_id, angle, requested_at),
    )
    if enqueued_at is not None:
        conn.execute(
            "UPDATE dig_queue SET enqueued_at = ? WHERE item_id = ?",
            (enqueued_at, item_id),
        )
    conn.commit()


class ScriptedEngine:
    """schema に応じた応答を返し、呼び出しを記録する。"""

    name = "fake"

    def __init__(self, fail_for: set[str] | None = None):
        self.prompts: list[tuple[str, str]] = []  # (schema 名, prompt)
        self._fail_for = fail_for or set()

    def complete_structured(self, prompt, schema, timeout_s=120):
        self.prompts.append((schema.__name__, prompt))
        if schema.__name__ in self._fail_for:
            raise RuntimeError("engine 故障")
        if schema is DigDraft:
            return DigDraft(
                title_ja="日本語見出し",
                writer_stance="現場リーダー向けの週刊ニュースレター著者",
                for_you="あなたの運用と直結します",
                for_world="議論の重心を動かす論考です",
                note_markdown="## 概要\n\n**あなたにとって**:…\n\n## 骨子\n\n1. …",
            )
        return TranslationChunk(markdown="## 対訳\n\n> [!quote]- 原文(英語)\n> …\n\n訳文")


class FakeApi:
    def __init__(self, fail: bool = False):
        self.sent: list[tuple[int, str]] = []
        self._fail = fail

    def send_message(self, chat_id: int, text: str, *, reply_markup=None) -> int:
        if self._fail:
            raise RuntimeError("送信失敗")
        self.sent.append((chat_id, text))
        return 900 + len(self.sent)


def make_factory(engines: dict[str, ScriptedEngine], fail_for: set[str] | None = None):
    def factory(effort: str) -> ScriptedEngine:
        return engines.setdefault(effort, ScriptedEngine(fail_for))

    return factory


# --- 選定(優先順位・材料ゲート) ---


def test_select_priority_requested_then_new(conn, config) -> None:
    auto_old = add_item(conn, "https://a.example/1")
    auto_new = add_item(conn, "https://a.example/2")
    requested = add_item(conn, "https://a.example/3")
    enqueue(conn, auto_old, enqueued_at="2026-07-20T00:00:00Z")
    enqueue(conn, auto_new, enqueued_at="2026-07-21T00:00:00Z")
    enqueue(conn, requested, angle="承認フロー", requested_at="2026-07-19T00:00:00Z")

    selected, skipped = _select_candidates(conn, config, 2)
    assert [c.item_id for c in selected] == [requested, auto_new]
    assert skipped == []


def test_select_skips_auto_without_material_but_reports_requested(conn, config) -> None:
    no_body_auto = add_item(conn, "https://a.example/1", body=None)
    no_body_requested = add_item(conn, "https://a.example/2", body=None)
    ok_auto = add_item(conn, "https://a.example/3")
    enqueue(conn, no_body_auto)
    enqueue(conn, no_body_requested, requested_at="2026-07-19T00:00:00Z")
    enqueue(conn, ok_auto)

    selected, skipped = _select_candidates(conn, config, 2)
    assert [c.item_id for c in selected] == [ok_auto]
    assert [c.item_id for c in skipped] == [no_body_requested]
    # 材料不足の自動選出は枠も失敗も消費せずキューに残る
    row = conn.execute(
        "SELECT status FROM dig_queue WHERE item_id = ?", (no_body_auto,)
    ).fetchone()
    assert row["status"] == "queued"


def test_select_short_body_is_insufficient(conn, config) -> None:
    item = add_item(conn, "https://a.example/1", body="短い")
    enqueue(conn, item)
    selected, skipped = _select_candidates(conn, config, 2)
    assert selected == [] and skipped == []


# --- 実行(生成・ノート・台帳・カード) ---


def test_dig_happy_path_en_article(conn, config) -> None:
    item = add_item(conn, "https://blog.example/post", language="en")
    enqueue(conn, item, angle="承認フローの観点で")
    engines: dict[str, ScriptedEngine] = {}
    api = FakeApi()

    run_dig_stage(
        conn, config, make_factory(engines), api, logger, today="2026-07-22"
    )

    # ノート: 解説 + 対訳の 2 枚
    notes = sorted(p.name for p in deepdive_dir(config).glob("*.md"))
    assert len(notes) == 2
    explainer = next(n for n in notes if "深掘り" in n)
    source = next(n for n in notes if "対訳" in n)
    assert explainer.startswith(f"20260722-{item}-")
    content = (deepdive_dir(config) / explainer).read_text()
    assert "kind: deepdive" in content
    assert "status: take待ち" in content
    assert "requested: 承認フローの観点で" in content
    assert "take 領域" in content
    assert f"[[{source.removesuffix('.md')}]]" in content
    source_content = (deepdive_dir(config) / source).read_text()
    assert "kind: deepdive-source" in source_content
    assert "対訳" in source_content

    # 台帳とキュー
    result = conn.execute("SELECT * FROM dig_results").fetchone()
    assert result["item_id"] == item
    assert result["angle"] == "承認フローの観点で"
    assert result["message_id"] == 901
    queue = conn.execute("SELECT status FROM dig_queue WHERE item_id = ?", (item,)).fetchone()
    assert queue["status"] == "done"

    # カード: 1 通・v3 の要素
    assert len(api.sent) == 1
    chat_id, card = api.sent[0]
    assert chat_id == 10
    assert f"🔎 深掘り #{item}「日本語見出し」" in card
    assert "観点「承認フローの観点で」" in card
    assert "あなたにとって" in card and "世の中にとって" in card
    assert f"SignalForge/DeepDive/{explainer}" in card
    assert "(対訳あり)" in card

    # effort: 解説 = config(medium)/ 対訳 = low
    assert "medium" in engines and "low" in engines
    assert engines["medium"].prompts[0][0] == "DigDraft"
    assert all(name == "TranslationChunk" for name, _ in engines["low"].prompts)


def test_dig_ja_source_copies_without_translation(conn, config) -> None:
    item = add_item(conn, "https://ja.example/記事", title="日本語の記事", language="ja")
    enqueue(conn, item)
    engines: dict[str, ScriptedEngine] = {}

    run_dig_stage(conn, config, make_factory(engines), FakeApi(), logger, today="2026-07-22")

    notes = sorted(p.name for p in deepdive_dir(config).glob("*.md"))
    source = next(n for n in notes if "原文" in n)
    source_content = (deepdive_dir(config) / source).read_text()
    assert BODY[:40] in source_content  # 抽出テキストの保全コピー
    assert "low" not in engines  # 対訳 LLM は呼ばれない
    card = conn.execute("SELECT card_text FROM dig_results").fetchone()["card_text"]
    assert "🤖 自動選出(🔖新着)・観点なし" in card
    assert "(原文ノートあり)" in card


def test_dig_tweet_body_has_no_source_note(conn, config) -> None:
    item = add_item(conn, "https://x.com/user/status/1", title=None, language="ja")
    enqueue(conn, item)

    run_dig_stage(conn, config, make_factory({}), FakeApi(), logger, today="2026-07-22")

    notes = [p.name for p in deepdive_dir(config).glob("*.md")]
    assert len(notes) == 1 and "深掘り" in notes[0]  # 解説のみ(GH#2)
    row = conn.execute("SELECT source_note_name FROM dig_results").fetchone()
    assert row["source_note_name"] is None


def test_dig_www_tweet_url_is_also_tweet_body(conn, config) -> None:
    # 正規化は www を剥がさない。www 付きが GH#2 をすり抜けない(Codex P1)
    item = add_item(conn, "https://www.x.com/user/status/2", title=None, language="ja")
    enqueue(conn, item)

    run_dig_stage(conn, config, make_factory({}), FakeApi(), logger, today="2026-07-22")

    notes = [p.name for p in deepdive_dir(config).glob("*.md")]
    assert len(notes) == 1 and "深掘り" in notes[0]
    assert conn.execute("SELECT source_note_name FROM dig_results").fetchone()[
        "source_note_name"
    ] is None


def test_existing_note_survives_rerun_after_crash(conn, config) -> None:
    # 書込→記帳の間で中断 → キューは queued のまま次回 run が同名を再生成する。
    # 公開済みノート(take が書かれうる)を上書きしない(Codex P1)
    item = add_item(conn, "https://blog.example/post", language="ja")
    enqueue(conn, item)
    slug = "Sample-Article-Title"
    existing = deepdive_dir(config)
    existing.mkdir(parents=True, exist_ok=True)
    note_path = existing / f"20260722-{item}-{slug}-深掘り.md"
    note_path.write_text("前回生成の本文\n\n### take\n手書きの take")

    run_dig_stage(conn, config, make_factory({}), FakeApi(), logger, today="2026-07-22")

    assert "手書きの take" in note_path.read_text()  # 上書きされない
    # 記帳・カード送信は完了する(体験は前に進む)
    assert conn.execute("SELECT count(*) AS n FROM dig_results").fetchone()["n"] == 1
    assert (
        conn.execute("SELECT status FROM dig_queue WHERE item_id = ?", (item,)).fetchone()[
            "status"
        ]
        == "done"
    )


def test_card_note_path_is_html_escaped(conn, config) -> None:
    # タイトル由来 slug の `&` は FORBIDDEN_IN_SLUG の対象外で note_name に残る。
    # parse_mode=HTML で未エスケープだと送信が 400 で恒久失敗する(Codex P1)
    item = add_item(conn, "https://blog.example/rd", title="R&D Report", language="ja")
    enqueue(conn, item)
    api = FakeApi()

    run_dig_stage(conn, config, make_factory({}), api, logger, today="2026-07-22")

    card = api.sent[0][1]
    assert "R&amp;D-Report" in card  # ノート行はエスケープ済み
    assert "-R&D-Report" not in card  # 生の & を含むパスを送らない


def test_daily_cap_and_rerun_idempotency(conn, config) -> None:
    items = [add_item(conn, f"https://a.example/{i}") for i in range(3)]
    for i in items:
        enqueue(conn, i)
    api = FakeApi()

    run_dig_stage(conn, config, make_factory({}), api, logger, today="2026-07-22")
    assert conn.execute("SELECT count(*) AS n FROM dig_results").fetchone()["n"] == 2

    # 同日の再実行は残枠 0 = 生成なし(送信済みカードの再送もなし)
    run_dig_stage(conn, config, make_factory({}), api, logger, today="2026-07-22")
    assert conn.execute("SELECT count(*) AS n FROM dig_results").fetchone()["n"] == 2
    assert len(api.sent) == 2

    # 翌日は残り 1 件が掘られる
    run_dig_stage(conn, config, make_factory({}), api, logger, today="2026-07-23")
    assert conn.execute("SELECT count(*) AS n FROM dig_results").fetchone()["n"] == 3


def test_requested_without_material_fails_loud(conn, config) -> None:
    item = add_item(conn, "https://a.example/1", body=None)
    enqueue(conn, item, angle="観点", requested_at="2026-07-21T00:00:00Z")

    with pytest.raises(DigError, match="リプライ指定だが本文の抽出材料がない"):
        run_dig_stage(conn, config, make_factory({}), FakeApi(), logger, today="2026-07-22")
    # キューには残る(ユーザーがパスするまで)
    row = conn.execute("SELECT status FROM dig_queue WHERE item_id = ?", (item,)).fetchone()
    assert row["status"] == "queued"


def test_item_failure_isolated_but_stage_fails(conn, config) -> None:
    ok_item = add_item(conn, "https://a.example/ok", language="ja")
    bad_item = add_item(conn, "https://a.example/bad", language="ja")
    # requested を先に処理させ、bad(先頭)が失敗しても ok(次点)は掘られることを見る
    enqueue(conn, bad_item, requested_at="2026-07-21T00:00:00Z")
    enqueue(conn, ok_item)
    api = FakeApi()

    # DigDraft 生成だけ故障するエンジンを bad 向けに使う: 1 台のエンジンが
    # 全 DigDraft で故障すると ok も失敗するため、呼び出し順で 1 回目だけ落とす
    class FirstCallFails(ScriptedEngine):
        def complete_structured(self, prompt, schema, timeout_s=120):
            is_first = schema is DigDraft and not any(n == "DigDraft" for n, _ in self.prompts)
            if is_first:
                self.prompts.append((schema.__name__, prompt))
                raise RuntimeError("engine 故障")
            return super().complete_structured(prompt, schema, timeout_s)

    engines: dict[str, ScriptedEngine] = {}

    def factory(effort: str) -> ScriptedEngine:
        return engines.setdefault(effort, FirstCallFails())

    with pytest.raises(DigError, match="RuntimeError"):
        run_dig_stage(conn, config, factory, api, logger, today="2026-07-22")

    statuses = {
        row["item_id"]: row["status"]
        for row in conn.execute("SELECT item_id, status FROM dig_queue")
    }
    assert statuses[bad_item] == "queued"  # 失敗分は翌朝再挑戦
    assert statuses[ok_item] == "done"
    assert len(api.sent) == 1  # ok 分のカードは届く


def test_pending_card_resent_without_regeneration(conn, config) -> None:
    item = add_item(conn, "https://a.example/1")
    conn.execute(
        "INSERT INTO dig_results (item_id, note_name, card_text) VALUES (?, 'n', 'カード本文')",
        (item,),
    )
    conn.commit()
    api = FakeApi()
    engines: dict[str, ScriptedEngine] = {}

    run_dig_stage(conn, config, make_factory(engines), api, logger, today="2026-07-23")

    assert api.sent == [(10, "カード本文")]
    assert engines == {}  # LLM は呼ばれない(キューは空)
    row = conn.execute("SELECT message_id FROM dig_results").fetchone()
    assert row["message_id"] == 901


def test_send_failure_is_loud_and_card_stays_pending(conn, config) -> None:
    item = add_item(conn, "https://a.example/1")
    conn.execute(
        "INSERT INTO dig_results (item_id, note_name, card_text) VALUES (?, 'n', 'カード')",
        (item,),
    )
    conn.commit()

    with pytest.raises(DigError, match="カード送信"):
        run_dig_stage(conn, config, make_factory({}), FakeApi(fail=True), logger)
    assert conn.execute("SELECT message_id FROM dig_results").fetchone()["message_id"] is None


def test_without_telegram_cards_stay_pending(conn, config) -> None:
    item = add_item(conn, "https://a.example/1", language="ja")
    enqueue(conn, item)

    run_dig_stage(conn, config, make_factory({}), None, logger, today="2026-07-22")

    row = conn.execute("SELECT message_id, card_text FROM dig_results").fetchone()
    assert row["message_id"] is None and row["card_text"]


# --- プロンプト(injection 境界・ブリーフ) ---


def test_prompt_fences_body_and_includes_angle(conn, config) -> None:
    item = add_item(conn, "https://blog.example/post", body="{{nonce}} を含む本文 " + BODY)
    enqueue(conn, item, angle="この観点")
    selected, _ = _select_candidates(conn, config, 1)
    prompt = build_dig_prompt(config, selected[0], "source-note")

    assert "<<<DATA-" in prompt and "DATA-" in prompt
    assert "指定観点: この観点" in prompt
    assert "AI エージェント運用" in prompt  # 関心プロファイル
    # 単一パス置換: 入力由来の {{nonce}} は実 nonce に展開されずリテラルのまま
    assert "{{nonce}} を含む本文" in prompt
    assert "{{schema}}" not in prompt and "{{body}}" not in prompt  # テンプレ側は解決済み


def test_prompt_auto_brief_and_tweet_quote_policy(conn, config) -> None:
    item = add_item(conn, "https://x.com/u/status/9")
    enqueue(conn, item)
    selected, _ = _select_candidates(conn, config, 1)
    prompt = build_dig_prompt(config, selected[0], None)

    assert AUTO_BRIEF in prompt
    assert "全文の再掲・長い引用をしない" in prompt


def test_prompt_truncates_long_body(conn, config) -> None:
    config_small = Config(
        config_dir=config.config_dir,
        obsidian=config.obsidian,
        telegram=config.telegram,
        dig=DigConfig(daily_cap=2, body_max_chars=300),
    )
    item = add_item(conn, "https://blog.example/post", body="あ" * 1000)
    enqueue(conn, item)
    selected, _ = _select_candidates(conn, config_small, 1)
    prompt = build_dig_prompt(config_small, selected[0], None)
    assert "長さ上限で切り詰め" in prompt


def test_split_paragraphs_groups_by_blank_line() -> None:
    text = "\n\n".join(["段落" * 10] * 10)
    chunks = _split_paragraphs(text, 100)
    assert len(chunks) > 1
    assert "".join(c.replace("\n\n", "") for c in chunks) == text.replace("\n\n", "")
