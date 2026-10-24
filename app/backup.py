"""日次バックアップ(設計 05 §5)。朝バッチ末尾(purge 後)に実行する。

- SQLite は backup API で一貫スナップショット(WAL 中のファイルコピーは破損リスク)
- 同梱: DB スナップショット / ~/.config/intel/(config.toml・.env・関心プロファイル)/
  prompts/(git 管理だが世代の一貫性のため)/ 設定ディレクトリの外の関心プロファイル
  ([analysis].profile_path で外に置いた場合だけ profile/ に。GH#199)
- tar.gz は 600 を強制(.env の secrets が同梱されるため。doctor #10 が検査)
- 日次14世代。同日の再実行は当日世代を置換(冪等)
- ~/Backups/intel/ をクラウド同期対象外に置く確認は運用(runbook)の責務
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tarfile
import tempfile
from pathlib import Path

from app.config import Config
from app.jst import today_jst

GENERATIONS = 14
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def backup_daily(
    conn: sqlite3.Connection,
    config: Config,
    logger: logging.Logger,
    *,
    run_id: str | None = None,
    prompts_dir: Path = PROMPTS_DIR,
) -> Path:
    """当日世代の tar.gz を作成し、パスを返す。古い世代は14世代を超えた分を削除。"""
    backup_dir = config.backup_dir  # 置き場の正本は config(既定 ~/Backups/intel。設計 05 §5)
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # 世代名は JST 日付(朝バッチ・digest_date と同じ暦日。UTC だと朝バッチ時点で1日ずれる)
    stamp = today_jst().replace("-", "")
    archive_path = backup_dir / f"intel-{stamp}.tar.gz"

    # アトミック置換のため、一時ファイルはアーカイブと同一ディレクトリに作る
    # (別ボリュームの tmp からの os.replace は EXDEV で失敗しうる)
    partial_path = backup_dir / f".{archive_path.name}.partial"
    with tempfile.TemporaryDirectory(prefix="intel-backup-") as tmp:
        snapshot = Path(tmp) / "intel.db"
        target = sqlite3.connect(snapshot)
        try:
            conn.backup(target)  # オンラインバックアップ API(一貫スナップショット)
        finally:
            target.close()

        # 0600 で作ってから書く(secrets 同梱のため、生成の瞬間から開かない)
        partial_path.unlink(missing_ok=True)  # 前回クラッシュの残骸は作り直す
        fd = os.open(partial_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh, tarfile.open(fileobj=fh, mode="w:gz") as tar:
                tar.add(snapshot, arcname="intel.db")
                _add_dir_if_exists(tar, config.config_dir, "config", logger, run_id=run_id)
                _add_profile_if_outside(tar, config, logger, run_id=run_id)
                _add_dir_if_exists(tar, prompts_dir, "prompts", logger, run_id=run_id)
            os.replace(partial_path, archive_path)  # 当日再実行は置換(冪等)
        except BaseException:
            partial_path.unlink(missing_ok=True)
            raise

    removed = _rotate(backup_dir)
    logger.info(
        "バックアップ作成",
        extra={
            "run_id": run_id,
            "stage": "backup",
            "extra": {"archive": archive_path.name, "rotated_out": removed},
        },
    )
    return archive_path


def _add_dir_if_exists(
    tar: tarfile.TarFile,
    directory: Path,
    arcname: str,
    logger: logging.Logger,
    *,
    run_id: str | None,
) -> None:
    # config_dir / prompts が無い環境(セットアップ前・テスト)は黙って skip せず
    # tar に含めない事実だけが残る(復旧時に気付ける)。存在すれば再帰追加
    if directory.is_symlink():
        # tarfile は既定で symlink をたどらず、リンクの 1 件だけが入って中身が残らない。
        # 実体を解決してから中身を入れる(arcname はそのまま。中の symlink はリンクのまま。GH#279)。
        # 循環・リンク切れで解決できなければ、その dir は入れずに WARN を残し、DB を含む世代は
        # 作る(resolve() は循環で Python 3.12 は RuntimeError、3.13 以降は OSError を出す)
        try:
            directory = directory.resolve(strict=True)
        except (OSError, RuntimeError) as e:
            logger.warning(
                f"symlink を解決できず {arcname}/ をバックアップに入れない: {type(e).__name__}",
                extra={"run_id": run_id, "stage": "backup"},
            )
            return
    if directory.is_dir():
        tar.add(directory, arcname=arcname)


def _add_profile_if_outside(
    tar: tarfile.TarFile,
    config: Config,
    logger: logging.Logger,
    *,
    run_id: str | None,
) -> None:
    # 関心プロファイルは分析・深掘り・返事が読む正本で、欠落すると止まる(設計 03 §4)。
    # [analysis].profile_path で設定ディレクトリの外に置くと config/ に入らないので、
    # 実際に読むファイルを profile/<読む名前> に別に入れる(GH#199)。中にあれば config/ に
    # 入っているので二重に入れない(設定ディレクトリ自体が symlink でも config/ には実体の
    # 中身が入るので、実体の側を指す profile_path も中として扱う。GH#279)。中の symlink が
    # 外を指す場合は config/ にリンクしか残らないので、解決した先で中か外かを決め、中身を入れる。
    # 無ければ入れない
    profile = config.profile_path
    if profile.is_symlink():
        # 循環・リンク切れで解決できなければ入れずに WARN を残し、DB を含む世代は作る
        # (_add_dir_if_exists と同じ扱い。resolve() は循環で Python 3.12 は RuntimeError を出す)
        try:
            profile = profile.resolve(strict=True)
        except (OSError, RuntimeError) as e:
            logger.warning(
                f"symlink を解決できず関心プロファイルを profile/ に入れない: {type(e).__name__}",
                extra={"run_id": run_id, "stage": "backup"},
            )
            return
    if not profile.is_file():
        return
    profile = profile.resolve()  # 親の dir の symlink も解決してから設定ディレクトリと比べる
    try:
        config_dir = config.config_dir.resolve(strict=True)
    except (OSError, RuntimeError):
        # 解決できない設定ディレクトリは config/ に入らない(GH#279)ので、外として扱う
        config_dir = None
    if config_dir is not None and profile.is_relative_to(config_dir):
        return
    tar.add(profile, arcname=f"profile/{config.profile_path.name}")


def _rotate(backup_dir: Path) -> list[str]:
    archives = sorted(backup_dir.glob("intel-*.tar.gz"))
    excess = archives[:-GENERATIONS] if len(archives) > GENERATIONS else []
    for path in excess:
        path.unlink()
    return [p.name for p in excess]
