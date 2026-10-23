import json
import logging
import sqlite3
import threading
from pathlib import Path

import httpx
import pytest

from app.config import Config, TelegramConfig
from app.db import apply_migrations, connect
from app.delivery import DeliveryError, TelegramApi, create_api, deliver_digest
from app.delivery.telegram import ITEM_TEXT_MAX_CHARS, escape_attr, escape_text
from app.jst import today_jst

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
    reason="理由",
    implication="示唆テキスト。",
    degraded=0,
) -> int:
    item_id = conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, title, published_at, content_hash, "
        "status) VALUES (2, ?, ?, ?, ?, 'sha256:x', 'digested')",
        (url, url, title, published_at),
    ).lastrowid
    conn.execute(
        "INSERT INTO analyses (item_id, title_ja, summary, category, relevance_score, novelty, "
        "recommendation_reason, implication, degraded, engine, model, prompt_version, "
        "input_hash) "
        "VALUES (?, ?, ?, '課題直結', 80, 'new', ?, ?, ?, 'codex-cli', 'm', 'analyze-v001', ?)",
        (item_id, title_ja, summary, reason, implication, degraded, f"sha256:{url}"),
    )
    return item_id


def add_digest(
    conn, item_ids, *, status="ok", trend_markdown=None, bookmark_item_ids=None
) -> int:
    return conn.execute(
        "INSERT INTO digests (digest_date, markdown, item_ids, item_count, status, "
        "trend_markdown, bookmark_item_ids) VALUES (?, '# md', ?, ?, ?, ?, ?)",
        (today_jst(), json.dumps(item_ids), len(item_ids), status, trend_markdown,
         json.dumps(bookmark_item_ids) if bookmark_item_ids is not None else None),
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


def test_recommendation_reason_shown_between_summary_and_implication(conn, tmp_path) -> None:
    # どの角度で推薦したか(analyses.recommendation_reason)を 🎯 行で出す(GH#90)
    a = add_item(
        conn, "https://a/1",
        summary="要約。", reason="すぐ試せる <Tips> & 手順", implication="示唆。",
    )
    digest_id = add_digest(conn, [a])
    recorder = ApiRecorder()
    deliver_digest(conn, make_config(tmp_path), recorder.api(), digest_id, logger)

    lines = recorder.texts()[1].splitlines()
    assert lines[2:] == ["要約。", "🎯 すぐ試せる &lt;Tips&gt; &amp; 手順", "💡 示唆。"]


@pytest.mark.parametrize(
    ("reason", "degraded"),
    [
        ("", 0),  # 理由が空なら行ごと省く
        ("LLM 分析失敗のため degraded 記録", 1),  # degraded の理由欄は記録用の定型文
    ],
)
def test_recommendation_reason_line_omitted(conn, tmp_path, reason, degraded) -> None:
    a = add_item(
        conn, "https://a/1",
        summary="要約。", reason=reason, implication="示唆。", degraded=degraded,
    )
    digest_id = add_digest(conn, [a])
    recorder = ApiRecorder()
    deliver_digest(conn, make_config(tmp_path), recorder.api(), digest_id, logger)

    text = recorder.texts()[1]
    assert "🎯" not in text
    assert text.splitlines()[2:] == ["要約。", "💡 示唆。"]


def test_long_summary_truncated_but_title_url_implication_kept(conn, tmp_path) -> None:
    a = add_item(
        conn, "https://a/1",
        summary="あ" * 3000, reason="理由は残る。", implication="示唆は残る。",
    )
    digest_id = add_digest(conn, [a])
    recorder = ApiRecorder()
    deliver_digest(conn, make_config(tmp_path), recorder.api(), digest_id, logger)

    text = recorder.texts()[1]
    assert len(text) <= ITEM_TEXT_MAX_CHARS
    assert "…" in text  # 要約が切り詰められた
    assert text.endswith("🎯 理由は残る。\n💡 示唆は残る。")  # 推薦の理由と示唆は削らない
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


def test_header_includes_trend_summary_html_escaped(conn, tmp_path) -> None:
    # 「今日のトレンド」節はヘッダ(冒頭)メッセージに同梱され、parse_mode=HTML 用に
    # escape される(LLM 生成の <>& で配信全体が 400 で落ちないこと。GH#75)
    a = add_item(conn, "https://a/1")
    trend = "今日のトレンド\n・A<B>&C — 権限と秘匿が話題。"
    digest_id = add_digest(conn, [a], trend_markdown=trend)
    recorder = ApiRecorder()
    deliver_digest(conn, make_config(tmp_path), recorder.api(), digest_id, logger)

    header = recorder.texts()[0]
    assert "のダイジェスト" in header  # 通常のヘッダ行
    assert "今日のトレンド" in header  # トレンド節が冒頭メッセージに載る
    assert "A&lt;B&gt;&amp;C" in header  # <>& が escape されている
    assert "A<B>&C" not in header  # 生の HTML 特殊文字は残さない


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


def test_concurrent_deliveries_send_each_message_once(conn, tmp_path, caplog) -> None:
    # 朝バッチと手動の intel deliver が重なっても 1 回だけ送る(GH#192)。flock は開いた
    # ファイルごとに効くので、同じプロセスの 2 スレッド(別々の接続)で 2 プロセスを模す
    a = add_item(conn, "https://a/1")
    b = add_item(conn, "https://a/2")
    digest_id = add_digest(conn, [a, b])
    db_path = tmp_path / "intel.db"
    recorder = ApiRecorder()
    guard = threading.Lock()
    first_send = threading.Event()  # 先の配信が 1 通目を送り始めた
    release = threading.Event()  # 先の配信の 1 通目を返す
    send_while_held = threading.Event()  # 先の配信が止まっている間に別の送信が来た

    def handler(request: httpx.Request) -> httpx.Response:
        with guard:
            response = recorder.handler(request)
            count = len(recorder.requests)
        if count == 1:
            first_send.set()
            release.wait(timeout=10)
        elif not release.is_set():
            send_while_held.set()
        return response

    results: dict[str, int] = {}
    errors: list[BaseException] = []

    def run(name: str) -> None:
        thread_conn = connect(db_path)
        api = TelegramApi(
            "test-token", transport=httpx.MockTransport(handler), sleep=lambda _s: None
        )
        try:
            results[name] = deliver_digest(
                thread_conn, make_config(tmp_path), api, digest_id, logger
            )
        except BaseException as e:  # スレッドの例外は本体で検査する
            errors.append(e)
        finally:
            thread_conn.close()

    first = threading.Thread(target=run, args=("first",))
    second = threading.Thread(target=run, args=("second",))
    with caplog.at_level(logging.INFO, logger="test-delivery"):
        first.start()
        try:
            assert first_send.wait(timeout=10)
            second.start()
            # 後の配信は、先の配信が台帳に書き終えるまで台帳を読まずに待つ
            sent_while_held = send_while_held.wait(timeout=0.5)
        finally:  # 検査が落ちてもスレッドを残さない
            release.set()
            first.join(timeout=10)
            if second.ident is not None:
                second.join(timeout=10)

    assert not sent_while_held
    assert errors == []
    assert results == {"first": 3, "second": 0}
    assert len(recorder.requests) == 3  # ヘッダ + 2 件。二重に送らない
    assert "別の配信が実行中のため、終わるのを待つ" in caplog.text
    ledger = conn.execute("SELECT item_id, message_id FROM digest_messages ORDER BY id").fetchall()
    assert [(r["item_id"], r["message_id"]) for r in ledger] == [(None, 101), (a, 102), (b, 103)]
    assert (tmp_path / "deliver.lock").exists()  # ロックは DB の隣


class FlakyLedgerConn:
    """digest_messages への INSERT を先頭から fail 回だけ失敗させる接続の代理(GH#225)。"""

    def __init__(self, conn: sqlite3.Connection, fail: int, error=sqlite3.OperationalError):
        self._conn = conn
        self._fail = fail
        self._error = error
        self.inserts = 0

    def execute(self, sql: str, params=()):
        if sql.startswith("INSERT INTO digest_messages"):
            self.inserts += 1
            if self._fail > 0:
                self._fail -= 1
                raise self._error("disk I/O error")
        return self._conn.execute(sql, params)


def test_ledger_write_failure_retries_record_without_resending(conn, tmp_path) -> None:
    # 送信の後の記帳が一時的に失敗しても、送り直さず記帳だけをやり直す(GH#225)
    a = add_item(conn, "https://a/1")
    digest_id = add_digest(conn, [a])
    recorder = ApiRecorder()
    sleeps: list[float] = []

    sent = deliver_digest(
        FlakyLedgerConn(conn, fail=2), make_config(tmp_path), recorder.api(), digest_id,
        logger, sleep=sleeps.append,
    )

    assert sent == 2
    assert len(recorder.requests) == 2  # ヘッダを送り直していない
    assert sleeps == [1.0, 2.0]
    ledger = conn.execute("SELECT item_id, message_id FROM digest_messages ORDER BY id").fetchall()
    assert [(r["item_id"], r["message_id"]) for r in ledger] == [(None, 101), (a, 102)]
    # 再走しても二重に送らない
    assert deliver_digest(conn, make_config(tmp_path), recorder.api(), digest_id, logger) == 0
    assert len(recorder.requests) == 2


@pytest.mark.parametrize(
    ("error", "inserts"),
    [
        (sqlite3.OperationalError, 4),  # 初回 + やり直し 3 回
        (sqlite3.IntegrityError, 1),  # 制約違反はやり直しても通らない
    ],
)
def test_ledger_write_failure_that_persists_logs_message_id_and_stops(
    conn, tmp_path, caplog, error, inserts
) -> None:
    # やり直しても記帳できなければ、送信済みの message_id をログに残して止める(GH#225)
    a = add_item(conn, "https://a/1")
    b = add_item(conn, "https://a/2")
    digest_id = add_digest(conn, [a, b])
    recorder = ApiRecorder()
    flaky = FlakyLedgerConn(conn, fail=99, error=error)

    with (
        caplog.at_level(logging.ERROR, logger="test-delivery"),
        pytest.raises(DeliveryError, match="message_id=101"),
    ):
        deliver_digest(
            flaky, make_config(tmp_path), recorder.api(), digest_id, logger,
            sleep=lambda _s: None,
        )

    assert len(recorder.requests) == 1  # 記帳できないまま先へ送らない
    assert flaky.inserts == inserts
    assert conn.execute("SELECT delivery_status FROM digests").fetchone()[0] == "failed"
    record = next(r for r in caplog.records if "台帳に記帳できない" in r.getMessage())
    assert record.item_id is None  # ヘッダ
    assert record.extra["digest_id"] == digest_id
    assert record.extra["message_id"] == 101
    # 復旧(runbook §15): ログの message_id を台帳に入れてから再走すると、ヘッダを送り直さない
    conn.execute(
        "INSERT INTO digest_messages (digest_id, item_id, message_id) VALUES (?, ?, ?)",
        (record.extra["digest_id"], record.item_id, record.extra["message_id"]),
    )
    assert deliver_digest(conn, make_config(tmp_path), recorder.api(), digest_id, logger) == 2
    assert len(recorder.requests) == 3
    assert all("のダイジェスト" not in text for text in recorder.texts()[1:])


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


def test_header_includes_bookmark_section_with_escaped_links(conn, tmp_path) -> None:
    # 🔖 保存節(GH#99)はヘッダ 1 通に同梱。リンクは属性エスケープ・表示は上限つき
    a = add_item(conn, "https://a/1")
    bm = add_item(conn, "https://blog.example/a?x=1&y=2", title=None, title_ja="保存記事<A>")
    digest_id = add_digest(conn, [a], bookmark_item_ids=[bm])
    recorder = ApiRecorder()
    deliver_digest(conn, make_config(tmp_path), recorder.api(), digest_id, logger)

    header = recorder.texts()[0]
    assert "🔖 保存(1件)" in header
    assert '<a href="https://blog.example/a?x=1&amp;y=2">' in header  # 属性エスケープ
    assert "保存記事&lt;A&gt;" in header  # テキストノードもエスケープ
    assert "件のダイジェスト" not in header.split("🔖")[1]  # 節はヘッダ末尾に付く


def test_header_bookmark_section_caps_display(conn, tmp_path) -> None:
    ids = [add_item(conn, f"https://b/{i}", title=f"保存{i}") for i in range(12)]
    digest_id = add_digest(conn, [], bookmark_item_ids=ids)
    recorder = ApiRecorder()
    deliver_digest(conn, make_config(tmp_path), recorder.api(), digest_id, logger)

    header = recorder.texts()[0]
    assert "🔖 保存(12件)" in header
    assert "(他 2 件は Items ノートへ)" in header
    assert "保存11" not in header  # 11・12 件目は表示しない(保存0〜9 の 10 件)


def test_send_message_reply_target(tmp_path) -> None:
    # 質問への返事(GH#126)は質問のリプライへの返信で送る。返信先が消えていても送る
    recorder = ApiRecorder()
    api = recorder.api()

    assert api.send_message(CHAT_ID, "返事", reply_to_message_id=55) == 101
    assert api.send_message(CHAT_ID, "通常") == 102

    assert json.loads(recorder.requests[0]["reply_parameters"]) == {
        "message_id": 55,
        "allow_sending_without_reply": True,
    }
    assert "reply_parameters" not in recorder.requests[1]
