"""UDP announcements used to discover MySync devices on an IPv4 LAN."""

from dataclasses import dataclass
import json
import socket
import time
from typing import Any
from uuid import UUID

from mysync.core.identity import validate_device_name


DISCOVERY_PROTOCOL = "mysync-discovery"
DISCOVERY_VERSION = 1
MAX_DATAGRAM_SIZE = 2048
DEFAULT_DISCOVERY_PORT = 37841

_ANNOUNCEMENT_FIELDS = {
    "protocol",
    "version",
    "device_id",
    "device_name",
    "tcp_port",
}


class DiscoveryProtocolError(ValueError):
    """A UDP datagram is not a valid MySync discovery announcement."""


def _validate_device_id(value: object) -> str:
    if not isinstance(value, str):
        raise DiscoveryProtocolError("device_id must be a UUID string")
    try:
        UUID(value)
    except (ValueError, AttributeError) as error:
        raise DiscoveryProtocolError("device_id must be a UUID string") from error
    return value


def _validate_port(value: object, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= 65535
    ):
        raise DiscoveryProtocolError(f"{name} must be an integer from 1 to 65535")
    return value


@dataclass(frozen=True)
class DiscoveryAnnouncement:
    """The version-1 identity information broadcast by one MySync device."""

    device_id: str
    device_name: str
    tcp_port: int

    def __post_init__(self) -> None:
        _validate_device_id(self.device_id)
        try:
            validate_device_name(self.device_name)
        except ValueError as error:
            raise DiscoveryProtocolError(str(error)) from error
        _validate_port(self.tcp_port, "tcp_port")


def _reject_constant(value: str) -> None:
    raise DiscoveryProtocolError(f"non-finite JSON number is not allowed: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DiscoveryProtocolError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def encode_announcement(announcement: DiscoveryAnnouncement) -> bytes:
    """Serialize an announcement to one size-limited UTF-8 JSON datagram."""
    if not isinstance(announcement, DiscoveryAnnouncement):
        raise TypeError("announcement must be a DiscoveryAnnouncement")
    payload = {
        "protocol": DISCOVERY_PROTOCOL,
        "version": DISCOVERY_VERSION,
        "device_id": announcement.device_id,
        "device_name": announcement.device_name,
        "tcp_port": announcement.tcp_port,
    }
    data = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(data) > MAX_DATAGRAM_SIZE:
        raise DiscoveryProtocolError(
            f"announcement exceeds {MAX_DATAGRAM_SIZE} bytes"
        )
    return data


def decode_announcement(data: bytes) -> DiscoveryAnnouncement:
    """Parse and strictly validate one version-1 discovery datagram."""
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    if len(data) > MAX_DATAGRAM_SIZE:
        raise DiscoveryProtocolError(
            f"announcement exceeds {MAX_DATAGRAM_SIZE} bytes"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DiscoveryProtocolError("announcement is not valid UTF-8") from error
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as error:
        raise DiscoveryProtocolError("announcement is not valid JSON") from error
    if not isinstance(payload, dict):
        raise DiscoveryProtocolError("announcement must be a JSON object")
    if set(payload) != _ANNOUNCEMENT_FIELDS:
        missing = sorted(_ANNOUNCEMENT_FIELDS - set(payload))
        extra = sorted(set(payload) - _ANNOUNCEMENT_FIELDS)
        raise DiscoveryProtocolError(
            f"announcement fields differ; missing={missing}, extra={extra}"
        )
    if payload["protocol"] != DISCOVERY_PROTOCOL:
        raise DiscoveryProtocolError("unknown discovery protocol marker")
    version = payload["version"]
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != DISCOVERY_VERSION
    ):
        raise DiscoveryProtocolError(f"unsupported discovery version: {version!r}")
    return DiscoveryAnnouncement(
        device_id=_validate_device_id(payload["device_id"]),
        device_name=payload["device_name"],
        tcp_port=_validate_port(payload["tcp_port"], "tcp_port"),
    )


class DiscoveryEndpoint:
    """An explicitly managed IPv4 UDP socket for discovery traffic."""

    def __init__(
        self,
        local_device_id: str,
        bind_host: str = "0.0.0.0",
        bind_port: int = 0,
        *,
        timeout: float | None = None,
    ) -> None:
        self._local_device_id = _validate_device_id(local_device_id)
        if not isinstance(bind_host, str):
            raise TypeError("bind_host must be a string")
        if (
            isinstance(bind_port, bool)
            or not isinstance(bind_port, int)
            or not 0 <= bind_port <= 65535
        ):
            raise ValueError("bind_port must be an integer from 0 to 65535")

        udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            udp_socket.bind((bind_host, bind_port))
            udp_socket.settimeout(timeout)
        except BaseException:
            udp_socket.close()
            raise
        self._socket: socket.socket | None = udp_socket

    @property
    def address(self) -> tuple[str, int]:
        """Return the endpoint's bound IPv4 address and UDP port."""
        return self._require_socket().getsockname()[:2]

    @property
    def closed(self) -> bool:
        return self._socket is None

    def _require_socket(self) -> socket.socket:
        if self._socket is None:
            raise OSError("discovery endpoint is closed")
        return self._socket

    def send_announcement(
        self,
        announcement: DiscoveryAnnouncement,
        destination: tuple[str, int] | None = None,
    ) -> None:
        """Send one announcement to a broadcast or explicit IPv4 destination."""
        if destination is None:
            destination = ("255.255.255.255", self.address[1])
        if not isinstance(destination, tuple) or len(destination) != 2:
            raise TypeError("destination must be a (host, port) tuple")
        host, port = destination
        if not isinstance(host, str):
            raise TypeError("destination host must be a string")
        _validate_port(port, "destination port")
        sent = self._require_socket().sendto(
            encode_announcement(announcement), destination
        )
        if sent == 0:
            raise OSError("UDP socket sent no data")

    def receive_announcement(self) -> tuple[DiscoveryAnnouncement, str]:
        """Receive the next non-local announcement and its sender IP address."""
        announcement, sender = self.receive_announcement_from()
        return announcement, sender[0]

    def receive_announcement_from(
        self,
    ) -> tuple[DiscoveryAnnouncement, tuple[str, int]]:
        """Receive a non-local announcement and its complete sender address."""
        udp_socket = self._require_socket()
        while True:
            data, sender = udp_socket.recvfrom(MAX_DATAGRAM_SIZE + 1)
            announcement = decode_announcement(data)
            if UUID(announcement.device_id) != UUID(self._local_device_id):
                return announcement, (sender[0], sender[1])

    def close(self) -> None:
        """Close the owned UDP socket. Repeated calls are harmless."""
        udp_socket = self._socket
        if udp_socket is not None:
            self._socket = None
            udp_socket.close()

    def __enter__(self) -> "DiscoveryEndpoint":
        self._require_socket()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def discover_devices(
    local: DiscoveryAnnouncement,
    *,
    udp_port: int = DEFAULT_DISCOVERY_PORT,
    timeout: float = 2.0,
    host: str = "255.255.255.255",
) -> tuple[tuple[DiscoveryAnnouncement, str], ...]:
    """Request announcements and collect unique replies until *timeout* expires."""
    _validate_port(udp_port, "udp_port")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("timeout must be a positive number")
    if not isinstance(host, str) or not host:
        raise ValueError("host must be a non-empty string")

    found: dict[str, tuple[DiscoveryAnnouncement, str]] = {}
    deadline = time.monotonic() + float(timeout)
    with DiscoveryEndpoint(
        local.device_id, bind_host="0.0.0.0", bind_port=0, timeout=float(timeout)
    ) as endpoint:
        endpoint.send_announcement(local, (host, udp_port))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            endpoint._require_socket().settimeout(remaining)
            try:
                announcement, sender = endpoint.receive_announcement_from()
            except socket.timeout:
                break
            except DiscoveryProtocolError:
                continue
            found[announcement.device_id] = (announcement, sender[0])
    return tuple(sorted(found.values(), key=lambda item: (
        item[0].device_name.casefold(), item[0].device_id
    )))


class DiscoveryResponder:
    """Reply to discovery announcements on the shared UDP port."""

    def __init__(
        self,
        local: DiscoveryAnnouncement,
        *,
        bind_host: str = "0.0.0.0",
        udp_port: int = DEFAULT_DISCOVERY_PORT,
        timeout: float = 0.5,
    ) -> None:
        self.local = local
        _validate_port(udp_port, "udp_port")
        encoded_local = encode_announcement(local)
        udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            udp_socket.bind((bind_host, udp_port))
            udp_socket.settimeout(timeout)
        except BaseException:
            udp_socket.close()
            raise
        self._socket: socket.socket | None = udp_socket
        self._encoded_local = encoded_local

    @property
    def closed(self) -> bool:
        return self._socket is None

    def respond_once(self) -> bool:
        """Handle at most one datagram; return whether a valid reply was sent."""
        if self._socket is None:
            raise OSError("discovery responder is closed")
        try:
            data, sender = self._socket.recvfrom(MAX_DATAGRAM_SIZE + 1)
        except socket.timeout:
            return False
        try:
            remote = decode_announcement(data)
        except DiscoveryProtocolError:
            return False
        if UUID(remote.device_id) == UUID(self.local.device_id):
            return False
        self._socket.sendto(self._encoded_local, sender)
        return True

    def close(self) -> None:
        udp_socket = self._socket
        if udp_socket is not None:
            self._socket = None
            udp_socket.close()

    def __enter__(self) -> "DiscoveryResponder":
        if self._socket is None:
            raise OSError("discovery responder is closed")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


__all__ = (
    "DISCOVERY_PROTOCOL",
    "DISCOVERY_VERSION",
    "DEFAULT_DISCOVERY_PORT",
    "MAX_DATAGRAM_SIZE",
    "DiscoveryAnnouncement",
    "DiscoveryEndpoint",
    "DiscoveryProtocolError",
    "DiscoveryResponder",
    "decode_announcement",
    "encode_announcement",
    "discover_devices",
)
