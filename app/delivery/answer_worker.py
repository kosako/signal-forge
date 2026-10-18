"""質問への返事の起動係(GH#126 / 設計 06 §6)。poller から `intel answer` を別プロセスで起動する。

- LLM の待ち(数十秒〜数分)で poller の受信(getUpdates)と内蔵タイマーを止めないため、
  返事は別プロセスで作る。子は poller とは別のセッションで動かし、poller が再起動しても
  作りかけの返事を道連れにしない(子の側の flock で同時に 1 つに絞るので重複しない)
- 起動のきっかけ: 質問を台帳に積んだ直後(kick)と、各周期の見回り(sweep)。子が台帳を
  読み終えた直後に積まれた質問は、その子では拾われないため見回りで拾う
- 送れていない返事・記録できていない take(GH#106)だけが残っているときは RETRY_UNSENT_S
  おきにだけ起動する(送信先や vault の障害が続く間に毎周期起動しない)
"""

from __future__ import annotations

import logging
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from app.dig_answer import has_pending, has_unsent
from app.dig_take import has_unrecorded_take

RETRY_UNSENT_S = 600


class AnswerWorker:
    def __init__(
        self,
        config_dir: Path,
        *,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._command = [
            sys.executable,
            "-m",
            "app.cli",
            "answer",
            "--config-dir",
            str(config_dir),
        ]
        self._popen = popen
        self._clock = clock
        self._proc: subprocess.Popen | None = None
        self._last_unsent_start: float | None = None

    def kick(self, logger: logging.Logger) -> None:
        """質問を積んだ直後に呼ぶ。子が動いていなければ起動する。"""
        if not self._running(logger):
            self._start(logger)

    def sweep(self, conn: sqlite3.Connection, logger: logging.Logger) -> None:
        """各周期の見回り。答えていない質問・送れていない返事・記録できていない take が
        残っていれば起動する。"""
        if self._running(logger):
            return
        if has_pending(conn):
            self._start(logger)
            return
        if (has_unsent(conn) or has_unrecorded_take(conn)) and (
            self._last_unsent_start is None
            or self._clock() - self._last_unsent_start >= RETRY_UNSENT_S
        ):
            self._last_unsent_start = self._clock()
            self._start(logger)

    def _running(self, logger: logging.Logger) -> bool:
        if self._proc is None:
            return False
        code = self._proc.poll()  # 終わった子はここで回収する(ゾンビを残さない)
        if code is None:
            return True
        if code != 0:
            logger.error(f"intel answer が異常終了(exit={code})", extra={"stage": "poll"})
        self._proc = None
        return False

    def _start(self, logger: logging.Logger) -> None:
        try:
            # 標準出力・エラーは poller のもの(launchd のログ)を引き継ぐ。詳細は answer.log
            self._proc = self._popen(
                self._command, stdin=subprocess.DEVNULL, start_new_session=True
            )
        except OSError as e:
            logger.error(f"intel answer を起動できない: {e}", extra={"stage": "poll"})
