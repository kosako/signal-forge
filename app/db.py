"""SQLite 接続ファクトリと migrations ランナー(設計 01 §1・§10)。

- 接続は autocommit モードで返す。**トランザクションは呼び出し側が明示的に張る**
  (`BEGIN IMMEDIATE` … `COMMIT`)。理由: collect の「全 INSERT + カーソル更新を
  同一トランザクション」等、境界は業務側が決める(設計 02 §1)。
- マイグレーションは forward-only(down なし)。適用前に DB の一貫スナップショットを
  `.bak` に取る(設計 01 §10。方式は backup API — 理由は _backup のコメント)。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_MIGRATION_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


class MigrationError(Exception):
    """マイグレーションファイルの構成不正・適用失敗。"""


def connect(db_path: Path) -> sqlite3.Connection:
    """PRAGMA を一元設定した接続を返す(設計 01 §1)。

    朝バッチと受信デーモンの2プロセスが並行書き込みするため WAL + busy_timeout が必須。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, autocommit=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@dataclass(frozen=True)
class Migration:
    version: int
    filename: str
    path: Path


def discover_migrations(migrations_dir: Path) -> list[Migration]:
    """`NNN_name.sql` を番号順に列挙し、連番(1始まり・欠番禁止・重複禁止)を検証する。"""
    migrations = []
    for path in sorted(migrations_dir.glob("*.sql")):
        m = _MIGRATION_NAME.match(path.name)
        if not m:
            raise MigrationError(f"{path.name}: NNN_name.sql 形式でない(設計 01 §10)")
        migrations.append(Migration(version=int(m.group(1)), filename=path.name, path=path))
    for i, migration in enumerate(migrations, start=1):
        if migration.version != i:
            raise MigrationError(
                f"連番が {i:03d} であるべき位置に {migration.filename}(欠番・重複禁止)"
            )
    return migrations


def apply_migrations(
    conn: sqlite3.Connection, db_path: Path, migrations_dir: Path = MIGRATIONS_DIR
) -> list[str]:
    """未適用のマイグレーションを番号順に適用し、適用したファイル名を返す。

    - 台帳: schema_migrations(version PRIMARY KEY, filename, applied_at)
    - 1 ファイル = 1 トランザクション(BEGIN IMMEDIATE)
    - 未適用があるときのみ、適用前に `<name>.bak` へ一貫スナップショット(backup API)
    """
    # 構成検証を台帳作成より先に行う(不正構成の検出だけで DB に痕跡を残さない)
    migrations = discover_migrations(migrations_dir)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version    INTEGER PRIMARY KEY,
          filename   TEXT NOT NULL,
          applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        ) STRICT
        """
    )
    applied = {
        row["version"]: row["filename"]
        for row in conn.execute("SELECT version, filename FROM schema_migrations")
    }
    for version, filename in applied.items():
        expected = next((m.filename for m in migrations if m.version == version), None)
        if expected != filename:
            raise MigrationError(
                f"台帳の {version:03d} は {filename} だがファイルは {expected}"
                "(適用済みファイルの編集・改名は禁止。設計 01 §10)"
            )

    pending = [m for m in migrations if m.version not in applied]
    if not pending:
        return []

    _backup(conn, db_path)
    for migration in pending:
        sql = migration.path.read_text(encoding="utf-8")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, filename) VALUES (?, ?)",
                (migration.version, migration.filename),
            )
            conn.execute("COMMIT")
        except sqlite3.Error as e:
            conn.execute("ROLLBACK")
            raise MigrationError(f"{migration.filename} の適用に失敗: {e}") from e
    return [m.filename for m in pending]


@dataclass(frozen=True)
class InsertOutcome:
    """items への挿入結果。outcome は 'inserted' / 'ignored' / 'duplicate'。"""

    outcome: str
    item_id: int | None  # ignored(UNIQUE 既存)のときは None


def insert_item_with_dedup(
    conn: sqlite3.Connection,
    *,
    source_id: int,
    external_id: str | None,
    url: str,
    canonical_url: str,
    title: str | None,
    author: str | None,
    published_at: str | None,
    raw_payload: str | None,
    content_hash: str,
    dedup_eligible: bool,
) -> InsertOutcome:
    """2層 dedup 付きの items 挿入(設計 01 §6)。

    1層: UNIQUE(canonical_url) / UNIQUE(source_id, external_id) に INSERT OR IGNORE
    (再実行・カーソル巻き戻し・再投入は無害に落ちる = 冪等性契約。設計 02 §1)。
    2層: 挿入成功直後、同一 content_hash で fetched_at が直近24h内の既存アイテム
    (status ≠ 'duplicate')があれば、新しい方を duplicate 化する。
    dedup_eligible=False(正規化テキストが空/20字未満。manual 等)は 2層の対象外。

    トランザクション境界は呼び出し側(collect / 受信デーモン)が張る。
    """
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO items
          (source_id, external_id, url, canonical_url, title, author, published_at,
           raw_payload, content_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            external_id,
            url,
            canonical_url,
            title,
            author,
            published_at,
            raw_payload,
            content_hash,
        ),
    )
    if cursor.rowcount == 0:
        return InsertOutcome(outcome="ignored", item_id=None)
    item_id = cursor.lastrowid

    if not dedup_eligible:
        return InsertOutcome(outcome="inserted", item_id=item_id)

    parent = conn.execute(
        """
        SELECT id FROM items
        WHERE content_hash = ? AND id <> ? AND status <> 'duplicate'
          AND fetched_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 day')
        ORDER BY fetched_at, id LIMIT 1
        """,
        (content_hash, item_id),
    ).fetchone()
    if parent is None:
        return InsertOutcome(outcome="inserted", item_id=item_id)

    conn.execute(
        "UPDATE items SET status = 'duplicate', dup_of_item_id = ? WHERE id = ?",
        (parent["id"], item_id),
    )
    return InsertOutcome(outcome="duplicate", item_id=item_id)


def _backup(conn: sqlite3.Connection, db_path: Path) -> None:
    if not db_path.exists():
        return
    # 単純な file copy や wal_checkpoint 後の copy は、他プロセス(受信デーモン)稼働中に
    # WAL 未反映分を取りこぼしうる。SQLite backup API なら並行 writer がいても一貫する
    # (設計 05 §5 の日次バックアップと同じ理由で .backup 相当を使う)。
    bak = sqlite3.connect(db_path.with_name(db_path.name + ".bak"))
    try:
        conn.backup(bak)
    finally:
        bak.close()
