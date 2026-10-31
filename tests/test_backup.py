import errno
import logging
import os
import shutil
import sqlite3
import tarfile
from pathlib import Path

import pytest

from app.analysis.profile import load_interest_profile
from app.backup import GENERATIONS, backup_daily
from app.config import AnalysisConfig, Config, StorageConfig, load_config
from app.db import apply_migrations, connect

logger = logging.getLogger("test-backup")


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    yield conn
    conn.close()


def make_config(tmp_path: Path) -> Config:
    config_dir = tmp_path / "conf"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.toml").write_text("# config\n", encoding="utf-8")
    (config_dir / ".env").write_text("TELEGRAM_BOT_TOKEN=tok\n", encoding="utf-8")
    return Config(
        config_dir=config_dir,
        storage=StorageConfig(backup_dir=str(tmp_path / "backups")),
    )


def make_prompts(tmp_path: Path) -> Path:
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    (prompts / "analyze-v001.md").write_text("prompt", encoding="utf-8")
    return prompts


def test_backup_creates_600_archive_with_expected_members(conn, tmp_path) -> None:
    config = make_config(tmp_path)
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed')")

    archive = backup_daily(conn, config, logger, prompts_dir=make_prompts(tmp_path))

    assert archive.exists()
    assert archive.stat().st_mode & 0o777 == 0o600  # secrets 同梱のため 600 強制
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert "intel.db" in names
    assert "config/config.toml" in names
    assert "config/.env" in names
    assert "prompts/analyze-v001.md" in names


def test_backup_snapshot_is_consistent_db(conn, tmp_path) -> None:
    config = make_config(tmp_path)
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed')")

    archive = backup_daily(conn, config, logger, prompts_dir=make_prompts(tmp_path))

    extract_dir = tmp_path / "restore"
    with tarfile.open(archive) as tar:
        tar.extract("intel.db", path=extract_dir, filter="data")
    restored = sqlite3.connect(extract_dir / "intel.db")
    names = {row[0] for row in restored.execute("SELECT name FROM sources")}
    restored.close()
    assert "feed" in names  # backup API で一貫スナップショットが取れている


def test_same_day_rerun_replaces_archive(conn, tmp_path) -> None:
    config = make_config(tmp_path)
    prompts = make_prompts(tmp_path)
    first = backup_daily(conn, config, logger, prompts_dir=prompts)
    second = backup_daily(conn, config, logger, prompts_dir=prompts)
    assert first == second
    archives = list(config.backup_dir.glob("intel-*.tar.gz"))
    assert len(archives) == 1  # 1日1世代(冪等)
    assert not list(config.backup_dir.glob(".*partial*"))  # 一時ファイルが残らない


def test_rotation_keeps_14_generations(conn, tmp_path, monkeypatch) -> None:
    # 当日の世代名は実時計の暦日で決まる。旧世代の直後の日に固定し、年が変わっても
    # 結果が変わらないようにする(GH#210)
    monkeypatch.setattr("app.backup.today_jst", lambda: "2026-06-17")
    config = make_config(tmp_path)
    config.backup_dir.mkdir(parents=True)
    for day in range(1, GENERATIONS + 3):  # 16 世代分の旧ファイル(06-01〜06-16)
        (config.backup_dir / f"intel-202606{day:02d}.tar.gz").write_bytes(b"")

    backup_daily(conn, config, logger, prompts_dir=make_prompts(tmp_path))

    archives = sorted(p.name for p in config.backup_dir.glob("intel-*.tar.gz"))
    # 最古の世代から削除され、当日分(06-17)を含む新しい 14 世代が残る
    assert archives == [f"intel-202606{day:02d}.tar.gz" for day in range(4, 18)]


def test_rotation_ignores_non_daily_archives(conn, tmp_path, monkeypatch) -> None:
    # 置き場に手で置いた intel-*.tar.gz(退避用など)は世代に数えず、消さない。日次の世代は
    # 14 のまま残る。名前順で日次の世代より前に並ぶ名前と後に並ぶ名前の両方を置く(GH#339)
    monkeypatch.setattr("app.backup.today_jst", lambda: "2026-06-17")
    config = make_config(tmp_path)
    config.backup_dir.mkdir(parents=True)
    for day in range(1, GENERATIONS + 3):  # 16 世代分の旧ファイル(06-01〜06-16)
        (config.backup_dir / f"intel-202606{day:02d}.tar.gz").write_bytes(b"")
    kept = ["intel-0-pre-restore.tar.gz", "intel-manual.tar.gz"]
    for name in kept:
        (config.backup_dir / name).write_bytes(b"")

    backup_daily(conn, config, logger, prompts_dir=make_prompts(tmp_path))

    archives = sorted(p.name for p in config.backup_dir.glob("intel-*.tar.gz"))
    assert archives == sorted(
        kept + [f"intel-202606{day:02d}.tar.gz" for day in range(4, 18)]
    )


def test_backup_follows_symlinked_config_dir(conn, tmp_path) -> None:
    # 設定ディレクトリが symlink(dotfiles の管理など)でも、リンクの 1 件ではなく中身が
    # config/ に入る。中にある symlink は今までどおりリンクのまま入る(GH#279)
    real = make_config(tmp_path).config_dir
    (real / "interest-profile.md").write_text("# profile\n", encoding="utf-8")
    (real / "profile-link.md").symlink_to(real / "interest-profile.md")
    link = tmp_path / "conf-link"
    link.symlink_to(real, target_is_directory=True)
    config = Config(
        config_dir=link,
        storage=StorageConfig(backup_dir=str(tmp_path / "backups")),
    )

    archive = backup_daily(conn, config, logger, prompts_dir=make_prompts(tmp_path))

    with tarfile.open(archive) as tar:
        names = tar.getnames()
        assert tar.getmember("config").isdir()  # 名前は config のまま、リンクではなく実体
        assert tar.getmember("config/profile-link.md").issym()
    assert "config/config.toml" in names
    assert "config/.env" in names
    assert "config/interest-profile.md" in names
    assert "prompts/analyze-v001.md" in names


def test_backup_skips_circular_symlink_config_dir_with_warning(conn, tmp_path, caplog) -> None:
    # 循環する symlink は resolve() が例外を出す(Python 3.12 は RuntimeError)。その dir は
    # 入れずに WARN を残し、DB を含む世代は作る(GH#279)
    loop_a = tmp_path / "loop-a"
    loop_b = tmp_path / "loop-b"
    loop_a.symlink_to(loop_b, target_is_directory=True)
    loop_b.symlink_to(loop_a, target_is_directory=True)
    config = Config(
        config_dir=loop_a,
        storage=StorageConfig(backup_dir=str(tmp_path / "backups")),
    )

    with caplog.at_level(logging.WARNING, logger=logger.name):
        archive = backup_daily(conn, config, logger, prompts_dir=make_prompts(tmp_path))

    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert "intel.db" in names
    assert "prompts/analyze-v001.md" in names
    assert not [n for n in names if n == "config" or n.startswith("config/")]
    assert any(
        r.levelno == logging.WARNING and "config/" in r.getMessage() for r in caplog.records
    )


# --- 設定ディレクトリの外の関心プロファイル(GH#199) ---

PROFILE_TEXT = "---\nupdated: 2026-10-01\n---\n# 関心プロファイル\n本文\n"


def test_outside_profile_is_backed_up_and_restorable(conn, tmp_path) -> None:
    # [analysis].profile_path で設定ディレクトリの外に置いたプロファイルも世代に入り、
    # 元のファイルを失っても、復元した config.toml が指す場所へ置けば分析が読める(設計 05 §5)
    profile = tmp_path / "vault" / "my-profile.md"
    profile.parent.mkdir()
    profile.write_text(PROFILE_TEXT, encoding="utf-8")
    config_dir = tmp_path / "conf"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        f'[analysis]\nprofile_path = "{profile}"\n\n'
        f'[storage]\nbackup_dir = "{tmp_path / "backups"}"\n',
        encoding="utf-8",
    )
    config = load_config(config_dir)

    archive = backup_daily(conn, config, logger, prompts_dir=make_prompts(tmp_path))
    profile.unlink()  # 元のプロファイルを失った

    restore_dir = tmp_path / "restore"
    with tarfile.open(archive) as tar:
        names = tar.getnames()
        tar.extractall(path=restore_dir, filter="data")
    assert "profile/my-profile.md" in names
    assert "config/config.toml" in names
    restored = load_config(restore_dir / "config")
    shutil.copy2(restore_dir / "profile" / restored.profile_path.name, restored.profile_path)
    assert load_interest_profile(restored.profile_path).updated == "2026-10-01"
    assert restored.profile_path.read_text(encoding="utf-8") == PROFILE_TEXT


def test_profile_inside_config_dir_is_not_added_twice(conn, tmp_path) -> None:
    config = make_config(tmp_path)  # profile_path 未設定 = <config_dir>/interest-profile.md
    (config.config_dir / "interest-profile.md").write_text(PROFILE_TEXT, encoding="utf-8")

    archive = backup_daily(conn, config, logger, prompts_dir=make_prompts(tmp_path))

    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert "config/interest-profile.md" in names
    assert not [n for n in names if n.split("/")[0] == "profile"]  # config/ と二重に入れない


def test_symlinked_profile_in_config_dir_backs_up_its_content(conn, tmp_path) -> None:
    # 設定ディレクトリの中の symlink が外を指すと config/ にはリンクしか残らないので、中身を入れる。
    # 復旧では、読む場所(未設定なら復旧で使う設定ディレクトリの interest-profile.md)の
    # 切れたリンクを profile/ のファイルで置き換えれば読める(設計 05 §5)
    target = tmp_path / "vault" / "real-profile.md"
    target.parent.mkdir()
    target.write_text(PROFILE_TEXT, encoding="utf-8")
    config = make_config(tmp_path)
    (config.config_dir / "interest-profile.md").symlink_to(target)

    archive = backup_daily(conn, config, logger, prompts_dir=make_prompts(tmp_path))
    target.unlink()  # リンクの先を失った

    restore_dir = tmp_path / "restore"
    with tarfile.open(archive) as tar:
        member = tar.getmember("profile/interest-profile.md")  # 読む名前で入る
        assert member.isfile()
        tar.extractall(path=restore_dir, filter="tar")  # config/ の絶対 path のリンクも戻す
    restored = load_config(restore_dir / "config")  # 元と別の設定ディレクトリ(--config-dir)
    assert restored.profile_path.is_symlink()
    restored.profile_path.unlink()
    shutil.copy2(restore_dir / "profile" / "interest-profile.md", restored.profile_path)
    assert load_interest_profile(restored.profile_path).updated == "2026-10-01"


def test_profile_on_real_side_of_symlinked_config_dir_goes_into_config(conn, tmp_path) -> None:
    # 設定ディレクトリ自体が symlink で、profile_path がリンクの先(実体の側)を指していても、
    # 中身は config/ に入る(GH#279 で config/ に実体の中身が入る)。中として扱い、
    # profile/ に二重に入れない(GH#199)
    real = tmp_path / "conf-real"
    real.mkdir()
    (real / "interest-profile.md").write_text(PROFILE_TEXT, encoding="utf-8")
    (real / "config.toml").write_text(
        f'[analysis]\nprofile_path = "{real / "interest-profile.md"}"\n\n'
        f'[storage]\nbackup_dir = "{tmp_path / "backups"}"\n',
        encoding="utf-8",
    )
    link = tmp_path / "conf-link"
    link.symlink_to(real, target_is_directory=True)
    config = load_config(link)

    archive = backup_daily(conn, config, logger, prompts_dir=make_prompts(tmp_path))

    with tarfile.open(archive) as tar:
        names = tar.getnames()
        member = tar.getmember("config/interest-profile.md")
        assert member.isfile()
        assert tar.extractfile(member).read().decode("utf-8") == PROFILE_TEXT
    assert not [n for n in names if n.split("/")[0] == "profile"]


@pytest.mark.parametrize("kind", ["loop", "dangling"])
def test_unresolvable_profile_symlink_is_skipped_with_warning(
    conn, tmp_path, caplog, kind
) -> None:
    # 循環・リンク切れの symlink のプロファイルは入れずに WARN を残し、DB を含む世代は作る
    # (設定ディレクトリの symlink と同じ扱い。GH#279)。Python 3.12 の resolve() は循環で
    # RuntimeError を出す
    config = make_config(tmp_path)
    link = config.config_dir / "interest-profile.md"
    link.symlink_to(link if kind == "loop" else tmp_path / "missing.md")

    with caplog.at_level(logging.WARNING, logger=logger.name):
        archive = backup_daily(conn, config, logger, prompts_dir=make_prompts(tmp_path))

    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert "intel.db" in names
    assert not [n for n in names if n.split("/")[0] == "profile"]
    assert any(
        r.levelno == logging.WARNING and "profile/" in r.getMessage() for r in caplog.records
    )


def test_outside_profile_is_backed_up_when_config_dir_cannot_be_resolved(conn, tmp_path) -> None:
    # 設定ディレクトリが循環する symlink だと config/ は入らない(WARN。GH#279)。外の
    # プロファイルを比べる先が解決できなくても止めずに profile/ に入れ、DB を含む世代は作る
    loop_a = tmp_path / "loop-a"
    loop_b = tmp_path / "loop-b"
    loop_a.symlink_to(loop_b, target_is_directory=True)
    loop_b.symlink_to(loop_a, target_is_directory=True)
    profile = tmp_path / "vault" / "my-profile.md"
    profile.parent.mkdir()
    profile.write_text(PROFILE_TEXT, encoding="utf-8")
    config = Config(
        config_dir=loop_a,
        analysis=AnalysisConfig(profile_path=str(profile)),
        storage=StorageConfig(backup_dir=str(tmp_path / "backups")),
    )

    archive = backup_daily(conn, config, logger, prompts_dir=make_prompts(tmp_path))

    with tarfile.open(archive) as tar:
        names = tar.getnames()
        assert tar.extractfile("profile/my-profile.md").read().decode("utf-8") == PROFILE_TEXT
    assert "intel.db" in names
    assert not [n for n in names if n.split("/")[0] == "config"]


# --- 同日の再実行での当日世代の置き換え(GH#254) ---

TODAY = "2026-06-17"  # 当日の世代名は暦日で決まるので固定する(GH#210)


def _restore(archive: Path, dest: Path) -> tuple[set[str], str]:
    """アーカイブを展開し、戻した DB の rss ソースの名前と config.toml の中身を返す。"""
    with tarfile.open(archive) as tar:
        tar.extractall(path=dest, filter="data")
    restored = sqlite3.connect(dest / "intel.db")
    try:
        # manual ソースは migration が入れる 1 行なので、テストで足した rss だけを見る
        names = {row[0] for row in restored.execute("SELECT name FROM sources WHERE type = 'rss'")}
    finally:
        restored.close()
    return names, (dest / "config" / "config.toml").read_text(encoding="utf-8")


def test_same_day_rerun_archive_restores_updated_db_and_config(conn, tmp_path, monkeypatch) -> None:
    # 収集や設定の修正のあとに同じ日に再実行したら、当日世代は新しい内容で置き換わり、
    # 2 回目のアーカイブから更新した DB と設定を戻せる(古い世代を残したまま返す回帰を検出する)
    monkeypatch.setattr("app.backup.today_jst", lambda: TODAY)
    config = make_config(tmp_path)
    prompts = make_prompts(tmp_path)
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'before')")
    first = backup_daily(conn, config, logger, prompts_dir=prompts)

    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'after')")
    (config.config_dir / "config.toml").write_text("# config v2\n", encoding="utf-8")
    second = backup_daily(conn, config, logger, prompts_dir=prompts)

    assert first == second == config.backup_dir / "intel-20260617.tar.gz"
    assert sorted(p.name for p in config.backup_dir.iterdir()) == [second.name]
    names, config_text = _restore(second, tmp_path / "restore")
    assert names == {"before", "after"}
    assert config_text == "# config v2\n"


@pytest.mark.parametrize("stage", ["create", "replace"])
def test_same_day_rerun_failure_keeps_previous_archive(conn, tmp_path, monkeypatch, stage) -> None:
    # 同じ日の再実行でアーカイブの作成(容量不足など)や置き換えに失敗しても、当日の旧世代は
    # 戻せるまま残り、一時ファイルは片付き、世代の削除には進まない(先に消してから作る形への
    # 回帰を検出する)
    monkeypatch.setattr("app.backup.today_jst", lambda: TODAY)
    config = make_config(tmp_path)
    prompts = make_prompts(tmp_path)
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'before')")
    archive = backup_daily(conn, config, logger, prompts_dir=prompts)
    previous = archive.read_bytes()
    # 世代の削除まで進めば最古の 06-03 が消える数(当日と合わせて GENERATIONS + 1)にしておく
    for day in range(17 - GENERATIONS, 17):
        (config.backup_dir / f"intel-202606{day:02d}.tar.gz").write_bytes(b"")
    before = sorted(p.name for p in config.backup_dir.iterdir())
    assert len(before) == GENERATIONS + 1

    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'after')")
    (config.config_dir / "config.toml").write_text("# config v2\n", encoding="utf-8")
    injected = OSError(errno.ENOSPC if stage == "create" else errno.EIO, "injected")
    seen: set[str] = set()  # 失敗の時点で置き場にあったファイル

    def fail(*args, **kwargs):
        seen.update(p.name for p in config.backup_dir.iterdir())
        raise injected

    with monkeypatch.context() as m:
        if stage == "create":
            m.setattr(tarfile.TarFile, "add", fail)  # 書いている途中で失敗する
        else:
            m.setattr(os, "replace", fail)  # 書き終えて置き換える時に失敗する
        with pytest.raises(OSError) as excinfo:
            backup_daily(conn, config, logger, prompts_dir=prompts)
    assert excinfo.value is injected

    assert seen - set(before)  # 一時ファイルはあった(片付けの確認が空振りしない)
    # 一時ファイルは残らず、世代も消えない
    assert sorted(p.name for p in config.backup_dir.iterdir()) == before
    assert archive.read_bytes() == previous  # 当日の旧世代はそのまま
    names, config_text = _restore(archive, tmp_path / "restore")
    assert names == {"before"}
    assert config_text == "# config\n"
