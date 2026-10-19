"""Acceptance tests for the TCP transport helpers (TASK-011)."""

import socket
import threading
import unittest

from mysync.network.transport import TcpListener, choose_free_tcp_port, connect_tcp


class TcpTransportTests(unittest.TestCase):
    def test_loopback_listener_and_client_exchange(self):
        errors = []
        with TcpListener("127.0.0.1", 0, timeout=2.0) as listener:
            accepted = []

            def server():
                try:
                    connection, address = listener.accept()
                    with connection:
                        connection.settimeout(2.0)
                        accepted.append(address)
                        self.assertEqual(connection.recv(4), b"ping")
                        connection.sendall(b"pong")
                except BaseException as error:  # surfaced in the main test thread
                    errors.append(error)

            thread = threading.Thread(target=server, name="transport-test-server")
            thread.start()
            with connect_tcp("127.0.0.1", listener.address[1], timeout=2.0) as client:
                client.settimeout(2.0)
                client.sendall(b"ping")
                self.assertEqual(client.recv(4), b"pong")
            thread.join(timeout=3.0)

        self.assertFalse(thread.is_alive(), "loopback exchange must not hang")
        if errors:
            raise errors[0]
        self.assertEqual(accepted[0][0], "127.0.0.1")
        self.assertTrue(listener.closed)

    def test_choose_free_port_returns_bindable_ipv4_port(self):
        port = choose_free_tcp_port("127.0.0.1")
        self.assertIsInstance(port, int)
        self.assertGreater(port, 0)
        with TcpListener("127.0.0.1", port) as listener:
            self.assertEqual(listener.address, ("127.0.0.1", port))

    def test_occupied_port_is_rejected_without_leaking_listener(self):
        with TcpListener("127.0.0.1", 0) as first:
            with self.assertRaises(OSError):
                TcpListener("127.0.0.1", first.address[1])

    def test_close_is_idempotent_and_closed_listener_rejects_operations(self):
        listener = TcpListener("127.0.0.1", 0)
        listener.close()
        listener.close()
        self.assertTrue(listener.closed)
        with self.assertRaises(OSError):
            _ = listener.address
        with self.assertRaises(OSError):
            listener.accept()

    def test_invalid_listener_arguments_are_rejected(self):
        cases = (
            (1, 1, 5),
            ("127.0.0.1", -1, 5),
            ("127.0.0.1", 65536, 5),
            ("127.0.0.1", True, 5),
            ("127.0.0.1", 0, 0),
            ("127.0.0.1", 0, True),
        )
        for host, port, backlog in cases:
            with self.subTest(host=host, port=port, backlog=backlog):
                with self.assertRaises((TypeError, ValueError)):
                    TcpListener(host, port, backlog=backlog)

    def test_failed_connection_returns_os_error_with_timeout(self):
        port = choose_free_tcp_port("127.0.0.1")
        with self.assertRaises(OSError):
            connect_tcp("127.0.0.1", port, timeout=0.2)


if __name__ == "__main__":
    unittest.main()
