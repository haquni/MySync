"""Tests for backups used by destructive file operations."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mysync.filesystem.backup import (
    NANOSECONDS_PER_DAY,
    backup_existing_file,
    delete_with_backup,
    purge_expired_backups,
)
from mysync.filesystem.receiver import UnsafePathError
from mysync.filesystem.scanner import FileChangedError


class BackupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def make_file(self, relative_path: str, content=b"data") -> Path:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_backup_preserves_content_structure_and_metadata_name(self):
        source = self.make_file("docs/report.txt", b"version one")
        record = backup_existing_file(self.root, "docs/report.txt", "device-1", 123)
        self.assertEqual(record.original_relative_path, "docs/report.txt")
        self.assertEqual(record.created_at_ns, 123)
        self.assertEqual(record.backup_path.read_bytes(), b"version one")
        self.assertEqual(record.backup_path.parent, self.root / "_mysync/backups/docs")
        self.assertEqual(record.backup_path.name, "report.txt__device-1__123.bak")
        self.assertEqual(record.backup_path.stat().st_mtime_ns, 123)
        self.assertEqual(source.read_bytes(), b"version one")

    def test_duplicate_name_gets_sequence_suffix(self):
        self.make_file("file.txt")
        first = backup_existing_file(self.root, "file.txt", "device", 10)
        second = backup_existing_file(self.root, "file.txt", "device", 10)
        self.assertNotEqual(first.backup_path, second.backup_path)
        self.assertEqual(second.backup_path.name, "file.txt__device__10__1.bak")

    def test_delete_happens_after_successful_backup(self):
        source = self.make_file("remove.bin", b"important")
        record = delete_with_backup(self.root, "remove.bin", "device", 20)
        self.assertFalse(source.exists())
        self.assertEqual(record.backup_path.read_bytes(), b"important")

    def test_invalid_sources_do_not_delete_user_file(self):
        source = self.make_file("safe.txt")
        for path in ("../safe.txt", "_mysync/data.txt"):
            with self.subTest(path=path), self.assertRaises(UnsafePathError):
                delete_with_backup(self.root, path, "device", 1)
        with self.assertRaises(ValueError):
            delete_with_backup(self.root, "safe.txt", "bad/id", 1)
        self.assertTrue(source.exists())

    def test_symlink_source_is_rejected(self):
        target = self.make_file("target.txt")
        link = self.root / "link.txt"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlink unavailable: {error}")
        with self.assertRaises(UnsafePathError):
            backup_existing_file(self.root, "link.txt", "device", 1)

    def test_change_during_backup_is_detected_and_no_backup_is_installed(self):
        source = self.make_file("changing.txt", b"before")
        original_copy = __import__("shutil").copyfileobj

        def copy_and_change(input_stream, output_stream, length):
            original_copy(input_stream, output_stream, length)
            source.write_bytes(b"changed and larger")

        with patch("mysync.filesystem.backup.shutil.copyfileobj", side_effect=copy_and_change):
            with self.assertRaises(FileChangedError):
                backup_existing_file(self.root, "changing.txt", "device", 5)
        self.assertEqual(list((self.root / "_mysync").rglob("*.bak")), [])
        self.assertEqual(list((self.root / "_mysync").rglob("*.tmp")), [])

    def test_purge_removes_only_expired_backup_files(self):
        old = self.make_file("_mysync/backups/old.bak")
        recent = self.make_file("_mysync/backups/recent.bak")
        other = self.make_file("_mysync/backups/note.txt")
        now = 10 * NANOSECONDS_PER_DAY
        os.utime(old, ns=(1, 1))
        os.utime(recent, ns=(now, now))
        deleted = purge_expired_backups(self.root, now, retention_days=7)
        self.assertEqual(deleted, (old,))
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(other.exists())

    def test_purge_empty_store_and_invalid_values(self):
        self.assertEqual(purge_expired_backups(self.root, 1), ())
        for now, days in ((-1, 7), (1, 0), (1, True)):
            with self.subTest(now=now, days=days), self.assertRaises(ValueError):
                purge_expired_backups(self.root, now, days)


if __name__ == "__main__":
    unittest.main()
