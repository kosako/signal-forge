"""interest-profile.md の読込(設計 03 §4)。

自作ファイルなので trusted としてプロンプトに verbatim 注入する。パースは front matter
のみ(本文は構造解釈しない — 自由文チューニングを毎日コード変更なしで行うため)。
書き換えは、週の仮説の問いで人が承認した 1 行を節の末尾に足す append_profile_line だけ(GH#440)。
"""

from __future__ import annotations

import os
import re
import secrets
import stat
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


# 承認した 1 行を足す節(GH#440 の 2 本目)。雛形(docs/templates/interest-profile.md)の見出しのまま
PROFILE_SECTIONS = {"up": "### ↑ 上げる", "down": "### ↓ 下げる"}


def append_profile_line(path: Path, section: str, line: str, *, today: str) -> str:
    """人が承認した 1 行を、関心プロファイルの節の末尾に箇条書きで足す(GH#440 の 2 本目)。

    週の仮説の問いで ⭕ そう と答え、送った案の「✅ 足す」を押したときだけ使う(設計 03 §4)。
    本文のほかの行はそのまま残し、front matter の updated を today(JST の YYYY-MM-DD)にする。
    同じ行が節に既にあれば書かずに "exists"、足したら "added" を返す。file が無い・front matter が
    不正・節の見出しが無いときは ProfileError(読めない・書けないときは OSError)。書くときは同じ
    directory の一時 file に書いてから置き換える(途中で止まっても元の file を壊さない)。path が
    symlink なら、指す先の file を書き換える。
    """
    target = path.resolve()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise ProfileError(f"{path} がない") from e
    m = _FRONT_MATTER.match(text)
    if not m or not _UPDATED.search(m.group(1)):
        raise ProfileError(f"{path}: front matter(--- updated: YYYY-MM-DD ---)がない")
    heading = PROFILE_SECTIONS[section]
    lines = text[m.end() :].split("\n")
    start = next((i for i, value in enumerate(lines) if value.strip() == heading), None)
    if start is None:
        raise ProfileError(f"{path}: 見出し「{heading}」がない")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("#")),
        len(lines),
    )
    bullet = f"- {line}"
    if any(value.strip() == bullet for value in lines[start + 1 : end]):
        return "exists"
    at = end
    while at > start + 1 and not lines[at - 1].strip():
        at -= 1  # 節の最後の行の直後(節の後ろの空行の前)に足す
    lines[at:at] = ["", bullet] if at == start + 1 else [bullet]
    front = (
        text[: m.start(1)]
        + _UPDATED.sub(f"updated: {today}", m.group(1), count=1)
        + text[m.end(1) : m.end()]
    )
    _replace_text(target, front + "\n".join(lines))
    return "added"


def _replace_text(target: Path, text: str) -> None:
    tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IMODE(target.stat().st_mode))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
