"""主のエンジンが失敗した呼び出しを予備でやり直す(GH#156)。"""

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from app.analysis.fallback import (
    BREAKER_THRESHOLD,
    PRIMARY_SKIPPED,
    FallbackEngine,
    FallbackLedger,
    _Breaker,
    fallback_cost_this_month,
    fallback_engine_factory,
)
from app.analysis.llm import (
    AnthropicApiEngine,
    CodexCliEngine,
    LLMError,
    LLMUnavailableError,
    LLMUsage,
    complete_with_retry,
)
from app.config import Config, LLMFallbackConfig
from app.db import apply_migrations, connect

logger = logging.getLogger("test-fallback")


class Verdict(BaseModel):
    score: int


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    yield conn
    conn.close()


class Scripted:
    """呼ばれるたびに次の結果(Verdict か例外)を返す。予備の役では usage も残す。"""

    def __init__(self, name: str, outcomes: list, *, model: str = "m", cost: float = 0.0):
        self.name = name
        self.model = model
        self.calls = 0
        self._outcomes = list(outcomes)
        self._cost = cost
        self.last_usage: LLMUsage | None = None

    def complete_structured(self, prompt, schema, timeout_s=120):
        self.calls += 1
        outcome = self._outcomes.pop(0)
        self.last_usage = LLMUsage(100, 10, self._cost) if self._cost else None
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def build(conn, primary, fallback, *, cap=10.0, breaker=None, stage="analyze", run_id="run-1"):
    ledger = FallbackLedger(conn, cap, stage=stage, run_id=run_id)
    return FallbackEngine(
        primary,
        lambda: fallback,
        ledger,
        breaker or _Breaker(),
        logger,
        fallback_model="claude-sonnet-5-5",
    )


def ledger_rows(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM llm_fallback_calls ORDER BY id").fetchall()


def test_primary_success_does_not_touch_fallback(conn) -> None:
    primary = Scripted("codex-cli", [Verdict(score=1)], model="engine-default")
    fallback = Scripted("anthropic-api", [])
    engine = build(conn, primary, fallback)
    assert engine.complete_structured("p", Verdict) == Verdict(score=1)
    assert fallback.calls == 0 and ledger_rows(conn) == []
    assert (engine.name, engine.model) == ("codex-cli", "engine-default")


def test_primary_failure_is_answered_by_fallback_and_recorded(conn) -> None:
    primary = Scripted("codex-cli", [LLMError("codex 非0終了(1): usage limit")])
    fallback = Scripted("anthropic-api", [Verdict(score=2)], model="claude-sonnet-5-5", cost=0.03)
    engine = build(conn, primary, fallback, stage="dig", run_id="run-9")
    assert engine.complete_structured("p", Verdict) == Verdict(score=2)
    # 記録には答えたエンジンを使う(analyzer が呼び出しの後に読む)
    assert (engine.name, engine.model) == ("anthropic-api", "claude-sonnet-5-5")
    (row,) = ledger_rows(conn)
    assert (row["stage"], row["run_id"], row["model"]) == ("dig", "run-9", "claude-sonnet-5-5")
    assert (row["primary_error"], row["ok"]) == ("LLMError", 1)
    assert (row["input_tokens"], row["output_tokens"], row["cost_usd"]) == (100, 10, 0.03)


def test_breaker_skips_primary_after_consecutive_failures(conn) -> None:
    failures = [LLMError("x")] * BREAKER_THRESHOLD
    primary = Scripted("codex-cli", failures)
    fallback = Scripted("anthropic-api", [Verdict(score=i) for i in range(4)], cost=0.01)
    engine = build(conn, primary, fallback)
    for _ in range(4):
        engine.complete_structured("p", Verdict)
    # 主は続けて失敗した 2 回だけ呼ばれ、その後は飛ばす(待たされない)
    assert primary.calls == BREAKER_THRESHOLD
    assert fallback.calls == 4
    reasons = [row["primary_error"] for row in ledger_rows(conn)]
    assert reasons == ["LLMError", "LLMError", PRIMARY_SKIPPED, PRIMARY_SKIPPED]


def test_primary_success_resets_the_consecutive_count(conn) -> None:
    primary = Scripted(
        "codex-cli", [LLMError("x"), Verdict(score=0), LLMError("x"), Verdict(score=0)]
    )
    fallback = Scripted("anthropic-api", [Verdict(score=9)] * 2)
    engine = build(conn, primary, fallback)
    for _ in range(4):
        engine.complete_structured("p", Verdict)
    assert primary.calls == 4  # 続けての失敗ではないので、主を飛ばさない


def test_unavailable_primary_opens_breaker_at_once(conn) -> None:
    primary = Scripted("codex-cli", [LLMUnavailableError("codex 起動失敗")])
    fallback = Scripted("anthropic-api", [Verdict(score=1), Verdict(score=2)])
    engine = build(conn, primary, fallback)
    engine.complete_structured("p", Verdict)
    engine.complete_structured("p", Verdict)
    assert primary.calls == 1


def test_breaker_is_shared_across_engines_of_one_factory(conn) -> None:
    breaker = _Breaker()
    primary = Scripted("codex-cli", [LLMUnavailableError("codex 起動失敗")])
    fallback = Scripted("anthropic-api", [Verdict(score=1), Verdict(score=2)])
    build(conn, primary, fallback, breaker=breaker).complete_structured("p", Verdict)
    build(conn, primary, fallback, breaker=breaker).complete_structured("p", Verdict)
    assert primary.calls == 1


@pytest.mark.parametrize("error_class", [LLMError, LLMUnavailableError])
def test_fallback_failure_raises_like_primary(conn, error_class) -> None:
    # 各段の今の扱い(LLMError = 劣化 / LLMUnavailableError = 段の失敗)を変えない
    primary = Scripted("codex-cli", [error_class("主の失敗")])
    fallback = Scripted("anthropic-api", [LLMError("Anthropic API 529")], cost=0.02)
    engine = build(conn, primary, fallback)
    with pytest.raises(error_class) as excinfo:
        engine.complete_structured("p", Verdict)
    assert type(excinfo.value) is error_class
    assert "主のエンジンが失敗" in str(excinfo.value) and "予備も失敗" in str(excinfo.value)
    (row,) = ledger_rows(conn)
    assert (row["ok"], row["cost_usd"]) == (0, 0.02)  # 失敗した応答の課金も残す


_CLAUDE_REPLY = {
    "content": [{"type": "text", "text": '{"score": 1}'}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 10, "output_tokens": 5},
}
# 費用が分からない呼び出しの最大の額: 入力は prompt「記事」の UTF-8 のバイト数(6)、出力は
# max_tokens(1000)。6 × $2/M + 1000 × $10/M
_UNKNOWN_COST_BOUND = 0.010012


def claude(handler) -> AnthropicApiEngine:
    """予備の役の実物(出力の上限 1000・Sonnet 5.5 の単価)。応答は handler が返す。"""
    return AnthropicApiEngine(
        api_key="k",
        model="claude-sonnet-5-5",
        effort="low",
        max_tokens=1000,
        input_usd_per_mtok=2.0,
        output_usd_per_mtok=10.0,
        transport=httpx.MockTransport(handler),
    )


def raising(error_class):
    def handler(request: httpx.Request) -> httpx.Response:
        raise error_class("x", request=request)

    return handler


@pytest.mark.parametrize(
    ("handler", "ok", "tokens", "cost"),
    [
        # 送った後に応答が来ない: 費用が分からないので、課金されうる最大の額で積む(GH#241)
        (raising(httpx.ReadTimeout), 0, (None, None), _UNKNOWN_COST_BOUND),
        (raising(httpx.RemoteProtocolError), 0, (None, None), _UNKNOWN_COST_BOUND),
        # 応答は来たが usage を読めない: 同じく最大の額
        (
            lambda request: httpx.Response(200, json={**_CLAUDE_REPLY, "usage": None}),
            1,
            (None, None),
            _UNKNOWN_COST_BOUND,
        ),
        # 接続を張れなかった(送っていない)・エラーの応答: 課金されないので 0 のまま
        (raising(httpx.ConnectError), 0, (None, None), 0.0),
        (raising(httpx.ConnectTimeout), 0, (None, None), 0.0),
        (lambda request: httpx.Response(529, text="overloaded"), 0, (None, None), 0.0),
        # 普通の応答: usage の実額のまま(10 × $2/M + 5 × $10/M)
        (lambda request: httpx.Response(200, json=_CLAUDE_REPLY), 1, (10, 5), 0.00007),
    ],
    ids=[
        "read-timeout",
        "disconnect",
        "no-usage",
        "connect-error",
        "connect-timeout",
        "529",
        "usage",
    ],
)
def test_fallback_cost_when_usage_is_unknown(conn, handler, ok, tokens, cost) -> None:
    engine = build(conn, Scripted("codex-cli", [LLMError("codex 非0終了(1)")]), claude(handler))
    if ok:
        assert engine.complete_structured("記事", Verdict) == Verdict(score=1)
    else:
        with pytest.raises(LLMError, match="予備も失敗"):
            engine.complete_structured("記事", Verdict)
    (row,) = ledger_rows(conn)
    assert row["ok"] == ok
    # トークン数が空 = usage を受け取れなかった呼び出し(額は実額でなく見積もり)
    assert (row["input_tokens"], row["output_tokens"]) == tokens
    assert row["cost_usd"] == pytest.approx(cost)


def test_skipped_primary_then_fallback_failure_keeps_last_primary_class(conn) -> None:
    primary = Scripted("codex-cli", [LLMUnavailableError("codex 起動失敗")])
    fallback = Scripted("anthropic-api", [Verdict(score=1), LLMError("529")])
    engine = build(conn, primary, fallback)
    engine.complete_structured("p", Verdict)
    with pytest.raises(LLMUnavailableError, match="続けて失敗"):
        engine.complete_structured("p", Verdict)


def test_fallback_not_used_over_monthly_cap(conn) -> None:
    start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    for moment, cost in ((start, 4.0), (start - timedelta(seconds=1), 100.0)):
        conn.execute(
            "INSERT INTO llm_fallback_calls (stage, model, primary_error, ok, cost_usd,"
            " created_at) VALUES ('analyze', 'm', 'LLMError', 1, ?, ?)",
            (cost, moment.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
    assert fallback_cost_this_month(conn) == 4.0  # 前の月の最後の 1 秒は数えない
    primary = Scripted("codex-cli", [LLMError("x"), LLMError("x")])
    fallback = Scripted("anthropic-api", [Verdict(score=1)])
    with pytest.raises(LLMError, match="上限"):
        build(conn, primary, fallback, cap=4.0).complete_structured("p", Verdict)
    assert fallback.calls == 0
    # 上限の手前なら使う
    assert build(conn, primary, fallback, cap=4.01).complete_structured("p", Verdict)
    assert fallback.calls == 1


def test_unknown_cost_counts_toward_monthly_cap(conn) -> None:
    # 応答が来ずに費用が分からない呼び出しも、最大の額で上限の判定に効く(0 円で数えると、
    # 上限に達しても予備を使い続ける。GH#241)
    sent: list[httpx.Request] = []

    def timeout(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        raise httpx.ReadTimeout("slow", request=request)

    primary = Scripted("codex-cli", [LLMError("x"), LLMError("x")])
    engine = build(conn, primary, claude(timeout), cap=0.01)
    with pytest.raises(LLMError, match="予備も失敗"):
        engine.complete_structured("記事", Verdict)
    assert fallback_cost_this_month(conn) == pytest.approx(_UNKNOWN_COST_BOUND)
    with pytest.raises(LLMError, match="上限"):
        engine.complete_structured("記事", Verdict)
    assert len(sent) == 1  # 上限に達したので、2 回目は予備を呼ばない


def test_fallback_that_cannot_be_built_raises_like_primary(conn) -> None:
    def no_key():
        raise LLMUnavailableError(".env に INTEL_ANTHROPIC_API_KEY が無い")

    engine = FallbackEngine(
        Scripted("codex-cli", [LLMError("x")]),
        no_key,
        FallbackLedger(conn, 10.0, stage="answer", run_id=None),
        _Breaker(),
        logger,
        fallback_model="claude-sonnet-5-5",
    )
    with pytest.raises(LLMError, match="予備を作れない") as excinfo:
        engine.complete_structured("p", Verdict)
    assert type(excinfo.value) is LLMError  # 予備の側の種類(Unavailable)にしない
    assert ledger_rows(conn) == []


def test_programming_error_is_not_sent_to_fallback(conn) -> None:
    primary = Scripted("codex-cli", [RuntimeError("バグ")])
    fallback = Scripted("anthropic-api", [Verdict(score=1)])
    with pytest.raises(RuntimeError):
        build(conn, primary, fallback).complete_structured("p", Verdict)
    assert fallback.calls == 0


def test_works_under_complete_with_retry(conn) -> None:
    # 予備も失敗する呼び出しは、既存のやり直し(同じエンジンで 1 回)の分だけ繰り返す
    primary = Scripted("codex-cli", [LLMError("x"), LLMError("y")])
    fallback = Scripted("anthropic-api", [LLMError("a"), LLMError("b")])
    engine = build(conn, primary, fallback)
    with pytest.raises(LLMError):
        complete_with_retry(engine, "p", Verdict)
    assert (primary.calls, fallback.calls) == (2, 2)
    assert [row["ok"] for row in ledger_rows(conn)] == [0, 0]


def test_ledger_rejects_unknown_stage(conn) -> None:
    with pytest.raises(ValueError):
        FallbackLedger(conn, 1.0, stage="collect", run_id=None)


def test_factory_without_fallback_returns_primary(conn, tmp_path) -> None:
    factory = fallback_engine_factory(conn, Config(config_dir=tmp_path), {}, logger, stage="dig")
    assert isinstance(factory("medium"), CodexCliEngine)


def test_factory_maps_effort_and_builds_fallback_lazily(conn, tmp_path, monkeypatch) -> None:
    config = Config(
        config_dir=tmp_path,
        llm_fallback=LLMFallbackConfig(engine="anthropic-api", model="claude-sonnet-5-5"),
    )
    built: list[AnthropicApiEngine] = []

    def fake_create(settings, env):
        assert env == {"INTEL_ANTHROPIC_API_KEY": "k"}
        engine = AnthropicApiEngine(
            api_key="k",
            model=settings.model,
            effort=settings.effort,
            max_tokens=settings.max_tokens,
            input_usd_per_mtok=settings.input_usd_per_mtok,
            output_usd_per_mtok=settings.output_usd_per_mtok,
        )
        built.append(engine)
        return engine

    monkeypatch.setattr("app.analysis.fallback.create_anthropic_engine", fake_create)
    factory = fallback_engine_factory(
        conn, config, {"INTEL_ANTHROPIC_API_KEY": "k"}, logger, stage="answer"
    )
    engine = factory("minimal")
    assert isinstance(engine, FallbackEngine) and engine.name == "codex-cli"
    assert built == []  # 主が失敗するまで予備は作らない
    engine._make_fallback()
    assert (built[0].model, built[0]._effort) == ("claude-sonnet-5-5", "low")  # minimal → low
    assert built[0]._input_price == 2.0 and built[0]._output_price == 10.0


def test_breaker_is_shared_within_a_factory_and_not_across(conn, tmp_path, monkeypatch) -> None:
    # 公開の入口(factory)経由: 同じ factory のエンジンは主を飛ばし、別の factory では主を呼ぶ
    primary = Scripted("codex-cli", [LLMUnavailableError("codex 起動失敗")] * 2)
    fallback = Scripted("anthropic-api", [Verdict(score=i) for i in range(3)])
    monkeypatch.setattr(
        "app.analysis.fallback.create_engine", lambda name, model_reasoning_effort: primary
    )
    monkeypatch.setattr(
        "app.analysis.fallback.create_anthropic_engine", lambda settings, env: fallback
    )
    config = Config(config_dir=tmp_path, llm_fallback=LLMFallbackConfig(engine="anthropic-api"))
    first = fallback_engine_factory(conn, config, {}, logger, stage="dig")
    first("medium").complete_structured("p", Verdict)
    first("low").complete_structured("p", Verdict)  # 同じ factory: 主を飛ばす
    assert primary.calls == 1
    second = fallback_engine_factory(conn, config, {}, logger, stage="dig")
    second("medium").complete_structured("p", Verdict)  # 別の factory: 主を呼び直す
    assert primary.calls == 2


def test_fallback_failure_logs_and_raises_without_reply_body(conn, caplog) -> None:
    # 予備(実物の AnthropicApiEngine)の応答が検証に失敗しても、本文はログにも例外にも出ない
    import json
    import traceback

    import httpx

    marker = "記事の本文そのものXYZ"
    reply = {
        "content": [{"type": "text", "text": json.dumps({"score": marker}, ensure_ascii=False)}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    claude = AnthropicApiEngine(
        api_key="k",
        model="claude-sonnet-5-5",
        effort="low",
        max_tokens=1000,
        input_usd_per_mtok=2.0,
        output_usd_per_mtok=10.0,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=reply)),
    )
    engine = build(conn, Scripted("codex-cli", [LLMError("codex 非0終了")]), claude)
    with caplog.at_level(logging.WARNING), pytest.raises(LLMError) as excinfo:
        engine.complete_structured("p", Verdict)
    assert "スキーマに合わない" in str(excinfo.value)
    assert marker not in "".join(traceback.format_exception(excinfo.value))
    assert marker not in caplog.text
    (row,) = ledger_rows(conn)
    assert row["ok"] == 0 and row["cost_usd"] > 0


def test_run_fallback_notice_summarizes_one_run(conn) -> None:
    from app.analysis.fallback import run_fallback_notice

    rows = [
        ("01RUN", "analyze", 1, 0.02),
        ("01RUN", "analyze", 1, 0.03),
        ("01RUN", "analyze", 0, 0.01),
        ("01RUN", "dig", 1, 0.10),
        ("01OTHER", "answer", 1, 5.00),  # ほかの実行は数えない(今月の合計には入る)
    ]
    for run_id, stage, ok, cost in rows:
        conn.execute(
            "INSERT INTO llm_fallback_calls (run_id, stage, model, primary_error, ok, cost_usd)"
            " VALUES (?, ?, 'claude-sonnet-5-5', 'LLMError', ?, ?)",
            (run_id, stage, ok, cost),
        )
    text = run_fallback_notice(conn, "01RUN", 10.0)
    assert text.splitlines() == [
        "⚠️ Codex(主のエンジン)が失敗したため、Claude API で代行しました",
        "・記事の判定: 3 回(うち予備も失敗した呼び出し 1 回)",
        "・深掘り: 1 回",
        "今回 $0.16 / 今月の予備 $5.16(上限 $10.00)",
        "Codex の上限・ログインの状態を確かめてください(runbook §14)",
    ]
    assert run_fallback_notice(conn, "01NONE", 10.0) is None


def test_factory_uses_injected_primary(conn, tmp_path) -> None:
    primary = Scripted("codex-cli", [Verdict(score=1)])
    made: list[str] = []

    def make_primary(effort: str):
        made.append(effort)
        return primary

    off = fallback_engine_factory(
        conn, Config(config_dir=tmp_path), {}, logger, stage="analyze", primary=make_primary
    )
    assert off is make_primary  # 予備が無効なら主の作り方そのもの
    on_config = Config(
        config_dir=tmp_path, llm_fallback=LLMFallbackConfig(engine="anthropic-api")
    )
    on = fallback_engine_factory(
        conn, on_config, {}, logger, stage="analyze", primary=make_primary
    )
    engine = on("low")
    assert engine.complete_structured("p", Verdict) == Verdict(score=1)
    assert made == ["low"]


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (LLMError("codex 非0終了(1): 記事の本文そのものXYZ"), "codex 非0終了(1)"),
        (LLMError("タイムアウト(120s)"), "タイムアウト"),
        (LLMError("JSON パース失敗: x(先頭: '記事の本文そのものXYZ')"), "JSON パース失敗"),
        (LLMUnavailableError("codex 起動失敗: [Errno 2]"), "codex 起動失敗"),
        (LLMError("記事の本文そのものXYZ"), "その他の失敗"),
    ],
)
def test_fallback_log_names_primary_failure_kind_without_body(conn, caplog, error, kind) -> None:
    # ログには主の失敗の種類だけを書く(codex の stderr・応答の先頭は外部の文を含みうる)
    primary = Scripted("codex-cli", [error])
    fallback = Scripted("anthropic-api", [Verdict(score=1)])
    with caplog.at_level(logging.WARNING):
        build(conn, primary, fallback).complete_structured("p", Verdict)
    assert f"{type(error).__name__}・{kind}" in caplog.text
    assert "記事の本文そのもの" not in caplog.text


@pytest.mark.parametrize("path", ["fallback_fails", "over_cap", "cannot_build"])
def test_reraised_error_carries_only_failure_kind(conn, caplog, path) -> None:
    # 予備で答えられずに投げ直す例外は、主の例外の全文(本文を含みうる)を持たない。
    # 各段がこのメッセージをログに書くため(analyzer のチャンクの失敗・深掘りの item の失敗)
    import traceback

    marker = "記事の本文そのものXYZ"
    primary = Scripted("codex-cli", [LLMError(f"JSON パース失敗: x(先頭: '{marker}')")])
    fallback = Scripted("anthropic-api", [LLMError("Anthropic API 529(overloaded_error)")])
    if path == "cannot_build":

        def no_key():
            raise LLMUnavailableError(".env に INTEL_ANTHROPIC_API_KEY が無い")

        engine = FallbackEngine(
            primary, no_key, FallbackLedger(conn, 10.0, stage="analyze", run_id=None),
            _Breaker(), logger, fallback_model="claude-sonnet-5-5",
        )
    else:
        engine = build(conn, primary, fallback, cap=0.0 if path == "over_cap" else 10.0)
        if path == "over_cap":
            conn.execute(
                "INSERT INTO llm_fallback_calls (stage, model, primary_error, ok, cost_usd)"
                " VALUES ('analyze', 'm', 'LLMError', 1, 0.01)"
            )
    with caplog.at_level(logging.WARNING), pytest.raises(LLMError) as excinfo:
        engine.complete_structured("p", Verdict)
    assert type(excinfo.value) is LLMError
    assert "JSON パース失敗" in str(excinfo.value)  # 種類は残す
    assert marker not in "".join(traceback.format_exception(excinfo.value))
    assert marker not in caplog.text
