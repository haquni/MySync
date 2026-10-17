"""Tests for verified installation of newly received files."""

import hashlib
from pathlib import Path
import tempfile
import unittest

from mysync.filesystem.receiver import IntegrityError, UnsafePathError, receive_new_file


class ReceiveNewFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def receive(self, path, chunks, *, digest=None, mtime_ns=None):
        content = b"".join(chunks)
        expected = digest or hashlib.sha256(content).hexdigest()
        return receive_new_file(self.root, path, chunks, expected, mtime_ns=mtime_ns)

    def assert_no_temporary_files(self):
        self.assertEqual(list(self.root.rglob(".mysync-*.tmp")), [])

    def test_binary_chunks_nested_path_and_mtime(self):
        chunks = [b"\x00abc", b"", bytearray(b"\xfftail"), memoryview(b"!")]
        target = self.receive("nested/data.bin", chunks, mtime_ns=1_700_000_000_000_000_000)
        self.assertEqual(target, self.root / "nested" / "data.bin")
        self.assertEqual(target.read_bytes(), b"\x00abc\xfftail!")
        self.assertEqual(target.stat().st_mtime_ns, 1_700_000_000_000_000_000)
        self.assert_no_temporary_files()

    def test_empty_file(self):
        target = self.receive("empty.txt", [])
        self.assertEqual(target.read_bytes(), b"")

    def test_hash_mismatch_removes_temporary_and_target(self):
        with self.assertRaises(IntegrityError):
            self.receive("bad.bin", [b"actual"], digest=hashlib.sha256(b"other").hexdigest())
        self.assertFalse((self.root / "bad.bin").exists())
        self.assert_no_temporary_files()

    def test_existing_target_is_preserved(self):
        target = self.root / "present.txt"
        target.write_bytes(b"original")
        with self.assertRaises(FileExistsError):
            self.receive("present.txt", [b"replacement"])
        self.assertEqual(target.read_bytes(), b"original")
        self.assert_no_temporary_files()

    def test_invalid_relative_paths(self):
        for path in ("", ".", "../escape", "a/../escape", "/absolute", "a//b", "a\\b"):
            with self.subTest(path=path):
                with self.assertRaises(UnsafePathError):
                    self.receive(path, [b"x"])

    def test_non_binary_chunk_leaves_no_file(self):
        digest = hashlib.sha256(b"good").hexdigest()
        with self.assertRaises(TypeError):
            receive_new_file(self.root, "bad.txt", [b"good", "text"], digest)
        self.assertFalse((self.root / "bad.txt").exists())
        self.assert_no_temporary_files()

    def test_invalid_digest_is_rejected_before_writing(self):
        with self.assertRaises(ValueError):
            receive_new_file(self.root, "file.txt", [b"x"], "invalid")
        self.assert_no_temporary_files()

    def test_symlink_parent_and_target_are_rejected(self):
        outside = self.root.parent / f"{self.root.name}-outside"
        outside.mkdir()
        self.addCleanup(outside.rmdir)
        try:
            (self.root / "linked").symlink_to(outside, target_is_directory=True)
            (self.root / "target-link").symlink_to(outside / "file")
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlink unavailable: {error}")
        with self.assertRaises(UnsafePathError):
            self.receive("linked/file.txt", [b"x"])
        with self.assertRaises(UnsafePathError):
            self.receive("target-link", [b"x"])
        self.assertFalse((outside / "file.txt").exists())

    def test_invalid_root(self):
        with self.assertRaises(FileNotFoundError):
            receive_new_file(self.root / "missing", "a", [], hashlib.sha256(b"").hexdigest())
        ordinary = self.root / "ordinary"
        ordinary.write_bytes(b"x")
        with self.assertRaises(NotADirectoryError):
            receive_new_file(ordinary, "a", [], hashlib.sha256(b"").hexdigest())


if __name__ == "__main__":
    unittest.main()
