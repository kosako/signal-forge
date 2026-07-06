import json
import logging
import sqlite3
from pathlib import Path

import httpx
import pytest

from app.config import Config
from app.db import apply_migrations, connect
from app.extraction import extract_pending
from app.http import HttpClient

logger = logging.getLogger("test-extract")

LONG_BODY = "本文の段落。" * 100  # 600字 — ok 判定に十分
ARTICLE_HTML = f"""<!doctype html><html><head><title>t</title></head>
<body><nav>menu menu</nav><article><h1>見出し</h1><p>{LONG_BODY}</p></article></body></html>"""


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed-a')")  # id=2
    conn.execute("INSERT INTO sources (type, name) VALUES ('x', 'home-tl')")  # id=3
    yield conn
    conn.close()


def add_item(conn, *, source_id=2, url="https://example.com/1", raw_content=None, status="new"):
    payload = json.dumps({"raw_content": raw_content}) if raw_content else None
    cursor = conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, content_hash, raw_payload, status) "
        "VALUES (?, ?, ?, 'sha256:x', ?, ?)",
        (source_id, url, url, payload, status),
    )
    return cursor.lastrowid


def make_http(handler, calls=None):
    def counting_handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        return handler(request)

    return HttpClient(
        transport=httpx.MockTransport(counting_handler),
        resolve_host=lambda host: ["93.184.216.34"],
        sleep=lambda _s: None,
    )


def config(tmp_path: Path) -> Config:
    return Config(config_dir=tmp_path)


def test_raw_content_sufficient_skips_http(conn, tmp_path) -> None:
    add_item(conn, raw_content=ARTICLE_HTML)
    calls: list[str] = []
    http = make_http(lambda request: httpx.Response(200, text=ARTICLE_HTML), calls)

    counts = extract_pending(conn, config(tmp_path), http, logger)
    assert counts == {"ok": 1, "partial": 0, "failed": 0}
    assert calls == []  # HTTP 節約
    doc = conn.execute("SELECT * FROM documents").fetchone()
    assert doc["extraction_status"] == "ok"
    assert "本文の段落。" in doc["extracted_text"]
    assert "menu" not in doc["extracted_text"]  # nav は除去される


def test_short_raw_content_falls_back_to_http(conn, tmp_path) -> None:
    add_item(conn, raw_content="<p>短い要約</p>")
    calls: list[str] = []
    http = make_http(lambda request: httpx.Response(200, text=ARTICLE_HTML), calls)

    counts = extract_pending(conn, config(tmp_path), http, logger)
    assert counts == {"ok": 1, "partial": 0, "failed": 0}
    assert calls == ["https://example.com/1"]


def test_no_raw_content_fetches_http(conn, tmp_path) -> None:
    add_item(conn)
    calls: list[str] = []
    http = make_http(lambda request: httpx.Response(200, text=ARTICLE_HTML), calls)
    extract_pending(conn, config(tmp_path), http, logger)
    assert calls == ["https://example.com/1"]


def test_fetch_failure_creates_failed_row(conn, tmp_path) -> None:
    add_item(conn)
    http = make_http(lambda request: httpx.Response(404))
    counts = extract_pending(conn, config(tmp_path), http, logger)
    assert counts == {"ok": 0, "partial": 0, "failed": 1}
    doc = conn.execute("SELECT * FROM documents").fetchone()
    assert doc["extraction_status"] == "failed"
    assert doc["extracted_text"] is None
    assert "404" in doc["extraction_error"]


def test_short_result_is_partial_and_short_raw_survives_fetch_failure(conn, tmp_path) -> None:
    add_item(conn, raw_content="<p>フィード同梱の短い本文だけが取れたケース</p>")
    http = make_http(lambda request: httpx.Response(500))
    counts = extract_pending(conn, config(tmp_path), http, logger)
    assert counts == {"ok": 0, "partial": 1, "failed": 0}
    doc = conn.execute("SELECT * FROM documents").fetchone()
    assert doc["extraction_status"] == "partial"
    assert "短い本文" in doc["extracted_text"]


def test_partial_from_fetch_failure_records_error(conn, tmp_path) -> None:
    add_item(conn, raw_content="<p>フィード同梱の短い本文だけが取れたケース</p>")
    http = make_http(lambda request: httpx.Response(500))
    extract_pending(conn, config(tmp_path), http, logger)
    doc = conn.execute("SELECT * FROM documents").fetchone()
    assert doc["extraction_status"] == "partial"
    assert doc["extraction_error"] is not None  # 「短い記事」と「取得失敗」を区別できる


def test_broken_raw_payload_isolated_and_rest_processed(conn, tmp_path) -> None:
    # json_valid は通るが object でない raw_payload(_raw_content で例外)
    conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, content_hash, raw_payload) "
        "VALUES (2, 'https://example.com/broken', 'https://example.com/broken', 'sha256:b', '[]')"
    )
    add_item(conn, url="https://example.com/fine")
    http = make_http(lambda request: httpx.Response(200, text=ARTICLE_HTML))

    counts = extract_pending(conn, config(tmp_path), http, logger)
    assert counts == {"ok": 1, "partial": 0, "failed": 1}
    rows = {
        r["canonical_url"]: r["extraction_status"]
        for r in conn.execute(
            "SELECT canonical_url, extraction_status FROM documents "
            "JOIN items ON items.id = documents.item_id"
        )
    }
    assert rows["https://example.com/broken"] == "failed"
    assert rows["https://example.com/fine"] == "ok"


def test_expires_at_by_source_type(conn, tmp_path) -> None:
    add_item(conn, source_id=2, url="https://example.com/rss")
    add_item(conn, source_id=3, url="https://example.com/x", raw_content="ツイート全文 " * 30)
    http = make_http(lambda request: httpx.Response(200, text=ARTICLE_HTML))
    extract_pending(conn, config(tmp_path), http, logger)

    rows = conn.execute(
        """
        SELECT sources.type AS t,
               CAST(julianday(expires_at) - julianday(extracted_at) AS INTEGER) AS days
        FROM documents JOIN items ON items.id = documents.item_id
        JOIN sources ON sources.id = items.source_id
        """
    ).fetchall()
    by_type = {r["t"]: r["days"] for r in rows}
    assert by_type == {"rss": 30, "x": 7}


def test_already_extracted_and_non_new_items_skipped(conn, tmp_path) -> None:
    done = add_item(conn, url="https://example.com/done")
    conn.execute(
        "INSERT INTO documents (item_id, extraction_status, expires_at) "
        "VALUES (?, 'ok', '2026-08-01T00:00:00Z')",
        (done,),
    )
    add_item(conn, url="https://example.com/dup", status="duplicate")
    add_item(conn, url="https://example.com/err", status="error")

    http = make_http(lambda r: httpx.Response(200))
    counts = extract_pending(conn, config(tmp_path), http, logger)
    assert counts == {"ok": 0, "partial": 0, "failed": 0}
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
