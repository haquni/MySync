"""Compare two directory snapshots without applying file operations."""

from dataclasses import dataclass

from mysync.filesystem.scanner import CaseCollisionError, FileEntry, ScanResult


@dataclass(frozen=True)
class FileChange:
    before: FileEntry
    after: FileEntry


@dataclass(frozen=True)
class SnapshotDiff:
    added: tuple[FileEntry, ...]
    missing: tuple[FileEntry, ...]
    changed: tuple[FileChange, ...]
    unchanged: tuple[FileEntry, ...]


def _index_files(snapshot: ScanResult) -> dict[str, FileEntry]:
    indexed: dict[str, FileEntry] = {}
    for entry in snapshot.files:
        key = entry.relative_path.casefold()
        if key in indexed:
            raise CaseCollisionError(
                f"Paths collide without case: {indexed[key].relative_path!r} "
                f"and {entry.relative_path!r}"
            )
        indexed[key] = entry
    return indexed


def compare_snapshots(before: ScanResult, after: ScanResult) -> SnapshotDiff:
    """Classify file differences between two snapshots.

    ``missing`` is an observation, not a deletion instruction. Matching
    content with a different mtime is unchanged; case-only path changes are
    reported as changed so that callers can handle the rename explicitly.
    """
    old_files = _index_files(before)
    new_files = _index_files(after)
    added: list[FileEntry] = []
    missing: list[FileEntry] = []
    changed: list[FileChange] = []
    unchanged: list[FileEntry] = []

    for key in sorted(old_files.keys() | new_files.keys()):
        old = old_files.get(key)
        new = new_files.get(key)
        if old is None:
            assert new is not None
            added.append(new)
        elif new is None:
            missing.append(old)
        elif old.sha256 != new.sha256 or old.relative_path != new.relative_path:
            changed.append(FileChange(old, new))
        else:
            unchanged.append(new)

    return SnapshotDiff(tuple(added), tuple(missing), tuple(changed), tuple(unchanged))
