"""Tests for read-only comparison of file snapshots."""

from dataclasses import FrozenInstanceError
import unittest

from mysync.filesystem.comparison import FileChange, compare_snapshots
from mysync.filesystem.scanner import CaseCollisionError, FileEntry, ScanResult


def file(path: str, digest: str = "a", mtime: int = 1) -> FileEntry:
    return FileEntry(path, 1, mtime, digest)


def snapshot(*entries: FileEntry) -> ScanResult:
    return ScanResult(entries, ())


class CompareSnapshotsTests(unittest.TestCase):
    def test_empty_snapshots(self):
        result = compare_snapshots(snapshot(), snapshot())
        self.assertEqual((result.added, result.missing, result.changed, result.unchanged),
                         ((), (), (), ()))

    def test_add_missing_change_and_unchanged(self):
        before = snapshot(file("lost.txt"), file("edit.txt", "old"), file("same.txt"))
        after = snapshot(file("new.txt"), file("edit.txt", "new"), file("same.txt"))
        result = compare_snapshots(before, after)
        self.assertEqual(result.added, (file("new.txt"),))
        self.assertEqual(result.missing, (file("lost.txt"),))
        self.assertEqual(result.changed, (FileChange(file("edit.txt", "old"),
                                                     file("edit.txt", "new")),))
        self.assertEqual(result.unchanged, (file("same.txt"),))
        self.assertEqual(before.files[0].relative_path, "lost.txt")

    def test_equal_content_ignores_mtime_but_uses_new_entry(self):
        result = compare_snapshots(snapshot(file("doc.txt", "hash", 1)),
                                   snapshot(file("doc.txt", "hash", 999)))
        self.assertEqual(result.changed, ())
        self.assertEqual(result.unchanged, (file("doc.txt", "hash", 999),))

    def test_case_only_path_change_is_reported(self):
        old = file("Report.txt")
        new = file("report.txt")
        result = compare_snapshots(snapshot(old), snapshot(new))
        self.assertEqual(result.changed, (FileChange(old, new),))
        self.assertEqual(result.added, ())
        self.assertEqual(result.missing, ())

    def test_unicode_and_sorted_output(self):
        result = compare_snapshots(snapshot(), snapshot(file("Я/файл.txt"),
                                                       file("b.txt"), file("A.txt")))
        self.assertEqual(tuple(item.relative_path for item in result.added),
                         ("A.txt", "b.txt", "Я/файл.txt"))

    def test_case_collision_is_rejected_in_either_snapshot(self):
        collision = snapshot(file("A.txt"), file("a.txt"))
        with self.assertRaises(CaseCollisionError):
            compare_snapshots(collision, snapshot())
        with self.assertRaises(CaseCollisionError):
            compare_snapshots(snapshot(), collision)

    def test_result_is_immutable(self):
        result = compare_snapshots(snapshot(), snapshot(file("a.txt")))
        with self.assertRaises(FrozenInstanceError):
            result.added = ()


if __name__ == "__main__":
    unittest.main()
