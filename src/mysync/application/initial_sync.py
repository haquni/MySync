"""Prepare a safe, read-only plan for the first two-device sync."""

from dataclasses import dataclass

from mysync.filesystem.comparison import FileChange, compare_snapshots
from mysync.filesystem.scanner import CaseCollisionError, FileEntry, ScanResult


@dataclass(frozen=True)
class InitialSyncPlan:
    copy_to_left: tuple[FileEntry, ...]
    copy_to_right: tuple[FileEntry, ...]
    create_on_left: tuple[str, ...]
    create_on_right: tuple[str, ...]
    file_conflicts: tuple[FileChange, ...]
    structure_conflicts: tuple[str, ...]


def _directory_index(snapshot: ScanResult) -> dict[str, str]:
    index: dict[str, str] = {}
    file_keys = {entry.relative_path.casefold() for entry in snapshot.files}
    for directory in snapshot.directories:
        key = directory.casefold()
        if key in index or key in file_keys:
            raise CaseCollisionError(f"Duplicate path without case: {directory!r}")
        index[key] = directory
    return index


def plan_initial_sync(left: ScanResult, right: ScanResult) -> InitialSyncPlan:
    """Plan the initial union; never interpret an absent file as deletion."""
    diff = compare_snapshots(left, right)
    left_dirs = _directory_index(left)
    right_dirs = _directory_index(right)
    left_files = {entry.relative_path.casefold(): entry for entry in left.files}
    right_files = {entry.relative_path.casefold(): entry for entry in right.files}

    conflict_keys = (left_files.keys() & right_dirs.keys()) | (
        right_files.keys() & left_dirs.keys()
    )

    def safe(path: str) -> bool:
        key = path.casefold()
        return all(key != conflict and not key.startswith(conflict + "/")
                   for conflict in conflict_keys)

    return InitialSyncPlan(
        copy_to_left=tuple(entry for entry in diff.added
                           if safe(entry.relative_path)),
        copy_to_right=tuple(entry for entry in diff.missing
                            if safe(entry.relative_path)),
        create_on_left=tuple(right_dirs[key] for key in sorted(right_dirs.keys() - left_dirs.keys())
                             if safe(right_dirs[key])),
        create_on_right=tuple(left_dirs[key] for key in sorted(left_dirs.keys() - right_dirs.keys())
                              if safe(left_dirs[key])),
        file_conflicts=tuple(change for change in diff.changed
                             if safe(change.before.relative_path)),
        structure_conflicts=tuple(
            (left_files.get(key) or right_files[key]).relative_path
            for key in sorted(conflict_keys)
        ),
    )
