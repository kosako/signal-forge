from dataclasses import fields
from pathlib import Path

import pytest

from app.config import _SECTIONS, Config, ConfigError, load_config, load_env


def write_config(tmp_path: Path, body: str) -> Path:
    (tmp_path / "config.toml").write_text(body, encoding="utf-8")
    return tmp_path


def empty_config(tmp_path: Path) -> Path:
    """空の config.toml だけを置いた別の設定ディレクトリ(全キーが既定値になる)。"""
    config_dir = tmp_path / "empty"
    config_dir.mkdir()
    return write_config(config_dir, "")


def test_missing_file_fails_with_searched_path(tmp_path: Path) -> None:
    # 既定値で続けると、--config-dir の誤指定や設定の消失で既定の DB を変更する(GH#338)
    with pytest.raises(ConfigError, match="config.toml が存在しない") as e:
        load_config(tmp_path / "nowhere")
    assert str(tmp_path / "nowhere" / "config.toml") in str(e.value)


def test_empty_file_returns_defaults(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, ""))
    assert isinstance(config, Config)
    assert config.digest.max_items == 10
    assert config.retention.cache_ttl_days == 30
    assert config.retention.x_cache_ttl_days == 7
    assert config.llm.engine == "codex-cli"
    assert config.x.enabled is False
    assert config.db_path == Path.home() / ".local" / "share" / "intel" / "intel.db"


def test_analysis_section_lists_become_tuples(tmp_path: Path) -> None:
    write_config(
        tmp_path,
        '[analysis]\nblock_domains = ["spam.example"]\nblock_keywords = ["宣伝"]\n'
        "daily_llm_limit = 100\n",
    )
    config = load_config(tmp_path)
    assert config.analysis.block_domains == ("spam.example",)
    assert config.analysis.block_keywords == ("宣伝",)
    assert config.analysis.daily_llm_limit == 100


def test_analysis_rejects_nonpositive_and_bad_list(tmp_path: Path) -> None:
    write_config(tmp_path, "[analysis]\ndaily_llm_limit = 0\n")
    with pytest.raises(ConfigError, match="正の整数"):
        load_config(tmp_path)
    write_config(tmp_path, "[analysis]\nblock_domains = [1, 2]\n")
    with pytest.raises(ConfigError, match="型が不正"):
        load_config(tmp_path)


def test_x_search_parse_and_defaults(tmp_path: Path) -> None:
    write_config(tmp_path, '[x_search]\nslots = ["ai agents", "llm"]\n')
    config = load_config(tmp_path)
    assert config.x_search.slots == ("ai agents", "llm")
    assert config.x_search.daily_read_cap == 35
    assert config.x.combined_daily_read_cap == 165
    assert load_config(empty_config(tmp_path)).x_search.slots == ()  # 既定 = 検索無効


def test_x_search_slots_validation(tmp_path: Path) -> None:
    write_config(tmp_path, '[x_search]\nslots = ["a", "b", "c", "d"]\n')
    with pytest.raises(ConfigError, match="最大 3 本"):  # 増やさない設計の担保(ADR 0001)
        load_config(tmp_path)
    write_config(tmp_path, '[x_search]\nslots = ["a", "a"]\n')
    with pytest.raises(ConfigError, match="重複"):
        load_config(tmp_path)
    write_config(tmp_path, '[x_search]\nslots = ["a", " "]\n')
    with pytest.raises(ConfigError, match="空のクエリ"):
        load_config(tmp_path)
    write_config(tmp_path, f'[x_search]\nslots = ["{"x" * 500}"]\n')
    with pytest.raises(ConfigError, match="長すぎる"):  # 括弧と自動付加分込みで 512 超
        load_config(tmp_path)


def test_x_search_slot_length_counts_grouping_parens(tmp_path: Path) -> None:
    # 送るクエリは「(スロット) -is:retweet -is:reply」(GH#230)。括弧の 2 字も 512 字に数える:
    # 512 - len("() -is:retweet -is:reply") = 488 字までのスロットは通し、489 字は止める
    write_config(tmp_path, f'[x_search]\nslots = ["{"x" * 488}"]\n')
    assert load_config(tmp_path).x_search.slots == ("x" * 488,)
    write_config(tmp_path, f'[x_search]\nslots = ["{"x" * 489}"]\n')
    with pytest.raises(ConfigError, match="長すぎる"):
        load_config(tmp_path)


@pytest.mark.parametrize(
    "slot",
    [
        "ai) OR (llm",  # 囲むと (ai) OR (llm) -is:retweet -is:reply → 除外が llm の側だけ
        "(ai OR llm",
        "ai OR llm)",
        '"ai OR llm',  # 閉じていない引用符が囲んだ閉じ括弧と除外句を飲み込む
    ],
)
def test_x_search_slot_rejects_unbalanced_parens_or_quotes(tmp_path: Path, slot: str) -> None:
    # 括弧で囲んでも途中で閉じるスロットは、除外句が全体に掛からない(GH#230)。起動時に止める
    write_config(tmp_path, f"[x_search]\nslots = ['{slot}']\n")  # TOML のリテラル文字列
    with pytest.raises(ConfigError, match="対応していない"):
        load_config(tmp_path)


def test_x_search_slot_accepts_nested_parens_and_parens_in_quotes(tmp_path: Path) -> None:
    slots = ("((ai OR llm) agents) lang:ja", '"ai :)" OR llm')  # 引用符の中の括弧は数えない
    write_config(tmp_path, f"[x_search]\nslots = ['{slots[0]}', '{slots[1]}']\n")
    assert load_config(tmp_path).x_search.slots == slots


def test_x_search_cap_requires_min_page_per_slot(tmp_path: Path) -> None:
    # 20 // 3 = 6 < 10(search/recent の max_results 下限)→ 黙って 0 件にせず起動時に止める
    write_config(tmp_path, '[x_search]\nslots = ["a", "b", "c"]\ndaily_read_cap = 20\n')
    with pytest.raises(ConfigError, match="少なすぎる"):
        load_config(tmp_path)


def test_x_combined_cap_cross_validation(tmp_path: Path) -> None:
    # slots ありで内訳合計(130 + 40)が合算 cap 165 を超える → エラー(ADR 0001 予算配分)
    write_config(tmp_path, '[x_search]\nslots = ["a"]\ndaily_read_cap = 40\n')
    with pytest.raises(ConfigError, match="combined_daily_read_cap"):
        load_config(tmp_path)
    # slots が空なら検索 cap は合算に数えない(130 ≤ 165 で通る)
    write_config(tmp_path, "[x_search]\ndaily_read_cap = 40\n")
    assert load_config(tmp_path).x_search.daily_read_cap == 40
    # 合算 cap 自体を上げれば通る(単一ノブ)
    write_config(
        tmp_path,
        '[x]\ncombined_daily_read_cap = 170\n[x_search]\nslots = ["a"]\ndaily_read_cap = 40\n',
    )
    assert load_config(tmp_path).x.combined_daily_read_cap == 170


def test_db_path_configurable(tmp_path: Path) -> None:
    write_config(tmp_path, f'[storage]\ndb_path = "{tmp_path}/custom.db"\n')
    assert load_config(tmp_path).db_path == tmp_path / "custom.db"


def test_valid_config_overrides_defaults(tmp_path: Path) -> None:
    write_config(
        tmp_path,
        """
        [digest]
        max_items = 5

        [x]
        enabled = true
        daily_read_cap = 100
        unit_price_usd = 0.001

        [llm]
        engine = "claude-cli"

        [telegram]
        owner_user_id = 12345
        """,
    )
    config = load_config(tmp_path)
    assert config.digest.max_items == 5
    assert config.x.enabled is True
    assert config.x.unit_price_usd == 0.001
    assert config.llm.engine == "claude-cli"
    assert config.telegram.owner_user_id == 12345
    assert config.telegram.owner_chat_id is None


def test_unknown_section_fails(tmp_path: Path) -> None:
    write_config(tmp_path, "[degist]\nmax_items = 5\n")
    with pytest.raises(ConfigError, match="不明なセクション"):
        load_config(tmp_path)


def test_unknown_key_fails(tmp_path: Path) -> None:
    write_config(tmp_path, "[digest]\nmax_item = 5\n")
    with pytest.raises(ConfigError, match="不明なキー"):
        load_config(tmp_path)


def test_wrong_type_fails(tmp_path: Path) -> None:
    write_config(tmp_path, '[digest]\nmax_items = "ten"\n')
    with pytest.raises(ConfigError, match="型が不正"):
        load_config(tmp_path)


def test_bool_is_not_int(tmp_path: Path) -> None:
    write_config(tmp_path, "[digest]\nmax_items = true\n")
    with pytest.raises(ConfigError, match="型が不正"):
        load_config(tmp_path)


def test_invalid_engine_fails(tmp_path: Path) -> None:
    write_config(tmp_path, '[llm]\nengine = "gpt-web"\n')
    with pytest.raises(ConfigError, match="engine"):
        load_config(tmp_path)


def test_nonpositive_value_fails(tmp_path: Path) -> None:
    write_config(tmp_path, "[digest]\nmax_items = 0\n")
    with pytest.raises(ConfigError, match="正の整数"):
        load_config(tmp_path)


def test_broken_toml_fails(tmp_path: Path) -> None:
    write_config(tmp_path, "[digest\n")
    with pytest.raises(ConfigError, match="構文エラー"):
        load_config(tmp_path)


def write_env(tmp_path: Path, body: str, mode: int = 0o600) -> Path:
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    path.chmod(mode)
    return path


def test_load_env_parses_and_strips_quotes(tmp_path: Path) -> None:
    write_env(tmp_path, '# comment\nX_API_KEY=abc123\nTELEGRAM_BOT_TOKEN="tok:en"\n\n')
    env = load_env(tmp_path)
    assert env == {"X_API_KEY": "abc123", "TELEGRAM_BOT_TOKEN": "tok:en"}


def test_load_env_rejects_world_readable(tmp_path: Path) -> None:
    write_env(tmp_path, "X_API_KEY=abc\n", mode=0o644)
    with pytest.raises(ConfigError, match="chmod 600"):
        load_env(tmp_path)


def test_load_env_accepts_stricter_than_600(tmp_path: Path) -> None:
    write_env(tmp_path, "X_API_KEY=abc\n", mode=0o400)
    assert load_env(tmp_path) == {"X_API_KEY": "abc"}


def test_load_env_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_env(tmp_path) == {}


def test_load_env_rejects_malformed_line(tmp_path: Path) -> None:
    write_env(tmp_path, "JUST_A_KEY\n")
    with pytest.raises(ConfigError, match="KEY=VALUE"):
        load_env(tmp_path)


def test_load_env_malformed_line_does_not_echo_content(tmp_path: Path) -> None:
    # 区切りの無い行は secret の一部(token の途中の改行・値だけの貼り付け)でありうるので、
    # 行番号と種別だけを出し、行の内容は先頭も含めて出さない(GH#205)
    write_env(tmp_path, "X_API_KEY=abc\nQz7Wk3Rv9Lm2Xp5Tn8Hd4Fj6\n")
    with pytest.raises(ConfigError, match=r"\.env:2: KEY=VALUE") as excinfo:
        load_env(tmp_path)
    assert "Qz7W" not in str(excinfo.value)


def test_telegram_timer_times_validated(tmp_path: Path) -> None:
    write_config(tmp_path, '[telegram]\nsurvey_time = "21:30"\nwatchdog_time = "07:15"\n')
    config = load_config(tmp_path)
    assert config.telegram.survey_time == "21:30"
    assert config.telegram.watchdog_time == "07:15"
    write_config(tmp_path, '[telegram]\nsurvey_time = "25:00"\n')
    with pytest.raises(ConfigError, match="HH:MM"):
        load_config(tmp_path)
    write_config(tmp_path, '[telegram]\nwatchdog_time = "7時"\n')
    with pytest.raises(ConfigError, match="HH:MM"):
        load_config(tmp_path)


def test_load_env_preserves_inner_and_unpaired_quotes(tmp_path: Path) -> None:
    # 剥がすのは「対になった囲みクォート」1組のみ。値に正当に含まれる引用符は壊さない
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join([
            "PAIRED=\"abc\"",
            "UNPAIRED='abc",
            "TRAILING=abc'",
            "INNER=a'b\"c",
            "NESTED='\"abc\"'",
        ]),
        encoding="utf-8",
    )
    env_path.chmod(0o600)
    env = load_env(tmp_path)
    assert env["PAIRED"] == "abc"
    assert env["UNPAIRED"] == "'abc"
    assert env["TRAILING"] == "abc'"
    assert env["INNER"] == "a'b\"c"
    assert env["NESTED"] == '"abc"'  # 外側の1組だけ剥がす


def test_x_oauth2_parse_and_defaults(tmp_path: Path) -> None:
    config = load_config(
        write_config(tmp_path, '[x_oauth2]\nclient_id = "cid"\ncallback_port = 9000\n')
    )
    assert config.x_oauth2.client_id == "cid"
    assert config.x_oauth2.callback_port == 9000
    # 既定: 未設定 = S1 未セットアップ(x-auth 側で fail fast)
    default = load_config(empty_config(tmp_path)).x_oauth2
    assert default.client_id is None
    assert default.callback_port == 8791


def test_x_oauth2_validation(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="callback_port"):
        load_config(write_config(tmp_path, "[x_oauth2]\ncallback_port = 0\n"))
    with pytest.raises(ConfigError, match="client_id"):
        load_config(write_config(tmp_path, '[x_oauth2]\nclient_id = " "\n'))


def test_obsidian_vault_path_rejects_blank(tmp_path: Path) -> None:
    # 空文字は Path("") = 実行ディレクトリに解決され、ノートが Vault に届かない。
    # 未設定はキーの省略で表す(GH#251)
    for blank in ("", "  "):
        with pytest.raises(ConfigError, match=r"\[obsidian\].vault_path"):
            load_config(write_config(tmp_path, f'[obsidian]\nvault_path = "{blank}"\n'))
    config = load_config(write_config(tmp_path, '[obsidian]\nvault_path = "~/vault"\n'))
    assert config.obsidian.vault_path == "~/vault"
    assert load_config(write_config(tmp_path, "[obsidian]\n")).obsidian.vault_path is None


@pytest.mark.parametrize(
    "folder", ["/tmp/SignalForge", "../outside", "SignalForge/../..", "a/../b", "", "  ", ".", "./"]
)
def test_obsidian_folder_rejects_path_outside_vault(tmp_path: Path, folder: str) -> None:
    # folder は vault の中の相対パスに限る。絶対パス・`..` は vault の外を指し、空は vault の
    # 直下に散らばる(GH#204)
    with pytest.raises(ConfigError, match=r"\[obsidian\].folder"):
        load_config(write_config(tmp_path, f'[obsidian]\nfolder = "{folder}"\n'))


@pytest.mark.parametrize(
    "folder", ["SignalForge", "Notes/SignalForge", "SignalForge/", "./SignalForge", "Signal Forge"]
)
def test_obsidian_folder_accepts_relative_path(tmp_path: Path, folder: str) -> None:
    # 既存の正しい設定(既定・入れ子の相対パス)は通す(GH#204)
    config = load_config(write_config(tmp_path, f'[obsidian]\nfolder = "{folder}"\n'))
    assert config.obsidian.folder == folder
    assert load_config(write_config(tmp_path, "[obsidian]\n")).obsidian.folder == "SignalForge"


def test_x_signals_cap_counts_toward_combined_budget(tmp_path: Path) -> None:
    # S1b 取込も合算予算の単一ノブの内側(ADR 0001)。既定 0 は挙動不変
    assert load_config(empty_config(tmp_path)).x_signals.daily_read_cap == 0
    config = load_config(write_config(tmp_path, "[x_signals]\ndaily_read_cap = 35\n"))
    assert config.x_signals.daily_read_cap == 35  # 130 + 35 = 165 ちょうどは OK
    with pytest.raises(ConfigError, match="combined_daily_read_cap"):
        load_config(write_config(tmp_path, "[x_signals]\ndaily_read_cap = 36\n"))


def test_x_signals_cap_below_page_min_rejected(tmp_path: Path) -> None:
    # 1〜4 は全 kind が黙って skip になる設定なので fail fast(0 = 無効は OK)
    with pytest.raises(ConfigError, match="daily_read_cap"):
        load_config(write_config(tmp_path, "[x_signals]\ndaily_read_cap = 4\n"))


def test_x_signals_weights_validation(tmp_path: Path) -> None:
    config = load_config(
        write_config(tmp_path, "[x_signals]\nweight_like = 2.0\nhalf_life_days = 7\n")
    )
    assert config.x_signals.weight_like == 2.0
    assert config.x_signals.half_life_days == 7
    with pytest.raises(ConfigError, match="half_life_days"):
        load_config(write_config(tmp_path, "[x_signals]\nhalf_life_days = 0\n"))
    with pytest.raises(ConfigError, match="weight_bookmark"):
        load_config(write_config(tmp_path, "[x_signals]\nweight_bookmark = -1.0\n"))


def test_x_signals_weights_reject_non_finite(tmp_path: Path) -> None:
    # nan/inf は TOML として合法だがスコア・順位を壊す
    with pytest.raises(ConfigError, match="half_life_days"):
        load_config(write_config(tmp_path, "[x_signals]\nhalf_life_days = nan\n"))
    with pytest.raises(ConfigError, match="weight_like"):
        load_config(write_config(tmp_path, "[x_signals]\nweight_like = inf\n"))


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
def test_float_settings_reject_non_finite(tmp_path: Path, bad: str) -> None:
    # nan は大小比較が常に偽、inf は「0 以上」「正」を満たすので、値域の検証より先に
    # 有限値だけを受ける。ランキングの閾値・係数と予算に限らず、浮動小数点の設定すべてに
    # 掛かる(GH#198)
    float_settings = [
        (section, f.name)
        for section, cls in _SECTIONS.items()
        for f in fields(cls)
        if f.type == "float"
    ]
    assert {("ranking", "min_score"), ("budget", "monthly_cap_usd")} <= set(float_settings)
    for section, key in float_settings:
        write_config(tmp_path, f"[{section}]\n{key} = {bad}\n")
        with pytest.raises(ConfigError, match=rf"\[{section}\]\.{key} は有限値"):
            load_config(tmp_path)


def test_dig_defaults_and_overrides(tmp_path: Path) -> None:
    assert load_config(write_config(tmp_path, "")).dig.daily_cap == 2  # 設計 06 §3 の既定
    config = load_config(
        write_config(tmp_path, "[dig]\ndaily_cap = 0\nreasoning_effort = \"high\"\n")
    )
    assert config.dig.daily_cap == 0
    assert config.dig.reasoning_effort == "high"
    assert config.dig.answer_reasoning_effort == "low"  # 返事は応答の速さ優先(GH#126)
    assert config.dig.answer_daily_cap == 20


def test_dig_answer_rejects_bad_values(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[dig\].answer_reasoning_effort"):
        load_config(write_config(tmp_path, "[dig]\nanswer_reasoning_effort = \"ultra\"\n"))
    with pytest.raises(ConfigError, match=r"\[dig\].answer_source_max_chars"):
        load_config(write_config(tmp_path, "[dig]\nanswer_source_max_chars = 0\n"))
    with pytest.raises(ConfigError, match=r"\[dig\].answer_daily_cap"):
        load_config(write_config(tmp_path, "[dig]\nanswer_daily_cap = -1\n"))


def test_dig_rejects_bad_values(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="reasoning_effort"):
        load_config(write_config(tmp_path, "[dig]\nreasoning_effort = \"ultra\"\n"))
    with pytest.raises(ConfigError, match=r"\[dig\].daily_cap"):
        load_config(write_config(tmp_path, "[dig]\ndaily_cap = -1\n"))
    with pytest.raises(ConfigError, match=r"\[dig\].timeout_s"):
        load_config(write_config(tmp_path, "[dig]\ntimeout_s = 0\n"))


def test_anthropic_api_defaults_and_overrides(tmp_path: Path) -> None:
    default = load_config(write_config(tmp_path, "")).anthropic_api
    assert (default.model, default.effort) == ("claude-opus-5-5", "medium")
    assert default.max_tokens == 16_000
    assert (default.input_usd_per_mtok, default.output_usd_per_mtok) == (4.0, 20.0)
    write_config(
        tmp_path,
        '[anthropic_api]\nmodel = "claude-sonnet-5-5"\neffort = "high"\nmax_tokens = 8000\n'
        "input_usd_per_mtok = 2\noutput_usd_per_mtok = 10.0\n",
    )
    config = load_config(tmp_path).anthropic_api
    assert (config.model, config.effort, config.max_tokens) == ("claude-sonnet-5-5", "high", 8000)
    assert (config.input_usd_per_mtok, config.output_usd_per_mtok) == (2, 10.0)


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ('effort = "minimal"', "effort"),  # codex の語彙は Claude API に無い
        ('model = " "', "model"),
        ("max_tokens = 0", "正の整数"),
        ("input_usd_per_mtok = -1", "input_usd_per_mtok"),
        ("output_usd_per_mtok = nan", "output_usd_per_mtok"),
    ],
)
def test_anthropic_api_rejects_bad_values(tmp_path: Path, body: str, match: str) -> None:
    write_config(tmp_path, f"[anthropic_api]\n{body}\n")
    with pytest.raises(ConfigError, match=match):
        load_config(tmp_path)


def test_dig_review_defaults_and_validation(tmp_path: Path) -> None:
    default = load_config(write_config(tmp_path, "")).dig
    assert (default.review, default.review_monthly_cap_usd) == (False, 10.0)
    write_config(tmp_path, "[dig]\nreview = true\nreview_monthly_cap_usd = 3\n")
    config = load_config(tmp_path).dig
    assert (config.review, config.review_monthly_cap_usd) == (True, 3)
    for bad in ("0", "-1", "inf"):
        write_config(tmp_path, f"[dig]\nreview_monthly_cap_usd = {bad}\n")
        with pytest.raises(ConfigError, match="review_monthly_cap_usd"):
            load_config(tmp_path)


def test_llm_fallback_defaults_and_overrides(tmp_path: Path) -> None:
    default = load_config(write_config(tmp_path, "")).llm_fallback
    assert (default.engine, default.model, default.max_tokens) == ("", "claude-sonnet-5-5", 16_000)
    assert (default.input_usd_per_mtok, default.output_usd_per_mtok) == (2.0, 10.0)
    assert default.monthly_cap_usd == 10.0
    write_config(
        tmp_path,
        '[llm_fallback]\nengine = "anthropic-api"\nmodel = "claude-haiku-5-5"\n'
        "input_usd_per_mtok = 0.1\noutput_usd_per_mtok = 0.5\nmonthly_cap_usd = 3\n",
    )
    config = load_config(tmp_path).llm_fallback
    assert (config.engine, config.model, config.monthly_cap_usd) == (
        "anthropic-api", "claude-haiku-5-5", 3
    )


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ('engine = "codex-cli"', "engine"),  # 主と同じエンジンは予備にならない
        ('model = ""', "model"),
        ("max_tokens = 0", "正の整数"),
        ("output_usd_per_mtok = -0.5", "output_usd_per_mtok"),
        ("monthly_cap_usd = 0", "monthly_cap_usd"),
        ("monthly_cap_usd = nan", "monthly_cap_usd"),
    ],
)
def test_llm_fallback_rejects_bad_values(tmp_path: Path, body: str, match: str) -> None:
    write_config(tmp_path, f"[llm_fallback]\n{body}\n")
    with pytest.raises(ConfigError, match=match):
        load_config(tmp_path)
