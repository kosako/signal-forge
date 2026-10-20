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
  - 外へ届く・実行するツール(アカウント側の接続サービス・Web・ブラウザ・shell・コード
    実行など)を機能ごと無効化する(GH#129。sandbox の外で動くツールは上の遮断で止まらない)
  - プロンプトは argv でなく stdin 渡し(ps でのプロンプト露出と shell 解釈事故の防止)
  - subprocess の環境変数は PATH/HOME のみ(ANTHROPIC_API_KEY を構造的に載せない)
- 出力は Pydantic でスキーマ検証。検証失敗・タイムアウトは LLMError に正規化し、
  リトライ(同一エンジンで1回)は complete_with_retry が担う。なお失敗したチャンクの
  degraded 直行は analyzer 側(設計 00 §6)
- `anthropic-api`(AnthropicApiEngine)は深掘りノートのレビュー役(GH#153)。推論専用は
  構造で作る: ツールを一切渡さない素の Messages API で、宛先は API に固定。キーは .env の
  別名キーだけから読む(設計 00 §4)。主経路(config `llm.engine`)にはまだ配線しない
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import AnthropicApiConfig

DEFAULT_TIMEOUT_S = 120

M = TypeVar("M", bound=BaseModel)


class LLMError(Exception):
    """呼び出し失敗(timeout・非0終了・JSON/スキーマ検証失敗)の正規化。"""


class LLMUnavailableError(LLMError):
    """エンジン実行不能(バイナリ不在・認証不在などの環境障害。GH#74)。

    transient な呼び出し失敗(LLMError)と違い、リトライしても・チャンクを進めても
    回復しない。per-chunk degraded で握ると全件が黙って劣化する(#72 の実障害)ため、
    呼び出し側は degraded にせず analyze 段の失敗として伝播させる。
    """


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
    except LLMUnavailableError:
        raise  # 環境障害はリトライで回復しない(GH#74)
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
                # セッションを保存しない(ユーザー設定の遮断は使い捨て CODEX_HOME が担う)
                "--ephemeral",
                # read-only sandbox は読取を許すため `-s read-only` の代わりに permission
                # profile を使う。profile なら read-only 相当(書込・network 遮断)を保ったまま
                # CODEX_HOME/実 ~/.codex の読取を deny できる(Issue #54)。`-s` を明示すると
                # profile の filesystem deny が無視されるため -s は付けない(実測)
                #
                # --strict-config: profile キー(default_permissions/permissions.*)を
                # 認識しない codex では未知 -c キーとしてエラー終了させる(fail-closed)。
                # これが無いと未対応版で -c が黙って無視され、隔離が exec 既定(read-only)に
                # 退化して deny-read だけ失われる(= #54 に静かに逆戻り)経路を塞ぐ(実測)
                "--strict-config",
                *_isolation_profile_args(codex_home),
                *_disabled_tool_args(),
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
                # バイナリ不在(FileNotFoundError = #72)・権限等の環境障害
                raise LLMUnavailableError(f"codex 起動失敗: {e}") from e
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
                if stderr_tail.lstrip().startswith(_CODEX_CONFIG_ERRORS):
                    # 起動引数(無効化する feature 名・profile キー)を codex が受け付けない
                    # = 版の不一致。リトライでも回復しない環境障害として扱う(GH#129)
                    raise LLMUnavailableError(f"codex の起動設定エラー: {tail}")
                raise LLMError(f"codex 非0終了({process.returncode}): {tail}")
            if not out_file.exists():
                raise LLMError("codex が最終応答ファイルを書かなかった")
            return parse_structured(out_file.read_text(encoding="utf-8"), schema)

    def _isolated_env(self, workdir: str, codex_home: str) -> dict[str, str]:
        """PATH + 隔離 HOME + CODEX_HOME のみの環境。

        実ユーザーの HOME を渡すと、read-only sandbox はファイル読み取りを防がないため
        `~/.ssh` 等が要約経由で流出する経路が残る。HOME は空の workspace に差し替え、
        codex の認証だけ CODEX_HOME で最小限見せる(CODEX_HOME には auth.json しか置かないので、
        ユーザーの config.toml・MCP・hooks 等の設定は読まれない)。
        ANTHROPIC_API_KEY を含む他の環境変数は構造的に渡さない(設計 00 §4)。
        """
        return {
            "PATH": os.environ.get("PATH", ""),
            "HOME": workdir,
            "CODEX_HOME": codex_home,
        }


_ISOLATION_PROFILE = "intel_isolated"


def _toml_basic_string(s: str) -> str:
    """任意の文字列を TOML basic string リテラル(前後の二重引用符込み)に変換する。

    パスをそのまま `"..."` に埋めると、`"` を含むパスで文字列を閉じて追加のキーを注入でき、
    隔離ポリシー自体への config injection が成立する(Codex レビュー must)。`\\`・`"`・
    制御文字を TOML 仕様どおりエスケープして、値が常に単一の well-formed な文字列になるよう
    構造的に保証する(注入を排除)。
    """
    out = ['"']
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ch < "\x20" or ch == "\x7f":
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _isolation_profile_args(codex_home: str) -> list[str]:
    """read-only 相当 + CODEX_HOME/実 ~/.codex の deny-read を課す profile の -c 群(Issue #54)。

    codex の permission profile: `default_permissions` で名前付き profile を選び、
    `[permissions.<name>].filesystem` の deny-read で auth 同居ディレクトリの読取を塞ぐ。
    filesystem に write を一切与えず network を空にすることで read-only sandbox と同等の
    隔離(書込・network 遮断)を保つ。deny 対象:
      - codex_home: この呼び出しの CODEX_HOME(auth.json symlink を含む使い捨て/検査用)
      - ~/.codex: symlink の実体側。symlink 経由の読取・絶対パス直読みの双方を塞ぐ(防御多層)
    codex 自身の auth 読取は sandbox 外で行われるため deny の影響を受けず、認証は維持される。

    パスは _toml_basic_string でエスケープしてから埋める(注入を構造的に排除)。
    """
    real_codex_home = str(Path.home() / ".codex")
    ch = _toml_basic_string(codex_home)
    rh = _toml_basic_string(real_codex_home)
    deny_fs = f"{{{ch}=\"none\",{rh}=\"none\"}}"
    prof = _ISOLATION_PROFILE
    return [
        "-c",
        f"default_permissions={prof}",
        "-c",
        f"permissions.{prof}.filesystem={deny_fs}",
        "-c",
        f"permissions.{prof}.network={{}}",
    ]


# codex exec の既定で有効な、外へ届く・実行するツールの機能(GH#129)。permission profile の
# 書込・network 遮断は shell の実行には効くが、アカウント側の接続サービスや Web のツールは
# sandbox の外で動くため止まらない(実測: 無効化前は多数のツールが見えていた)。外部本文を
# 読む呼び出しで呼ばれないよう、機能ごと無効化する。--strict-config の下で未知の feature 名は
# 起動エラーになる(黙って無視されず fail-closed)
_DISABLED_FEATURES = (
    "apps",  # アカウント側の接続サービス
    "plugins",
    "tool_suggest",
    "browser_use",
    "browser_use_external",
    "in_app_browser",
    "computer_use",
    "image_generation",
    "view_image",  # ローカルの画像ファイルを読む
    "shell_tool",
    "unified_exec",
    "code_mode_host",  # コード実行のホスト
    "multi_agent",
    "goals",
    "sleep_tool",
)


# --strict-config の下で codex が起動引数を拒否したときの stderr の書き出し(実測)。
# 先頭一致に限る: 起動設定の検査はプロンプトを読む前に終わるため、外部本文が混ざる
# 余地のない位置で判定する
_CODEX_CONFIG_ERRORS = ("Error: Unknown feature flag", "Error loading config")


def _disabled_tool_args() -> list[str]:
    """推論専用に要らないツールを機能ごと外す -c / --disable 群(GH#129)。"""
    args: list[str] = []
    for feature in _DISABLED_FEATURES:
        args += ["--disable", feature]
    return [*args, "-c", 'web_search="disabled"']


def _ephemeral_codex_home(base: Path) -> str:
    """auth.json(~/.codex/auth.json への symlink)だけを含む使い捨て CODEX_HOME を作る。

    symlink なので codex 側のトークンリフレッシュは実ファイルに反映される。
    ディレクトリごと呼び出し後に破棄されるため、codex が書くランタイムファイル
    (logs/memories/state)による汚染・持ち越しは構造的に起きない。
    """
    real_auth = Path.home() / ".codex" / "auth.json"
    if not real_auth.exists():
        raise LLMUnavailableError("~/.codex/auth.json がない(先に codex login が必要)")
    home = base / "codex-home"
    home.mkdir()
    (home / "auth.json").symlink_to(real_auth)
    return str(home)


def create_engine(engine_name: str, *, model_reasoning_effort: str = "low") -> LLMEngine:
    """config `llm.engine` からエンジンを生成する。

    config は語彙(enum)の検証のみ。未実装エンジンはここで fail fast する
    (doctor の未実装エンジン検査は #21)。effort の既定 low は analyze 段の
    現行契約(呼び出し側が用途に応じて上げる。調査段 = GH#105)。
    """
    if engine_name == "codex-cli":
        return CodexCliEngine(model_reasoning_effort=model_reasoning_effort)
    raise LLMError(
        f"エンジン {engine_name!r} は主経路に未実装(主経路は codex-cli のみ。anthropic-api は"
        "深掘りのレビュー役だけ(GH#153)、claude-cli は拡張点。設計 00 §5)"
    )


# --- anthropic-api(GH#153) ---

# .env の別名キー。ANTHROPIC_API_KEY は使わない(claude -p の無自覚な API 課金を防ぐため、
# 環境に載せない方針。設計 00 §4 / doctor #12)
ANTHROPIC_API_KEY_ENV = "INTEL_ANTHROPIC_API_KEY"
_ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_ANTHROPIC_CONNECT_TIMEOUT_S = 10
# 一時的な失敗(同一エンジンで 1 回だけやり直す)。他の非 200 も LLMError だが、
# 認証・権限・モデル ID の誤りはやり直しても直らない環境障害として分ける
_ANTHROPIC_UNAVAILABLE_STATUSES = frozenset({401, 403, 404})
# 正常終了の stop_reason。max_tokens(打ち切り)・refusal(拒否)などは出力が
# スキーマを満たす保証がないので失敗にする
_ANTHROPIC_OK_STOP_REASONS = frozenset({"end_turn", "stop_sequence"})


@dataclass(frozen=True)
class LLMUsage:
    """1 呼び出しで使ったトークン数と見積もり額(思考のトークンは output に含まれる)。"""

    input_tokens: int
    output_tokens: int
    cost_usd: float


class AnthropicApiEngine:
    """Claude の Messages API による推論(深掘りノートのレビュー役。GH#153)。

    推論専用は宣言でなく構造で作る: リクエストに tools・mcp_servers・container を
    一切付けないので、モデルは読む・書く・取りに行く手段を持たない。宛先は API の
    URL に固定し、プロキシの環境変数は読まない(trust_env=False)。

    課金は応答ごとに発生するので、成功・失敗にかかわらず応答の usage を
    `last_usage` に残す(呼び出し側が費用を記録する)。
    """

    name = "anthropic-api"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        effort: str,
        max_tokens: int,
        input_usd_per_mtok: float,
        output_usd_per_mtok: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        # 印字可能な ASCII 以外(空白・制御文字・非 ASCII)を含むキーは、HTTP ヘッダの検査で
        # キーを含む例外になりうるため、送る前に止める(例外にキーを載せない)
        if not re.fullmatch(r"[\x21-\x7e]+", api_key):
            raise LLMUnavailableError("API キーの形が不正(印字可能な ASCII 以外を含む)")
        self._api_key = api_key
        self.model = model  # analyses.model への記録と同じ属性名(analyzer の getattr)
        self._effort = effort
        self._max_tokens = max_tokens
        self._input_price = input_usd_per_mtok
        self._output_price = output_usd_per_mtok
        self._transport = transport
        self.last_usage: LLMUsage | None = None

    def complete_structured(
        self, prompt: str, schema: type[M], timeout_s: int = DEFAULT_TIMEOUT_S
    ) -> M:
        self.last_usage = None
        body = {
            "model": self.model,
            "max_tokens": self._max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {"effort": self._effort},
        }
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        timeout = httpx.Timeout(timeout_s, connect=_ANTHROPIC_CONNECT_TIMEOUT_S)
        try:
            with httpx.Client(
                transport=self._transport, trust_env=False, timeout=timeout
            ) as client:
                response = client.post(_ANTHROPIC_MESSAGES_URL, headers=headers, json=body)
        # 元の例外はつながない(from None)。通信の例外のメッセージはヘッダの値(= キー)を
        # 含みうり、つなぐと traceback 経由でログに出る(logs は原因の例外も書き出す)
        except httpx.TimeoutException as e:
            raise LLMError(f"タイムアウト({timeout_s}s)・{type(e).__name__}") from None
        except httpx.HTTPError as e:
            raise LLMError(f"Anthropic API への通信失敗: {type(e).__name__}") from None

        if response.status_code != 200:
            # 応答本文のメッセージは載せない(種別だけ)。ログに残る文字列を有界に保つ
            detail = f"Anthropic API {response.status_code}({_anthropic_error_type(response)})"
            if response.status_code in _ANTHROPIC_UNAVAILABLE_STATUSES:
                raise LLMUnavailableError(detail)
            raise LLMError(detail)
        try:
            data = response.json()
        except ValueError as e:
            raise LLMError("Anthropic API の応答が JSON でない") from e
        if not isinstance(data, dict):
            raise LLMError("Anthropic API の応答の形が不正")

        self.last_usage = self._usage(data.get("usage"))
        stop_reason = data.get("stop_reason")
        if stop_reason not in _ANTHROPIC_OK_STOP_REASONS:
            reason = re.sub(r"[^\w-]", "", str(stop_reason))[:40]
            raise LLMError(f"Anthropic API の応答が途中で終わった(stop_reason={reason})")
        content = data.get("content")
        if not isinstance(content, list):
            raise LLMError("Anthropic API の応答に content が無い")
        # 思考のブロック(thinking)は読まず、本文のテキストだけをつなぐ
        text = "".join(
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
        try:
            return parse_structured(text, schema)
        except LLMError as e:
            # 応答の本文(外部の記事の文を含みうる)を例外 = ログに載せない。合わない場所だけ
            # (parse_structured は応答の先頭・入力値を載せ、原因の例外にも本文が残るため
            # つながない。GH#156 のレビュー)
            raise LLMError(_content_free_parse_error(e)) from None

    def _usage(self, usage: object) -> LLMUsage | None:
        if not isinstance(usage, dict):
            return None
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if not (
            isinstance(input_tokens, int)
            and not isinstance(input_tokens, bool)
            and isinstance(output_tokens, int)
            and not isinstance(output_tokens, bool)
        ):
            return None
        cost = (input_tokens * self._input_price + output_tokens * self._output_price) / 1_000_000
        return LLMUsage(input_tokens, output_tokens, round(cost, 6))


def _content_free_parse_error(error: LLMError) -> str:
    """parse_structured の失敗を、応答の本文を含まない説明にする(場所と種類だけ)。"""
    cause = error.__cause__
    if isinstance(cause, json.JSONDecodeError):
        return f"Anthropic API の応答が JSON でない({cause.msg}・位置 {cause.pos})"
    if isinstance(cause, ValidationError):
        places = [
            f"{'.'.join(str(part) for part in item['loc'])}: {item['type']}"
            for item in cause.errors(
                include_url=False, include_context=False, include_input=False
            )[:5]
        ]
        detail = re.sub(r"[^\w.:/ -]", "", " / ".join(places))[:200]
        return f"Anthropic API の応答がスキーマに合わない({detail})"
    return "Anthropic API の応答を検証できない"


def _anthropic_error_type(response: httpx.Response) -> str:
    """エラー応答の種別(`error.type`)だけを取り出す。読めなければ "unknown"。"""
    try:
        error = response.json().get("error", {})
        kind = error.get("type") if isinstance(error, dict) else None
    except (ValueError, AttributeError):
        kind = None
    if not isinstance(kind, str):
        return "unknown"
    return re.sub(r"[^\w-]", "", kind)[:40] or "unknown"


def create_anthropic_engine(
    settings: AnthropicApiConfig,
    env: Mapping[str, str],
    *,
    transport: httpx.BaseTransport | None = None,
) -> AnthropicApiEngine:
    """config `[anthropic_api]` と .env(load_env の結果)から生成する。

    キーが無ければ環境障害(LLMUnavailableError)。os.environ は読まない(設計 00 §4)。
    """
    api_key = env.get(ANTHROPIC_API_KEY_ENV, "").strip()
    if not api_key:
        raise LLMUnavailableError(f".env に {ANTHROPIC_API_KEY_ENV} が無い")
    return AnthropicApiEngine(
        api_key=api_key,
        model=settings.model,
        effort=settings.effort,
        max_tokens=settings.max_tokens,
        input_usd_per_mtok=settings.input_usd_per_mtok,
        output_usd_per_mtok=settings.output_usd_per_mtok,
        transport=transport,
    )
