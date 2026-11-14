"""質問への返事(GH#126 / 設計 06 §6)と今すぐの深掘り(GH#388 / 設計 06 §3)の起動係。poller から
`intel answer`・`intel dig-now` を別プロセスで起動する。

- LLM の待ち(数十秒〜数分)で poller の受信(getUpdates)と内蔵タイマーを止めないため、
  返事は別プロセスで作る。子は poller とは別のセッションで動かし、poller が再起動しても
  作りかけの返事を道連れにしない(子の側の flock で同時に 1 つに絞るので重複しない)
- 起動のきっかけ: 質問・依頼を台帳に積んだ直後(kick)と、各周期の見回り(sweep)。子が台帳を
  読み終えた直後に積まれた質問は、その子では拾われないため見回りで拾う
- 送れていない返事・記録できていない take(GH#106)・送れていない深掘りのカードだけが
  残っているときは RETRY_UNSENT_S おきにだけ起動する(送信先や vault の障害が続く間に毎周期
  起動しない)
- 今すぐの深掘りは朝バッチの dig 段とロックを共有する。段の間に起動した子は何もせず終わり、
  依頼が残るので、見回りが次の周期に起動し直す(段が終わった後に作る)
"""

from __future__ import annotations

import logging
import sqlite3
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

from app.dig import has_now_requests, has_unsent_cards
from app.dig_answer import has_pending, has_unsent
from app.dig_take import has_unrecorded_take
from app.investigate_ledger import has_investigation_requests, has_unsent_investigations

RETRY_UNSENT_S = 600


class _ChildWorker(ABC):
    """`python -m app.cli <subcommand>` を 1 つだけ動かす起動係。

    何を見て起動するかは派生のクラスで決める。
    """

    subcommand: str

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
            self.subcommand,
            "--config-dir",
            str(config_dir),
        ]
        self._popen = popen
        self._clock = clock
        self._proc: subprocess.Popen | None = None
        self._last_unsent_start: float | None = None

    @abstractmethod
    def _has_pending(self, conn: sqlite3.Connection) -> bool:
        """子がすぐに片付ける仕事があるか(あれば毎周期でも起動する)。"""

    @abstractmethod
    def _has_unsent(self, conn: sqlite3.Connection) -> bool:
        """送れていない分だけが残っているか(RETRY_UNSENT_S おきに起動する)。"""

    def kick(self, logger: logging.Logger) -> None:
        """台帳に積んだ直後に呼ぶ。子が動いていなければ起動する。"""
        if not self._running(logger):
            self._start(logger)

    def sweep(self, conn: sqlite3.Connection, logger: logging.Logger) -> None:
        """各周期の見回り。残っている仕事があれば起動する(送れていない分は RETRY_UNSENT_S おき)。"""
        if self._running(logger):
            return
        if self._has_pending(conn):
            self._start(logger)
            return
        if self._has_unsent(conn) and (
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
            logger.error(
                f"intel {self.subcommand} が異常終了(exit={code})", extra={"stage": "poll"}
            )
        self._proc = None
        return False

    def _start(self, logger: logging.Logger) -> None:
        try:
            # 標準出力・エラーは poller のもの(launchd のログ)を引き継ぐ。詳細は子のログ
            self._proc = self._popen(
                self._command, stdin=subprocess.DEVNULL, start_new_session=True
            )
        except OSError as e:
            logger.error(f"intel {self.subcommand} を起動できない: {e}", extra={"stage": "poll"})


class AnswerWorker(_ChildWorker):
    """質問への返事(GH#126)。答えていない質問・送れていない返事・記録できていない take を見る。"""

    subcommand = "answer"

    def _has_pending(self, conn: sqlite3.Connection) -> bool:
        return has_pending(conn)

    def _has_unsent(self, conn: sqlite3.Connection) -> bool:
        return has_unsent(conn) or has_unrecorded_take(conn)


class DigNowWorker(_ChildWorker):
    """今すぐの深掘り(GH#388)と出来事の調査(GH#415)。作っていない依頼と、送れていないカードを見る。

    送れていないカードは作った経路を問わない(朝の段が今すぐの依頼を引き受けて作った分も、
    朝を待たずに送り直す。dig-now は台帳の未公開・未送信を公開・送信してから依頼を見る)。
    出来事の調査も dig-now の中で作るので、同じ子が受け持つ。
    """

    subcommand = "dig-now"

    def _has_pending(self, conn: sqlite3.Connection) -> bool:
        return has_now_requests(conn) or has_investigation_requests(conn)

    def _has_unsent(self, conn: sqlite3.Connection) -> bool:
        return has_unsent_cards(conn) or has_unsent_investigations(conn)
