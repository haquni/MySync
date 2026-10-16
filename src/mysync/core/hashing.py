"""Streaming file hashing without modifying file contents."""

import hashlib
from pathlib import Path


_CHUNK_SIZE = 1024 * 1024


def sha256_file(path: Path) -> str:
    """Return a file's lowercase SHA-256 digest, reading at most 1 MiB at once.

    Filesystem errors propagate to the caller. The file is closed even if
    reading fails. Concurrent file changes are not detected by this function.
    """
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()
