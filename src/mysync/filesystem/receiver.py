"""Safely install a new file received as binary chunks."""

import hashlib
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import Iterable


class IntegrityError(OSError):
    """Received bytes do not match the expected SHA-256 digest."""


class UnsafePathError(ValueError):
    """A relative path could escape the synchronization root."""


def _path_parts(relative_path: str) -> tuple[str, ...]:
    if not isinstance(relative_path, str):
        raise TypeError("relative_path must be a string")
    if not relative_path or "\\" in relative_path:
        raise UnsafePathError(f"Unsafe relative path: {relative_path!r}")
    raw_parts = relative_path.split("/")
    path = PurePosixPath(relative_path)
    if path.is_absolute() or any(part in ("", ".", "..") for part in raw_parts):
        raise UnsafePathError(f"Unsafe relative path: {relative_path!r}")
    return tuple(raw_parts)


def _validate_digest(expected_sha256: str) -> str:
    if not isinstance(expected_sha256, str):
        raise TypeError("expected_sha256 must be a string")
    normalized = expected_sha256.lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError("expected_sha256 must contain 64 hexadecimal characters")
    return normalized


def receive_new_file(
    root: Path,
    relative_path: str,
    chunks: Iterable[bytes],
    expected_sha256: str,
    *,
    mtime_ns: int | None = None,
) -> Path:
    """Write and verify a new file below *root*, then install it atomically.

    Existing targets are never replaced. Parent directories may be created.
    """
    root = Path(root)
    if root.is_symlink():
        raise UnsafePathError(f"Root must not be a symbolic link: {root}")
    if not root.exists():
        raise FileNotFoundError(root)
    if not root.is_dir():
        raise NotADirectoryError(root)

    parts = _path_parts(relative_path)
    expected = _validate_digest(expected_sha256)
    parent = root
    for part in parts[:-1]:
        candidate = parent / part
        if candidate.is_symlink():
            raise UnsafePathError(f"Symbolic link in destination path: {candidate}")
        if candidate.exists():
            if not candidate.is_dir():
                raise NotADirectoryError(candidate)
        else:
            candidate.mkdir()
        if candidate.is_symlink():
            raise UnsafePathError(f"Symbolic link in destination path: {candidate}")
        parent = candidate

    target = parent / parts[-1]
    if target.is_symlink():
        raise UnsafePathError(f"Destination is a symbolic link: {target}")
    if target.exists():
        raise FileExistsError(target)

    temporary_path: Path | None = None
    installed = False
    try:
        digest = hashlib.sha256()
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=parent, prefix=".mysync-", suffix=".tmp", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            for chunk in chunks:
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise TypeError("file chunks must be bytes-like")
                temporary.write(chunk)
                digest.update(chunk)
            temporary.flush()
            os.fsync(temporary.fileno())

        actual = digest.hexdigest()
        if actual != expected:
            raise IntegrityError(f"SHA-256 mismatch: expected {expected}, got {actual}")
        if target.is_symlink():
            raise UnsafePathError(f"Destination became a symbolic link: {target}")
        if target.exists():
            raise FileExistsError(target)
        os.replace(temporary_path, target)
        temporary_path = None
        installed = True
        if mtime_ns is not None:
            os.utime(target, ns=(mtime_ns, mtime_ns), follow_symlinks=False)
        return target
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        if installed:
            target.unlink(missing_ok=True)
        raise
