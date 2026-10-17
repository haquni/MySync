"""Read-only plans for the first two-device synchronization."""

from dataclasses import FrozenInstanceError
import unittest

from mysync.application.initial_sync import plan_initial_sync
from mysync.filesystem.comparison import FileChange
from mysync.filesystem.scanner import CaseCollisionError, FileEntry, ScanResult


def entry(path: str, digest: str = "same") -> FileEntry:
    return FileEntry(path, 1, 1, digest)


def snapshot(files=(), directories=()) -> ScanResult:
    return ScanResult(tuple(files), tuple(directories))


class InitialSyncTests(unittest.TestCase):
    def test_empty_snapshots(self):
        plan = plan_initial_sync(snapshot(), snapshot())
        self.assertEqual(plan.copy_to_left, ())
        self.assertEqual(plan.copy_to_right, ())
        self.assertEqual(plan.create_on_left, ())
        self.assertEqual(plan.create_on_right, ())
        self.assertEqual(plan.file_conflicts, ())
        self.assertEqual(plan.structure_conflicts, ())

    def test_unique_files_copy_both_ways_without_deleting(self):
        plan = plan_initial_sync(snapshot((entry("left.txt"),)),
                                 snapshot((entry("right.txt"),)))
        self.assertEqual(plan.copy_to_left, (entry("right.txt"),))
        self.assertEqual(plan.copy_to_right, (entry("left.txt"),))
        self.assertEqual(plan.file_conflicts, ())

    def test_identical_file_is_not_transferred(self):
        left = snapshot((entry("same.txt"),))
        right = snapshot((FileEntry("same.txt", 1, 900, "same"),))
        plan = plan_initial_sync(left, right)
        self.assertEqual(plan.copy_to_left + plan.copy_to_right, ())
        self.assertEqual(plan.file_conflicts, ())

    def test_different_content_and_case_only_difference_conflict(self):
        left = snapshot((entry("doc.txt", "old"), entry("Report.txt")))
        right = snapshot((entry("doc.txt", "new"), entry("report.txt")))
        plan = plan_initial_sync(left, right)
        self.assertEqual(plan.file_conflicts, (
            FileChange(entry("doc.txt", "old"), entry("doc.txt", "new")),
            FileChange(entry("Report.txt"), entry("report.txt")),
        ))
        self.assertEqual(plan.copy_to_left + plan.copy_to_right, ())

    def test_empty_directories_are_planned_in_each_direction(self):
        plan = plan_initial_sync(snapshot(directories=("z", "a")),
                                 snapshot(directories=("b", "a")))
        self.assertEqual(plan.create_on_left, ("b",))
        self.assertEqual(plan.create_on_right, ("z",))

    def test_file_directory_conflict_blocks_subtree_but_not_other_files(self):
        left = snapshot((entry("folder"), entry("left-safe.txt")))
        right = snapshot((entry("folder/nested.txt"), entry("right-safe.txt")),
                         ("folder",))
        plan = plan_initial_sync(left, right)
        self.assertEqual(plan.structure_conflicts, ("folder",))
        self.assertEqual(plan.copy_to_left, (entry("right-safe.txt"),))
        self.assertEqual(plan.copy_to_right, (entry("left-safe.txt"),))
        self.assertEqual(plan.create_on_left, ())

    def test_duplicate_case_insensitive_paths_are_rejected(self):
        with self.assertRaises(CaseCollisionError):
            plan_initial_sync(snapshot((entry("A"), entry("a"))), snapshot())
        with self.assertRaises(CaseCollisionError):
            plan_initial_sync(snapshot(), snapshot(directories=("A", "a")))
        with self.assertRaises(CaseCollisionError):
            plan_initial_sync(snapshot((entry("A"),), ("a",)), snapshot())

    def test_plan_is_immutable(self):
        plan = plan_initial_sync(snapshot(), snapshot())
        with self.assertRaises(FrozenInstanceError):
            plan.copy_to_left = ()


if __name__ == "__main__":
    unittest.main()
