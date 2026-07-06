"""interest-profile.md の読込(設計 03 §4)。

自作ファイルなので trusted としてプロンプトに verbatim 注入する。パースは front matter
のみ(本文は構造解釈しない — 自由文チューニングを毎日コード変更なしで行うため)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_UPDATED = re.compile(r"^updated:\s*(\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)


class ProfileError(Exception):
    """プロファイルの欠落・front matter 不正。分析を進めない(関心注入が本体機能のため)。"""


@dataclass(frozen=True)
class InterestProfile:
    updated: str  # YYYY-MM-DD(doctor が 14 日超で警告する材料)
    body: str  # front matter を除いた本文(プロンプトへ verbatim 注入)


def load_interest_profile(path: Path) -> InterestProfile:
    if not path.exists():
        raise ProfileError(
            f"{path} がない。docs/templates/interest-profile.md を雛形にして作成すること"
        )
    text = path.read_text(encoding="utf-8")
    m = _FRONT_MATTER.match(text)
    if not m:
        raise ProfileError(f"{path}: front matter(--- updated: YYYY-MM-DD ---)がない")
    updated = _UPDATED.search(m.group(1))
    if not updated:
        raise ProfileError(f"{path}: front matter に updated: YYYY-MM-DD がない")
    # 本文は verbatim(設計 03 §4)。strip 等の加工もしない
    return InterestProfile(updated=updated.group(1), body=text[m.end() :])
