import json
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from app.logs import BACKUP_DAYS, setup_logging, stage_logger


def read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_json_lines_format_with_context(tmp_path: Path) -> None:
    logger = setup_logging(tmp_path, "morning.log")
    log = stage_logger(logger, run_id="01TEST", stage="collect", source_id=3)
    log.info("fetched", extra={"extra": {"items": 12}})
    logging.shutdown()

    (line,) = read_lines(tmp_path / "morning.log")
    assert line["level"] == "INFO"
    assert line["run_id"] == "01TEST"
    assert line["stage"] == "collect"
    assert line["source_id"] == 3
    assert line["item_id"] is None
    assert line["msg"] == "fetched"
    assert line["extra"] == {"items": 12}
    assert "T" in line["ts"]  # ISO 8601


def test_rotation_is_daily_14_generations(tmp_path: Path) -> None:
    logger = setup_logging(tmp_path, "morning.log")
    (handler,) = logger.handlers
    assert isinstance(handler, TimedRotatingFileHandler)
    assert handler.when == "MIDNIGHT"
    assert handler.backupCount == BACKUP_DAYS == 14


def test_setup_twice_does_not_duplicate_handlers(tmp_path: Path) -> None:
    setup_logging(tmp_path, "morning.log")
    logger = setup_logging(tmp_path, "morning.log")
    assert len(logger.handlers) == 1
    logger.info("once")
    logging.shutdown()
    assert len(read_lines(tmp_path / "morning.log")) == 1
