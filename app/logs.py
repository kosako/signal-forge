"""JSON Lines ログ基盤(設計 05 §3)。

- 出力先: `~/.local/state/intel/logs/`(引数で差し替え可能)。
- 形式: 1行1 JSON。フィールド: ts / level / run_id / stage / source_id / item_id / msg / extra。
- ローテーション: TimedRotatingFileHandler(日次・14世代)。
- 外部本文・プロンプト全文はログに書かない(untrusted data の拡散防止)。書かない規律は
  呼び出し側の責務で、本モジュールは msg / extra をそのまま出す。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

DEFAULT_LOG_DIR = Path.home() / ".local" / "state" / "intel" / "logs"

BACKUP_DAYS = 14

# JSON 行に含める、logging 標準属性以外のフィールド(無ければ null)
_CONTEXT_FIELDS = ("run_id", "stage", "source_id", "item_id", "extra")


class JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
        }
        for name in _CONTEXT_FIELDS:
            line[name] = getattr(record, name, None)
        line["msg"] = record.getMessage()
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        return json.dumps(line, ensure_ascii=False, default=str)


def setup_logging(log_dir: Path, filename: str, *, verbose: bool = False) -> logging.Logger:
    """JSON Lines ロガーを構成して返す。

    filename は `morning.log` / `poller.log`(launchd の2プロセスに対応)。
    再呼び出し時はハンドラを付け直す(テスト・poll の再起動で重複出力しない)。
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(
        log_dir / filename, when="midnight", backupCount=BACKUP_DAYS, encoding="utf-8"
    )
    handler.setFormatter(JsonLinesFormatter())

    logger = logging.getLogger("intel")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    for old in logger.handlers[:]:
        logger.removeHandler(old)
        old.close()
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def stage_logger(logger: logging.Logger, **context: object) -> logging.LoggerAdapter:
    """run_id / stage / source_id / item_id を固定したアダプタを返す。

    使い方: `log = stage_logger(logger, run_id=run_id, stage="collect")` →
    `log.info("fetched", extra={"extra": {"items": 12}})` の extra と併用できる。
    """
    unknown = set(context) - set(_CONTEXT_FIELDS)
    if unknown:
        raise ValueError(f"未対応のログフィールド: {', '.join(sorted(unknown))}")

    class _ContextAdapter(logging.LoggerAdapter):
        def process(self, msg: str, kwargs: dict) -> tuple[str, dict]:  # type: ignore[override]
            extra = {**self.extra, **kwargs.get("extra", {})}
            kwargs["extra"] = extra
            return msg, kwargs

    return _ContextAdapter(logger, context)
