"""scripts/day0_x_probe.py の CSV のセルの無害化(GH#246)。

script は requests と requests_oauthlib を import するが、本体の依存には無い(Day 0 は
uv run --with で足して走らせる)。読み込む間だけ stub に差し替え、変換の関数だけを確かめる。
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "day0_x_probe.py"


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setitem(sys.modules, "requests", MagicMock())
    monkeypatch.setitem(sys.modules, "requests_oauthlib", MagicMock())
    spec = importlib.util.spec_from_file_location("day0_x_probe", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "value",
    [
        "=1+1",
        '=HYPERLINK("https://example.invalid/","x")',
        "+1",
        "-1",
        "@SUM(A1:A2)",
        "\t=1+1",
        "\r=1+1",
    ],
)
def test_formula_like_value_gets_quote_prefix(probe: ModuleType, value: str) -> None:
    assert probe.spreadsheet_safe(value) == "'" + value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "ふつうの本文 =1+1",
        "a@b",
        "1234567890123456789",
        "2026-10-24T00:00:00.000Z",
        "user_name",
    ],
)
def test_other_value_unchanged(probe: ModuleType, value: str) -> None:
    # 先頭以外の記号・id・時刻・ユーザー名は変えない
    assert probe.spreadsheet_safe(value) == value
