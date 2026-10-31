"""config.toml + .env の読込・検証。

- 置き場: `~/.config/intel/`(設計 00 §4)。テストと doctor のため引数で差し替え可能。
- 検証は fail fast: ファイル欠落・不明キー・型不一致・値域外は ConfigError で止める
  (黙ってデフォルトに倒すと設定ミスが実行時まで潜伏するため)。
- .env は os.environ に export しない(secrets を子プロセスへ拡散させない。設計 00 §4)。
"""

from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "intel"

_LLM_ENGINES = ("codex-cli", "claude-cli", "anthropic-api")

# X 検索(search/recent)の契約値。コネクタ(connectors/x_search.py)と検証の両方が参照する
# (コネクタ側に置くと config が httpx 依存を吸ってしまうため、軽い側=ここに置く)
X_SEARCH_MAX_SLOTS = 3  # 3スロット固定・入れ替え式(ADR 0001。増やさない設計の担保)
X_SEARCH_MIN_PAGE = 10  # search/recent の max_results 下限(X API v2 仕様)
X_SEARCH_QUERY_MAX = 512  # クエリ長上限(Basic tier)
X_SEARCH_QUERY_SUFFIX = "-is:retweet -is:reply"  # 各スロットに自動付加(設計 02 §2)


def x_search_query(slot: str) -> str:
    """スロットから実際に送る検索クエリ(コネクタの送信と長さの検証が共有する。設計 02 §2)。

    スロットを括弧で囲んでから除外句を付ける。X の検索は AND を OR より先に結び付けるため、
    括弧なしの `a OR b -is:retweet -is:reply` では除外が右の枝にしか掛からない(GH#230)。
    """
    return f"({slot}) {X_SEARCH_QUERY_SUFFIX}"


# S1b 取込の 1 ページ下限(liked_tweets / user tweets の max_results 下限 = 5 に統一。
# 設計 02 §2)。コネクタ側に置くと config が httpx 依存を吸うため、軽い側 = ここに置く
X_SIGNALS_MIN_PAGE = 5


class ConfigError(Exception):
    """設定の欠落・型不一致・値域外。呼び出し側は捕捉せず終了させる(CLI の入口が 1 行で出す)。"""


@dataclass(frozen=True)
class XConfig:
    enabled: bool = False  # Day 0 ゲート(#1)通過後、config で明示 on にする
    # ホームTL の日次読取上限(ADR 0001 予算配分)。JST の暦日の上限で、同じ日の実行で共有する
    # (再走は台帳の当日の合計を引いた残りだけを読む。GH#178)
    daily_read_cap: int = 130
    unit_price_usd: float = 0.004  # Day 0 実測(2026-07-10)。config で上書き可
    # ホームTL + 検索(+S1 の取込)の合算日次読取上限(単一ノブ。ADR 0001 予算配分)。
    # 内訳(daily_read_cap / [x_search].daily_read_cap)の合計がこれを超えると起動時エラー
    combined_daily_read_cap: int = 165


@dataclass(frozen=True)
class XOAuth2Config:
    """X OAuth 2.0(PKCE)クライアント設定(S1 = GH#58 / ADR 0001)。

    client_id は secret ではない(認可はブラウザ+PKCE で完結)。confidential client
    (portal で Web App 型)の client secret のみ .env(X_OAUTH2_CLIENT_SECRET)。
    """

    client_id: str | None = None  # 未設定 = S1 未セットアップ(intel x-auth が fail fast)
    # loopback redirect のポート。portal 登録の redirect URI
    # (http://127.0.0.1:<port>/callback)と完全一致していること
    callback_port: int = 8791


@dataclass(frozen=True)
class XSignalsConfig:
    """X 興味シグナル取込(S1b)+ インサイト重み(S1c)(GH#58 / ADR 0001)。

    daily_read_cap 0 = 取込無効(既定)。有効化は合算予算(combined_daily_read_cap)の
    内側での再配分 = 人間の config 変更(例: home 115 / search 35 / signals 15)。
    上限は JST の暦日の上限で、同じ日の実行で共有する(GH#178。backfill は台帳の印で別枠)。

    重みは「種類の重み × 直近の重み」(ADR 0001)。直近の重みは半減期
    half_life_days の指数減衰(移動平均の実装)。intel x-insights の表示にのみ
    使い、ランキング・検索スロットへの自動反映はしない(S2 の人間ゲート)。
    """

    daily_read_cap: int = 0
    weight_bookmark: float = 3.0  # 強・正(意図的に「後で」保存)
    weight_like: float = 1.0  # 弱・正(儀礼的も混じる)
    weight_self_rt: float = 3.0  # 強・正(是認・拡散)
    half_life_days: float = 14.0


@dataclass(frozen=True)
class XSearchConfig:
    # 検索クエリのスロット(最大 X_SEARCH_MAX_SLOTS 本・入れ替え式。ADR 0001)。
    # 空 = 検索無効(コネクタを配線しない)。演算子は書いてよいが
    # `-is:retweet -is:reply` はコネクタがスロットを括弧で囲んで自動付加する(設計 02 §2)
    slots: tuple[str, ...] = ()
    # 検索の日次読取上限(全スロットで均等割。ADR 0001 予算配分)。暦日の上限で同じ日の実行で
    # 共有する(GH#178)
    daily_read_cap: int = 35


@dataclass(frozen=True)
class DigestConfig:
    max_items: int = 10  # 「無効日3日連続なら5に削減」の運用ノブ(設計 03 §6)


# codex の model_reasoning_effort の語彙(実在値のみ。タイポで黙って既定に
# 落ちる事故を config 検証で止める)
DIG_EFFORTS = ("minimal", "low", "medium", "high")


@dataclass(frozen=True)
class DigConfig:
    """朝の調査段(GH#105 / 設計 06 §3〜4)。

    daily_cap は「1 日に掘る上限」(JST 暦日。同日の再実行では残枠だけ掘る)。
    0 = 調査段無効(段ごと省く。x_signals と同じ流儀)。
    """

    daily_cap: int = 2  # 設計 06 §3 の既定
    reasoning_effort: str = "medium"  # 解説ノート生成の effort(対訳は low 固定)
    timeout_s: int = 600  # LLM 1 呼び出しの上限(解説・対訳チャンク共通)
    body_max_chars: int = 40_000  # 解説生成に渡す本文の上限(超過は切り詰め注記)
    translation_max_chars: int = 24_000  # 対訳対象の本文上限(超過分は打ち切り注記)
    min_body_chars: int = 200  # これ未満は材料不足(自動選出は skip・指定は失敗)
    # カードへの質問への返事(GH#126)。スマホで待つので effort は既定 low(応答の速さ優先)
    answer_reasoning_effort: str = "low"
    answer_source_max_chars: int = 30_000  # 返事の材料に渡す原文/対訳ノートの上限
    # 1 日に受け付ける質問の上限(JST 暦日。暴走と費用の歯止め)。0 = 返事をしない(記録だけ)
    answer_daily_cap: int = 20
    # 解説ノートを別系統の AI(Claude API = [anthropic_api])にレビューさせ、指摘があれば
    # 書き手が 1 回だけ直す(GH#153)。キーは .env の INTEL_ANTHROPIC_API_KEY
    review: bool = False
    # レビューの API 費用の月の上限(UTC の暦月 = doctor #9 の当月)。達したらその月はレビューを省く
    review_monthly_cap_usd: float = 10.0


@dataclass(frozen=True)
class LLMConfig:
    engine: str = "codex-cli"  # 仮決定(Issue #3)。値は _LLM_ENGINES のいずれか


# Claude の Messages API の effort の語彙(output_config.effort。モデルにより使えない
# 段がある。使えない値は API が 400 で返す)
ANTHROPIC_EFFORTS = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class AnthropicApiConfig:
    """Claude API(`anthropic-api` エンジン。GH#153)。キーは .env の INTEL_ANTHROPIC_API_KEY。

    単価は費用の見積もりに使う(請求の正本は Anthropic 側)。モデルを替えたら単価も合わせる。
    """

    model: str = "claude-opus-5-5"
    effort: str = "medium"  # Opus 5.5 の既定と同じ(思考の量 = 費用と時間の主なつまみ)
    max_tokens: int = 16_000  # 出力の上限(思考 + 本文の合計。超えると打ち切り = 失敗)
    input_usd_per_mtok: float = 4.0  # Opus 5.5 の単価(2026-10 時点)
    output_usd_per_mtok: float = 20.0


# 予備のエンジンの語彙(GH#156)。"" = 切り替えない
LLM_FALLBACK_ENGINES = ("", "anthropic-api")


@dataclass(frozen=True)
class LLMFallbackConfig:
    """主のエンジン(Codex)が失敗した呼び出しを予備でやり直す(GH#156 / 設計 00 §5)。

    予備は anthropic-api(キーは .env の INTEL_ANTHROPIC_API_KEY)。effort は主の effort に
    合わせる。単価は費用の見積もりに使う(モデルを替えたら単価も合わせる)。
    """

    engine: str = ""  # "" = 切り替えない(既定)/ "anthropic-api"
    model: str = "claude-sonnet-5-5"
    max_tokens: int = 16_000  # 出力の上限(思考 + 本文の合計)
    input_usd_per_mtok: float = 2.0  # Sonnet 5.5 の単価(2026-10 時点)
    output_usd_per_mtok: float = 10.0
    # 予備の費用の月(UTC の暦月 = doctor #9 の当月)の上限。達したらその月は予備を使わない
    monthly_cap_usd: float = 10.0


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
    folder: str = "SignalForge"  # Vault 内の出力フォルダ(vault の中の相対パス。設計 04 §7)


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
    daily_llm_limit: int = 150  # 規則4(JST の暦日の上限。同じ日の実行で共有する。GH#178)
    batch_size: int = 15  # Stage 2 の1呼び出し件数(設計 03 §2)
    body_max_chars: int = 2000  # Stage 2 の本文切り詰め
    profile_path: str | None = None  # 未設定なら <config_dir>/interest-profile.md


@dataclass(frozen=True)
class Config:
    config_dir: Path
    x: XConfig = field(default_factory=XConfig)
    x_oauth2: XOAuth2Config = field(default_factory=XOAuth2Config)
    x_search: XSearchConfig = field(default_factory=XSearchConfig)
    x_signals: XSignalsConfig = field(default_factory=XSignalsConfig)
    digest: DigestConfig = field(default_factory=DigestConfig)
    dig: DigConfig = field(default_factory=DigConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    anthropic_api: AnthropicApiConfig = field(default_factory=AnthropicApiConfig)
    llm_fallback: LLMFallbackConfig = field(default_factory=LLMFallbackConfig)
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
    "x_oauth2": XOAuth2Config,
    "x_search": XSearchConfig,
    "x_signals": XSignalsConfig,
    "digest": DigestConfig,
    "dig": DigConfig,
    "llm": LLMConfig,
    "anthropic_api": AnthropicApiConfig,
    "llm_fallback": LLMFallbackConfig,
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

    ファイルが存在しない場合も ConfigError で止める。既定値で続けると、`--config-dir` の
    誤指定や設定の消失のときに、意図と別の既定の DB を変更してしまうため(GH#338)。
    初回セットアップ前の `intel doctor` は、呼ぶ前に存在を確かめて #1 の FAIL にし、残りの
    検査を続ける(設計 05 §2)。未設定のキーだけが既定値になる。
    """
    path = config_dir / "config.toml"
    if not path.exists():
        raise ConfigError(
            f"{path} が存在しない(--config-dir の指定と設定ファイルの置き場を確かめる。"
            "既定の設定で既定の DB を変更しないよう、ここで止める)"
        )

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
        # nan/inf は TOML として合法だが、nan は大小比較が常に偽、inf は「0 以上」「正」を満たすので
        # 値域の検証(_validate)をすり抜ける。浮動小数点の設定はすべて先に有限値だけを受ける
        # (GH#198)。int の値は常に有限で、巨大な int を math.isfinite に渡すと OverflowError
        if fields[key] == "float" and isinstance(value, float) and not math.isfinite(value):
            raise ConfigError(f"{path}: [{name}].{key} は有限値であること(nan/inf は不可): {value}")
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
    _validate_x_search(path, config)
    # nan/inf は TOML として合法だがスコア計算・順位を壊すため有限値のみ受ける
    if not (
        math.isfinite(config.x_signals.half_life_days) and config.x_signals.half_life_days > 0
    ):
        raise ConfigError(f"{path}: [x_signals].half_life_days は正の有限値(減衰の半減期)")
    for key, value in (
        ("weight_bookmark", config.x_signals.weight_bookmark),
        ("weight_like", config.x_signals.weight_like),
        ("weight_self_rt", config.x_signals.weight_self_rt),
    ):
        if not (math.isfinite(value) and value >= 0):
            raise ConfigError(
                f"{path}: [x_signals].{key} は 0 以上の有限値(負のシグナルは無い)"
            )
    signals_cap = config.x_signals.daily_read_cap
    if 0 < signals_cap < X_SIGNALS_MIN_PAGE:
        raise ConfigError(
            f"{path}: [x_signals].daily_read_cap は 0(無効)か {X_SIGNALS_MIN_PAGE} 以上"
            f"(1 ページ下限。全 kind が黙って skip になる): {signals_cap}"
        )
    if not (1 <= config.x_oauth2.callback_port <= 65535):
        raise ConfigError(
            f"{path}: [x_oauth2].callback_port はポート番号(1〜65535): "
            f"{config.x_oauth2.callback_port}"
        )
    if config.x_oauth2.client_id is not None and not config.x_oauth2.client_id.strip():
        raise ConfigError(f"{path}: [x_oauth2].client_id が空文字(未使用なら行ごと削除)")
    # 空文字の vault_path は Path("") = 実行ディレクトリに解決され、ノートが Vault に届かない
    # (doctor・export は None だけを未設定として扱う)。未設定はキーの省略で表す(GH#251)
    if config.obsidian.vault_path is not None and not config.obsidian.vault_path.strip():
        raise ConfigError(f"{path}: [obsidian].vault_path が空文字(未設定なら行ごと削除)")
    # folder は vault の中の相対パスに限る。絶対パスや `..` は vault の外を指し、空(`.` を含む)は
    # ノートが vault の直下に散らばる(書き込み先を vault の中に限る。設計 04 §7・06 §4。GH#204)
    folder = Path(config.obsidian.folder)
    if (
        not config.obsidian.folder.strip()
        or not folder.parts
        or folder.is_absolute()
        or ".." in folder.parts
    ):
        raise ConfigError(
            f"{path}: [obsidian].folder は vault の中の相対パス(絶対パス・`..`・空は不可): "
            f"{config.obsidian.folder!r}"
        )
    cap = config.dig.review_monthly_cap_usd
    if not (math.isfinite(cap) and cap > 0):
        raise ConfigError(f"{path}: [dig].review_monthly_cap_usd は正の有限値: {cap}")
    if config.anthropic_api.effort not in ANTHROPIC_EFFORTS:
        raise ConfigError(
            f"{path}: [anthropic_api].effort は {'/'.join(ANTHROPIC_EFFORTS)} のいずれか: "
            f"{config.anthropic_api.effort!r}"
        )
    if config.llm_fallback.engine not in LLM_FALLBACK_ENGINES:
        raise ConfigError(
            f"{path}: [llm_fallback].engine は \"\"(切り替えない)か anthropic-api: "
            f"{config.llm_fallback.engine!r}"
        )
    for section, settings in (
        ("anthropic_api", config.anthropic_api),
        ("llm_fallback", config.llm_fallback),
    ):
        if not settings.model.strip():
            raise ConfigError(f"{path}: [{section}].model が空文字")
        for key in ("input_usd_per_mtok", "output_usd_per_mtok"):
            price = getattr(settings, key)
            if not (math.isfinite(price) and price >= 0):
                raise ConfigError(f"{path}: [{section}].{key} は 0 以上の有限値: {price}")
    fallback_cap = config.llm_fallback.monthly_cap_usd
    if not (math.isfinite(fallback_cap) and fallback_cap > 0):
        raise ConfigError(f"{path}: [llm_fallback].monthly_cap_usd は正の有限値: {fallback_cap}")
    for key in ("reasoning_effort", "answer_reasoning_effort"):
        if getattr(config.dig, key) not in DIG_EFFORTS:
            raise ConfigError(
                f"{path}: [dig].{key} は {'/'.join(DIG_EFFORTS)} のいずれか: "
                f"{getattr(config.dig, key)!r}"
            )
    positives = [
        ("x", "daily_read_cap", config.x.daily_read_cap),
        ("dig", "timeout_s", config.dig.timeout_s),
        ("dig", "body_max_chars", config.dig.body_max_chars),
        ("dig", "translation_max_chars", config.dig.translation_max_chars),
        ("dig", "min_body_chars", config.dig.min_body_chars),
        ("dig", "answer_source_max_chars", config.dig.answer_source_max_chars),
        ("x", "combined_daily_read_cap", config.x.combined_daily_read_cap),
        ("x_search", "daily_read_cap", config.x_search.daily_read_cap),
        ("digest", "max_items", config.digest.max_items),
        ("retention", "cache_ttl_days", config.retention.cache_ttl_days),
        ("retention", "x_cache_ttl_days", config.retention.x_cache_ttl_days),
        ("analysis", "freshness_hours", config.analysis.freshness_hours),
        ("analysis", "daily_llm_limit", config.analysis.daily_llm_limit),
        ("analysis", "batch_size", config.analysis.batch_size),
        ("analysis", "body_max_chars", config.analysis.body_max_chars),
        ("ranking", "max_per_source", config.ranking.max_per_source),
        ("anthropic_api", "max_tokens", config.anthropic_api.max_tokens),
        ("llm_fallback", "max_tokens", config.llm_fallback.max_tokens),
    ]
    nonnegatives = [
        ("x_signals", "daily_read_cap", config.x_signals.daily_read_cap),
        ("dig", "daily_cap", config.dig.daily_cap),
        ("dig", "answer_daily_cap", config.dig.answer_daily_cap),
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


def _validate_x_search(path: Path, config: Config) -> None:
    """[x_search] と合算予算の検証(ADR 0001 予算配分 / 設計 02 §2)。

    slots が空 = 検索無効(検証はスロット非依存の合算 cap のみ)。
    """
    slots = config.x_search.slots
    if len(slots) > X_SEARCH_MAX_SLOTS:
        raise ConfigError(
            f"{path}: [x_search].slots は最大 {X_SEARCH_MAX_SLOTS} 本"
            f"(増やさない設計。ADR 0001): {len(slots)} 本ある"
        )
    if len(set(slots)) != len(slots):
        raise ConfigError(f"{path}: [x_search].slots に重複クエリがある")
    for slot in slots:
        if not slot.strip():
            raise ConfigError(f"{path}: [x_search].slots に空のクエリがある")
        effective = x_search_query(slot)
        if len(effective) > X_SEARCH_QUERY_MAX:
            raise ConfigError(
                f"{path}: [x_search].slots のクエリが長すぎる(括弧と自動付加分込みで"
                f" {len(effective)} > {X_SEARCH_QUERY_MAX}): {slot[:40]}…"
            )
        if not _parens_balanced(slot):
            raise ConfigError(
                f"{path}: [x_search].slots のクエリの括弧か引用符が対応していない"
                f"(囲んだ括弧が途中で閉じ、除外句が全体に掛からない。GH#230): {slot[:40]}…"
            )
    if slots and config.x_search.daily_read_cap // len(slots) < X_SEARCH_MIN_PAGE:
        raise ConfigError(
            f"{path}: [x_search].daily_read_cap({config.x_search.daily_read_cap})が"
            f"少なすぎる(スロットあたり {X_SEARCH_MIN_PAGE} 件以上必要。"
            f"search/recent の max_results 下限)"
        )
    cap_sum = (
        config.x.daily_read_cap
        + (config.x_search.daily_read_cap if slots else 0)
        + config.x_signals.daily_read_cap  # S1b 取込も同じ予算の内側(ADR 0001)
    )
    if cap_sum > config.x.combined_daily_read_cap:
        raise ConfigError(
            f"{path}: X 読取の内訳合計({cap_sum})が [x].combined_daily_read_cap"
            f"({config.x.combined_daily_read_cap})を超えている(ADR 0001 予算配分)"
        )


def _parens_balanced(slot: str) -> bool:
    """スロットの括弧(引用符の外)が正しく入れ子になり、引用符が閉じているか(GH#230)。

    `ai) OR (llm` は囲むと `(ai) OR (llm) -is:retweet -is:reply` になり、除外が後ろの枝にしか
    掛からない。閉じていない引用符も囲んだ閉じ括弧と除外句を句の中に飲み込む。引用符(完全一致の
    句)の中の括弧は数えない。
    """
    depth = 0
    quoted = False
    for char in slot:
        if char == '"':
            quoted = not quoted
        elif quoted:
            continue
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not quoted


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
            # 行の内容は出さない(token の途中の改行や値だけの貼り付けでは secret の一部になり、
            # doctor・CLI の出力や launchd のログに残るため。行番号と種別だけを返す。GH#205)
            raise ConfigError(f"{path}:{lineno}: KEY=VALUE 形式でない行(= が無い)")
        key, _, value = stripped.partition("=")
        value = value.strip()
        # 囲みクォートは「対になっている場合のみ」1組だけ剥がす(str.strip だと
        # 値の先頭・末尾に正当に含まれる引用符まで削れて token を破壊する)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        env[key.strip()] = value
    return env
