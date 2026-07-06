"""LLM 抽象層(設計 00 §5)。

- MVP は LLMEngine Protocol + 第一候補エンジン `codex-cli` の1本のみ(Issue #3 で仮決定)。
  複数エンジン・フォールバック網は拡張点。切替は config `llm.engine`(コード変更なし)
- 推論専用モードは宣言でなく構成で強制する:
  - read-only sandbox(書き込み遮断。サンドボックス内コマンドのネットワークも遮断される)
  - 空の専用作業ディレクトリに固定(read-only でもファイル読み取りは可能なため、
    `.env`・SSH 鍵等が要約経由で出力に流出する経路を隔離で塞ぐ)
  - プロンプトは argv でなく stdin 渡し(ps でのプロンプト露出と shell 解釈事故の防止)
  - subprocess の環境変数は PATH/HOME のみ(ANTHROPIC_API_KEY を構造的に載せない)
- 出力は Pydantic でスキーマ検証。検証失敗・タイムアウトは LLMError に正規化し、
  リトライ(同一エンジンで1回)は complete_with_retry が担う。なお失敗したチャンクの
  degraded 直行は analyzer 側(設計 00 §6)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

DEFAULT_TIMEOUT_S = 120

M = TypeVar("M", bound=BaseModel)


class LLMError(Exception):
    """呼び出し失敗(timeout・非0終了・JSON/スキーマ検証失敗)の正規化。"""


class LLMEngine(Protocol):
    name: str

    def complete_structured(
        self, prompt: str, schema: type[M], timeout_s: int = DEFAULT_TIMEOUT_S
    ) -> M: ...


def complete_with_retry[M: BaseModel](
    engine: LLMEngine, prompt: str, schema: type[M], timeout_s: int = DEFAULT_TIMEOUT_S
) -> M:
    """同一エンジンで1リトライ(設計 00 §5 の呼び出し規約)。なお失敗は呼び出し側で degraded へ。"""
    try:
        return engine.complete_structured(prompt, schema, timeout_s)
    except LLMError:
        return engine.complete_structured(prompt, schema, timeout_s)


# 出力が ```json フェンスで包まれるケースの除去(エンジン出力の揺れ対策)
_FENCE = re.compile(r"\A\s*```(?:json)?\s*(.*?)\s*```\s*\Z", re.DOTALL)


def parse_structured[M: BaseModel](raw: str, schema: type[M]) -> M:
    """フェンス除去 → JSON パース → Pydantic 検証。失敗は LLMError に正規化。"""
    text = raw.strip()
    m = _FENCE.match(text)
    if m:
        text = m.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError(f"JSON パース失敗: {e}(先頭: {text[:120]!r})") from e
    try:
        return schema.model_validate(data)
    except ValidationError as e:
        raise LLMError(f"スキーマ検証失敗: {e}") from e


class CodexCliEngine:
    """`codex exec` によるサブスク枠ヘッドレス推論(第一候補。Issue #3)。"""

    name = "codex-cli"

    def __init__(self, *, codex_bin: str = "codex", model_reasoning_effort: str = "low") -> None:
        self._codex_bin = codex_bin
        self._effort = model_reasoning_effort

    def complete_structured(
        self, prompt: str, schema: type[M], timeout_s: int = DEFAULT_TIMEOUT_S
    ) -> M:
        # 呼び出しごとに空の作業ディレクトリを作る(隔離。使い回さない)
        with tempfile.TemporaryDirectory(prefix="intel-llm-") as workdir:
            out_file = Path(workdir) / "last-message.txt"
            command = [
                self._codex_bin,
                "exec",
                "--skip-git-repo-check",
                "-s",
                "read-only",
                "-c",
                "approval_policy=never",
                "-c",
                f"model_reasoning_effort={self._effort}",
                "-C",
                workdir,
                "-o",
                str(out_file),
                "-",  # プロンプトは stdin から
            ]
            try:
                completed = subprocess.run(
                    command,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                    env=_isolated_env(),
                    cwd=workdir,  # -C と二重だが、プロセス自体も隔離 dir で走らせる
                )
            except subprocess.TimeoutExpired as e:
                raise LLMError(f"タイムアウト({timeout_s}s)") from e
            except OSError as e:
                raise LLMError(f"codex 起動失敗: {e}") from e
            if completed.returncode != 0:
                raise LLMError(
                    f"codex 非0終了({completed.returncode}): {completed.stderr[-500:]}"
                )
            if not out_file.exists():
                raise LLMError("codex が最終応答ファイルを書かなかった")
            return parse_structured(out_file.read_text(encoding="utf-8"), schema)


def _isolated_env() -> dict[str, str]:
    """PATH/HOME のみの環境(設計 00 §4: ANTHROPIC_API_KEY を実行環境に載せない)。

    HOME は codex の認証(~/.codex)に必要。それ以外の環境変数は渡さない。
    """
    return {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}


def create_engine(engine_name: str) -> LLMEngine:
    """config `llm.engine` からエンジンを生成する(値の検証は config 層で済んでいる)。"""
    if engine_name == "codex-cli":
        return CodexCliEngine()
    raise LLMError(
        f"エンジン {engine_name!r} は未実装(MVP は codex-cli のみ。claude-cli / "
        "anthropic-api は拡張点。設計 00 §5)"
    )
