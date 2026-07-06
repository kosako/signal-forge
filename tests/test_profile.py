from pathlib import Path

import pytest

from app.analysis.profile import ProfileError, load_interest_profile

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
