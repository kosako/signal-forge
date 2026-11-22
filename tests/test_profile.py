import os
from pathlib import Path

import pytest

from app.analysis.profile import ProfileError, append_profile_line, load_interest_profile

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_loads_template(tmp_path: Path) -> None:
    template = REPO_ROOT / "docs" / "templates" / "interest-profile.md"
    profile = load_interest_profile(template)
    assert profile.updated == "2026-07-06"
    assert "価値基準" in profile.body
    assert not profile.body.startswith("---")  # front matter は本文から除外


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="雛形"):
        load_interest_profile(tmp_path / "interest-profile.md")


def test_missing_front_matter_raises(tmp_path: Path) -> None:
    path = tmp_path / "interest-profile.md"
    path.write_text("# 関心プロファイル\n")
    with pytest.raises(ProfileError, match="front matter"):
        load_interest_profile(path)


def test_missing_updated_raises(tmp_path: Path) -> None:
    path = tmp_path / "interest-profile.md"
    path.write_text("---\nauthor: me\n---\n# 関心\n")
    with pytest.raises(ProfileError, match="updated"):
        load_interest_profile(path)


# --- 承認した 1 行を足す(GH#440 の 2 本目) ---

PROFILE = """---
updated: 2026-07-15
---
# 関心プロファイル

## 価値基準

### ↑ 上げる

- 今の課題に直結してすぐ使える
- 組織での AI 活用の実例

### ↓ 下げる

- 細かい Tips

## 実例
"""


def test_append_profile_line_adds_to_the_end_of_the_section(tmp_path: Path) -> None:
    path = tmp_path / "interest-profile.md"
    path.write_text(PROFILE, encoding="utf-8")
    path.chmod(0o640)

    assert append_profile_line(path, "up", "道具の実運用の事例", today="2026-11-22") == "added"
    assert append_profile_line(path, "down", "紹介するだけの記事", today="2026-11-22") == "added"

    text = path.read_text(encoding="utf-8")
    assert text == PROFILE.replace("updated: 2026-07-15", "updated: 2026-11-22").replace(
        "- 組織での AI 活用の実例\n", "- 組織での AI 活用の実例\n- 道具の実運用の事例\n"
    ).replace("- 細かい Tips\n", "- 細かい Tips\n- 紹介するだけの記事\n")
    assert load_interest_profile(path).updated == "2026-11-22"
    assert path.stat().st_mode & 0o777 == 0o640  # 権限は元のまま
    assert [p.name for p in tmp_path.iterdir()] == ["interest-profile.md"]  # 一時 file を残さない


def test_append_profile_line_keeps_an_existing_line(tmp_path: Path) -> None:
    path = tmp_path / "interest-profile.md"
    path.write_text(PROFILE, encoding="utf-8")

    assert append_profile_line(path, "down", "細かい Tips", today="2026-11-22") == "exists"
    assert path.read_text(encoding="utf-8") == PROFILE  # 日付も変えない


def test_append_profile_line_fills_an_empty_section(tmp_path: Path) -> None:
    path = tmp_path / "interest-profile.md"
    path.write_text("---\nupdated: 2026-07-15\n---\n### ↓ 下げる\n\n## 実例\n", encoding="utf-8")

    append_profile_line(path, "down", "紹介するだけの記事", today="2026-11-22")

    assert path.read_text(encoding="utf-8") == (
        "---\nupdated: 2026-11-22\n---\n### ↓ 下げる\n\n- 紹介するだけの記事\n\n## 実例\n"
    )


def test_append_profile_line_writes_through_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "dotfiles" / "interest-profile.md"
    real.parent.mkdir()
    real.write_text(PROFILE, encoding="utf-8")
    link = tmp_path / "interest-profile.md"
    link.symlink_to(real)

    append_profile_line(link, "up", "道具の実運用の事例", today="2026-11-22")

    assert link.is_symlink()
    assert "- 道具の実運用の事例" in real.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("text", "match"),
    [
        (None, "ない"),
        ("# 関心プロファイル\n### ↑ 上げる\n", "front matter"),
        ("---\nupdated: 2026-07-15\n---\n### ↓ 下げる\n", "↑ 上げる"),
    ],
)
def test_append_profile_line_refuses_without_the_section(
    tmp_path: Path, text: str | None, match: str
) -> None:
    path = tmp_path / "interest-profile.md"
    if text is not None:
        path.write_text(text, encoding="utf-8")

    with pytest.raises(ProfileError, match=match):
        append_profile_line(path, "up", "道具の実運用の事例", today="2026-11-22")

    if text is not None:
        assert path.read_text(encoding="utf-8") == text


def test_append_profile_line_leaves_the_file_when_the_write_fails(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "interest-profile.md"
    path.write_text(PROFILE, encoding="utf-8")

    def broken_replace(src, dst):
        raise OSError("書けない")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError):
        append_profile_line(path, "up", "道具の実運用の事例", today="2026-11-22")

    assert path.read_text(encoding="utf-8") == PROFILE
    assert [p.name for p in tmp_path.iterdir()] == ["interest-profile.md"]
