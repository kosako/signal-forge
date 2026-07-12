"""LLM 抽象層(設計 00 §5)。

- MVP は LLMEngine Protocol + 第一候補エンジン `codex-cli` の1本のみ(Issue #3 で仮決定)。
  複数エンジン・フォールバック網は拡張点。切替は config `llm.engine`(コード変更なし)
- 推論専用モードは宣言でなく構成で強制する:
  - permission profile で read-only 相当(書き込み遮断・network 遮断)に固定し、さらに
    CODEX_HOME と実 `~/.codex` の**読取を deny** する(Issue #54)。read-only sandbox は
    ファイル読取自体は許すため、auth.json を同居させる CODEX_HOME が要約経由で流出する
    経路が残る。deny-read でその経路(symlink 経由の実体読取・絶対パス直読みを含む)を塞ぐ
  - 空の専用作業ディレクトリに固定(read でも `.env`・SSH 鍵等が要約経由で出力に流出する
    経路を隔離で塞ぐ)
  - プロンプトは argv でなく stdin 渡し(ps でのプロンプト露出と shell 解釈事故の防止)
  - subprocess の環境変数は PATH/HOME のみ(ANTHROPIC_API_KEY を構造的に載せない)
- 出力は Pydantic でスキーマ検証。検証失敗・タイムアウトは LLMError に正規化し、
  リトライ(同一エンジンで1回)は complete_with_retry が担う。なお失敗したチャンクの
  degraded 直行は analyzer 側(設計 00 §6)
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
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
_FENCE = re.compile(r"\A\s*```[ \t]*(?:json)?\s*(.*?)\s*```\s*\Z", re.DOTALL | re.IGNORECASE)


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

    def __init__(
        self,
        *,
        codex_bin: str = "codex",
        model_reasoning_effort: str = "low",
        codex_home: str | None = None,
    ) -> None:
        self._codex_bin = codex_bin
        self._effort = model_reasoning_effort
        # None なら呼び出しごとに使い捨ての最小 CODEX_HOME(auth.json symlink のみ)を作る。
        # 恒久ディレクトリにしない理由: codex はランタイムで logs/memories/state を
        # CODEX_HOME に書き込む(実測)ため、(a) 汚染で「auth のみ」の境界が壊れる
        # (b) 実行間で memories が持ち越され、外部本文由来の指示が持続する経路になる
        self._codex_home = codex_home

    def complete_structured(
        self, prompt: str, schema: type[M], timeout_s: int = DEFAULT_TIMEOUT_S
    ) -> M:
        # 呼び出しごとに空の作業ディレクトリと使い捨て CODEX_HOME を作る(使い回さない)。
        # codex-home は workspace の外(sibling)に置き、エージェントの作業領域から
        # 相対パスで auth に触れない配置にする
        with tempfile.TemporaryDirectory(prefix="intel-llm-") as tmp:
            workdir = str(Path(tmp) / "workspace")
            Path(workdir).mkdir()
            codex_home = self._codex_home or _ephemeral_codex_home(Path(tmp))
            out_file = Path(tmp) / "last-message.txt"
            command = [
                self._codex_bin,
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",  # ユーザーの config.toml を読まない(認証は CODEX_HOME)
                # read-only sandbox は読取を許すため `-s read-only` の代わりに permission
                # profile を使う。profile なら read-only 相当(書込・network 遮断)を保ったまま
                # CODEX_HOME/実 ~/.codex の読取を deny できる(Issue #54)。`-s` を明示すると
                # profile の filesystem deny が無視されるため -s は付けない(実測)
                *_isolation_profile_args(codex_home),
                # -a/--ask-for-approval はトップレベル専用で exec では使えない(実測)。
                # exec では config override で承認を止める
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
            stderr_tail = ""
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    encoding="utf-8",  # locale 非依存(launchd 環境で UTF-8 保証がない)
                    env=self._isolated_env(workdir, codex_home),
                    cwd=workdir,  # -C と二重だが、プロセス自体も隔離 dir で走らせる
                    start_new_session=True,  # timeout 時に孫プロセスごと落とすための別セッション
                )
            except OSError as e:
                raise LLMError(f"codex 起動失敗: {e}") from e
            try:
                _, stderr_tail = process.communicate(input=prompt, timeout=timeout_s)
            except subprocess.TimeoutExpired as e:
                # timeout と kill の間に自然終了していたら ProcessLookupError(取り残しなし)
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                process.wait()
                raise LLMError(f"タイムアウト({timeout_s}s)") from e
            if process.returncode != 0:
                # stderr は外部本文(プロンプト経由)が混入しうる untrusted data。
                # 例外(=ログ)には制御文字を除去した末尾200字だけを載せる
                tail = re.sub(r"[\x00-\x1f\x7f]", " ", stderr_tail)[-200:]
                raise LLMError(f"codex 非0終了({process.returncode}): {tail}")
            if not out_file.exists():
                raise LLMError("codex が最終応答ファイルを書かなかった")
            return parse_structured(out_file.read_text(encoding="utf-8"), schema)

    def _isolated_env(self, workdir: str, codex_home: str) -> dict[str, str]:
        """PATH + 隔離 HOME + CODEX_HOME のみの環境。

        実ユーザーの HOME を渡すと、read-only sandbox はファイル読み取りを防がないため
        `~/.ssh` 等が要約経由で流出する経路が残る。HOME は空の workspace に差し替え、
        codex の認証だけ CODEX_HOME で最小限見せる(auth 以外の設定は --ephemeral で遮断)。
        ANTHROPIC_API_KEY を含む他の環境変数は構造的に渡さない(設計 00 §4)。
        """
        return {
            "PATH": os.environ.get("PATH", ""),
            "HOME": workdir,
            "CODEX_HOME": codex_home,
        }


_ISOLATION_PROFILE = "intel_isolated"


def _isolation_profile_args(codex_home: str) -> list[str]:
    """read-only 相当 + CODEX_HOME/実 ~/.codex の deny-read を課す profile の -c 群(Issue #54)。

    codex の permission profile: `default_permissions` で名前付き profile を選び、
    `[permissions.<name>].filesystem` の deny-read で auth 同居ディレクトリの読取を塞ぐ。
    filesystem に write を一切与えず network を空にすることで read-only sandbox と同等の
    隔離(書込・network 遮断)を保つ。deny 対象:
      - codex_home: この呼び出しの CODEX_HOME(auth.json symlink を含む使い捨て/検査用)
      - ~/.codex: symlink の実体側。symlink 経由の読取・絶対パス直読みの双方を塞ぐ(防御多層)
    codex 自身の auth 読取は sandbox 外で行われるため deny の影響を受けず、認証は維持される。

    パスは TOML basic string(二重引用符)で埋める。CODEX_HOME は tempfile 由来、~/.codex は
    ユーザーホーム配下で、いずれも `"`/`\\` を含まない(含めば TOML パースが fail-close する)。
    """
    real_codex_home = str(Path.home() / ".codex")
    deny_fs = f'{{"{codex_home}"="none","{real_codex_home}"="none"}}'
    prof = _ISOLATION_PROFILE
    return [
        "-c",
        f"default_permissions={prof}",
        "-c",
        f"permissions.{prof}.filesystem={deny_fs}",
        "-c",
        f"permissions.{prof}.network={{}}",
    ]


def _ephemeral_codex_home(base: Path) -> str:
    """auth.json(~/.codex/auth.json への symlink)だけを含む使い捨て CODEX_HOME を作る。

    symlink なので codex 側のトークンリフレッシュは実ファイルに反映される。
    ディレクトリごと呼び出し後に破棄されるため、codex が書くランタイムファイル
    (logs/memories/state)による汚染・持ち越しは構造的に起きない。
    """
    real_auth = Path.home() / ".codex" / "auth.json"
    if not real_auth.exists():
        raise LLMError("~/.codex/auth.json がない(先に codex login が必要)")
    home = base / "codex-home"
    home.mkdir()
    (home / "auth.json").symlink_to(real_auth)
    return str(home)


def create_engine(engine_name: str) -> LLMEngine:
    """config `llm.engine` からエンジンを生成する。

    config は語彙(enum)の検証のみ。未実装エンジンはここで fail fast する
    (doctor の未実装エンジン検査は #21)。
    """
    if engine_name == "codex-cli":
        return CodexCliEngine()
    raise LLMError(
        f"エンジン {engine_name!r} は未実装(MVP は codex-cli のみ。claude-cli / "
        "anthropic-api は拡張点。設計 00 §5)"
    )
