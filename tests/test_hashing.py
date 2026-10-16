"""Independent contract tests for TASK-001; standard library only."""

import hashlib
import io
from contextlib import ExitStack, contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mysync.core.hashing import sha256_file


MAX_READ_SIZE = 1024 * 1024


class CheckedStream(io.BytesIO):
    """Reject unbounded reads and optionally simulate a read failure."""

    def __init__(self, data, failure=None):
        super().__init__(data)
        self.failure = failure
        self.read_sizes = []

    def read(self, size=-1):
        if not isinstance(size, int) or not 0 < size <= MAX_READ_SIZE:
            raise AssertionError("Reads must request 1..1048576 bytes")
        self.read_sizes.append(size)
        if self.failure is not None and len(self.read_sizes) > 1:
            raise self.failure
        return super().read(size)


@contextmanager
def replace_file_open(stream=None, failure=None):
    """Instrument public Python file-opening APIs, not module internals."""

    def fake_open(path, mode="r", *args, **kwargs):
        if mode != "rb":
            raise AssertionError("The input must be opened read-only in binary mode")
        if failure is not None:
            raise failure
        return stream

    with ExitStack() as stack:
        stack.enter_context(patch.object(Path, "open", fake_open))
        stack.enter_context(patch("builtins.open", fake_open))
        stack.enter_context(patch("io.open", fake_open))
        yield


class Sha256FileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def make_file(self, content, name="sample.bin"):
        path = self.root / name
        path.write_bytes(content)
        return path

    def test_empty_file_known_digest(self):
        self.assertEqual(
            sha256_file(self.make_file(b"")),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )

    def test_abc_known_digest_and_format(self):
        digest = sha256_file(self.make_file(b"abc"))
        self.assertIsInstance(digest, str)
        self.assertRegex(digest, r"\A[0-9a-f]{64}\Z")
        self.assertEqual(
            digest,
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )

    def test_binary_data(self):
        data = bytes(range(256)) * 7
        self.assertEqual(sha256_file(self.make_file(data)), hashlib.sha256(data).hexdigest())

    def test_large_file_and_block_boundaries(self):
        for size in (MAX_READ_SIZE - 1, MAX_READ_SIZE, MAX_READ_SIZE + 1,
                     2 * MAX_READ_SIZE + 137):
            with self.subTest(size=size):
                data = (bytes(range(256)) * (size // 256 + 1))[:size]
                self.assertEqual(
                    sha256_file(self.make_file(data)), hashlib.sha256(data).hexdigest()
                )

    def test_unicode_and_spaces_in_path(self):
        directory = self.root / "Рабочие документы"
        directory.mkdir()
        path = directory / "отчёт 日本語 01.bin"
        data = b"\x00\xff\r\nUnicode path"
        path.write_bytes(data)
        self.assertEqual(sha256_file(path), hashlib.sha256(data).hexdigest())

    def test_repeated_hashing_preserves_content_size_and_mtime(self):
        data = b"unchanged\x00\r\n" * 37
        path = self.make_file(data)
        before = path.stat()
        first = sha256_file(path)
        self.assertEqual(sha256_file(path), first)
        self.assertEqual(path.read_bytes(), data)
        after = path.stat()
        self.assertEqual(after.st_size, before.st_size)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            sha256_file(self.root / "missing.bin")

    def test_directory_raises_os_error(self):
        with self.assertRaises(OSError):
            sha256_file(self.root)

    def test_permission_error_is_propagated(self):
        failure = PermissionError("simulated access denial")
        with replace_file_open(failure=failure):
            with self.assertRaises(PermissionError) as caught:
                sha256_file(self.root / "denied.bin")
        self.assertIs(caught.exception, failure)

    def test_bounded_binary_reads_hash_entire_stream_and_close(self):
        data = bytes(range(256)) * (3 * MAX_READ_SIZE // 256) + b"tail"
        stream = CheckedStream(data)
        with replace_file_open(stream=stream):
            digest = sha256_file(self.root / "instrumented.bin")
        self.assertEqual(digest, hashlib.sha256(data).hexdigest())
        self.assertGreaterEqual(len(stream.read_sizes), 4)
        self.assertTrue(stream.closed, "Input stream must close after success")

    def test_read_error_is_propagated_and_stream_closes(self):
        failure = OSError("simulated read failure")
        stream = CheckedStream(b"abc", failure=failure)
        with replace_file_open(stream=stream):
            with self.assertRaises(OSError) as caught:
                sha256_file(self.root / "broken.bin")
        self.assertIs(caught.exception, failure)
        self.assertTrue(stream.closed, "Input stream must close after a read error")


if __name__ == "__main__":
    unittest.main()
