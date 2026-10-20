"""主のエンジン(Codex)が失敗した LLM の呼び出しを予備(Claude API)でやり直す(GH#156 / 設計 00 §5)。

使い分けは「残量を見て切り替える」ではなく「落ちたら次へ」: 主の失敗そのものを合図にする
(サブスクの残量は外から読めない。GH#153 の相談)。

- 主が失敗した呼び出しだけを予備でやり直す。主が答えた呼び出しには予備を使わない
- 主が続けて失敗したら(起動できないときは 1 回で)、同じ factory から作るエンジンは主を飛ばして
  予備へ行く(Codex が止まった朝に、チャンクごとに主の失敗を待たない)。主に戻るのは次の
  プロセスから(朝バッチは毎朝、返事は質問ごとに起動し直す)
- 予備も失敗したら、主の失敗と同じ種類のエラーにする(LLMUnavailableError なら段の失敗、
  LLMError なら劣化、という各段の今の扱いを変えない)
- 予備の呼び出しは台帳(llm_fallback_calls。migration 018)に残し、月(UTC の暦月。doctor #9 の
  当月と同じ)の上限額に達したら予備を使わない
- LLM の失敗(LLMError)以外の例外(実装の誤り)は予備に回さず、そのまま伝える
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import BaseModel

from app.analysis.llm import (
    DEFAULT_TIMEOUT_S,
    LLMEngine,
    LLMError,
    LLMUnavailableError,
    LLMUsage,
    create_anthropic_engine,
    create_engine,
)
from app.config import AnthropicApiConfig, Config

FALLBACK_STAGES = ("analyze", "trend", "dig", "answer")  # migration 018 の CHECK と同じ
# 主が続けてこの回数失敗したら、主を飛ばす(起動できない = LLMUnavailableError は 1 回で)
BREAKER_THRESHOLD = 2
PRIMARY_SKIPPED = "skipped"  # 台帳の primary_error: 主を飛ばした回
# codex の effort → Claude API の effort(ほかは同じ名前。DIG_EFFORTS と ANTHROPIC_EFFORTS)
_CLAUDE_EFFORTS = {"minimal": "low"}
_STAGE_LABELS = {"analyze": "記事の判定", "trend": "トレンド", "dig": "深掘り", "answer": "返事"}
# 主の失敗の種類(app/analysis/llm.py が上げるメッセージの書き出し)。ログにはこの種類だけを書き、
# 続く本文(codex の stderr の末尾・応答の先頭など、外部の文を含みうる)は書かない
_PRIMARY_FAILURE_KINDS = (
    "タイムアウト",
    "codex 非0終了",
    "codex 起動失敗",
    "codex の起動設定エラー",
    "codex が最終応答ファイルを書かなかった",
    "~/.codex/auth.json がない",
    "JSON パース失敗",
    "スキーマ検証失敗",
)


def _primary_failure_kind(error: LLMError | None) -> str:
    """主の失敗の種類(本文を含まない)。非0終了は終了コードも付ける。"""
    if error is None:
        return "続けて失敗したので主を呼ばなかった"
    message = str(error)
    for kind in _PRIMARY_FAILURE_KINDS:
        if message.startswith(kind):
            code = re.match(r"codex 非0終了\((-?\d{1,4})\)", message)
            return f"{kind}({code.group(1)})" if code else kind
    return "その他の失敗"


def fallback_cost_this_month(conn: sqlite3.Connection) -> float:
    """今の UTC の暦月に予備で使った額の見積もり(doctor #9 の当月と同じ月)。"""
    month_prefix = datetime.now(UTC).strftime("%Y-%m")
    return conn.execute(
        "SELECT coalesce(sum(cost_usd), 0) AS cost FROM llm_fallback_calls "
        "WHERE created_at LIKE ? || '%'",
        (month_prefix,),
    ).fetchone()["cost"]


class FallbackLedger:
    """予備の呼び出しの台帳と月の上限(1 つの段・1 回の実行ぶん)。"""

    def __init__(
        self, conn: sqlite3.Connection, cap_usd: float, *, stage: str, run_id: str | None
    ) -> None:
        if stage not in FALLBACK_STAGES:
            raise ValueError(f"未知の段: {stage}")
        self._conn = conn
        self._cap = cap_usd
        self._stage = stage
        self._run_id = run_id

    def spent(self) -> float:
        return fallback_cost_this_month(self._conn)

    def allows(self) -> bool:
        return self.spent() < self._cap

    @property
    def cap_usd(self) -> float:
        return self._cap

    def record(self, *, model: str, primary_error: str, ok: bool, usage: LLMUsage | None) -> None:
        self._conn.execute(
            "INSERT INTO llm_fallback_calls (run_id, stage, model, primary_error, ok,"
            " input_tokens, output_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._run_id,
                self._stage,
                model,
                primary_error,
                int(ok),
                usage.input_tokens if usage else None,
                usage.output_tokens if usage else None,
                usage.cost_usd if usage else 0.0,
            ),
        )


class _Breaker:
    """主が続けて失敗したかを、同じ factory から作るエンジンの間で共有する。"""

    def __init__(self) -> None:
        self.consecutive_failures = 0
        self.open = False
        # 主を飛ばした回に、予備も失敗したら返すエラーの種類(最後に見た主の失敗と同じ)
        self.last_error_class: type[LLMError] = LLMError

    def failure(self, error: LLMError) -> None:
        self.consecutive_failures += 1
        self.last_error_class = type(error)
        if isinstance(error, LLMUnavailableError) or (
            self.consecutive_failures >= BREAKER_THRESHOLD
        ):
            self.open = True

    def success(self) -> None:
        self.consecutive_failures = 0


class FallbackEngine:
    """主が失敗した呼び出しを予備でやり直す LLMEngine。

    `name` と `model` は最後に答えたエンジンのもの。呼び出しの直後に読めば、その呼び出しに
    答えたエンジンになる(analyzer は結果ごとに控えて analyses の engine / model に記録する。
    1 つのチャンクの中で主と予備が混ざりうるため)。
    """

    def __init__(
        self,
        primary: LLMEngine,
        make_fallback: Callable[[], LLMEngine],
        ledger: FallbackLedger,
        breaker: _Breaker,
        logger: logging.Logger,
        *,
        fallback_model: str,
        log_extra: dict | None = None,
    ) -> None:
        self._primary = primary
        self._make_fallback = make_fallback
        self._ledger = ledger
        self._breaker = breaker
        self._logger = logger
        self._fallback_model = fallback_model
        self._extra = log_extra or {}
        self._last: LLMEngine = primary

    @property
    def name(self) -> str:
        return self._last.name

    @property
    def model(self) -> str:
        return str(getattr(self._last, "model", "engine-default"))

    def complete_structured[M: BaseModel](
        self, prompt: str, schema: type[M], timeout_s: int = DEFAULT_TIMEOUT_S
    ) -> M:
        primary_error: LLMError | None = None
        if not self._breaker.open:
            try:
                result = self._primary.complete_structured(prompt, schema, timeout_s)
            except LLMError as e:
                primary_error = e
                self._breaker.failure(e)
            else:
                self._breaker.success()
                self._last = self._primary
                return result
        reason = type(primary_error).__name__ if primary_error else PRIMARY_SKIPPED
        # ログに書く主の失敗: 例外の型と種類だけ(本文は書かない)
        primary = f"{reason}・{_primary_failure_kind(primary_error)}"

        if not self._ledger.allows():
            self._logger.warning(
                f"主のエンジンが失敗したが、予備は今月の上限 ${self._ledger.cap_usd:.2f} に"
                f"達しているので使わない(主: {primary})",
                extra=self._extra,
            )
            raise self._error_like_primary(primary_error, "予備は今月の上限に達したため使わない")
        try:
            fallback = self._make_fallback()
        except LLMError as e:  # キーが無いなど。予備を作れない
            self._logger.warning(
                f"主のエンジンが失敗したが、予備を作れない(主: {primary} / 予備: "
                f"{type(e).__name__}: {e})",
                extra=self._extra,
            )
            raise self._error_like_primary(primary_error, f"予備を作れない: {e}") from e
        try:
            result = fallback.complete_structured(prompt, schema, timeout_s)
        except LLMError as e:
            self._ledger.record(
                model=self._fallback_model,
                primary_error=reason,
                ok=False,
                usage=getattr(fallback, "last_usage", None),
            )
            self._logger.warning(
                f"主のエンジンも予備も失敗(主: {primary} / 予備: {type(e).__name__}: {e})",
                extra=self._extra,
            )
            raise self._error_like_primary(
                primary_error, f"予備も失敗: {type(e).__name__}: {e}"
            ) from e
        self._ledger.record(
            model=self._fallback_model,
            primary_error=reason,
            ok=True,
            usage=getattr(fallback, "last_usage", None),
        )
        self._logger.warning(
            f"主のエンジンが失敗したため予備({self._fallback_model})で代行(主: {primary})",
            extra=self._extra,
        )
        self._last = fallback
        return result

    def _error_like_primary(self, primary_error: LLMError | None, note: str) -> LLMError:
        """主の失敗と同じ種類のエラー(各段の劣化・失敗の扱いを変えない)。"""
        if primary_error is None:  # 主を飛ばした回: 最後に見た主の失敗と同じ種類
            return self._breaker.last_error_class(f"主のエンジンは続けて失敗していた。{note}")
        return type(primary_error)(f"{primary_error}。{note}")


def fallback_engine_factory(
    conn: sqlite3.Connection,
    config: Config,
    env: dict[str, str],
    logger: logging.Logger,
    *,
    stage: str,
    run_id: str | None = None,
    primary: Callable[[str], LLMEngine] | None = None,
) -> Callable[[str], LLMEngine]:
    """effort → エンジン。予備が無効(既定)なら主のエンジンをそのまま返す。

    1 つの factory が作るエンジンは、主が続けて失敗したかの記録を共有する(1 つの段・1 回の
    実行ぶん)。予備のエンジンは主が失敗したときに初めて作る(キーが無いなどの失敗も、その
    ときの呼び出しの失敗として扱う)。primary は主のエンジンの作り方(既定は config の
    `[llm].engine`。朝バッチは run_pipeline に渡された factory を使う)。
    """
    settings = config.llm_fallback
    if primary is None:

        def primary(effort: str) -> LLMEngine:
            return create_engine(config.llm.engine, model_reasoning_effort=effort)

    if settings.engine == "":
        return primary
    ledger = FallbackLedger(conn, settings.monthly_cap_usd, stage=stage, run_id=run_id)
    breaker = _Breaker()
    extra = {"run_id": run_id, "stage": stage}

    def make(effort: str) -> LLMEngine:
        claude = AnthropicApiConfig(
            model=settings.model,
            effort=_CLAUDE_EFFORTS.get(effort, effort),
            max_tokens=settings.max_tokens,
            input_usd_per_mtok=settings.input_usd_per_mtok,
            output_usd_per_mtok=settings.output_usd_per_mtok,
        )
        return FallbackEngine(
            primary(effort),
            lambda: create_anthropic_engine(claude, env),
            ledger,
            breaker,
            logger,
            fallback_model=settings.model,
            log_extra=extra,
        )

    return make


def run_fallback_notice(conn: sqlite3.Connection, run_id: str, cap_usd: float) -> str | None:
    """朝バッチ 1 回ぶんの予備の利用の知らせ(使っていなければ None)。

    予備で答えられても、Codex が止まっていることには変わらないので知らせる(黙って Claude API に
    頼り続けない)。文言は固定の語と数だけ(外部の文字列を含まない)。
    """
    rows = conn.execute(
        "SELECT stage, count(*) AS calls, sum(ok) AS answered, sum(cost_usd) AS cost"
        " FROM llm_fallback_calls WHERE run_id = ? GROUP BY stage ORDER BY min(id)",
        (run_id,),
    ).fetchall()
    if not rows:
        return None
    lines = ["⚠️ Codex(主のエンジン)が失敗したため、Claude API で代行しました"]
    for row in rows:
        failed = row["calls"] - row["answered"]
        detail = f"(うち予備も失敗した呼び出し {failed} 回)" if failed else ""
        lines.append(f"・{_STAGE_LABELS[row['stage']]}: {row['calls']} 回{detail}")
    spent = sum(row["cost"] for row in rows)
    lines.append(
        f"今回 ${spent:.2f} / 今月の予備 ${fallback_cost_this_month(conn):.2f}"
        f"(上限 ${cap_usd:.2f})"
    )
    lines.append("Codex の上限・ログインの状態を確かめてください(runbook §14)")
    return "\n".join(lines)
