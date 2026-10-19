"""Integration tests for discovery and synchronization services (TASK-011)."""

from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from mysync.application.service import DiscoveredDevice, find_devices, serve, sync_with_device
from mysync.application.session import PeerIdentity, SessionProtocolError, run_server_session
from mysync.network.discovery import DiscoveryAnnouncement, DiscoveryResponder
from mysync.network.transport import TcpListener, choose_free_tcp_port
from mysync.storage.config import AppConfig


def free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_socket:
        udp_socket.bind(("127.0.0.1", 0))
        return int(udp_socket.getsockname()[1])


def config(root, name, *, tcp_port=None, udp_port=None):
    return AppConfig(
        root_path=str(root),
        device_id=str(uuid4()),
        device_name=name,
        tcp_port=tcp_port or choose_free_tcp_port("127.0.0.1"),
        udp_port=udp_port or free_udp_port(),
        auto_sync=False,
    )


class ServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.client_root = base / "client"
        self.server_root = base / "server"
        self.client_root.mkdir()
        self.server_root.mkdir()

    def test_real_udp_request_reply_discovers_server(self):
        udp_port = free_udp_port()
        local = config(self.client_root, "Client", udp_port=udp_port)
        remote = config(self.server_root, "Server", udp_port=udp_port)
        responder = DiscoveryResponder(
            DiscoveryAnnouncement(remote.device_id, remote.device_name, remote.tcp_port),
            bind_host="127.0.0.1",
            udp_port=udp_port,
            timeout=1.0,
        )
        errors = []

        def reply():
            try:
                self.assertTrue(responder.respond_once())
            except BaseException as error:
                errors.append(error)
            finally:
                responder.close()

        thread = threading.Thread(target=reply, name="udp-responder-test")
        thread.start()
        found = find_devices(local, timeout=0.3, host="127.0.0.1")
        thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive(), "UDP responder must not hang")
        if errors:
            raise errors[0]
        self.assertEqual(
            found,
            (
                DiscoveredDevice(
                    remote.device_id, remote.device_name, "127.0.0.1", remote.tcp_port
                ),
            ),
        )

    def test_discovery_timeout_without_server_returns_empty(self):
        local = config(self.client_root, "Client", udp_port=free_udp_port())
        started = time.monotonic()
        self.assertEqual(
            find_devices(local, timeout=0.15, host="127.0.0.1"), ()
        )
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.10)
        self.assertLess(elapsed, 1.5)

    def test_serve_once_is_discovered_and_syncs_both_directions(self):
        udp_port = free_udp_port()
        client = config(self.client_root, "Client", udp_port=udp_port)
        server = config(self.server_root, "Server", udp_port=udp_port)
        (self.client_root / "from-client.txt").write_bytes(b"client")
        (self.server_root / "from-server.txt").write_bytes(b"server")
        results = []
        errors = []

        def run_server():
            try:
                serve(
                    server,
                    once=True,
                    bind_host="127.0.0.1",
                    on_result=results.append,
                )
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=run_server, name="serve-once-test")
        thread.start()
        devices = ()
        deadline = time.monotonic() + 4.0
        while not devices and time.monotonic() < deadline:
            devices = find_devices(client, timeout=0.15, host="127.0.0.1")
        self.assertEqual(len(devices), 1)
        client_result = sync_with_device(client, devices[0])
        thread.join(timeout=8.0)

        self.assertFalse(thread.is_alive(), "serve --once must exit after one session")
        if errors:
            raise errors[0]
        self.assertEqual(len(results), 1)
        self.assertEqual(client_result.peer.device_id, server.device_id)
        self.assertEqual((self.client_root / "from-server.txt").read_bytes(), b"server")
        self.assertEqual((self.server_root / "from-client.txt").read_bytes(), b"client")

    def test_occupied_udp_port_is_reported_and_original_socket_survives(self):
        udp_port = free_udp_port()
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(blocker.close)
        blocker.bind(("127.0.0.1", udp_port))
        announcement = DiscoveryAnnouncement(str(uuid4()), "Blocked", 12345)
        with self.assertRaises(OSError):
            DiscoveryResponder(
                announcement, bind_host="127.0.0.1", udp_port=udp_port
            )
        blocker.sendto(b"still-open", blocker.getsockname())
        self.assertEqual(blocker.recvfrom(32)[0], b"still-open")

    def test_sync_to_unavailable_tcp_port_fails_promptly(self):
        client = config(self.client_root, "Client", udp_port=free_udp_port())
        unavailable = choose_free_tcp_port("127.0.0.1")
        device = DiscoveredDevice(str(uuid4()), "Gone", "127.0.0.1", unavailable)
        started = time.monotonic()
        with self.assertRaises(OSError):
            sync_with_device(client, device)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_sync_rejects_tcp_peer_that_does_not_match_discovery_identity(self):
        client = config(self.client_root, "Client", udp_port=free_udp_port())
        actual_identity = PeerIdentity(str(uuid4()), "Actual server")
        claimed_identity = str(uuid4())
        (self.client_root / "private.txt").write_bytes(b"must not be disclosed")
        listener = TcpListener("127.0.0.1", 0, timeout=2.0)
        self.addCleanup(listener.close)
        server_errors = []

        def server():
            try:
                connection, _address = listener.accept()
                with connection:
                    connection.settimeout(3.0)
                    run_server_session(connection, self.server_root, actual_identity)
            except BaseException as error:
                server_errors.append(error)

        thread = threading.Thread(target=server, name="identity-mismatch-server")
        thread.start()
        advertised = DiscoveredDevice(
            claimed_identity, "Claimed server", "127.0.0.1", listener.address[1]
        )
        caught = None
        try:
            sync_with_device(client, advertised)
        except BaseException as error:
            caught = error
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive(), "identity mismatch must close the session")
        self.assertFalse((self.server_root / "private.txt").exists())
        self.assertIsInstance(caught, SessionProtocolError)


if __name__ == "__main__":
    unittest.main()
