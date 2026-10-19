"""Length-prefixed JSON service messages over an established TCP socket."""

from dataclasses import dataclass
import json
import math
import re
import socket
import struct
from typing import Any


MESSAGE_VERSION = 1
MAX_MESSAGE_SIZE = 1024 * 1024
HEADER_SIZE = 4

_MESSAGE_FIELDS = {"version", "type", "request_id", "payload"}
_MESSAGE_TYPE = re.compile(r"[a-z][a-z0-9_.-]*\Z", re.ASCII)


class MessageProtocolError(ValueError):
    """A service-message frame or body violates the protocol."""


class ConnectionClosedError(EOFError):
    """The peer closed its connection before one complete frame arrived."""


def _validate_non_empty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MessageProtocolError(f"{name} must be a non-empty string")
    return value


def _validate_message_type(value: object) -> str:
    if not isinstance(value, str) or _MESSAGE_TYPE.fullmatch(value) is None:
        raise MessageProtocolError(
            "type must start with a lowercase ASCII letter and contain only "
            "lowercase letters, digits, '_', '-' or '.'"
        )
    return value


@dataclass(frozen=True)
class ServiceMessage:
    """A validated version-1 MySync service message."""

    type: str
    request_id: str
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        _validate_message_type(self.type)
        _validate_non_empty_string(self.request_id, "request_id")
        if not isinstance(self.payload, dict):
            raise MessageProtocolError("payload must be a JSON object")
        _validate_json_value(self.payload)
        _encode_json_body(self)


def _validate_json_value(value: object, active: set[int] | None = None) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MessageProtocolError("non-finite JSON numbers are not allowed")
        return
    if not isinstance(value, (dict, list)):
        raise MessageProtocolError(
            f"unsupported JSON value type: {type(value).__name__}"
        )

    if active is None:
        active = set()
    identity = id(value)
    if identity in active:
        raise MessageProtocolError("circular JSON data is not allowed")
    active.add(identity)
    try:
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise MessageProtocolError("JSON object keys must be strings")
                _validate_json_value(item, active)
        else:
            for item in value:
                _validate_json_value(item, active)
    finally:
        active.remove(identity)


def _json_constant_error(value: str) -> None:
    raise MessageProtocolError(f"non-finite JSON number is not allowed: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MessageProtocolError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _encode_json_body(message: ServiceMessage) -> bytes:
    _validate_json_value(message.payload)
    body = {
        "version": MESSAGE_VERSION,
        "type": message.type,
        "request_id": message.request_id,
        "payload": message.payload,
    }
    try:
        encoded = json.dumps(
            body,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise MessageProtocolError(
            f"message contains unsupported JSON data: {error}"
        ) from error
    if not encoded:
        raise MessageProtocolError("message body must not be empty")
    if len(encoded) > MAX_MESSAGE_SIZE:
        raise MessageProtocolError(f"message exceeds {MAX_MESSAGE_SIZE} bytes")
    return encoded


def encode_message(message: ServiceMessage) -> bytes:
    """Return a complete four-byte-length-prefixed message frame."""
    if not isinstance(message, ServiceMessage):
        raise TypeError("message must be a ServiceMessage")
    body = _encode_json_body(message)
    return struct.pack("!I", len(body)) + body


def decode_message(body: bytes) -> ServiceMessage:
    """Decode and strictly validate a frame body without its length header."""
    if not isinstance(body, bytes):
        raise TypeError("body must be bytes")
    if not body:
        raise MessageProtocolError("message body must not be empty")
    if len(body) > MAX_MESSAGE_SIZE:
        raise MessageProtocolError(f"message exceeds {MAX_MESSAGE_SIZE} bytes")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MessageProtocolError("message body is not valid UTF-8") from error
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_json_constant_error,
        )
    except json.JSONDecodeError as error:
        raise MessageProtocolError("message body is not valid JSON") from error
    if not isinstance(value, dict):
        raise MessageProtocolError("message body must be a JSON object")
    if set(value) != _MESSAGE_FIELDS:
        missing = sorted(_MESSAGE_FIELDS - set(value))
        extra = sorted(set(value) - _MESSAGE_FIELDS)
        raise MessageProtocolError(
            f"message fields differ; missing={missing}, extra={extra}"
        )
    version = value["version"]
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != MESSAGE_VERSION
    ):
        raise MessageProtocolError(f"unsupported message version: {version!r}")
    return ServiceMessage(
        type=_validate_message_type(value["type"]),
        request_id=_validate_non_empty_string(value["request_id"], "request_id"),
        payload=value["payload"],
    )


def send_message(tcp_socket: socket.socket, message: ServiceMessage) -> None:
    """Send one complete service-message frame using ``sendall``."""
    tcp_socket.sendall(encode_message(message))


def _receive_exactly(tcp_socket: socket.socket, size: int, part: str) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = tcp_socket.recv(remaining)
        if not chunk:
            received = size - remaining
            raise ConnectionClosedError(
                f"connection closed after {received} of {size} {part} bytes"
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def receive_message(tcp_socket: socket.socket) -> ServiceMessage:
    """Read exactly one frame, preserving bytes belonging to later frames."""
    header = _receive_exactly(tcp_socket, HEADER_SIZE, "header")
    (body_size,) = struct.unpack("!I", header)
    if body_size == 0:
        raise MessageProtocolError("message body must not be empty")
    if body_size > MAX_MESSAGE_SIZE:
        raise MessageProtocolError(f"message exceeds {MAX_MESSAGE_SIZE} bytes")
    return decode_message(_receive_exactly(tcp_socket, body_size, "body"))


__all__ = (
    "ConnectionClosedError",
    "HEADER_SIZE",
    "MAX_MESSAGE_SIZE",
    "MESSAGE_VERSION",
    "MessageProtocolError",
    "ServiceMessage",
    "decode_message",
    "encode_message",
    "receive_message",
    "send_message",
)
