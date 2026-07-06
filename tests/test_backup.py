import logging
import sqlite3
import tarfile
from pathlib import Path

import pytest

from app.backup import GENERATIONS, backup_daily
from app.config import Config, StorageConfig
from app.db import apply_migrations, connect

logger = logging.getLogger("test-backup")


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    yield conn
    conn.close()


def make_config(tmp_path: Path) -> Config:
    config_dir = tmp_path / "conf"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.toml").write_text("# config\n", encoding="utf-8")
    (config_dir / ".env").write_text("TELEGRAM_BOT_TOKEN=tok\n", encoding="utf-8")
    return Config(
        config_dir=config_dir,
        storage=StorageConfig(backup_dir=str(tmp_path / "backups")),
    )


def make_prompts(tmp_path: Path) -> Path:
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    (prompts / "analyze-v001.md").write_text("prompt", encoding="utf-8")
    return prompts


def test_backup_creates_600_archive_with_expected_members(conn, tmp_path) -> None:
    config = make_config(tmp_path)
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed')")

    archive = backup_daily(conn, config, logger, prompts_dir=make_prompts(tmp_path))

    assert archive.exists()
    assert archive.stat().st_mode & 0o777 == 0o600  # secrets 同梱のため 600 強制
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert "intel.db" in names
    assert "config/config.toml" in names
    assert "config/.env" in names
    assert "prompts/analyze-v001.md" in names


def test_backup_snapshot_is_consistent_db(conn, tmp_path) -> None:
    config = make_config(tmp_path)
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed')")

    archive = backup_daily(conn, config, logger, prompts_dir=make_prompts(tmp_path))

    extract_dir = tmp_path / "restore"
    with tarfile.open(archive) as tar:
        tar.extract("intel.db", path=extract_dir, filter="data")
    restored = sqlite3.connect(extract_dir / "intel.db")
    names = {row[0] for row in restored.execute("SELECT name FROM sources")}
    restored.close()
    assert "feed" in names  # backup API で一貫スナップショットが取れている


def test_same_day_rerun_replaces_archive(conn, tmp_path) -> None:
    config = make_config(tmp_path)
    prompts = make_prompts(tmp_path)
    first = backup_daily(conn, config, logger, prompts_dir=prompts)
    second = backup_daily(conn, config, logger, prompts_dir=prompts)
    assert first == second
    archives = list(config.backup_dir.glob("intel-*.tar.gz"))
    assert len(archives) == 1  # 1日1世代(冪等)
    assert not list(config.backup_dir.glob(".*partial*"))  # 一時ファイルが残らない


def test_rotation_keeps_14_generations(conn, tmp_path) -> None:
    config = make_config(tmp_path)
    config.backup_dir.mkdir(parents=True)
    for day in range(1, GENERATIONS + 3):  # 16 世代分の旧ファイル
        (config.backup_dir / f"intel-202606{day:02d}.tar.gz").write_bytes(b"")

    backup_daily(conn, config, logger, prompts_dir=make_prompts(tmp_path))

    archives = sorted(config.backup_dir.glob("intel-*.tar.gz"))
    assert len(archives) == GENERATIONS
    # 最古の世代から削除され、当日分が残る
    assert archives[-1].name.startswith("intel-2026")
    assert not (config.backup_dir / "intel-20260601.tar.gz").exists()
