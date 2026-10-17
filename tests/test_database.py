"""Independent contract tests for the TASK-005 SQLite state database."""

from dataclasses import FrozenInstanceError
from pathlib import Path
import sqlite3
import tempfile
import unittest

from mysync.filesystem.scanner import CaseCollisionError, FileEntry, ScanResult
from mysync.storage.database import DeviceRecord, SnapshotUpdate, StateDatabase


def file_entry(
    path: str,
    digest: str = "a" * 64,
    *,
    size: int = 1,
    mtime_ns: int = 1,
) -> FileEntry:
    return FileEntry(path, size, mtime_ns, digest)


def snapshot(*entries: FileEntry, directories: tuple[str, ...] = ()) -> ScanResult:
    return ScanResult(entries, directories)


class StateDatabaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "state.sqlite3"
        self.database = StateDatabase(self.path)
        self.database.initialize()
        self.local = DeviceRecord("local", "Local", "192.0.2.1", True, 10)
        self.database.upsert_device(self.local)

    def query(self, sql: str, parameters=()):
        connection = sqlite3.connect(self.path)
        try:
            return connection.execute(sql, parameters).fetchall()
        finally:
            connection.close()

    def event_types(self, device_id: str = "local") -> list[str]:
        rows = self.query(
            "SELECT event_type FROM file_events WHERE device_id = ? ORDER BY rowid",
            (device_id,),
        )
        return [row[0] for row in rows]

    def test_records_are_immutable_and_snapshot_update_uses_tuples(self):
        with self.assertRaises(FrozenInstanceError):
            self.local.name = "Changed"
        update = SnapshotUpdate(("a",), ("b",), ("c",))
        self.assertEqual(update.created, ("a",))
        self.assertEqual(update.modified, ("b",))
        self.assertEqual(update.deleted, ("c",))

    def test_initialize_creates_five_tables_and_is_idempotent(self):
        self.database.initialize()
        tables = {
            row[0]
            for row in self.query(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertTrue(
            {"devices", "files", "file_events", "conflicts", "sync_runs"}
            <= tables
        )

    def test_every_connection_enforces_foreign_keys(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.save_snapshot(
                "missing-device", snapshot(file_entry("orphan.txt")), 20
            )
        self.assertEqual(self.query("SELECT * FROM files"), [])
        self.assertEqual(self.query("SELECT * FROM file_events"), [])

    def test_device_crud_upsert_and_sorted_listing(self):
        second = DeviceRecord("z-device", "Zulu", "198.51.100.3", False, 0)
        first = DeviceRecord("a-device", "Alpha", "198.51.100.2", True, 5)
        self.database.upsert_device(second)
        self.database.upsert_device(first)
        self.assertEqual(
            self.database.list_devices(),
            (first, self.local, second),
        )

        updated = DeviceRecord("local", "Renamed", "203.0.113.4", False, 99)
        self.database.upsert_device(updated)
        self.assertIn(updated, self.database.list_devices())
        self.assertNotIn(self.local, self.database.list_devices())

        self.database.remove_device("local")
        self.assertNotIn(updated, self.database.list_devices())
        self.database.remove_device("unknown")

    def test_first_snapshot_creates_events_and_ignores_directories(self):
        initial = snapshot(
            file_entry("b.txt", "b" * 64, size=2, mtime_ns=20),
            file_entry("Folder/a.txt", "a" * 64, size=3, mtime_ns=10),
            directories=("Folder", "empty"),
        )
        update = self.database.save_snapshot("local", initial, 100)
        self.assertEqual(update.created, ("b.txt", "Folder/a.txt"))
        self.assertEqual(update.modified, ())
        self.assertEqual(update.deleted, ())
        self.assertEqual(
            self.database.load_snapshot("local"),
            ScanResult(initial.files, ()),
        )
        self.assertEqual(self.event_types(), ["created", "created"])

    def test_identical_snapshot_is_idempotent(self):
        current = snapshot(file_entry("same.txt", mtime_ns=10))
        self.database.save_snapshot("local", current, 100)
        update = self.database.save_snapshot("local", current, 200)
        self.assertEqual(update, SnapshotUpdate((), (), ()))
        self.assertEqual(self.event_types(), ["created"])

    def test_content_change_creates_modified_event(self):
        before = snapshot(file_entry("doc.txt", "a" * 64, size=1, mtime_ns=10))
        after = snapshot(file_entry("doc.txt", "b" * 64, size=9, mtime_ns=20))
        self.database.save_snapshot("local", before, 100)
        update = self.database.save_snapshot("local", after, 200)
        self.assertEqual(update, SnapshotUpdate((), ("doc.txt",), ()))
        self.assertEqual(self.database.load_snapshot("local"), after)
        self.assertEqual(self.event_types(), ["created", "modified"])

    def test_case_only_path_change_creates_modified_event(self):
        before = snapshot(file_entry("Report.txt", mtime_ns=10))
        after = snapshot(file_entry("report.txt", mtime_ns=20))
        self.database.save_snapshot("local", before, 100)
        update = self.database.save_snapshot("local", after, 200)
        self.assertEqual(update, SnapshotUpdate((), ("report.txt",), ()))
        self.assertEqual(self.database.load_snapshot("local"), after)
        self.assertEqual(self.event_types(), ["created", "modified"])

    def test_mtime_only_change_updates_state_without_event(self):
        before = snapshot(file_entry("doc.txt", mtime_ns=10))
        after = snapshot(file_entry("doc.txt", mtime_ns=999))
        self.database.save_snapshot("local", before, 100)
        update = self.database.save_snapshot("local", after, 200)
        self.assertEqual(update, SnapshotUpdate((), (), ()))
        self.assertEqual(self.database.load_snapshot("local"), after)
        self.assertEqual(self.event_types(), ["created"])

    def test_missing_file_is_deleted_and_not_loaded_as_active(self):
        before = snapshot(file_entry("gone.txt"), file_entry("kept.txt"))
        self.database.save_snapshot("local", before, 100)
        update = self.database.save_snapshot(
            "local", snapshot(file_entry("kept.txt")), 200
        )
        self.assertEqual(update, SnapshotUpdate((), (), ("gone.txt",)))
        self.assertEqual(
            self.database.load_snapshot("local"), snapshot(file_entry("kept.txt"))
        )
        self.assertEqual(self.event_types(), ["created", "created", "deleted"])

    def test_deleted_file_reappears_as_created(self):
        entry = file_entry("return.txt")
        self.database.save_snapshot("local", snapshot(entry), 100)
        self.database.save_snapshot("local", snapshot(), 200)
        update = self.database.save_snapshot("local", snapshot(entry), 300)
        self.assertEqual(update, SnapshotUpdate(("return.txt",), (), ()))
        self.assertEqual(self.event_types(), ["created", "deleted", "created"])

    def test_devices_have_independent_snapshots(self):
        remote = DeviceRecord("remote", "Remote", "192.0.2.2", True, 20)
        self.database.upsert_device(remote)
        local_snapshot = snapshot(file_entry("local.txt", "1" * 64))
        remote_snapshot = snapshot(file_entry("remote.txt", "2" * 64))
        self.database.save_snapshot("local", local_snapshot, 100)
        self.database.save_snapshot("remote", remote_snapshot, 110)
        self.assertEqual(self.database.load_snapshot("local"), local_snapshot)
        self.assertEqual(self.database.load_snapshot("remote"), remote_snapshot)

    def test_case_collision_rolls_back_without_changing_existing_state(self):
        existing = snapshot(file_entry("safe.txt"))
        self.database.save_snapshot("local", existing, 100)
        events_before = self.query("SELECT * FROM file_events ORDER BY rowid")
        colliding = snapshot(
            file_entry("A.txt", "1" * 64),
            file_entry("a.txt", "2" * 64),
        )
        with self.assertRaises(CaseCollisionError):
            self.database.save_snapshot("local", colliding, 200)
        self.assertEqual(self.database.load_snapshot("local"), existing)
        self.assertEqual(
            self.query("SELECT * FROM file_events ORDER BY rowid"), events_before
        )

    def test_data_survives_reopening_database(self):
        expected = snapshot(file_entry("persisted.bin", "f" * 64, size=8))
        self.database.save_snapshot("local", expected, 100)
        reopened = StateDatabase(self.path)
        reopened.initialize()
        self.assertEqual(reopened.list_devices(), (self.local,))
        self.assertEqual(reopened.load_snapshot("local"), expected)

    def test_parent_directory_must_already_exist(self):
        missing_path = self.root / "missing" / "state.sqlite3"
        with self.assertRaises(sqlite3.OperationalError):
            StateDatabase(missing_path).initialize()
        self.assertFalse(missing_path.exists())


if __name__ == "__main__":
    unittest.main()
