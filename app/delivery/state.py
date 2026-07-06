"""telegram_state(受信デーモンの永続 KV。設計 01 §3)の読み書き。"""

from __future__ import annotations

import sqlite3


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM telegram_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO telegram_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')",
        (key, value),
    )
