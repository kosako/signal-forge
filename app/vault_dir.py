"""vault の中のフォルダとノートを、設定した vault の実体から symlink を辿らずに開く(GH#204)。

ノートの出力(export・深掘りの公開)、返事の材料の読み取り、take の追記、doctor の書込テストが
同じ境界を使う。vault_path を resolve した実体を起点に、その下のフォルダを 1 段ずつ O_NOFOLLOW
で開き、開いたフォルダの fd を基準にノートを読み書きする(途中のパスを辿り直さない)。途中の
フォルダが外への symlink でも、外の置き場を基準にしない。ノート自体も symlink なら開かない。

vault_path そのものは本人が設定した起点なので、symlink(同期フォルダへのリンクなど)でもよい。
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from app.config import Config


class VaultPathError(OSError):
    """vault の中のフォルダ・ノートが symlink・フォルダ(通常のファイル)でない、または名前が不正。

    vault の外を指しうるので開かない。やり直しても直らない(本人が直す)。
    """


def check_name(name: str) -> None:
    """フォルダの中の 1 つの名前か(区切りを含まず、`.` `..` でない)。"""
    if name in ("", ".", "..") or os.sep in name:
        raise VaultPathError(f"vault の中の名前が不正: {name!r}")


@dataclass(frozen=True)
class VaultDir:
    """vault の中のフォルダ(vault からの名前の並び)。開くたびに vault の実体から辿り直す。"""

    vault_path: str
    parts: tuple[str, ...]

    def open(self, *, create: bool = False) -> int:
        """フォルダを fd で開く(閉じるのは呼び出し側)。

        create なら無いフォルダを作る(vault そのものも。従来の mkdir(parents=True) と同じ)。
        作らないときに vault や途中のフォルダが無ければ FileNotFoundError(一時的に見えない
        だけかもしれないので、扱いは呼び出し側が決める)。途中が symlink・フォルダでない・
        名前が不正なら VaultPathError。
        """
        vault = Path(self.vault_path).expanduser()
        if create:
            vault.mkdir(parents=True, exist_ok=True)
        fd = os.open(vault.resolve(strict=True), os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in self.parts:
                check_name(part)
                child = _open_child_dir(fd, part, create=create)
                os.close(fd)
                fd = child
        except BaseException:
            os.close(fd)
            raise
        return fd

    @contextlib.contextmanager
    def opened(self, *, create: bool = False) -> Iterator[int]:
        fd = self.open(create=create)
        try:
            yield fd
        finally:
            os.close(fd)

    def ensure(self) -> None:
        """無ければ作る(途中が symlink・フォルダでなければ VaultPathError)。"""
        os.close(self.open(create=True))


def folder_dir(config: Config, *sub: str) -> VaultDir:
    """`{vault}/{folder}/{sub...}`。folder の検証は設定の読み込み(GH#204)で、ここでも名前を見る。"""
    if config.obsidian.vault_path is None:
        raise ValueError("[obsidian].vault_path が未設定")
    return VaultDir(config.obsidian.vault_path, (*Path(config.obsidian.folder).parts, *sub))


def _open_child_dir(dir_fd: int, name: str, *, create: bool) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        try:
            return os.open(name, flags, dir_fd=dir_fd)
        except FileNotFoundError:
            if not create:
                raise
        with contextlib.suppress(FileExistsError):  # 同時に作られた(symlink かは次の open で見る)
            os.mkdir(name, dir_fd=dir_fd)
        return os.open(name, flags, dir_fd=dir_fd)
    except OSError as e:
        # symlink を O_DIRECTORY | O_NOFOLLOW で開くと ELOOP か ENOTDIR(macOS は ENOTDIR)
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise VaultPathError(f"vault の中の {name} が symlink かフォルダでない") from e
        raise


def open_regular(dir_fd: int, name: str, flags: int = os.O_RDONLY) -> int:
    """dir_fd の中の通常のファイルを symlink を辿らずに開く(無ければ FileNotFoundError)。"""
    check_name(name)
    try:
        fd = os.open(name, flags | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise VaultPathError(f"vault の中の {name} が symlink") from e
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise VaultPathError(f"vault の中の {name} が通常のファイルでない")
    except BaseException:
        os.close(fd)
        raise
    return fd


def entry_exists(dir_fd: int, name: str) -> bool:
    """dir_fd の中に name があるか(symlink は辿らず、リンク自体があれば True)。"""
    check_name(name)
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def create_temp(dir_fd: int, name: str) -> tuple[int, str]:
    """name の隣に一時ファイル `.{name}.{乱数}.tmp` を作る(tempfile.mkstemp と同じく排他・0600)。"""
    check_name(name)
    for _ in range(100):
        tmp_name = f".{name}.{secrets.token_hex(4)}.tmp"
        try:
            fd = os.open(
                tmp_name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=dir_fd,
            )
        except FileExistsError:
            continue
        return fd, tmp_name
    raise FileExistsError(errno.EEXIST, f"一時ファイルの空いている名前が無い: {name}")
