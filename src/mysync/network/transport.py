"""Portable TCP listener and client helpers for MySync sessions."""

from __future__ import annotations

import socket


DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_SESSION_TIMEOUT = 1.0


def choose_free_tcp_port(bind_host: str = "0.0.0.0") -> int:
    """Ask the operating system for a currently free IPv4 TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((bind_host, 0))
        return int(listener.getsockname()[1])


def connect_tcp(
    host: str,
    port: int,
    *,
    timeout: float = DEFAULT_CONNECT_TIMEOUT,
) -> socket.socket:
    """Connect to an IPv4/IPv6 host and return a blocking TCP socket."""
    connection = socket.create_connection((host, port), timeout=timeout)
    connection.settimeout(DEFAULT_SESSION_TIMEOUT)
    return connection


class TcpListener:
    """Explicitly owned TCP listener with deterministic cleanup."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        backlog: int = 5,
        timeout: float | None = 0.5,
    ) -> None:
        if not isinstance(host, str):
            raise TypeError("host must be a string")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("port must be an integer from 0 to 65535")
        if isinstance(backlog, bool) or not isinstance(backlog, int) or backlog <= 0:
            raise ValueError("backlog must be a positive integer")

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((host, port))
            listener.listen(backlog)
            listener.settimeout(timeout)
        except BaseException:
            listener.close()
            raise
        self._socket: socket.socket | None = listener

    @property
    def address(self) -> tuple[str, int]:
        listener = self._require_socket()
        address = listener.getsockname()
        return str(address[0]), int(address[1])

    @property
    def closed(self) -> bool:
        return self._socket is None

    def _require_socket(self) -> socket.socket:
        if self._socket is None:
            raise OSError("TCP listener is closed")
        return self._socket

    def accept(self) -> tuple[socket.socket, tuple[str, int]]:
        connection, address = self._require_socket().accept()
        connection.settimeout(DEFAULT_SESSION_TIMEOUT)
        return connection, (str(address[0]), int(address[1]))

    def close(self) -> None:
        listener = self._socket
        if listener is not None:
            self._socket = None
            listener.close()

    def __enter__(self) -> "TcpListener":
        self._require_socket()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


__all__ = (
    "DEFAULT_CONNECT_TIMEOUT",
    "DEFAULT_SESSION_TIMEOUT",
    "TcpListener",
    "choose_free_tcp_port",
    "connect_tcp",
)
