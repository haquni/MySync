"""Contract tests for UDP device discovery (TASK-009)."""

from dataclasses import FrozenInstanceError
import json
import socket
import subprocess
import sys
import unittest
from unittest import mock
from uuid import uuid4

from mysync.network import (
    DiscoveryAnnouncement,
    DiscoveryEndpoint,
    DiscoveryProtocolError,
    decode_announcement,
    encode_announcement,
)


MAX_DATAGRAM_SIZE = 2048


def announcement(*, device_id=None, device_name="Рабочая станция", tcp_port=32001):
    return DiscoveryAnnouncement(
        device_id=device_id or str(uuid4()),
        device_name=device_name,
        tcp_port=tcp_port,
    )


def decoded_json(value):
    return json.loads(encode_announcement(value).decode("utf-8"))


class DiscoveryCodecTests(unittest.TestCase):
    def test_unicode_round_trip_and_exact_v1_fields(self):
        original = announcement(device_name="Ноутбук ☃")
        encoded = encode_announcement(original)

        self.assertLessEqual(len(encoded), MAX_DATAGRAM_SIZE)
        self.assertEqual(decode_announcement(encoded), original)
        payload = json.loads(encoded.decode("utf-8"))
        self.assertEqual(
            set(payload),
            {"protocol", "version", "device_id", "device_name", "tcp_port"},
        )
        self.assertIsInstance(payload["protocol"], str)
        self.assertTrue(payload["protocol"])
        self.assertEqual(payload["version"], 1)

    def test_model_is_immutable(self):
        value = announcement()
        with self.assertRaises(FrozenInstanceError):
            value.device_name = "changed"

    def test_invalid_model_values(self):
        valid = {
            "device_id": str(uuid4()),
            "device_name": "PC",
            "tcp_port": 12345,
        }
        cases = (
            {"device_id": "not-a-uuid"},
            {"device_id": 1},
            {"device_name": ""},
            {"device_name": "   "},
            {"device_name": 1},
            {"tcp_port": 0},
            {"tcp_port": 65536},
            {"tcp_port": True},
            {"tcp_port": "12345"},
        )
        for update in cases:
            with self.subTest(update=update), self.assertRaises(DiscoveryProtocolError):
                DiscoveryAnnouncement(**(valid | update))

    def test_rejects_bad_utf8_json_and_non_object(self):
        for data in (b"\xff", b"{broken", b"[]", b"null"):
            with self.subTest(data=data), self.assertRaises(DiscoveryProtocolError):
                decode_announcement(data)

    def test_rejects_version_protocol_and_field_set_changes(self):
        valid = decoded_json(announcement())
        variants = (
            valid | {"version": 2},
            valid | {"version": True},
            valid | {"protocol": "other-protocol"},
            valid | {"extra": 1},
            {key: value for key, value in valid.items() if key != "tcp_port"},
        )
        for payload in variants:
            with self.subTest(payload=payload), self.assertRaises(DiscoveryProtocolError):
                decode_announcement(json.dumps(payload).encode("utf-8"))

    def test_rejects_duplicate_json_fields(self):
        valid = decoded_json(announcement())
        text = json.dumps(valid).replace(
            '"tcp_port":', '"tcp_port": 12345, "tcp_port":', 1
        )
        with self.assertRaises(DiscoveryProtocolError):
            decode_announcement(text.encode("utf-8"))

    def test_rejects_invalid_decoded_values(self):
        valid = decoded_json(announcement())
        variants = (
            valid | {"device_id": "bad"},
            valid | {"device_name": ""},
            valid | {"device_name": "   "},
            valid | {"tcp_port": 0},
            valid | {"tcp_port": 65536},
            valid | {"tcp_port": True},
        )
        for payload in variants:
            with self.subTest(payload=payload), self.assertRaises(DiscoveryProtocolError):
                decode_announcement(json.dumps(payload).encode("utf-8"))

    def test_rejects_oversized_datagram(self):
        oversized = b"{" + b" " * MAX_DATAGRAM_SIZE + b"}"
        self.assertGreater(len(oversized), MAX_DATAGRAM_SIZE)
        with self.assertRaises(DiscoveryProtocolError):
            decode_announcement(oversized)

        with self.assertRaises(DiscoveryProtocolError):
            encode_announcement(announcement(device_name="x" * MAX_DATAGRAM_SIZE))


class DiscoveryEndpointTests(unittest.TestCase):
    def endpoint(self, value):
        endpoint = DiscoveryEndpoint(
            value.device_id, bind_host="127.0.0.1", bind_port=0, timeout=1.0
        )
        self.addCleanup(endpoint.close)
        return endpoint

    def test_loopback_exchange_returns_announcement_and_sender_ip(self):
        receiver = self.endpoint(announcement(device_name="Получатель"))
        sender_value = announcement(device_name="Отправитель ☃", tcp_port=43210)
        sender = self.endpoint(sender_value)

        sender.send_announcement(sender_value, receiver.address)
        received, sender_ip = receiver.receive_announcement()

        self.assertEqual(received, sender_value)
        self.assertEqual(received.tcp_port, 43210)
        self.assertEqual(sender_ip, "127.0.0.1")

    def test_own_announcement_is_ignored(self):
        own = announcement()
        receiver = self.endpoint(own)
        other = announcement()
        raw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(raw.close)

        raw.sendto(encode_announcement(own), receiver.address)
        raw.sendto(encode_announcement(other), receiver.address)

        received, _ = receiver.receive_announcement()
        self.assertEqual(received, other)

    def test_invalid_packet_is_reported_as_protocol_error(self):
        receiver = self.endpoint(announcement())
        raw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(raw.close)
        raw.sendto(b"not-json", receiver.address)

        with self.assertRaises(DiscoveryProtocolError):
            receiver.receive_announcement()

    def test_context_manager_closes_endpoint_and_close_is_idempotent(self):
        with DiscoveryEndpoint(
            announcement().device_id, bind_host="127.0.0.1", bind_port=0
        ) as endpoint:
            address = endpoint.address
            self.assertEqual(address[0], "127.0.0.1")
        endpoint.close()
        with self.assertRaises(OSError):
            endpoint.send_announcement(announcement(), address)

    def test_socket_is_ipv4_udp_and_broadcast_enabled(self):
        fake_socket = mock.Mock()
        fake_socket.sendto.return_value = 1
        fake_socket.getsockname.return_value = ("0.0.0.0", 32002)
        value = announcement()
        with mock.patch("mysync.network.discovery.socket.socket", return_value=fake_socket) as factory:
            endpoint = DiscoveryEndpoint(value.device_id, bind_port=32002)
            endpoint.send_announcement(value, ("255.255.255.255", 32002))
            endpoint.close()

        factory.assert_called_once_with(socket.AF_INET, socket.SOCK_DGRAM)
        fake_socket.setsockopt.assert_called_once_with(
            socket.SOL_SOCKET, socket.SO_BROADCAST, 1
        )
        fake_socket.sendto.assert_called_once()


class DiscoveryImportTests(unittest.TestCase):
    def test_import_does_not_open_socket_or_start_thread(self):
        script = """
import socket
import threading
def fail(*args, **kwargs):
    raise AssertionError("network activity during import")
socket.socket = fail
threading.Thread.start = fail
import mysync.network.discovery
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
            "DiscoveryAnnouncement",
            "DiscoveryEndpoint",
            "DiscoveryProtocolError",
            "decode_announcement",
            "encode_announcement",
        ):
            self.assertTrue(hasattr(network, name), name)


if __name__ == "__main__":
    unittest.main()
