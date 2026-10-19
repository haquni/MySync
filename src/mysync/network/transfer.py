"""Safe raw-byte transfer helpers used after framed metadata messages."""

from __future__ import annotations

import hashlib
from pathlib import Path
import socket
from stat import S_ISREG
from typing import Iterator

from mysync.filesystem.receiver import UnsafePathError, _path_parts
from mysync.filesystem.scanner import FileChangedError, FileEntry
from mysync.network.messages import ConnectionClosedError


FILE_CHUNK_SIZE = 1024 * 1024


def _source_file(root: Path, relative_path: str) -> Path:
    root = Path(root)
    if root.is_symlink():
        raise UnsafePathError(f"Root must not be a symbolic link: {root}")
    if not root.is_dir():
        raise NotADirectoryError(root)
    current = root
    for part in _path_parts(relative_path):
        current = current / part
        if current.is_symlink():
            raise UnsafePathError(f"Symbolic link in source path: {current}")
    stat = current.stat()
    if not S_ISREG(stat.st_mode):
        raise OSError(f"Not a regular file: {current}")
    return current


def _fingerprint(path: Path) -> tuple[int | None, ...]:
    stat = path.stat()
    return tuple(
        getattr(stat, field, None)
        for field in ("st_size", "st_mtime_ns", "st_ctime_ns", "st_dev", "st_ino")
    )


def send_file_bytes(tcp_socket: socket.socket, root: Path, entry: FileEntry) -> None:
    """Send exactly one scanned file and reject concurrent source changes."""
    path = _source_file(root, entry.relative_path)
    before = _fingerprint(path)
    if before[0] != entry.size or before[1] != entry.mtime_ns:
        raise FileChangedError(f"File changed since scan: {path}")

    digest = hashlib.sha256()
    sent = 0
    with path.open("rb") as stream:
        while sent < entry.size:
            chunk = stream.read(min(FILE_CHUNK_SIZE, entry.size - sent))
            if not chunk:
                raise FileChangedError(f"File became shorter during transfer: {path}")
            tcp_socket.sendall(chunk)
            digest.update(chunk)
            sent += len(chunk)
        if stream.read(1):
            raise FileChangedError(f"File became larger during transfer: {path}")

    if _fingerprint(path) != before or digest.hexdigest() != entry.sha256:
        raise FileChangedError(f"File changed during transfer: {path}")


def receive_file_chunks(
    tcp_socket: socket.socket,
    size: int,
    *,
    chunk_size: int = FILE_CHUNK_SIZE,
) -> Iterator[bytes]:
    """Yield exactly *size* raw bytes without consuming the following frame."""
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError("size must be a non-negative integer")
    remaining = size
    while remaining:
        chunk = tcp_socket.recv(min(chunk_size, remaining))
        if not chunk:
            raise ConnectionClosedError(
                f"connection closed with {remaining} file bytes remaining"
            )
        remaining -= len(chunk)
        yield chunk


__all__ = ("FILE_CHUNK_SIZE", "receive_file_chunks", "send_file_bytes")
