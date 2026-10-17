"""Create local backups before destructive file operations."""

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import tempfile

from mysync.filesystem.receiver import UnsafePathError, _path_parts
from mysync.filesystem.scanner import FileChangedError


NANOSECONDS_PER_DAY = 86_400_000_000_000


@dataclass(frozen=True)
class BackupRecord:
    original_relative_path: str
    backup_path: Path
    created_at_ns: int


def _fingerprint(path: Path) -> tuple[int | None, ...]:
    stat = path.stat()
    return tuple(
        getattr(stat, field, None)
        for field in ("st_size", "st_mtime_ns", "st_ctime_ns", "st_dev", "st_ino")
    )


def _safe_device_id(device_id: str) -> str:
    if not isinstance(device_id, str) or not device_id:
        raise ValueError("device_id must be a non-empty string")
    if any(not (character.isalnum() or character in "-_.") for character in device_id):
        raise ValueError("device_id contains unsafe characters")
    return device_id


def _existing_source(root: Path, relative_path: str) -> tuple[Path, tuple[str, ...]]:
    parts = _path_parts(relative_path)
    if parts[0].casefold() == "_mysync":
        raise UnsafePathError("Service files are not recursively backed up")
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise UnsafePathError(f"Symbolic link in source path: {current}")
    if not current.exists():
        raise FileNotFoundError(current)
    if not current.is_file():
        raise IsADirectoryError(current)
    return current, parts


def _backup_parent(root: Path, parts: tuple[str, ...]) -> Path:
    current = root
    for part in ("_mysync", "backups", *parts[:-1]):
        current = current / part
        if current.is_symlink():
            raise UnsafePathError(f"Symbolic link in backup path: {current}")
        if current.exists() and not current.is_dir():
            raise NotADirectoryError(current)
        current.mkdir(exist_ok=True)
    return current


def backup_existing_file(
    root: Path, relative_path: str, device_id: str, created_at_ns: int
) -> BackupRecord:
    """Copy one user file into `_mysync/backups` through a temporary file."""
    root = Path(root)
    if root.is_symlink():
        raise UnsafePathError(f"Root must not be a symbolic link: {root}")
    if not root.exists():
        raise FileNotFoundError(root)
    if not root.is_dir():
        raise NotADirectoryError(root)
    if isinstance(created_at_ns, bool) or not isinstance(created_at_ns, int) or created_at_ns < 0:
        raise ValueError("created_at_ns must be a non-negative integer")

    source, parts = _existing_source(root, relative_path)
    safe_device = _safe_device_id(device_id)
    parent = _backup_parent(root, parts)
    base_name = f"{parts[-1]}__{safe_device}__{created_at_ns}"
    destination = parent / f"{base_name}.bak"
    sequence = 1
    while destination.exists() or destination.is_symlink():
        destination = parent / f"{base_name}__{sequence}.bak"
        sequence += 1

    temporary_path: Path | None = None
    try:
        before = _fingerprint(source)
        with source.open("rb") as input_stream, tempfile.NamedTemporaryFile(
            mode="wb", dir=parent, prefix=".mysync-backup-", suffix=".tmp", delete=False
        ) as output_stream:
            temporary_path = Path(output_stream.name)
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        if _fingerprint(source) != before:
            raise FileChangedError(f"File changed during backup: {source}")
        os.utime(temporary_path, ns=(created_at_ns, created_at_ns))
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return BackupRecord(relative_path, destination, created_at_ns)


def delete_with_backup(
    root: Path, relative_path: str, device_id: str, created_at_ns: int
) -> BackupRecord:
    """Back up a regular user file and delete it only after backup succeeds."""
    source, _parts = _existing_source(Path(root), relative_path)
    before = _fingerprint(source)
    record = backup_existing_file(root, relative_path, device_id, created_at_ns)
    source, _parts = _existing_source(Path(root), relative_path)
    if _fingerprint(source) != before:
        raise FileChangedError(f"File changed before deletion: {source}")
    source.unlink()
    return record


def purge_expired_backups(
    root: Path, now_ns: int, retention_days: int = 7
) -> tuple[Path, ...]:
    """Remove regular `.bak` files older than the retention period."""
    if isinstance(now_ns, bool) or not isinstance(now_ns, int) or now_ns < 0:
        raise ValueError("now_ns must be a non-negative integer")
    if isinstance(retention_days, bool) or not isinstance(retention_days, int) or retention_days <= 0:
        raise ValueError("retention_days must be a positive integer")
    backup_root = Path(root) / "_mysync" / "backups"
    if backup_root.is_symlink():
        raise UnsafePathError(f"Backup root must not be a symbolic link: {backup_root}")
    if not backup_root.exists():
        return ()
    if not backup_root.is_dir():
        raise NotADirectoryError(backup_root)

    cutoff = now_ns - retention_days * NANOSECONDS_PER_DAY
    deleted: list[Path] = []
    for path in backup_root.rglob("*.bak"):
        if path.is_symlink() or not path.is_file():
            continue
        if path.stat().st_mtime_ns < cutoff:
            path.unlink()
            deleted.append(path)
    return tuple(sorted(deleted, key=lambda path: path.as_posix()))
