"""Independent filesystem scanner contract tests for TASK-002."""

from dataclasses import FrozenInstanceError
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mysync.filesystem.scanner import (
    CaseCollisionError,
    FileChangedError,
    scan_directory,
)


class ScannerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_empty_root(self):
        result = scan_directory(self.root)
        self.assertEqual(result.files, ())
        self.assertEqual(result.directories, ())

    def test_nested_files_empty_directories_and_metadata(self):
        (self.root / "z_empty").mkdir()
        (self.root / "a" / "nested").mkdir(parents=True)
        first = self.root / "a" / "nested" / "binary.bin"
        first.write_bytes(b"\x00\xffabc")
        second = self.root / "a" / "hello.txt"
        second.write_bytes(b"hello")

        result = scan_directory(self.root)
        self.assertIsInstance(result.files, tuple)
        self.assertIsInstance(result.directories, tuple)
        self.assertEqual(result.directories, ("a", "a/nested", "z_empty"))
        self.assertEqual([entry.relative_path for entry in result.files],
                         ["a/hello.txt", "a/nested/binary.bin"])
        for entry, path in zip(result.files, (second, first)):
            content = path.read_bytes()
            self.assertEqual(entry.size, len(content))
            self.assertEqual(entry.mtime_ns, path.stat().st_mtime_ns)
            self.assertEqual(entry.sha256, hashlib.sha256(content).hexdigest())
            with self.assertRaises(FrozenInstanceError):
                entry.size = 0
        with self.assertRaises(FrozenInstanceError):
            result.files = ()

    def test_hidden_unicode_and_spaces_are_included(self):
        folder = self.root / "Рабочие файлы"
        folder.mkdir()
        (folder / "отчёт 日本語.txt").write_bytes(b"data")
        (self.root / ".env").write_bytes(b"x=1")
        (self.root / "_mysync").mkdir()
        (self.root / "_mysync" / "backup.bak").write_bytes(b"old")
        result = scan_directory(self.root)
        self.assertEqual(
            {entry.relative_path for entry in result.files},
            {".env", "_mysync/backup.bak", "Рабочие файлы/отчёт 日本語.txt"},
        )
        self.assertIn("_mysync", result.directories)

    def test_invalid_roots(self):
        with self.assertRaises(FileNotFoundError):
            scan_directory(self.root / "absent")
        ordinary = self.root / "ordinary.txt"
        ordinary.write_bytes(b"x")
        with self.assertRaises(NotADirectoryError):
            scan_directory(ordinary)

    def test_symlinks_are_skipped_and_symlink_root_rejected(self):
        external = self.root / "external"
        external.mkdir()
        (external / "actual.txt").write_bytes(b"x")
        target = self.root / "target.txt"
        target.write_bytes(b"target")
        link = self.root / "link.txt"
        try:
            link.symlink_to(target)
            (external / "loop").symlink_to(external, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlink unavailable: {error}")
        with self.assertRaises(ValueError):
            scan_directory(external / "loop")
        result = scan_directory(self.root)
        self.assertNotIn("link.txt", [entry.relative_path for entry in result.files])
        self.assertNotIn("external/loop", result.directories)
        self.assertEqual(
            {entry.relative_path for entry in result.files},
            {"external/actual.txt", "target.txt"},
        )

    def test_case_collision_between_two_files(self):
        upper = self.root / "Report.txt"
        lower = self.root / "report.txt"
        upper.write_bytes(b"one")
        lower.write_bytes(b"two")
        if upper.samefile(lower):
            self.skipTest("filesystem cannot store distinct case variants")
        with self.assertRaises(CaseCollisionError):
            scan_directory(self.root)

    def test_case_collision_between_file_and_directory(self):
        upper = self.root / "Project"
        lower = self.root / "project"
        upper.mkdir()
        try:
            lower.write_bytes(b"data")
        except IsADirectoryError:
            self.skipTest("filesystem cannot store distinct case variants")
        with self.assertRaises(CaseCollisionError):
            scan_directory(self.root)

    def test_case_collision_between_directories(self):
        upper = self.root / "Data"
        lower = self.root / "data"
        upper.mkdir()
        try:
            lower.mkdir()
        except FileExistsError:
            self.skipTest("filesystem cannot store distinct case variants")
        with self.assertRaises(CaseCollisionError):
            scan_directory(self.root)

    def test_change_during_hashing_raises(self):
        path = self.root / "changing.txt"
        path.write_bytes(b"before")

        def hash_and_change(candidate):
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
            candidate.write_bytes(b"after and larger")
            return digest

        with patch("mysync.filesystem.scanner.sha256_file", side_effect=hash_and_change):
            with self.assertRaises(FileChangedError):
                scan_directory(self.root)

    def test_permission_error_during_hashing_propagates(self):
        (self.root / "denied.txt").write_bytes(b"x")
        failure = PermissionError("simulated access denial")
        with patch("mysync.filesystem.scanner.sha256_file", side_effect=failure):
            with self.assertRaises(PermissionError) as caught:
                scan_directory(self.root)
        self.assertIs(caught.exception, failure)

    def test_permission_error_during_directory_iteration_propagates(self):
        nested = self.root / "nested"
        nested.mkdir()
        failure = PermissionError("simulated directory access denial")
        original_iterdir = Path.iterdir

        def guarded_iterdir(path):
            if path == nested:
                raise failure
            return original_iterdir(path)

        with patch.object(Path, "iterdir", guarded_iterdir):
            with self.assertRaises(PermissionError) as caught:
                scan_directory(self.root)
        self.assertIs(caught.exception, failure)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is unavailable on this platform")
    def test_fifo_raises_without_hashing(self):
        fifo = self.root / "incoming.pipe"
        os.mkfifo(fifo)
        with patch(
            "mysync.filesystem.scanner.sha256_file",
            side_effect=AssertionError("FIFO must not be hashed"),
        ) as hashing:
            with self.assertRaises(OSError):
                scan_directory(self.root)
        hashing.assert_not_called()

    def test_repeated_scans_preserve_file_data_and_mtime(self):
        path = self.root / "preserved.bin"
        data = bytes(range(256)) * 17
        path.write_bytes(data)
        before = path.stat()
        first = scan_directory(self.root)
        second = scan_directory(self.root)
        self.assertEqual(first, second)
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)


if __name__ == "__main__":
    unittest.main()
