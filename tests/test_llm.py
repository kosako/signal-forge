import json
import os
import stat
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.analysis.llm import (
    CodexCliEngine,
    LLMError,
    complete_with_retry,
    create_engine,
    parse_structured,
)


class Verdict(BaseModel):
    score: int
    reason: str


VALID = '{"score": 80, "reason": "ok"}'


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


def test_engine_timeout_raises_llmerror(tmp_path: Path) -> None:
    codex = fake_codex(tmp_path, "sleep 5\n")
    with pytest.raises(LLMError, match="タイムアウト"):
        CodexCliEngine(codex_bin=codex).complete_structured("p", Verdict, timeout_s=1)


def test_engine_nonzero_exit_raises(tmp_path: Path) -> None:
    codex = fake_codex(tmp_path, "echo boom >&2\nexit 3\n")
    with pytest.raises(LLMError, match="非0終了"):
        CodexCliEngine(codex_bin=codex).complete_structured("p", Verdict)


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


def test_default_codex_home_is_minimal_auth_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    real = tmp_path / ".codex"
    real.mkdir()
    (real / "auth.json").write_text("{}")
    (real / "sessions.db").write_text("secret sessions")  # 見せてはいけない同居物

    engine = CodexCliEngine(codex_bin="/bin/true")
    home = Path(engine._codex_home)
    assert home != real  # ~/.codex 丸ごとではない
    assert [p.name for p in home.iterdir()] == ["auth.json"]  # auth のみ
    assert (home / "auth.json").resolve() == (real / "auth.json").resolve()  # symlink で共有


def test_default_codex_home_requires_login(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(LLMError, match="codex login"):
        CodexCliEngine(codex_bin="/bin/true")


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


def test_create_engine_only_codex_cli() -> None:
    assert create_engine("codex-cli").name == "codex-cli"
    with pytest.raises(LLMError, match="未実装"):
        create_engine("claude-cli")
