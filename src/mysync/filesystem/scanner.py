"""Build a read-only snapshot of a synchronization directory."""

from dataclasses import dataclass
from pathlib import Path
from stat import S_ISREG

from mysync.core.hashing import sha256_file
from mysync.filesystem.paths import path_parts


@dataclass(frozen=True)
class FileEntry:
    relative_path: str
    size: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True)
class ScanResult:
    files: tuple[FileEntry, ...]
    directories: tuple[str, ...]


class FileChangedError(OSError):
    """A file changed while its contents were being hashed."""


class CaseCollisionError(ValueError):
    """Two paths differ only in letter case."""


def scan_directory(root: Path) -> ScanResult:
    """Scan regular files and directories without following symbolic links.

    Relative paths use POSIX separators. Filesystem errors propagate, and no
    partial result is returned if the scan cannot be completed.
    """
    if root.is_symlink():
        raise ValueError(f"Root must not be a symbolic link: {root}")
    if not root.exists():
        raise FileNotFoundError(root)
    if not root.is_dir():
        raise NotADirectoryError(root)

    files: list[FileEntry] = []
    directories: list[str] = []
    seen: dict[str, str] = {}

    def register(relative_path: str) -> None:
        key = relative_path.casefold()
        previous = seen.get(key)
        if previous is not None:
            raise CaseCollisionError(
                f"Paths collide without case: {previous!r} and {relative_path!r}"
            )
        seen[key] = relative_path

    def visit(directory: Path) -> None:
        for path in directory.iterdir():
            if path.is_symlink():
                continue
            relative_path = path.relative_to(root).as_posix()
            path_parts(relative_path)
            if path.is_dir():
                register(relative_path)
                directories.append(relative_path)
                visit(path)
            else:
                before = path.stat()
                if not S_ISREG(before.st_mode):
                    raise OSError(f"Not a regular file: {path}")
                register(relative_path)
                digest = sha256_file(path)
                after = path.stat()
                fields = ("st_size", "st_mtime_ns", "st_ctime_ns", "st_dev", "st_ino")
                if any(getattr(before, field, None) != getattr(after, field, None) for field in fields):
                    raise FileChangedError(f"File changed during scan: {path}")
                files.append(FileEntry(relative_path, before.st_size, before.st_mtime_ns, digest))

    visit(root)
    return ScanResult(
        files=tuple(sorted(files, key=lambda entry: entry.relative_path)),
        directories=tuple(sorted(directories)),
    )
