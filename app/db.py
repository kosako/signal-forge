"""SQLite 接続ファクトリと migrations ランナー(設計 01 §1・§10)。

- 接続は autocommit モードで返す。**トランザクションは呼び出し側が明示的に張る**
  (`BEGIN IMMEDIATE` … `COMMIT`)。理由: collect の「全 INSERT + カーソル更新を
  同一トランザクション」等、境界は業務側が決める(設計 02 §1)。
- マイグレーションは forward-only(down なし)。適用前に DB ファイルを `.bak` に
  コピーする(設計 01 §10)。
"""

from __future__ import annotations

import re
import shutil
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
    - 未適用があるときのみ、適用前に DB を `<name>.bak` へコピー(単一ファイル復元)
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version    INTEGER PRIMARY KEY,
          filename   TEXT NOT NULL,
          applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        ) STRICT
        """
    )
    migrations = discover_migrations(migrations_dir)
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


def _backup(conn: sqlite3.Connection, db_path: Path) -> None:
    if not db_path.exists():
        return
    # WAL の未反映分を main ファイルへ書き戻してからコピーする(コピーの一貫性)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.copy2(db_path, db_path.with_name(db_path.name + ".bak"))
