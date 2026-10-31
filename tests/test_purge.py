import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import Config, RetentionConfig
from app.db import apply_migrations, connect
from app.purge import purge_expired

logger = logging.getLogger("test-purge")


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed-a')")  # id=2
    conn.execute("INSERT INTO sources (type, name) VALUES ('x', 'home-tl')")  # id=3
    yield conn
    conn.close()


def add_item(conn, url, *, source_id, fetched_days_ago, payload='{"raw_content": "x"}'):
    return conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, content_hash, raw_payload, fetched_at) "
        "VALUES (?, ?, ?, 'sha256:x', ?, strftime('%Y-%m-%dT%H:%M:%SZ','now', ?))",
        (source_id, url, url, payload, f"-{fetched_days_ago} days"),
    ).lastrowid


def add_document(conn, item_id, *, expires_days: int):
    sign = "+" if expires_days >= 0 else ""
    conn.execute(
        "INSERT INTO documents (item_id, extraction_status, expires_at) "
        "VALUES (?, 'ok', strftime('%Y-%m-%dT%H:%M:%SZ','now', ?))",
        (item_id, f"{sign}{expires_days} days"),
    )


def test_expired_documents_deleted(conn, tmp_path) -> None:
    live = add_item(conn, "https://a/live", source_id=2, fetched_days_ago=1)
    dead = add_item(conn, "https://a/dead", source_id=2, fetched_days_ago=40)
    add_document(conn, live, expires_days=10)
    add_document(conn, dead, expires_days=-1)

    counts = purge_expired(conn, Config(config_dir=tmp_path), logger)
    assert counts["documents_deleted"] == 1
    remaining = [r["item_id"] for r in conn.execute("SELECT item_id FROM documents")]
    assert remaining == [live]


def test_raw_payload_nullified_by_source_ttl(conn, tmp_path) -> None:
    rss_fresh = add_item(conn, "https://a/1", source_id=2, fetched_days_ago=10)  # 30日未満
    rss_old = add_item(conn, "https://a/2", source_id=2, fetched_days_ago=31)
    x_fresh = add_item(conn, "https://x/1", source_id=3, fetched_days_ago=6)  # 7日未満
    x_old = add_item(conn, "https://x/2", source_id=3, fetched_days_ago=8)
    # documents を持たない・status を問わない行も対象(duplicate)
    dup_old = add_item(conn, "https://a/3", source_id=2, fetched_days_ago=31)
    conn.execute("UPDATE items SET status='duplicate' WHERE id = ?", (dup_old,))

    counts = purge_expired(conn, Config(config_dir=tmp_path), logger)
    assert counts["raw_payload_nullified"] == 3
    payloads = {
        r["id"]: r["raw_payload"] for r in conn.execute("SELECT id, raw_payload FROM items")
    }
    assert payloads[rss_fresh] is not None
    assert payloads[x_fresh] is not None
    assert payloads[rss_old] is None
    assert payloads[x_old] is None
    assert payloads[dup_old] is None


def utc_iso(delta: timedelta = timedelta()) -> str:
    return (datetime.now(UTC) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_raw_payload_ttl_counts_from_cached_at(conn, tmp_path) -> None:
    # 全文を書き戻した payload は cached_at から TTL を数える(fetched_at は初回の取込の
    # 時刻のまま。GH#332)。cached_at の無い行は従来どおり fetched_at
    revived = add_item(
        conn, "https://x/r", source_id=3, fetched_days_ago=30,
        payload=json.dumps({"raw_content": "x", "cached_at": utc_iso()}),
    )
    revived_old = add_item(
        conn, "https://x/o", source_id=3, fetched_days_ago=30,
        payload='{"raw_content": "x", "cached_at": "2020-01-01T00:00:00Z"}',
    )
    plain_old = add_item(conn, "https://x/p", source_id=3, fetched_days_ago=30)

    counts = purge_expired(conn, Config(config_dir=tmp_path), logger)
    assert counts["raw_payload_nullified"] == 2
    payloads = {
        r["id"]: r["raw_payload"] for r in conn.execute("SELECT id, raw_payload FROM items")
    }
    assert payloads[revived] is not None
    assert payloads[revived_old] is None
    assert payloads[plain_old] is None


@pytest.mark.parametrize(
    "cached_at",
    [
        "not-a-date",
        "2999-01-01T00:00:00Z",  # 未来: そのままだと長く消されない
        "+1 day",  # 下で今の 1 日後に置き換える(近い未来も同じ)
        "2026-02-30T00:00:00Z",  # 実在しない日付
        "2025-02-29T00:00:00Z",  # 平年の 2/29
        "2026-00-10T00:00:00Z",  # 月の 00
        "2026-10-00T00:00:00Z",  # 日の 00
        "2026-13-01T00:00:00Z",
        "2026-10-28T24:00:00Z",  # 時の 24(修飾子の無い strftime はそのまま返す)
        "2026-10-28T24:30:00Z",
        "2026-10-28T25:00:00Z",
        "2026-10-28T23:60:00Z",  # 分の 60
        "2026-10-28T23:59:60Z",  # 秒の 60
        "2026-10-28T12:00:00.5Z",  # 小数の秒
        "2026-10-28T12:00:00+09:00",  # 時差つき
        "2026-10-31 00:00:00",  # 形が違う
        "2026-10-31T00:00:00Z ",
        12345,
        None,
        {"at": "2026-10-31T00:00:00Z"},
    ],
)
def test_raw_payload_with_malformed_cached_at_is_purged(conn, tmp_path, cached_at) -> None:
    # 異常な cached_at は期限切れとして扱い、fetched_at が新しくても本文を消す。
    # X の本文を長く残す向きに外さない(設計 01 §6。GH#332)。日付の古さで消えたのでなく
    # 異常として消えたことを見るため、TTL を十分に長くする(どの日付も TTL の内)
    if cached_at == "+1 day":
        cached_at = utc_iso(timedelta(days=1))
    item = add_item(
        conn, "https://x/m", source_id=3, fetched_days_ago=1,
        payload=json.dumps({"raw_content": "x", "cached_at": cached_at}),
    )
    long_ttl = RetentionConfig(cache_ttl_days=36500, x_cache_ttl_days=36500)
    counts = purge_expired(conn, Config(config_dir=tmp_path, retention=long_ttl), logger)
    assert counts["raw_payload_nullified"] == 1
    assert conn.execute(
        "SELECT raw_payload FROM items WHERE id = ?", (item,)
    ).fetchone()["raw_payload"] is None


def test_cached_at_with_hour_24_within_ttl_is_purged(conn, tmp_path) -> None:
    # 既定の TTL の内の「N 日前の 24:30」も異常として消す。正規化を強いないと一致と未来の
    # 検査を通り、TTL の間は残る(GH#332)。修飾子の無い strftime が 24 時をそのまま返すか
    # は日付によって違う(月末の 3 日は日を繰り上げる)ため、1〜6 日前をすべて入れて
    # 少なくとも 1 日はそのまま返る日を含める
    bad = []
    for days in range(1, 7):
        day = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
        bad.append(add_item(
            conn, f"https://x/h{days}", source_id=3, fetched_days_ago=1,
            payload=json.dumps({"raw_content": "x", "cached_at": f"{day}T24:30:00Z"}),
        ))
    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    good = add_item(
        conn, "https://x/g", source_id=3, fetched_days_ago=30,
        payload=json.dumps({"raw_content": "x", "cached_at": f"{yesterday}T00:30:00Z"}),
    )
    purge_expired(conn, Config(config_dir=tmp_path), logger)
    payloads = {
        r["id"]: r["raw_payload"] for r in conn.execute("SELECT id, raw_payload FROM items")
    }
    assert [payloads[i] for i in bad] == [None] * 6
    assert payloads[good] is not None


def test_vacuum_triggered_by_nullified_rows(conn, tmp_path, monkeypatch) -> None:
    import app.purge as purge_module

    monkeypatch.setattr(purge_module, "VACUUM_THRESHOLD_ROWS", 2)
    for i in range(3):
        add_item(conn, f"https://a/{i}", source_id=2, fetched_days_ago=31)

    class SpyConn:
        def __init__(self, inner):
            self._inner = inner
            self.sqls: list[str] = []

        def execute(self, sql, *args):
            self.sqls.append(sql)
            return self._inner.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    spy = SpyConn(conn)
    counts = purge_expired(spy, Config(config_dir=tmp_path), logger)
    assert counts["raw_payload_nullified"] == 3
    assert any(sql.strip() == "VACUUM" for sql in spy.sqls)  # NULL 化のみでも発動


def test_purge_is_idempotent(conn, tmp_path) -> None:
    item = add_item(conn, "https://a/1", source_id=2, fetched_days_ago=31)
    add_document(conn, item, expires_days=-1)
    purge_expired(conn, Config(config_dir=tmp_path), logger)
    counts = purge_expired(conn, Config(config_dir=tmp_path), logger)
    assert counts == {
        "documents_deleted": 0,
        "raw_payload_nullified": 0,
        "x_signal_excerpts_nullified": 0,
    }


def test_x_signal_excerpts_nullified_by_ttl(conn, tmp_path) -> None:
    # x_signals の本文抜粋は TTL キャッシュのみ(Issue #2 / GH#58 S1b)。行自体は残す
    conn.execute(
        "INSERT INTO x_signals (kind, tweet_id, text_excerpt, observed_at) "
        "VALUES ('bookmark', 'old', '古い本文', strftime('%Y-%m-%dT%H:%M:%SZ','now','-8 days'))"
    )
    conn.execute(
        "INSERT INTO x_signals (kind, tweet_id, text_excerpt) VALUES ('like', 'new', '新しい本文')"
    )
    counts = purge_expired(conn, Config(config_dir=tmp_path), logger)
    assert counts["x_signal_excerpts_nullified"] == 1
    rows = {
        r["tweet_id"]: r["text_excerpt"]
        for r in conn.execute("SELECT tweet_id, text_excerpt FROM x_signals")
    }
    assert rows == {"old": None, "new": "新しい本文"}  # シグナル行は消えない
