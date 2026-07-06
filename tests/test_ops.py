import plistlib
import re
from pathlib import Path

from app.doctor import LAUNCHD_JOBS

OPS_DIR = Path(__file__).resolve().parent.parent / "ops" / "launchd"

PLACEHOLDERS = {
    "__UV_BIN__": "/opt/homebrew/bin/uv",
    "__UV_DIR__": "/opt/homebrew/bin",
    "__REPO_DIR__": "/tmp/repo",
    "__HOME__": "/tmp/home",
}


def plist_text_without_comments(name: str) -> str:
    text = (OPS_DIR / f"{name}.plist").read_text(encoding="utf-8")
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def load_plist(name: str) -> dict:
    text = plist_text_without_comments(name)
    for placeholder, value in PLACEHOLDERS.items():
        text = text.replace(placeholder, value)
    # runbook の sed が置換する 4 種以外のプレースホルダが紛れていない(置換漏れ検知)
    assert not re.search(r"__[A-Z_]+__", text)
    return plistlib.loads(text.encode("utf-8"))


def test_labels_match_doctor_launchd_jobs() -> None:
    labels = {load_plist(job)["Label"] for job in LAUNCHD_JOBS}
    assert labels == set(LAUNCHD_JOBS)  # doctor #8 の検査対象と一致


def test_morning_plist_contract() -> None:
    plist = load_plist("com.intel.morning")
    assert plist["ProgramArguments"] == ["/opt/homebrew/bin/uv", "run", "intel", "run"]
    assert plist["StartCalendarInterval"] == {"Hour": 6, "Minute": 30}  # 仮 H5(設計 05 §1.1)
    assert "KeepAlive" not in plist  # 自動再実行しない(手動再走に一本化)
    # EnvironmentVariables は PATH/HOME のみ。ANTHROPIC_API_KEY を構成で遮断(設計 05 §1.1)
    assert set(plist["EnvironmentVariables"]) == {"PATH", "HOME"}


def test_poller_plist_contract() -> None:
    plist = load_plist("com.intel.poller")
    assert plist["ProgramArguments"] == ["/opt/homebrew/bin/uv", "run", "intel", "poll"]
    assert plist["KeepAlive"] is True
    assert plist["ThrottleInterval"] == 30
    assert set(plist["EnvironmentVariables"]) == {"PATH", "HOME"}


def test_no_secrets_in_plists() -> None:
    for job in LAUNCHD_JOBS:
        text = plist_text_without_comments(job)  # コメントは説明用語を含むため除外
        assert "ANTHROPIC" not in text
        assert "TOKEN" not in text
        assert "/Users/" not in text  # 実パスの焼き込みなし(public safety)
