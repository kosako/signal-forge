import json
import logging
import sqlite3
from pathlib import Path

import httpx
import pytest

from app.config import Config, TelegramConfig
from app.db import apply_migrations, connect
from app.delivery import DeliveryError, TelegramApi, create_api, deliver_digest
from app.delivery.telegram import ITEM_TEXT_MAX_CHARS, escape_attr, escape_text
from app.digest.builder import today_jst

logger = logging.getLogger("test-delivery")

CHAT_ID = 777


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'TechCrunch')")  # id=2
    yield conn
    conn.close()


def make_config(tmp_path: Path, *, chat_id: int | None = CHAT_ID) -> Config:
    return Config(config_dir=tmp_path, telegram=TelegramConfig(owner_chat_id=chat_id))


def add_item(
    conn,
    url,
    *,
    title="記事タイトル",
    title_ja=None,
    published_at="2026-07-04T10:00:00Z",
    summary="要約テキスト。",
    implication="示唆テキスト。",
) -> int:
    item_id = conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, title, published_at, content_hash, "
        "status) VALUES (2, ?, ?, ?, ?, 'sha256:x', 'digested')",
        (url, url, title, published_at),
    ).lastrowid
    conn.execute(
        "INSERT INTO analyses (item_id, title_ja, summary, category, relevance_score, novelty, "
        "recommendation_reason, implication, engine, model, prompt_version, input_hash) "
        "VALUES (?, ?, ?, '課題直結', 80, 'new', '理由', ?, 'codex-cli', 'm', 'analyze-v001', ?)",
        (item_id, title_ja, summary, implication, f"sha256:{url}"),
    )
    return item_id


def add_digest(conn, item_ids, *, status="ok") -> int:
    return conn.execute(
        "INSERT INTO digests (digest_date, markdown, item_ids, item_count, status) "
        "VALUES (?, '# md', ?, ?, ?)",
        (today_jst(), json.dumps(item_ids), len(item_ids), status),
    ).lastrowid


class ApiRecorder:
    """sendMessage への実 HTTP を MockTransport で受け、payload を記録する。"""

    def __init__(self, fail_statuses: list[int] | None = None):
        self.requests: list[dict] = []
        self._fail_statuses = fail_statuses or []
        self._next_message_id = 100

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = dict(httpx.QueryParams(request.content.decode()))
        self.requests.append(payload)
        if self._fail_statuses:
            status = self._fail_statuses.pop(0)
            if status == 429:
                return httpx.Response(
                    429, json={"ok": False, "description": "Too Many Requests",
                               "parameters": {"retry_after": 7}},
                )
            return httpx.Response(status, json={"ok": False, "description": f"err {status}"})
        self._next_message_id += 1
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": self._next_message_id}}
        )

    def api(self, sleeps: list[float] | None = None) -> TelegramApi:
        sleep = sleeps.append if sleeps is not None else (lambda _s: None)
        return TelegramApi(
            "test-token", transport=httpx.MockTransport(self.handler), sleep=sleep
        )

    def texts(self) -> list[str]:
        return [r["text"] for r in self.requests]


def test_delivers_header_and_items_and_marks_sent(conn, tmp_path) -> None:
    a = add_item(conn, "https://a/1", title="タイトルA")
    b = add_item(conn, "https://a/2", title="タイトルB")
    digest_id = add_digest(conn, [a, b])
    recorder = ApiRecorder()

    sent = deliver_digest(conn, make_config(tmp_path), recorder.api(), digest_id, logger)

    assert sent == 3  # ヘッダ1 + アイテム2
    texts = recorder.texts()
    assert "のダイジェスト — 2件" in texts[0]
    assert "1. <b>タイトルA</b>" in texts[1]
    assert "2. <b>タイトルB</b>" in texts[2]
    assert all(r["chat_id"] == str(CHAT_ID) for r in recorder.requests)
    row = conn.execute("SELECT delivery_status, sent_at FROM digests").fetchone()
    assert row["delivery_status"] == "sent"
    assert row["sent_at"] is not None
    ledger = conn.execute(
        "SELECT item_id, message_id FROM digest_messages ORDER BY id"
    ).fetchall()
    assert [(r["item_id"], r["message_id"]) for r in ledger] == [
        (None, 101), (a, 102), (b, 103)
    ]


def test_item_message_format_source_link_date_summary_implication(conn, tmp_path) -> None:
    a = add_item(
        conn, "https://a/1",
        title="タイトル", published_at="2026-07-04T10:00:00Z",
        summary="要約1文目。", implication="再提案の材料になる。",
    )
    digest_id = add_digest(conn, [a])
    recorder = ApiRecorder()
    deliver_digest(conn, make_config(tmp_path), recorder.api(), digest_id, logger)

    text = recorder.texts()[1]
    assert '<a href="https://a/1">TechCrunch</a> ・7/4' in text
    assert "要約1文目。" in text
    assert text.endswith("💡 再提案の材料になる。")
    # 4ボタン InlineKeyboard(callback_data は設計 04 §3 の形式・64バイト以内)
    markup = json.loads(recorder.requests[1]["reply_markup"])
    buttons = markup["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == [
        f"s:{a}:up", f"s:{a}:dig", f"s:{a}:knw", f"s:{a}:no"
    ]
    assert all(len(b["callback_data"].encode()) <= 64 for b in buttons)


def test_html_escaping_of_title_and_url(conn, tmp_path) -> None:
    a = add_item(
        conn, 'https://a/1?q="x"&y=<z>',
        title="<b>タグ入り & タイトル</b>", summary="要約 <s>", implication="示唆 & 例",
    )
    digest_id = add_digest(conn, [a])
    recorder = ApiRecorder()
    deliver_digest(conn, make_config(tmp_path), recorder.api(), digest_id, logger)

    text = recorder.texts()[1]
    assert "&lt;b&gt;タグ入り &amp; タイトル&lt;/b&gt;" in text
    assert 'href="https://a/1?q=&quot;x&quot;&amp;y=&lt;z&gt;"' in text
    assert "要約 &lt;s&gt;" in text
    assert "💡 示唆 &amp; 例" in text


def test_title_ja_preferred_in_item_text(conn, tmp_path) -> None:
    a = add_item(conn, "https://a/en", title="English Title", title_ja="日本語の見出し")
    digest_id = add_digest(conn, [a])
    recorder = ApiRecorder()
    deliver_digest(conn, make_config(tmp_path), recorder.api(), digest_id, logger)

    text = recorder.texts()[1]
    assert "<b>日本語の見出し</b>" in text
    assert "English Title" not in text


def test_long_summary_truncated_but_title_url_implication_kept(conn, tmp_path) -> None:
    a = add_item(conn, "https://a/1", summary="あ" * 3000, implication="示唆は残る。")
    digest_id = add_digest(conn, [a])
    recorder = ApiRecorder()
    deliver_digest(conn, make_config(tmp_path), recorder.api(), digest_id, logger)

    text = recorder.texts()[1]
    assert len(text) <= ITEM_TEXT_MAX_CHARS
    assert "…" in text  # 要約が切り詰められた
    assert text.endswith("💡 示唆は残る。")  # 示唆は削らない
    assert '<a href="https://a/1">' in text  # URL も削らない


def test_truncation_never_splits_html_entity(conn, tmp_path) -> None:
    # 切り詰め境界に & が来るよう、エスケープで膨らむ文字を敷き詰める
    # (エスケープ後 slice だと末尾が「&am…」等の壊れた entity になる)
    a = add_item(conn, "https://a/1", summary="&" * 2000, implication="示唆。")
    digest_id = add_digest(conn, [a])
    recorder = ApiRecorder()
    deliver_digest(conn, make_config(tmp_path), recorder.api(), digest_id, logger)

    text = recorder.texts()[1]
    assert len(text) <= ITEM_TEXT_MAX_CHARS
    # 出現する & はすべて完全な entity(&amp;)である
    stripped = text.replace("&amp;", "").replace("&lt;", "").replace("&gt;", "")
    assert "&" not in stripped
    assert "…" in text


@pytest.mark.parametrize("boundary_char", ["&", "<", ">"])
def test_truncation_boundary_chars_stay_wellformed(conn, tmp_path, boundary_char) -> None:
    # budget 境界の直前に entity 化される文字を1つ置く
    a = add_item(
        conn, "https://a/1",
        summary="あ" * 890 + boundary_char + "い" * 200, implication="",
    )
    digest_id = add_digest(conn, [a])
    recorder = ApiRecorder()
    deliver_digest(conn, make_config(tmp_path), recorder.api(), digest_id, logger)

    text = recorder.texts()[1]
    assert len(text) <= ITEM_TEXT_MAX_CHARS
    # 要約行(タイトル・ソース行の次)は生の & < > を含まない(entity のみ)
    summary_line = text.splitlines()[2]
    stripped = summary_line.replace("&amp;", "").replace("&lt;", "").replace("&gt;", "")
    assert not any(c in stripped for c in "&<>")


def test_degraded_digest_header_has_warning(conn, tmp_path) -> None:
    a = add_item(conn, "https://a/1", summary="", implication="")
    digest_id = add_digest(conn, [a], status="degraded")
    recorder = ApiRecorder()
    deliver_digest(conn, make_config(tmp_path), recorder.api(), digest_id, logger)

    assert "⚠️ 本文抽出失敗/分析縮退あり" in recorder.texts()[0]
    # summary/implication 空でもタイトル+ソース行のメッセージは成立する
    assert "1. <b>" in recorder.texts()[1]
    assert "💡" not in recorder.texts()[1]


def test_empty_digest_sends_header_only(conn, tmp_path) -> None:
    digest_id = add_digest(conn, [])
    recorder = ApiRecorder()
    sent = deliver_digest(conn, make_config(tmp_path), recorder.api(), digest_id, logger)
    assert sent == 1
    assert "0件" in recorder.texts()[0]
    assert conn.execute("SELECT delivery_status FROM digests").fetchone()[0] == "sent"


def test_already_sent_digest_is_skipped(conn, tmp_path) -> None:
    a = add_item(conn, "https://a/1")
    digest_id = add_digest(conn, [a])
    conn.execute("UPDATE digests SET delivery_status = 'sent' WHERE id = ?", (digest_id,))
    recorder = ApiRecorder()
    assert deliver_digest(conn, make_config(tmp_path), recorder.api(), digest_id, logger) == 0
    assert recorder.requests == []


def test_partial_resend_sends_only_missing_messages(conn, tmp_path) -> None:
    a = add_item(conn, "https://a/1")
    b = add_item(conn, "https://a/2", title="未送信タイトル")
    digest_id = add_digest(conn, [a, b])
    # 前回: ヘッダ + item a まで送信済みで失敗した状態
    conn.execute(
        "INSERT INTO digest_messages (digest_id, item_id, message_id) VALUES (?, NULL, 1)",
        (digest_id,),
    )
    conn.execute(
        "INSERT INTO digest_messages (digest_id, item_id, message_id) VALUES (?, ?, 2)",
        (digest_id, a),
    )
    conn.execute("UPDATE digests SET delivery_status = 'failed' WHERE id = ?", (digest_id,))
    recorder = ApiRecorder()

    sent = deliver_digest(conn, make_config(tmp_path), recorder.api(), digest_id, logger)

    assert sent == 1  # item b のみ
    assert len(recorder.requests) == 1
    assert "未送信タイトル" in recorder.texts()[0]
    assert "2. <b>" in recorder.texts()[0]  # 順位は digest 内の位置を維持
    assert conn.execute("SELECT delivery_status FROM digests").fetchone()[0] == "sent"


def test_midway_failure_marks_failed_and_keeps_ledger(conn, tmp_path) -> None:
    a = add_item(conn, "https://a/1")
    b = add_item(conn, "https://a/2")
    digest_id = add_digest(conn, [a, b])
    # 3通目(item b)だけ恒久 4xx で失敗させる
    recorder = ApiRecorder()
    calls = {"n": 0}

    def failing_handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 3:
            return httpx.Response(400, json={"ok": False, "description": "Bad Request"})
        return recorder.handler(request)

    api = TelegramApi(
        "test-token", transport=httpx.MockTransport(failing_handler), sleep=lambda _s: None
    )
    with pytest.raises(DeliveryError):
        deliver_digest(conn, make_config(tmp_path), api, digest_id, logger)

    assert conn.execute("SELECT delivery_status FROM digests").fetchone()[0] == "failed"
    ledger = conn.execute("SELECT item_id FROM digest_messages ORDER BY id").fetchall()
    assert [r["item_id"] for r in ledger] == [None, a]  # ヘッダ + a は記録済み


def test_retry_on_429_honors_retry_after_then_succeeds(conn, tmp_path) -> None:
    a = add_item(conn, "https://a/1")
    digest_id = add_digest(conn, [a])
    recorder = ApiRecorder(fail_statuses=[429])
    sleeps: list[float] = []

    sent = deliver_digest(conn, make_config(tmp_path), recorder.api(sleeps), digest_id, logger)

    assert sent == 2
    assert 7 in sleeps  # retry_after 尊重
    assert conn.execute("SELECT delivery_status FROM digests").fetchone()[0] == "sent"


def test_retry_on_5xx_then_gives_up_after_limit(conn, tmp_path) -> None:
    a = add_item(conn, "https://a/1")
    digest_id = add_digest(conn, [a])
    recorder = ApiRecorder(fail_statuses=[500, 502, 503, 500])  # 初回+リトライ3回 全滅

    with pytest.raises(DeliveryError, match="リトライ上限"):
        deliver_digest(conn, make_config(tmp_path), recorder.api(), digest_id, logger)
    assert len(recorder.requests) == 4
    assert conn.execute("SELECT delivery_status FROM digests").fetchone()[0] == "failed"


def test_permanent_4xx_does_not_retry(conn, tmp_path) -> None:
    a = add_item(conn, "https://a/1")
    digest_id = add_digest(conn, [a])
    recorder = ApiRecorder(fail_statuses=[403])

    with pytest.raises(DeliveryError, match="403"):
        deliver_digest(conn, make_config(tmp_path), recorder.api(), digest_id, logger)
    assert len(recorder.requests) == 1


def test_missing_chat_id_fails_fast(conn, tmp_path) -> None:
    digest_id = add_digest(conn, [])
    with pytest.raises(DeliveryError, match="owner_chat_id"):
        deliver_digest(
            conn, make_config(tmp_path, chat_id=None), ApiRecorder().api(), digest_id, logger
        )


def test_create_api_requires_token() -> None:
    with pytest.raises(DeliveryError, match="TELEGRAM_BOT_TOKEN"):
        create_api({})


def test_invalid_published_at_drops_date_but_delivers(conn, tmp_path) -> None:
    a = add_item(conn, "https://a/1", published_at="not-a-date")
    digest_id = add_digest(conn, [a])
    recorder = ApiRecorder()
    deliver_digest(conn, make_config(tmp_path), recorder.api(), digest_id, logger)
    assert "・" not in recorder.texts()[1].splitlines()[1]  # 日付表示なし


def test_escape_helpers() -> None:
    assert escape_text("a<b>&c") == "a&lt;b&gt;&amp;c"
    assert escape_attr('a"b\'c&') == "a&quot;b&#x27;c&amp;"


def test_huge_title_is_truncated_and_delivery_succeeds(conn, tmp_path) -> None:
    # RSS 由来の異常に長いタイトル1件で Telegram の 4096 字上限を踏んで
    # 配信全体が failed にならない(タイトルにも上限。監査対応)
    a = add_item(conn, "https://a/1", title="長" * 5000, summary="要約。", implication="示唆。")
    digest_id = add_digest(conn, [a])
    recorder = ApiRecorder()

    sent = deliver_digest(conn, make_config(tmp_path), recorder.api(), digest_id, logger)

    assert sent == 2
    text = recorder.texts()[1]
    assert len(text) <= ITEM_TEXT_MAX_CHARS + 400  # タイトル上限 300 + 構造分で十分小さい
    assert "…" in text.splitlines()[0]  # タイトルが切り詰められている
    assert text.endswith("💡 示唆。")  # 示唆は残る
    assert conn.execute("SELECT delivery_status FROM digests").fetchone()[0] == "sent"
