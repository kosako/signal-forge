"""config.toml + .env の読込・検証。

- 置き場: `~/.config/intel/`(設計 00 §4)。テストと doctor のため引数で差し替え可能。
- 検証は fail fast: ファイル欠落・不明キー・型不一致・値域外は ConfigError で止める
  (黙ってデフォルトに倒すと設定ミスが実行時まで潜伏するため)。
- .env は os.environ に export しない(secrets を子プロセスへ拡散させない。設計 00 §4)。
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "intel"

_LLM_ENGINES = ("codex-cli", "claude-cli", "anthropic-api")


class ConfigError(Exception):
    """設定の欠落・型不一致・値域外。呼び出し側は捕捉せず終了させる。"""


@dataclass(frozen=True)
class XConfig:
    enabled: bool = False  # Day 0 ゲート(#1)通過後、config で明示 on にする
    daily_read_cap: int = 130  # ホームTL の日次読取上限(ADR 0001 予算配分。検索は #57)
    unit_price_usd: float = 0.004  # Day 0 実測(2026-07-10)。config で上書き可


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
    # 内蔵タイマー(JST・HH:MM)。夜アンケート(設計 04 §4)/ watchdog(設計 05 §4)。
    # watchdog は配信時刻(宿題 H5)+15分に合わせて調整する
    survey_time: str = "21:30"
    watchdog_time: str = "07:15"


@dataclass(frozen=True)
class ObsidianConfig:
    vault_path: str | None = None  # 宿題 H4 確定までは未設定を許容(export 実行時に要求)
    folder: str = "SignalForge"  # Vault 内の出力フォルダ(設計 04 §7)


@dataclass(frozen=True)
class StorageConfig:
    db_path: str | None = None  # 未設定なら ~/.local/share/intel/intel.db(設計 01 §1)
    backup_dir: str | None = None  # 未設定なら ~/Backups/intel(設計 05 §5)


@dataclass(frozen=True)
class RankingConfig:
    # スコア式 v1 の係数(設計 03 §6)。すべてコード変更なしで調整可
    min_score: float = 40.0  # 未満は件数が余っていても載せない
    novelty_new: float = 1.0
    novelty_update: float = 0.9
    novelty_known: float = 0.3
    negative_penalty: float = 15.0  # negative ラベル1個につき
    freshness_penalty_24_48: float = 5.0
    freshness_penalty_48_72: float = 10.0
    topic_dup_penalty: float = 20.0  # 当日バッチ内 topic 一致の2件目以降
    max_per_source: int = 4  # 上位内の同一ソース上限(超過は次点繰上げ)


@dataclass(frozen=True)
class AnalysisConfig:
    block_domains: tuple[str, ...] = ()  # 静的 NG(設計 03 §2 規則1)
    block_keywords: tuple[str, ...] = ()  # タイトル部分一致
    freshness_hours: int = 72  # 規則2(manual は適用外)
    daily_llm_limit: int = 150  # 規則4
    batch_size: int = 15  # Stage 2 の1呼び出し件数(設計 03 §2)
    body_max_chars: int = 2000  # Stage 2 の本文切り詰め
    profile_path: str | None = None  # 未設定なら <config_dir>/interest-profile.md


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
    storage: StorageConfig = field(default_factory=StorageConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    ranking: RankingConfig = field(default_factory=RankingConfig)

    @property
    def db_path(self) -> Path:
        if self.storage.db_path is not None:
            return Path(self.storage.db_path).expanduser()
        return Path.home() / ".local" / "share" / "intel" / "intel.db"

    @property
    def backup_dir(self) -> Path:
        if self.storage.backup_dir is not None:
            return Path(self.storage.backup_dir).expanduser()
        return Path.home() / "Backups" / "intel"

    @property
    def profile_path(self) -> Path:
        if self.analysis.profile_path is not None:
            return Path(self.analysis.profile_path).expanduser()
        return self.config_dir / "interest-profile.md"


_SECTIONS = {
    "x": XConfig,
    "digest": DigestConfig,
    "llm": LLMConfig,
    "retention": RetentionConfig,
    "budget": BudgetConfig,
    "telegram": TelegramConfig,
    "obsidian": ObsidianConfig,
    "storage": StorageConfig,
    "analysis": AnalysisConfig,
    "ranking": RankingConfig,
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
    # TOML の配列は list で来るが、frozen dataclass の既定は不変の tuple に揃える
    coerced = {
        key: tuple(value) if isinstance(value, list) else value for key, value in values.items()
    }
    return cls(**coerced)


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
        case "tuple[str, ...]":
            return isinstance(value, list) and all(isinstance(v, str) for v in value)
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
        ("analysis", "freshness_hours", config.analysis.freshness_hours),
        ("analysis", "daily_llm_limit", config.analysis.daily_llm_limit),
        ("analysis", "batch_size", config.analysis.batch_size),
        ("analysis", "body_max_chars", config.analysis.body_max_chars),
        ("ranking", "max_per_source", config.ranking.max_per_source),
    ]
    nonnegatives = [
        ("ranking", "min_score", config.ranking.min_score),
        ("ranking", "negative_penalty", config.ranking.negative_penalty),
        ("ranking", "freshness_penalty_24_48", config.ranking.freshness_penalty_24_48),
        ("ranking", "freshness_penalty_48_72", config.ranking.freshness_penalty_48_72),
        ("ranking", "topic_dup_penalty", config.ranking.topic_dup_penalty),
        ("ranking", "novelty_new", config.ranking.novelty_new),
        ("ranking", "novelty_update", config.ranking.novelty_update),
        ("ranking", "novelty_known", config.ranking.novelty_known),
    ]
    for section, key, value in nonnegatives:
        if value < 0:
            raise ConfigError(f"{path}: [{section}].{key} は 0 以上であること: {value}")
    for section, key, value in positives:
        if value <= 0:
            raise ConfigError(f"{path}: [{section}].{key} は正の整数であること: {value}")
    if config.x.unit_price_usd < 0 or config.budget.monthly_cap_usd <= 0:
        raise ConfigError(f"{path}: 単価・予算上限は正の値であること")
    for key, value in (
        ("survey_time", config.telegram.survey_time),
        ("watchdog_time", config.telegram.watchdog_time),
    ):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ConfigError(f"{path}: [telegram].{key} は HH:MM 形式であること: {value!r}")


def load_env(config_dir: Path) -> dict[str, str]:
    """`.env`(KEY=VALUE 形式)を dict で返す。os.environ には入れない。

    ファイルが無ければ空 dict(キーの要求は使う側 = doctor / 各コネクタの責務)。
    """
    path = config_dir / ".env"
    if not path.exists():
        return {}
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise ConfigError(
            f"{path}: パーミッションが {mode:o}。group/other に開いた .env は読み込まない"
            "(chmod 600 にすること。設計 00 §4)"
        )
    env: dict[str, str] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise ConfigError(f"{path}:{lineno}: KEY=VALUE 形式でない行: {stripped[:20]}…")
        key, _, value = stripped.partition("=")
        value = value.strip()
        # 囲みクォートは「対になっている場合のみ」1組だけ剥がす(str.strip だと
        # 値の先頭・末尾に正当に含まれる引用符まで削れて token を破壊する)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        env[key.strip()] = value
    return env
