"""End-to-end and protocol acceptance tests for sync sessions (TASK-011)."""

import hashlib
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from uuid import uuid4

from mysync.application.session import (
    PeerIdentity,
    SessionProtocolError,
    run_client_session,
    run_server_session,
)
from mysync.network.messages import ServiceMessage, receive_message, send_message


def identity(name):
    return PeerIdentity(str(uuid4()), name)


class SessionAcceptanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.left = base / "left"
        self.right = base / "right"
        self.left.mkdir()
        self.right.mkdir()
        self.left_identity = identity("Левый узел")
        self.right_identity = identity("Правый узел")

    @staticmethod
    def write(root, relative, content):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def run_pair(self):
        client_socket, server_socket = socket.socketpair()
        client_socket.settimeout(8.0)
        server_socket.settimeout(8.0)
        server_results = []
        server_errors = []

        def server():
            try:
                server_results.append(
                    run_server_session(server_socket, self.right, self.right_identity)
                )
            except BaseException as error:
                server_errors.append(error)
            finally:
                server_socket.close()

        thread = threading.Thread(target=server, name="session-test-server")
        thread.start()
        try:
            client_result = run_client_session(
                client_socket, self.left, self.left_identity
            )
        finally:
            client_socket.close()
        thread.join(timeout=10.0)
        self.assertFalse(thread.is_alive(), "sync session must not hang")
        if server_errors:
            raise server_errors[0]
        self.assertEqual(len(server_results), 1)
        return client_result, server_results[0]

    def test_two_way_union_large_unicode_empty_and_conflicts_then_idempotent(self):
        large = bytes(range(256)) * (1024 * 1024 // 256 + 257)
        self.write(self.left, "left-only.txt", b"left")
        self.write(self.left, "nested/пустой.txt", b"")
        self.write(self.left, "binary/large.bin", large)
        self.write(self.right, "right-only.txt", b"right")
        self.write(self.right, "日本語/данные.bin", b"\x00\xffright")
        self.write(self.left, "same.txt", b"same")
        self.write(self.right, "same.txt", b"same")
        self.write(self.left, "conflict.txt", b"left version")
        self.write(self.right, "conflict.txt", b"right version")
        self.write(self.left, "shape", b"left file")
        (self.right / "shape").mkdir()
        self.write(self.right, "shape/child.txt", b"must stay right only")
        (self.left / "left-empty").mkdir()
        (self.right / "right-empty").mkdir()

        client, server = self.run_pair()

        self.assertEqual((self.left / "right-only.txt").read_bytes(), b"right")
        self.assertEqual((self.right / "left-only.txt").read_bytes(), b"left")
        self.assertEqual((self.right / "nested/пустой.txt").read_bytes(), b"")
        self.assertEqual((self.right / "binary/large.bin").read_bytes(), large)
        self.assertEqual((self.left / "日本語/данные.bin").read_bytes(), b"\x00\xffright")
        self.assertTrue((self.left / "right-empty").is_dir())
        self.assertTrue((self.right / "left-empty").is_dir())
        self.assertEqual((self.left / "conflict.txt").read_bytes(), b"left version")
        self.assertEqual((self.right / "conflict.txt").read_bytes(), b"right version")
        self.assertTrue((self.left / "shape").is_file())
        self.assertEqual((self.left / "shape").read_bytes(), b"left file")
        self.assertEqual((self.right / "shape/child.txt").read_bytes(), b"must stay right only")
        self.assertEqual(set(client.conflicts), {"conflict.txt", "shape"})
        self.assertEqual(set(server.conflicts), {"conflict.txt", "shape"})
        self.assertNotIn("same.txt", client.copied_to_local + client.copied_to_remote)

        second_client, second_server = self.run_pair()
        self.assertEqual(second_client.copied_to_local, ())
        self.assertEqual(second_client.copied_to_remote, ())
        self.assertEqual(second_client.created_on_local, ())
        self.assertEqual(second_client.created_on_remote, ())
        self.assertEqual(second_server.copied_to_local, ())
        self.assertEqual(second_server.copied_to_remote, ())
        self.assertEqual(set(second_client.conflicts), {"conflict.txt", "shape"})

    def test_server_rejects_wrong_initial_command(self):
        peer, server = socket.socketpair()
        self.addCleanup(peer.close)
        self.addCleanup(server.close)
        peer.sendall(_encoded(ServiceMessage("sync.snapshot", "request", {})))
        with self.assertRaises(SessionProtocolError):
            run_server_session(server, self.right, self.right_identity)

    def test_server_rejects_wrong_request_id(self):
        error = self._drive_server_until_snapshot(
            lambda peer, request_id: send_message(
                peer,
                ServiceMessage(
                    "sync.snapshot", "another-request", {"files": [], "directories": []}
                ),
            )
        )
        self.assertIsInstance(error, SessionProtocolError)
        self.assertIn("request_id", str(error))

    def test_server_rejects_traversal_and_backslash_in_snapshot(self):
        for unsafe in ("../escape", "/absolute", "folder\\file"):
            with self.subTest(unsafe=unsafe):
                payload = {
                    "files": [
                        {
                            "path": unsafe,
                            "size": 0,
                            "mtime_ns": 0,
                            "sha256": hashlib.sha256(b"").hexdigest(),
                        }
                    ],
                    "directories": [],
                }
                error = self._drive_server_until_snapshot(
                    lambda peer, request_id, payload=payload: send_message(
                        peer, ServiceMessage("sync.snapshot", request_id, payload)
                    )
                )
                self.assertIsInstance(error, SessionProtocolError)
                self.assertIn("unsafe file path", str(error))

    def test_server_rejects_early_phase_completion(self):
        digest = hashlib.sha256(b"x").hexdigest()
        snapshot = {
            "files": [
                {"path": "expected.txt", "size": 1, "mtime_ns": 1, "sha256": digest}
            ],
            "directories": [],
        }

        def action(peer, request_id):
            send_message(peer, ServiceMessage("sync.snapshot", request_id, snapshot))
            received = receive_message(peer)
            self.assertEqual(received.type, "sync.snapshot")
            send_message(peer, ServiceMessage("phase.complete", request_id, {}))

        error = self._drive_server_until_snapshot(action)
        self.assertIsInstance(error, SessionProtocolError)
        self.assertIn("before all expected items", str(error))

    def test_server_rejects_unexpected_file_offer(self):
        digest = hashlib.sha256(b"x").hexdigest()
        expected = {
            "files": [
                {"path": "expected.txt", "size": 1, "mtime_ns": 1, "sha256": digest}
            ],
            "directories": [],
        }
        unexpected = {
            "path": "other.txt", "size": 1, "mtime_ns": 1, "sha256": digest
        }

        def action(peer, request_id):
            send_message(peer, ServiceMessage("sync.snapshot", request_id, expected))
            received = receive_message(peer)
            self.assertEqual(received.type, "sync.snapshot")
            send_message(peer, ServiceMessage("file.offer", request_id, unexpected))

        error = self._drive_server_until_snapshot(action)
        self.assertIsInstance(error, SessionProtocolError)
        self.assertIn("unexpected file offer", str(error))
        self.assertFalse((self.right / "other.txt").exists())

    def _drive_server_until_snapshot(self, action):
        peer, server = socket.socketpair()
        peer.settimeout(3.0)
        server.settimeout(3.0)
        request_id = str(uuid4())
        errors = []

        def target():
            try:
                run_server_session(server, self.right, self.right_identity)
            except BaseException as error:
                errors.append(error)
            finally:
                server.close()

        thread = threading.Thread(target=target, name="invalid-session-server")
        thread.start()
        send_message(
            peer,
            ServiceMessage(
                "session.hello",
                request_id,
                {
                    "protocol": 1,
                    "device_id": self.left_identity.device_id,
                    "device_name": self.left_identity.device_name,
                },
            ),
        )
        hello = receive_message(peer)
        self.assertEqual(hello.type, "session.hello")
        try:
            action(peer, request_id)
        finally:
            peer.close()
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive(), "rejected session must terminate")
        self.assertEqual(len(errors), 1)
        return errors[0]


def _encoded(message):
    from mysync.network.messages import encode_message

    return encode_message(message)


if __name__ == "__main__":
    unittest.main()
