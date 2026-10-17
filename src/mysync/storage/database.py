"""Transactional SQLite storage for devices and filesystem snapshots."""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Iterator

from mysync.filesystem.scanner import (
    CaseCollisionError,
    FileEntry,
    ScanResult,
)


@dataclass(frozen=True)
class DeviceRecord:
    device_id: str
    name: str
    ip_address: str
    trusted: bool
    last_seen_ns: int


@dataclass(frozen=True)
class SnapshotUpdate:
    created: tuple[str, ...]
    modified: tuple[str, ...]
    deleted: tuple[str, ...]


class StateDatabase:
    """Store device records and the latest observed file state."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create the database schema if it does not already exist."""
        statements = (
            """
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                ip_address TEXT NOT NULL,
                trusted INTEGER NOT NULL CHECK (trusted IN (0, 1)),
                last_seen_ns INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS files (
                device_id TEXT NOT NULL,
                path_key TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('active', 'deleted')),
                observed_at_ns INTEGER NOT NULL,
                PRIMARY KEY (device_id, path_key),
                FOREIGN KEY (device_id) REFERENCES devices(device_id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS file_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                path_key TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                event_type TEXT NOT NULL
                    CHECK (event_type IN ('created', 'modified', 'deleted')),
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                observed_at_ns INTEGER NOT NULL,
                FOREIGN KEY (device_id) REFERENCES devices(device_id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS conflicts (
                conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
                path_key TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                local_device_id TEXT NOT NULL,
                remote_device_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'resolved')),
                created_at_ns INTEGER NOT NULL,
                resolved_at_ns INTEGER,
                FOREIGN KEY (local_device_id) REFERENCES devices(device_id)
                    ON DELETE CASCADE,
                FOREIGN KEY (remote_device_id) REFERENCES devices(device_id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS sync_runs (
                sync_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                local_device_id TEXT NOT NULL,
                remote_device_id TEXT NOT NULL,
                started_at_ns INTEGER NOT NULL,
                finished_at_ns INTEGER,
                status TEXT NOT NULL,
                FOREIGN KEY (local_device_id) REFERENCES devices(device_id)
                    ON DELETE CASCADE,
                FOREIGN KEY (remote_device_id) REFERENCES devices(device_id)
                    ON DELETE CASCADE
            )
            """,
        )
        with self._connection() as connection:
            for statement in statements:
                connection.execute(statement)

    def upsert_device(self, record: DeviceRecord) -> None:
        """Insert a device or replace its mutable fields."""
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO devices (
                    device_id, name, ip_address, trusted, last_seen_ns
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    name = excluded.name,
                    ip_address = excluded.ip_address,
                    trusted = excluded.trusted,
                    last_seen_ns = excluded.last_seen_ns
                """,
                (
                    record.device_id,
                    record.name,
                    record.ip_address,
                    int(record.trusted),
                    record.last_seen_ns,
                ),
            )

    def list_devices(self) -> tuple[DeviceRecord, ...]:
        """Return devices sorted by name and then by identifier."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT device_id, name, ip_address, trusted, last_seen_ns
                FROM devices
                ORDER BY name COLLATE NOCASE, device_id
                """
            ).fetchall()
        return tuple(
            DeviceRecord(
                device_id=row[0],
                name=row[1],
                ip_address=row[2],
                trusted=bool(row[3]),
                last_seen_ns=row[4],
            )
            for row in rows
        )

    def remove_device(self, device_id: str) -> None:
        """Remove a device and records linked to it by foreign keys."""
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM devices WHERE device_id = ?",
                (device_id,),
            )

    def save_snapshot(
        self,
        device_id: str,
        snapshot: ScanResult,
        observed_at_ns: int,
    ) -> SnapshotUpdate:
        """Persist one complete file snapshot and record state transitions."""
        new_files = self._index_snapshot(snapshot)

        with self._connection() as connection:
            if connection.execute(
                "SELECT 1 FROM devices WHERE device_id = ?",
                (device_id,),
            ).fetchone() is None:
                raise sqlite3.IntegrityError(f"Unknown device: {device_id}")

            rows = connection.execute(
                """
                SELECT path_key, relative_path, size, mtime_ns, sha256, status
                FROM files
                WHERE device_id = ?
                """,
                (device_id,),
            ).fetchall()
            old_files = {row[0]: row[1:] for row in rows}

            created: list[str] = []
            modified: list[str] = []
            deleted: list[str] = []

            for path_key in sorted(new_files):
                entry = new_files[path_key]
                previous = old_files.get(path_key)
                if previous is None or previous[4] == "deleted":
                    event_type = "created"
                    created.append(entry.relative_path)
                elif previous[3] != entry.sha256 or previous[0] != entry.relative_path:
                    event_type = "modified"
                    modified.append(entry.relative_path)
                else:
                    event_type = None

                connection.execute(
                    """
                    INSERT INTO files (
                        device_id, path_key, relative_path, size, mtime_ns,
                        sha256, status, observed_at_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
                    ON CONFLICT(device_id, path_key) DO UPDATE SET
                        relative_path = excluded.relative_path,
                        size = excluded.size,
                        mtime_ns = excluded.mtime_ns,
                        sha256 = excluded.sha256,
                        status = excluded.status,
                        observed_at_ns = excluded.observed_at_ns
                    """,
                    (
                        device_id,
                        path_key,
                        entry.relative_path,
                        entry.size,
                        entry.mtime_ns,
                        entry.sha256,
                        observed_at_ns,
                    ),
                )
                if event_type is not None:
                    self._insert_event(
                        connection,
                        device_id,
                        path_key,
                        entry,
                        event_type,
                        observed_at_ns,
                    )

            for path_key in sorted(old_files.keys() - new_files.keys()):
                previous = old_files[path_key]
                if previous[4] == "deleted":
                    continue
                relative_path, size, mtime_ns, digest, _status = previous
                entry = FileEntry(relative_path, size, mtime_ns, digest)
                connection.execute(
                    """
                    UPDATE files
                    SET status = 'deleted', observed_at_ns = ?
                    WHERE device_id = ? AND path_key = ?
                    """,
                    (observed_at_ns, device_id, path_key),
                )
                self._insert_event(
                    connection,
                    device_id,
                    path_key,
                    entry,
                    "deleted",
                    observed_at_ns,
                )
                deleted.append(relative_path)

        return SnapshotUpdate(tuple(created), tuple(modified), tuple(deleted))

    def load_snapshot(self, device_id: str) -> ScanResult:
        """Load active files for a device; directory state is not persisted."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT relative_path, size, mtime_ns, sha256
                FROM files
                WHERE device_id = ? AND status = 'active'
                ORDER BY path_key
                """,
                (device_id,),
            ).fetchall()
        return ScanResult(
            files=tuple(FileEntry(*row) for row in rows),
            directories=(),
        )

    @staticmethod
    def _index_snapshot(snapshot: ScanResult) -> dict[str, FileEntry]:
        seen: dict[str, str] = {}
        indexed: dict[str, FileEntry] = {}

        for relative_path in snapshot.directories:
            key = relative_path.casefold()
            previous = seen.get(key)
            if previous is not None:
                raise CaseCollisionError(
                    f"Paths collide without case: {previous!r} and {relative_path!r}"
                )
            seen[key] = relative_path

        for entry in snapshot.files:
            key = entry.relative_path.casefold()
            previous = seen.get(key)
            if previous is not None:
                raise CaseCollisionError(
                    f"Paths collide without case: {previous!r} "
                    f"and {entry.relative_path!r}"
                )
            seen[key] = entry.relative_path
            indexed[key] = entry

        return indexed

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        device_id: str,
        path_key: str,
        entry: FileEntry,
        event_type: str,
        observed_at_ns: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO file_events (
                device_id, path_key, relative_path, event_type,
                size, mtime_ns, sha256, observed_at_ns
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                device_id,
                path_key,
                entry.relative_path,
                event_type,
                entry.size,
                entry.mtime_ns,
                entry.sha256,
                observed_at_ns,
            ),
        )
