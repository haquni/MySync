"""Acceptance tests for bounded raw file transfer (TASK-011)."""

import hashlib
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from mysync.filesystem.receiver import UnsafePathError, receive_new_file
from mysync.filesystem.scanner import FileChangedError, FileEntry, scan_directory
from mysync.network.messages import ConnectionClosedError
from mysync.network.transfer import FILE_CHUNK_SIZE, receive_file_chunks, send_file_bytes


class RecordingSocket:
    def __init__(self, on_first_send=None):
        self.data = bytearray()
        self.on_first_send = on_first_send
        self.calls = 0

    def sendall(self, data):
        self.calls += 1
        self.data.extend(data)
        if self.calls == 1 and self.on_first_send is not None:
            self.on_first_send()


class FileTransferTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def entry(self, relative_path):
        snapshot = scan_directory(self.root)
        return next(item for item in snapshot.files if item.relative_path == relative_path)

    def test_streams_large_binary_exactly_and_preserves_following_frame_bytes(self):
        content = bytes(range(256)) * (FILE_CHUNK_SIZE // 256 + 513)
        path = self.root / "данные large.bin"
        path.write_bytes(content)
        entry = self.entry(path.name)
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        left.settimeout(5.0)
        right.settimeout(5.0)
        errors = []

        def sender():
            try:
                send_file_bytes(left, self.root, entry)
                left.sendall(b"NEXT")
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=sender, name="file-transfer-sender")
        thread.start()
        received = b"".join(receive_file_chunks(right, len(content), chunk_size=65537))
        self.assertEqual(right.recv(4), b"NEXT")
        thread.join(timeout=6.0)

        self.assertFalse(thread.is_alive(), "file sender must not hang")
        if errors:
            raise errors[0]
        self.assertEqual(received, content)
        self.assertEqual(hashlib.sha256(received).hexdigest(), entry.sha256)

    def test_empty_file_sends_no_raw_bytes(self):
        (self.root / "empty.txt").write_bytes(b"")
        recorder = RecordingSocket()
        send_file_bytes(recorder, self.root, self.entry("empty.txt"))
        self.assertEqual(recorder.data, b"")
        self.assertEqual(list(receive_file_chunks(_NoRecvSocket(), 0)), [])

    def test_premature_eof_reports_remaining_bytes(self):
        left, right = socket.socketpair()
        self.addCleanup(right.close)
        left.sendall(b"short")
        left.close()
        with self.assertRaises(ConnectionClosedError) as caught:
            b"".join(receive_file_chunks(right, 10, chunk_size=2))
        self.assertIn("5 file bytes remaining", str(caught.exception))

    def test_source_content_changed_after_scan_is_rejected(self):
        path = self.root / "changed.bin"
        path.write_bytes(b"AAAA")
        entry = self.entry(path.name)
        path.write_bytes(b"BBBB")
        with self.assertRaises(FileChangedError):
            send_file_bytes(RecordingSocket(), self.root, entry)

    def test_source_changed_during_send_is_rejected(self):
        path = self.root / "changing.bin"
        path.write_bytes(b"x" * (FILE_CHUNK_SIZE + 10))
        entry = self.entry(path.name)

        def mutate():
            path.write_bytes(b"y" * len(path.read_bytes()))

        with self.assertRaises(FileChangedError):
            send_file_bytes(RecordingSocket(mutate), self.root, entry)

    def test_source_mtime_changed_after_scan_is_rejected(self):
        path = self.root / "retouched.txt"
        path.write_bytes(b"same bytes")
        entry = self.entry(path.name)
        changed_mtime = entry.mtime_ns + 10_000_000
        os.utime(path, ns=(changed_mtime, changed_mtime))
        with self.assertRaises(FileChangedError):
            send_file_bytes(RecordingSocket(), self.root, entry)

    def test_unsafe_and_symbolic_source_paths_are_rejected(self):
        digest = hashlib.sha256(b"x").hexdigest()
        for relative in ("../escape", "/absolute", "a\\b", "a/../b"):
            with self.subTest(relative=relative), self.assertRaises(UnsafePathError):
                send_file_bytes(
                    RecordingSocket(), self.root, FileEntry(relative, 1, 1, digest)
                )

        target = self.root / "target.txt"
        target.write_bytes(b"x")
        link = self.root / "link.txt"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlink unavailable: {error}")
        with self.assertRaises(UnsafePathError):
            send_file_bytes(
                RecordingSocket(), self.root, FileEntry("link.txt", 1, 1, digest)
            )

    def test_invalid_receive_sizes_are_rejected(self):
        for size in (-1, True, 1.5):
            with self.subTest(size=size), self.assertRaises(ValueError):
                next(receive_file_chunks(_NoRecvSocket(), size))

    def test_concurrently_created_destination_is_never_overwritten(self):
        content = b"received"
        target = self.root / "race.txt"
        real_link = os.link

        def create_user_file_then_link(source, destination, *args, **kwargs):
            Path(destination).write_bytes(b"user-created")
            return real_link(source, destination, *args, **kwargs)

        with patch(
            "mysync.filesystem.receiver.os.link",
            side_effect=create_user_file_then_link,
        ):
            caught = None
            try:
                receive_new_file(
                    self.root,
                    "race.txt",
                    [content],
                    hashlib.sha256(content).hexdigest(),
                )
            except BaseException as error:
                caught = error
        self.assertEqual(target.read_bytes(), b"user-created")
        self.assertIsInstance(caught, FileExistsError)


class _NoRecvSocket:
    def recv(self, size):  # pragma: no cover - must never be called
        raise AssertionError("recv must not be called")


if __name__ == "__main__":
    unittest.main()
