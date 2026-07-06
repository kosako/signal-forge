import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.analysis.analyzer import AnalysisBatch, ItemAnalysis
from app.config import AnalysisConfig, Config, ObsidianConfig
from app.connectors.base import FetchResult, FetchStats, RawItem
from app.db import apply_migrations, connect
from app.digest.builder import today_jst
from app.http import HttpClient
from app.run import run_pipeline

logger = logging.getLogger("test-run")

LONG_BODY = "分析対象の本文。" * 60


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    conn.execute(
        "INSERT INTO sources (type, name, config) VALUES ('rss', 'feed-a', '{\"url\": \"https://a\"}')"
    )
    yield conn
    conn.close()


class StaticConnector:
    source_type = "rss"

    def fetch(self, source_row, cursor):
        item = RawItem(
            external_id="guid-1",
            url="https://example.com/article",
            title="十分に長い記事タイトルのサンプルです",
            author=None,
            published_at=datetime.now(UTC),
            raw_content=f"<p>{LONG_BODY}</p>",
            meta={},
        )
        return FetchResult(
            items=[item],
            next_cursor={"etag": None, "last_modified": None},
            stats=FetchStats(items_fetched=1, api_reads=0, http_status=200, duration_ms=1),
        )


class ScriptedEngine:
    name = "codex-cli"
    model = "fake"

    def __init__(self, conn):
        self._conn = conn

    def complete_structured(self, prompt, schema, timeout_s=120):
        ids = [r["id"] for r in self._conn.execute("SELECT id FROM items WHERE status='new'")]
        return AnalysisBatch(
            results=[
                ItemAnalysis(
                    item_id=i, summary="要約", relevance=90, category="課題直結",
                    negative=[], novelty="new", topic=f"t{i}", reason="理由", implication="示唆",
                )
                for i in ids
            ]
        )


def make_config(tmp_path: Path) -> Config:
    profile = tmp_path / "interest-profile.md"
    profile.write_text("---\nupdated: 2026-07-06\n---\n# 関心\n")
    return Config(
        config_dir=tmp_path,
        obsidian=ObsidianConfig(vault_path=str(tmp_path / "vault")),
        analysis=AnalysisConfig(profile_path=str(profile)),
    )


def make_http() -> HttpClient:
    return HttpClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(404)),
        resolve_host=lambda host: ["93.184.216.34"],
        sleep=lambda _s: None,
    )


def test_full_pipeline_end_to_end(conn, tmp_path) -> None:
    config = make_config(tmp_path)
    failures = run_pipeline(
        conn, config, {"rss": StaticConnector()}, make_http(),
        lambda: ScriptedEngine(conn), logger, "01RUNID",
    )
    assert failures == []

    digest = conn.execute("SELECT * FROM digests").fetchone()
    assert digest["run_id"] == "01RUNID"  # run_id が digests を貫通
    assert digest["item_count"] == 1
    analysis = conn.execute("SELECT run_id FROM analyses").fetchone()
    assert analysis["run_id"] == "01RUNID"  # analyses も貫通
    item = conn.execute("SELECT status FROM items").fetchone()
    assert item["status"] == "digested"
    daily = tmp_path / "vault" / "SignalForge" / "Daily" / f"{today_jst()}.md"
    assert daily.exists()
    assert list((tmp_path / "vault" / "SignalForge" / "Items").glob("*.md"))


def test_rerun_is_idempotent(conn, tmp_path) -> None:
    config = make_config(tmp_path)
    args = (conn, config, {"rss": StaticConnector()}, make_http())
    assert run_pipeline(*args, lambda: ScriptedEngine(conn), logger, "01RUN1") == []
    assert run_pipeline(*args, lambda: ScriptedEngine(conn), logger, "01RUN2") == []
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM digests").fetchone()[0] == 1


def test_stage_failure_is_isolated_and_reported(conn, tmp_path) -> None:
    # プロファイル欠落 → analyze 段は失敗するが、digest(空)・purge まで進む
    config = Config(
        config_dir=tmp_path, obsidian=ObsidianConfig(vault_path=str(tmp_path / "vault"))
    )
    failures = run_pipeline(
        conn, config, {"rss": StaticConnector()}, make_http(),
        lambda: ScriptedEngine(conn), logger, "01RUNID",
    )
    assert "analyze" in failures
    assert "purge" not in failures
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1  # collect は成功
    digest = conn.execute("SELECT item_count FROM digests").fetchone()
    assert digest["item_count"] == 0  # 分析なしでも digest 段は空で完走


def test_export_failure_reported_but_purge_runs(conn, tmp_path) -> None:
    profile = tmp_path / "interest-profile.md"
    profile.write_text("---\nupdated: 2026-07-06\n---\n# 関心\n")
    config = Config(  # vault 未設定 → export 失敗
        config_dir=tmp_path,
        analysis=AnalysisConfig(profile_path=str(profile)),
    )
    failures = run_pipeline(
        conn, config, {"rss": StaticConnector()}, make_http(),
        lambda: ScriptedEngine(conn), logger, "01RUNID",
    )
    assert failures == ["export"]


def test_run_id_in_json_log_lines(conn, tmp_path) -> None:
    from app.logs import setup_logging

    log = setup_logging(tmp_path / "logs", "morning.log")
    config = make_config(tmp_path)
    run_pipeline(
        conn, config, {"rss": StaticConnector()}, make_http(),
        lambda: ScriptedEngine(conn), log, "01RUNID",
    )
    logging.shutdown()
    lines = [
        json.loads(line)
        for line in (tmp_path / "logs" / "morning.log").read_text().splitlines()
    ]
    stages = {line["stage"] for line in lines if line["run_id"] == "01RUNID"}
    # 主要ステージのログに run_id が貫通している
    assert {"collect", "extract", "analyze", "digest", "export", "purge", "run"} <= stages
