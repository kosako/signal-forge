import json
import os
import stat
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from app.analysis.llm import (
    ANTHROPIC_API_KEY_ENV,
    AnthropicApiEngine,
    CodexCliEngine,
    LLMError,
    LLMUnavailableError,
    LLMUsage,
    complete_with_retry,
    create_anthropic_engine,
    create_engine,
    parse_structured,
)
from app.config import AnthropicApiConfig


class Verdict(BaseModel):
    score: int
    reason: str


VALID = '{"score": 80, "reason": "ok"}'


@pytest.fixture(autouse=True)
def fake_user_home(tmp_path_factory, monkeypatch):
    """既定の使い捨て CODEX_HOME が実ユーザーの ~/.codex に触れないよう HOME を差し替える。"""
    home = tmp_path_factory.mktemp("home")
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    return home


def fake_codex(tmp_path: Path, script_body: str) -> str:
    """-o で渡されたファイルに応答を書く codex の偽物。"""
    path = tmp_path / "fake-codex"
    path.write_text("#!/bin/bash\n" + script_body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


OUT_FILE_SNIPPET = """
# -o の次の引数を拾う
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "-o" ]]; then out="$2"; fi
  shift
done
"""


def test_parse_structured_plain_and_fenced() -> None:
    assert parse_structured(VALID, Verdict).score == 80
    assert parse_structured(f"```json\n{VALID}\n```", Verdict).score == 80


def test_parse_structured_rejects_bad_json_and_schema() -> None:
    with pytest.raises(LLMError, match="JSON パース失敗"):
        parse_structured("не json", Verdict)
    with pytest.raises(LLMError, match="スキーマ検証失敗"):
        parse_structured('{"score": "high"}', Verdict)


def test_engine_returns_validated_model(tmp_path: Path) -> None:
    codex = fake_codex(tmp_path, OUT_FILE_SNIPPET + f"echo '{VALID}' > \"$out\"\n")
    result = CodexCliEngine(codex_bin=codex).complete_structured("prompt", Verdict)
    assert result == Verdict(score=80, reason="ok")


def test_engine_passes_prompt_via_stdin_not_argv(tmp_path: Path) -> None:
    argv_file = tmp_path / "argv.txt"
    stdin_file = tmp_path / "stdin.txt"
    # argv 退避 → -o 解析(OUT_FILE_SNIPPET は "$@" を消費)→ stdin 保存 → 応答
    codex = fake_codex(
        tmp_path,
        f'echo "$@" > "{argv_file}"\n'
        + OUT_FILE_SNIPPET
        + f'cat > "{stdin_file}"\n'
        + f"echo '{VALID}' > \"$out\"\n",
    )
    secret_prompt = "機密を含むプロンプト本文"
    CodexCliEngine(codex_bin=codex).complete_structured(secret_prompt, Verdict)
    assert secret_prompt in stdin_file.read_text()  # プロンプトは stdin 経由
    assert secret_prompt not in argv_file.read_text()  # argv に露出しない(ps 対策)


def test_isolation_profile_args_exact(tmp_path: Path) -> None:
    # Issue #54 / Codex review: 引数を単位で完全一致検証する(部分文字列でなく)。
    from app.analysis.llm import _isolation_profile_args

    home_codex = str(Path.home() / ".codex")
    args = _isolation_profile_args("/fake/codex-auth")
    assert args == [
        "-c",
        "default_permissions=intel_isolated",
        "-c",
        f'permissions.intel_isolated.filesystem={{"/fake/codex-auth"="none",'
        f'"{home_codex}"="none"}}',
        "-c",
        "permissions.intel_isolated.network={}",
    ]


def test_isolation_profile_args_escapes_toml_injection() -> None:
    # Codex review must: パスに " を含めても文字列を閉じて追加キーを注入できない。
    # 構築した TOML を実際にパースし、悪意あるパスが「単一キー」のまま(=構造注入なし)
    # であることを検証する。
    import tomllib

    from app.analysis.llm import _isolation_profile_args

    evil = '/x"="write"} injected = {"/etc'  # 文字列を閉じてキー注入を狙うパス
    args = _isolation_profile_args(evil)
    fs = next(a for a in args if a.startswith("permissions.intel_isolated.filesystem="))
    inline = fs.split("=", 1)[1]  # {"..."="none",...}
    parsed = tomllib.loads(f"t = {inline}")["t"]
    home_codex = str(Path.home() / ".codex")
    # キーは deny 対象 2 つだけ(injected 等の追加キーが生えていない)。悪意パスは原文のまま
    assert set(parsed) == {evil, home_codex}
    assert parsed[evil] == "none"
    assert "injected" not in parsed


def test_toml_basic_string_escapes_specials() -> None:
    from app.analysis.llm import _toml_basic_string

    assert _toml_basic_string("/plain/path") == '"/plain/path"'
    assert _toml_basic_string('a"b') == '"a\\"b"'
    assert _toml_basic_string("a\\b") == '"a\\\\b"'
    assert _toml_basic_string("a\x01b") == '"a\\u0001b"'
    assert _toml_basic_string("a\nb") == '"a\\nb"'


def test_engine_command_isolation_flags(tmp_path: Path) -> None:
    # Issue #54: engine が実際に組み立てる argv を検証する。
    argv_file = tmp_path / "argv.txt"
    codex = fake_codex(
        tmp_path,
        f'printf "%s\\n" "$@" > "{argv_file}"\n'
        + OUT_FILE_SNIPPET
        + f"echo '{VALID}' > \"$out\"\n",
    )
    CodexCliEngine(codex_bin=codex, codex_home="/fake/codex-auth").complete_structured(
        "p", Verdict
    )
    argv = argv_file.read_text().splitlines()
    # 未対応 codex で profile を黙って無視させない fail-closed ガード
    assert "--strict-config" in argv
    assert "default_permissions=intel_isolated" in argv
    assert "permissions.intel_isolated.network={}" in argv
    # deny 対象に この呼び出しの CODEX_HOME と実 ~/.codex の双方が入る(防御多層)
    fs = next(a for a in argv if a.startswith("permissions.intel_isolated.filesystem="))
    assert '"/fake/codex-auth"="none"' in fs
    assert f'"{Path.home() / ".codex"}"="none"' in fs
    # -s read-only は付けない(profile が無効化されるため)
    assert "read-only" not in argv
    # GH#129: sandbox の外で動くツール(接続サービス・Web 等)と実行系を機能ごと外す。
    # 実測でツールが消えることを確かめた組み合わせを、実装の定数とは独立に固定する
    disabled = {argv[i + 1] for i, a in enumerate(argv) if a == "--disable"}
    assert disabled == {
        "apps", "plugins", "tool_suggest", "browser_use", "browser_use_external",
        "in_app_browser", "computer_use", "image_generation", "view_image", "shell_tool",
        "unified_exec", "code_mode_host", "multi_agent", "goals", "sleep_tool",
    }
    web = argv.index('web_search="disabled"')
    assert argv[web - 1] == "-c"


def test_engine_launch_failure_raises_unavailable(tmp_path: Path) -> None:
    # バイナリ不在(#72 の実障害)は transient と区別して環境障害に分類する(GH#74)
    with pytest.raises(LLMUnavailableError, match="起動失敗"):
        CodexCliEngine(codex_bin=str(tmp_path / "no-such-codex")).complete_structured(
            "p", Verdict
        )


def test_missing_auth_raises_unavailable(tmp_path: Path, fake_user_home: Path) -> None:
    (fake_user_home / ".codex" / "auth.json").unlink()
    codex = fake_codex(tmp_path, OUT_FILE_SNIPPET + f"echo '{VALID}' > \"$out\"\n")
    with pytest.raises(LLMUnavailableError, match="auth.json"):
        CodexCliEngine(codex_bin=codex).complete_structured("p", Verdict)


def test_engine_timeout_raises_llmerror(tmp_path: Path) -> None:
    codex = fake_codex(tmp_path, "sleep 5\n")
    with pytest.raises(LLMError, match="タイムアウト"):
        CodexCliEngine(codex_bin=codex).complete_structured("p", Verdict, timeout_s=1)


def test_engine_nonzero_exit_raises(tmp_path: Path) -> None:
    codex = fake_codex(tmp_path, "echo boom >&2\nexit 3\n")
    with pytest.raises(LLMError, match="非0終了"):
        CodexCliEngine(codex_bin=codex).complete_structured("p", Verdict)


@pytest.mark.parametrize(
    "stderr",
    [
        "Error: Unknown feature flag: view_image",
        "Error loading config.toml: unknown configuration field `x` in -c/--config override",
    ],
)
def test_engine_config_rejection_is_unavailable(tmp_path: Path, stderr: str) -> None:
    # GH#129: 無効化する feature 名や profile キーを codex が受け付けない = 版の不一致。
    # 記事を degraded で進めないよう、リトライでも回復しない環境障害に分類する(GH#74)
    codex = fake_codex(tmp_path, f"echo '{stderr}' >&2\nexit 1\n")
    with pytest.raises(LLMUnavailableError, match="起動設定エラー"):
        CodexCliEngine(codex_bin=codex).complete_structured("p", Verdict)


def test_engine_config_error_text_later_in_stderr_stays_transient(tmp_path: Path) -> None:
    # 先頭以外に現れた同じ文言(外部本文の混入がありうる位置)では分類を変えない
    codex = fake_codex(
        tmp_path, "echo boom >&2\necho 'Error: Unknown feature flag: x' >&2\nexit 1\n"
    )
    with pytest.raises(LLMError, match="非0終了") as excinfo:
        CodexCliEngine(codex_bin=codex).complete_structured("p", Verdict)
    assert not isinstance(excinfo.value, LLMUnavailableError)


def test_engine_missing_output_raises(tmp_path: Path) -> None:
    codex = fake_codex(tmp_path, "exit 0\n")
    with pytest.raises(LLMError, match="最終応答ファイル"):
        CodexCliEngine(codex_bin=codex).complete_structured("p", Verdict)


def test_env_is_isolated_no_anthropic_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("SOME_OTHER_SECRET", "x")
    reply = (
        'echo "{\\"score\\": 1, \\"reason\\": '
        '\\"${ANTHROPIC_API_KEY:-ABSENT}-${SOME_OTHER_SECRET:-ABSENT}-${HOME}\\"}" > "$out"\n'
    )
    codex = fake_codex(tmp_path, OUT_FILE_SNIPPET + reply)
    result = CodexCliEngine(codex_bin=codex).complete_structured("p", Verdict)
    absent_a, absent_b, home = result.reason.split("-", 2)
    assert (absent_a, absent_b) == ("ABSENT", "ABSENT")  # 環境変数が構造的に渡らない
    assert "intel-llm" in home  # HOME は実ユーザーの HOME でなく隔離 workdir
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-secret"  # 呼び出し側の環境は不変


def test_default_codex_home_is_ephemeral_and_auth_only(
    tmp_path: Path, fake_user_home: Path
) -> None:
    (fake_user_home / ".codex" / "sessions.db").write_text("secret")  # 見せてはいけない同居物
    reply = (
        'echo "{\\"score\\": 1, \\"reason\\": '
        '\\"$(ls -A "$CODEX_HOME" | tr "\\n" ",")|$CODEX_HOME\\"}" > "$out"\n'
    )
    codex = fake_codex(tmp_path, OUT_FILE_SNIPPET + reply)
    result = CodexCliEngine(codex_bin=codex).complete_structured("p", Verdict)
    contents, home = result.reason.split("|")
    assert contents.strip(",") == "auth.json"  # auth.json のみ(sessions 等は見えない)
    assert "intel-llm-" in home  # 呼び出しごとの使い捨てディレクトリ
    assert not Path(home).exists()  # 呼び出し後に破棄される(汚染・持ち越しなし)


def test_default_codex_home_requires_login(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))  # .codex/auth.json なし
    with pytest.raises(LLMError, match="codex login"):
        CodexCliEngine(codex_bin="/bin/true").complete_structured("p", Verdict)


def test_codex_home_points_to_auth_dir_only(tmp_path: Path) -> None:
    codex = fake_codex(
        tmp_path,
        OUT_FILE_SNIPPET
        + 'echo "{\\"score\\": 1, \\"reason\\": \\"${CODEX_HOME:-ABSENT}\\"}" > "$out"\n',
    )
    engine = CodexCliEngine(codex_bin=codex, codex_home="/fake/codex-auth")
    assert engine.complete_structured("p", Verdict).reason == "/fake/codex-auth"


def test_engine_runs_in_empty_isolated_workdir(tmp_path: Path) -> None:
    codex = fake_codex(
        tmp_path,
        OUT_FILE_SNIPPET
        + 'count=$(ls -A "$PWD" | wc -l | tr -d " ")\n'
        + 'echo "{\\"score\\": $count, \\"reason\\": \\"$PWD\\"}" > "$out"\n',
    )
    result = CodexCliEngine(codex_bin=codex).complete_structured("p", Verdict)
    assert result.score == 0  # 空ディレクトリ
    assert "intel-llm-" in result.reason  # 専用の一時ディレクトリ


def test_complete_with_retry_retries_once(tmp_path: Path) -> None:
    class Flaky:
        name = "flaky"

        def __init__(self) -> None:
            self.calls = 0

        def complete_structured(self, prompt, schema, timeout_s=120):
            self.calls += 1
            if self.calls == 1:
                raise LLMError("一時失敗")
            return schema.model_validate(json.loads(VALID))

    engine = Flaky()
    assert complete_with_retry(engine, "p", Verdict).score == 80
    assert engine.calls == 2


def test_complete_with_retry_gives_up_after_two(tmp_path: Path) -> None:
    class Broken:
        name = "broken"
        calls = 0

        def complete_structured(self, prompt, schema, timeout_s=120):
            Broken.calls += 1
            raise LLMError("常に失敗")

    with pytest.raises(LLMError):
        complete_with_retry(Broken(), "p", Verdict)
    assert Broken.calls == 2


def test_complete_with_retry_no_retry_on_unavailable() -> None:
    class Unavailable:
        name = "unavailable"
        calls = 0

        def complete_structured(self, prompt, schema, timeout_s=120):
            Unavailable.calls += 1
            raise LLMUnavailableError("codex 起動失敗")

    with pytest.raises(LLMUnavailableError):
        complete_with_retry(Unavailable(), "p", Verdict)
    assert Unavailable.calls == 1  # 環境障害はリトライで回復しない(GH#74)


def test_create_engine_only_codex_cli() -> None:
    assert create_engine("codex-cli").name == "codex-cli"
    with pytest.raises(LLMError, match="未実装"):
        create_engine("claude-cli")


# --- anthropic-api(GH#153) ---

FAKE_KEY = "test-key"


def anthropic_reply(text: str = VALID, **overrides) -> dict:
    reply = {
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": text},
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 12_000, "output_tokens": 3_000},
    }
    reply.update(overrides)
    return reply


def anthropic_engine(handler, **kwargs) -> AnthropicApiEngine:
    params = {
        "api_key": FAKE_KEY,
        "model": "claude-opus-5-5",
        "effort": "medium",
        "max_tokens": 16_000,
        "input_usd_per_mtok": 4.0,
        "output_usd_per_mtok": 20.0,
        "transport": httpx.MockTransport(handler),
    }
    params.update(kwargs)
    return AnthropicApiEngine(**params)


def test_anthropic_request_has_no_tools_and_fixed_destination() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=anthropic_reply())

    result = anthropic_engine(handler).complete_structured("プロンプト本文", Verdict)

    assert result == Verdict(score=80, reason="ok")
    (request,) = seen
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == FAKE_KEY
    assert request.headers["anthropic-version"] == "2023-06-01"
    body = json.loads(request.content)
    # 推論専用は構造で作る: ツール・MCP・コンテナの類は一切付けない(この 4 キーだけ)
    assert body == {
        "model": "claude-opus-5-5",
        "max_tokens": 16_000,
        "messages": [{"role": "user", "content": "プロンプト本文"}],
        "output_config": {"effort": "medium"},
    }


def test_anthropic_records_usage_and_cost() -> None:
    engine = anthropic_engine(lambda request: httpx.Response(200, json=anthropic_reply()))
    engine.complete_structured("p", Verdict)
    # 12k × $4/M + 3k × $20/M = 0.048 + 0.06
    assert engine.last_usage == LLMUsage(12_000, 3_000, 0.108)


def test_anthropic_joins_text_blocks_and_skips_thinking() -> None:
    reply = anthropic_reply(
        content=[
            {"type": "thinking", "thinking": '{"score": 1, "reason": "思考"}'},
            {"type": "text", "text": '```json\n{"score": 80,'},
            {"type": "text", "text": ' "reason": "ok"}\n```'},
        ]
    )
    engine = anthropic_engine(lambda request: httpx.Response(200, json=reply))
    assert engine.complete_structured("p", Verdict) == Verdict(score=80, reason="ok")


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal", "pause_turn", None])
def test_anthropic_incomplete_stop_is_error_but_usage_kept(stop_reason) -> None:
    reply = anthropic_reply(stop_reason=stop_reason)
    engine = anthropic_engine(lambda request: httpx.Response(200, json=reply))
    with pytest.raises(LLMError, match="途中で終わった") as excinfo:
        engine.complete_structured("p", Verdict)
    assert not isinstance(excinfo.value, LLMUnavailableError)
    assert engine.last_usage == LLMUsage(12_000, 3_000, 0.108)  # 課金は発生している


@pytest.mark.parametrize("status", [401, 403, 404])
def test_anthropic_auth_and_model_errors_are_unavailable(status: int) -> None:
    error = {"type": "error", "error": {"type": "authentication_error", "message": "秘密 key"}}
    engine = anthropic_engine(lambda request: httpx.Response(status, json=error))
    with pytest.raises(LLMUnavailableError) as excinfo:
        engine.complete_structured("p", Verdict)
    assert f"{status}(authentication_error)" in str(excinfo.value)
    assert "秘密" not in str(excinfo.value)  # 応答のメッセージ本文は載せない
    assert engine.last_usage is None


@pytest.mark.parametrize("status", [400, 429, 500, 529])
def test_anthropic_other_errors_are_transient(status: int) -> None:
    engine = anthropic_engine(lambda request: httpx.Response(status, text="<html>"))
    with pytest.raises(LLMError, match=rf"{status}\(unknown\)") as excinfo:
        engine.complete_structured("p", Verdict)
    assert not isinstance(excinfo.value, LLMUnavailableError)


def test_anthropic_timeout_and_transport_errors() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(LLMError, match="タイムアウト"):
        anthropic_engine(timeout).complete_structured("p", Verdict, timeout_s=5)
    with pytest.raises(LLMError, match="ConnectError"):
        anthropic_engine(refused).complete_structured("p", Verdict)


def test_anthropic_malformed_reply_is_error() -> None:
    cases = [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=["list"]),
        httpx.Response(200, json=anthropic_reply(content="text")),
        httpx.Response(200, json=anthropic_reply(text='{"score": "高い"}')),
    ]
    for response in cases:
        engine = anthropic_engine(lambda request, response=response: response)
        with pytest.raises(LLMError):
            engine.complete_structured("p", Verdict)


def test_anthropic_usage_reset_per_call() -> None:
    replies = iter([anthropic_reply(), anthropic_reply(usage=None)])
    engine = anthropic_engine(lambda request: httpx.Response(200, json=next(replies)))
    engine.complete_structured("p", Verdict)
    engine.complete_structured("p", Verdict)
    assert engine.last_usage is None  # 前回の usage を持ち越さない


def test_create_anthropic_engine_reads_alias_key_only() -> None:
    settings = AnthropicApiConfig(model="claude-sonnet-5-5", effort="high")
    engine = create_anthropic_engine(settings, {ANTHROPIC_API_KEY_ENV: " k "})
    assert (engine.name, engine.model) == ("anthropic-api", "claude-sonnet-5-5")
    for env in ({}, {ANTHROPIC_API_KEY_ENV: "  "}, {"ANTHROPIC_API_KEY": "k"}):
        with pytest.raises(LLMUnavailableError, match=ANTHROPIC_API_KEY_ENV):
            create_anthropic_engine(settings, env)


def test_create_engine_keeps_anthropic_off_main_path() -> None:
    with pytest.raises(LLMError, match="主経路に未実装"):
        create_engine("anthropic-api")
