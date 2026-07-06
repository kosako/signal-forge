"""config.toml + .env の読込・検証。

- 置き場: `~/.config/intel/`(設計 00 §4)。テストと doctor のため引数で差し替え可能。
- 検証は fail fast: ファイル欠落・不明キー・型不一致・値域外は ConfigError で止める
  (黙ってデフォルトに倒すと設定ミスが実行時まで潜伏するため)。
- .env は os.environ に export しない(secrets を子プロセスへ拡散させない。設計 00 §4)。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "intel"

_LLM_ENGINES = ("codex-cli", "claude-cli", "anthropic-api")


class ConfigError(Exception):
    """設定の欠落・型不一致・値域外。呼び出し側は捕捉せず終了させる。"""


@dataclass(frozen=True)
class XConfig:
    enabled: bool = False  # Day 0 ゲート(#1)通過までは無効が既定
    daily_read_cap: int = 130
    unit_price_usd: float = 0.005  # Day 0 実測値で上書きする


@dataclass(frozen=True)
class DigestConfig:
    max_items: int = 10  # 「無効日3日連続なら5に削減」の運用ノブ(設計 03 §6)


@dataclass(frozen=True)
class LLMConfig:
    engine: str = "codex-cli"  # 仮決定(Issue #3)。値は _LLM_ENGINES のいずれか


@dataclass(frozen=True)
class RetentionConfig:
    cache_ttl_days: int = 30  # RSS/manual(設計 01 §6)
    x_cache_ttl_days: int = 7  # 確定値(Issue #2)


@dataclass(frozen=True)
class BudgetConfig:
    monthly_cap_usd: float = 30.0


@dataclass(frozen=True)
class TelegramConfig:
    # bot token は .env 側。ここは認可 allowlist(設計 04 §6)
    owner_user_id: int | None = None
    owner_chat_id: int | None = None


@dataclass(frozen=True)
class ObsidianConfig:
    vault_path: str | None = None  # 宿題 H4 確定までは未設定を許容(#13 が要求)


@dataclass(frozen=True)
class Config:
    config_dir: Path
    x: XConfig = field(default_factory=XConfig)
    digest: DigestConfig = field(default_factory=DigestConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    obsidian: ObsidianConfig = field(default_factory=ObsidianConfig)

    @property
    def db_path(self) -> Path:
        return self.config_dir / "intel.db"


_SECTIONS = {
    "x": XConfig,
    "digest": DigestConfig,
    "llm": LLMConfig,
    "retention": RetentionConfig,
    "budget": BudgetConfig,
    "telegram": TelegramConfig,
    "obsidian": ObsidianConfig,
}


def load_config(config_dir: Path) -> Config:
    """config.toml を読み込み、型・値域を検証して Config を返す。

    ファイルが存在しない場合は全既定値で返す(初回セットアップ前でも
    `intel doctor` が動く必要があるため。欠落の警告は doctor #1 の責務)。
    """
    path = config_dir / "config.toml"
    if not path.exists():
        return Config(config_dir=config_dir)

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: TOML の構文エラー: {e}") from e

    sections: dict[str, object] = {}
    for name, value in raw.items():
        cls = _SECTIONS.get(name)
        if cls is None:
            raise ConfigError(f"{path}: 不明なセクション [{name}]")
        if not isinstance(value, dict):
            raise ConfigError(f"{path}: [{name}] はテーブルであること")
        sections[name] = _build_section(path, name, cls, value)

    config = Config(config_dir=config_dir, **sections)  # type: ignore[arg-type]
    _validate(path, config)
    return config


def _build_section(path: Path, name: str, cls: type, values: dict) -> object:
    fields = {f.name: f.type for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    unknown = set(values) - set(fields)
    if unknown:
        raise ConfigError(f"{path}: [{name}] に不明なキー: {', '.join(sorted(unknown))}")
    for key, value in values.items():
        if not _type_ok(fields[key], value):
            raise ConfigError(f"{path}: [{name}].{key} の型が不正: {value!r}")
    return cls(**values)


def _type_ok(annotation: str, value: object) -> bool:
    # dataclass の型注釈(from __future__ 有効時は文字列)と TOML 値の突合。
    # bool は int のサブクラスなので int 判定より先に弾く。
    match annotation:
        case "bool":
            return isinstance(value, bool)
        case "int" | "int | None":
            return isinstance(value, int) and not isinstance(value, bool)
        case "float":
            return isinstance(value, int | float) and not isinstance(value, bool)
        case "str" | "str | None":
            return isinstance(value, str)
    raise ConfigError(f"内部エラー: 未対応の型注釈 {annotation}")


def _validate(path: Path, config: Config) -> None:
    if config.llm.engine not in _LLM_ENGINES:
        raise ConfigError(
            f"{path}: [llm].engine は {'/'.join(_LLM_ENGINES)} のいずれか: {config.llm.engine!r}"
        )
    positives = [
        ("x", "daily_read_cap", config.x.daily_read_cap),
        ("digest", "max_items", config.digest.max_items),
        ("retention", "cache_ttl_days", config.retention.cache_ttl_days),
        ("retention", "x_cache_ttl_days", config.retention.x_cache_ttl_days),
    ]
    for section, key, value in positives:
        if value <= 0:
            raise ConfigError(f"{path}: [{section}].{key} は正の整数であること: {value}")
    if config.x.unit_price_usd < 0 or config.budget.monthly_cap_usd <= 0:
        raise ConfigError(f"{path}: 単価・予算上限は正の値であること")


def load_env(config_dir: Path) -> dict[str, str]:
    """`.env`(KEY=VALUE 形式)を dict で返す。os.environ には入れない。

    ファイルが無ければ空 dict(キーの要求は使う側 = doctor / 各コネクタの責務)。
    """
    path = config_dir / ".env"
    if not path.exists():
        return {}
    env: dict[str, str] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise ConfigError(f"{path}:{lineno}: KEY=VALUE 形式でない行: {stripped[:20]}…")
        key, _, value = stripped.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env
