"""Contract tests for framed TCP service messages (TASK-009)."""

from dataclasses import FrozenInstanceError
import json
import socket
import struct
import subprocess
import sys
import unittest

from mysync.network import (
    ConnectionClosedError,
    MessageProtocolError,
    ServiceMessage,
    receive_message,
    send_message,
)


MAX_BODY_SIZE = 1024 * 1024


def message(**updates):
    values = {
        "type": "sync.offer",
        "request_id": "request-001",
        "payload": {"path": "данные/файл.txt", "items": [1, True, None, {"x": "☃"}]},
    }
    return ServiceMessage(**(values | updates))


def frame(body):
    return struct.pack(">I", len(body)) + body


def json_frame(payload):
    return frame(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class RecordingSocket:
    def __init__(self):
        self.sent = []

    def sendall(self, data):
        self.sent.append(bytes(data))

    def send(self, data):  # pragma: no cover - a failure guard
        raise AssertionError("send_message must use sendall, not send")


class PartialRecvSocket:
    def __init__(self, data, chunk_sizes=(1, 2, 3)):
        self.data = bytearray(data)
        self.chunk_sizes = iter(chunk_sizes)

    def recv(self, size):
        if not self.data:
            return b""
        try:
            requested = next(self.chunk_sizes)
        except StopIteration:
            requested = 1
        count = min(size, requested, len(self.data))
        result = bytes(self.data[:count])
        del self.data[:count]
        return result


class MessageModelAndSendTests(unittest.TestCase):
    def test_unicode_nested_payload_round_trip_and_big_endian_header(self):
        original = message()
        recorder = RecordingSocket()
        send_message(recorder, original)

        self.assertEqual(len(recorder.sent), 1)
        wire = recorder.sent[0]
        body_size = struct.unpack(">I", wire[:4])[0]
        self.assertEqual(body_size, len(wire) - 4)
        payload = json.loads(wire[4:].decode("utf-8"))
        self.assertEqual(
            set(payload), {"version", "type", "request_id", "payload"}
        )
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["type"], original.type)
        self.assertEqual(payload["request_id"], original.request_id)
        self.assertEqual(payload["payload"], original.payload)

        decoded = receive_message(PartialRecvSocket(wire))
        self.assertEqual(decoded, original)

    def test_model_is_immutable(self):
        value = message()
        with self.assertRaises(FrozenInstanceError):
            value.request_id = "changed"

    def test_invalid_model_fields(self):
        cases = (
            {"type": ""},
            {"type": "contains space"},
            {"type": "НЕбезопасно"},
            {"type": 1},
            {"request_id": ""},
            {"request_id": "   "},
            {"request_id": 1},
            {"payload": []},
            {"payload": None},
        )
        for update in cases:
            with self.subTest(update=update), self.assertRaises(MessageProtocolError):
                message(**update)

    def test_rejects_nan_infinity_and_unsupported_json_values(self):
        for payload in (
            {"value": float("nan")},
            {"value": float("inf")},
            {"value": float("-inf")},
            {"value": {1, 2}},
            {1: "non-string-key"},
        ):
            with self.subTest(payload=payload), self.assertRaises(MessageProtocolError):
                send_message(RecordingSocket(), message(payload=payload))

    def test_rejects_body_larger_than_one_mib(self):
        with self.assertRaises(MessageProtocolError):
            send_message(
                RecordingSocket(), message(payload={"data": "x" * MAX_BODY_SIZE})
            )


class MessageReceiveTests(unittest.TestCase):
    def valid_payload(self):
        recorder = RecordingSocket()
        send_message(recorder, message())
        return json.loads(recorder.sent[0][4:].decode("utf-8"))

    def test_two_sequential_frames_do_not_mix(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        first = message(request_id="one")
        second = message(request_id="two", payload={"sequence": 2})

        send_message(left, first)
        send_message(left, second)

        self.assertEqual(receive_message(right), first)
        self.assertEqual(receive_message(right), second)

    def test_socket_remains_owned_by_caller(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)

        send_message(left, message())
        self.assertEqual(receive_message(right), message())
        self.assertNotEqual(left.fileno(), -1)
        self.assertNotEqual(right.fileno(), -1)
        left.sendall(b"x")
        self.assertEqual(right.recv(1), b"x")

    def test_empty_and_oversized_lengths_are_rejected_before_body_read(self):
        for size in (0, MAX_BODY_SIZE + 1, 0xFFFFFFFF):
            with self.subTest(size=size), self.assertRaises(MessageProtocolError):
                receive_message(PartialRecvSocket(struct.pack(">I", size)))

    def test_connection_close_before_or_during_frame_has_separate_error(self):
        valid = json_frame(self.valid_payload())
        truncated = (
            b"",
            valid[:2],
            valid[:4],
            valid[:-1],
        )
        for data in truncated:
            with self.subTest(length=len(data)), self.assertRaises(ConnectionClosedError):
                receive_message(PartialRecvSocket(data))

    def test_rejects_bad_utf8_json_and_non_object(self):
        bodies = (b"\xff", b"{broken", b"[]", b"null")
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(MessageProtocolError):
                receive_message(PartialRecvSocket(frame(body)))

    def test_rejects_version_field_set_and_field_types(self):
        valid = self.valid_payload()
        variants = (
            valid | {"version": 2},
            valid | {"version": True},
            valid | {"extra": 1},
            {key: value for key, value in valid.items() if key != "payload"},
            valid | {"type": ""},
            valid | {"type": "unsafe type"},
            valid | {"request_id": ""},
            valid | {"request_id": 1},
            valid | {"payload": []},
        )
        for payload in variants:
            with self.subTest(payload=payload), self.assertRaises(MessageProtocolError):
                receive_message(PartialRecvSocket(json_frame(payload)))

    def test_rejects_duplicate_json_fields(self):
        valid = self.valid_payload()
        text = json.dumps(valid).replace(
            '"request_id":', '"request_id": "duplicate", "request_id":', 1
        )
        body = text.encode("utf-8")
        with self.assertRaises(MessageProtocolError):
            receive_message(PartialRecvSocket(frame(body)))

    def test_rejects_non_standard_json_numbers_on_receive(self):
        valid = self.valid_payload()
        for token in ("NaN", "Infinity", "-Infinity"):
            text = json.dumps(valid, ensure_ascii=False).replace(
                '"payload": {', f'"payload": {{"bad": {token}, '
            )
            body = text.encode("utf-8")
            with self.subTest(token=token), self.assertRaises(MessageProtocolError):
                receive_message(PartialRecvSocket(frame(body)))


class MessageImportTests(unittest.TestCase):
    def test_import_does_not_open_socket_or_start_thread(self):
        script = """
import socket
import threading
def fail(*args, **kwargs):
    raise AssertionError("network activity during import")
socket.socket = fail
threading.Thread.start = fail
import mysync.network.messages
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_public_api_is_reexported(self):
        import mysync.network as network

        for name in (
            "ConnectionClosedError",
            "MessageProtocolError",
            "ServiceMessage",
            "receive_message",
            "send_message",
        ):
            self.assertTrue(hasattr(network, name), name)


if __name__ == "__main__":
    unittest.main()
