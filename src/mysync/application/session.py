"""Strict first-sync session over one established TCP connection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import socket
from typing import Any
from uuid import UUID, uuid4

from mysync.application.initial_sync import InitialSyncPlan, plan_initial_sync
from mysync.core.identity import validate_device_name
from mysync.filesystem.receiver import UnsafePathError, _path_parts, receive_new_file
from mysync.filesystem.scanner import FileEntry, ScanResult, scan_directory
from mysync.network.messages import (
    MessageProtocolError,
    ServiceMessage,
    receive_message,
    send_message,
)
from mysync.network.transfer import receive_file_chunks, send_file_bytes


SESSION_PROTOCOL = 1


class SessionProtocolError(MessageProtocolError):
    """A peer violated the required first-sync command sequence."""


@dataclass(frozen=True)
class PeerIdentity:
    device_id: str
    device_name: str


@dataclass(frozen=True)
class SyncResult:
    peer: PeerIdentity
    copied_to_local: tuple[str, ...]
    copied_to_remote: tuple[str, ...]
    created_on_local: tuple[str, ...]
    created_on_remote: tuple[str, ...]
    conflicts: tuple[str, ...]
    final_snapshot: ScanResult


def _identity_payload(identity: PeerIdentity) -> dict[str, Any]:
    if not isinstance(identity.device_id, str):
        raise SessionProtocolError("device_id must be a UUID string")
    try:
        UUID(identity.device_id)
    except (ValueError, AttributeError, TypeError) as error:
        raise SessionProtocolError("device_id must be a UUID string") from error
    try:
        validate_device_name(identity.device_name)
    except ValueError as error:
        raise SessionProtocolError(str(error)) from error
    return {
        "protocol": SESSION_PROTOCOL,
        "device_id": identity.device_id,
        "device_name": identity.device_name,
    }


def _parse_identity(payload: dict[str, Any]) -> PeerIdentity:
    if set(payload) != {"protocol", "device_id", "device_name"}:
        raise SessionProtocolError("invalid session.hello payload fields")
    protocol = payload["protocol"]
    if isinstance(protocol, bool) or not isinstance(protocol, int) or protocol != SESSION_PROTOCOL:
        raise SessionProtocolError("unsupported session protocol")
    identity = PeerIdentity(payload["device_id"], payload["device_name"])
    _identity_payload(identity)
    return identity


def _file_payload(entry: FileEntry) -> dict[str, Any]:
    return {
        "path": entry.relative_path,
        "size": entry.size,
        "mtime_ns": entry.mtime_ns,
        "sha256": entry.sha256,
    }


def _parse_file(value: object) -> FileEntry:
    if not isinstance(value, dict) or set(value) != {"path", "size", "mtime_ns", "sha256"}:
        raise SessionProtocolError("invalid file metadata fields")
    path = value["path"]
    size = value["size"]
    mtime = value["mtime_ns"]
    digest = value["sha256"]
    if not isinstance(path, str):
        raise SessionProtocolError("file path must be a string")
    try:
        _path_parts(path)
    except (TypeError, ValueError) as error:
        raise SessionProtocolError(f"unsafe file path: {path!r}") from error
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise SessionProtocolError("file size must be a non-negative integer")
    if isinstance(mtime, bool) or not isinstance(mtime, int) or mtime < 0:
        raise SessionProtocolError("file mtime_ns must be a non-negative integer")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise SessionProtocolError("file SHA-256 must be lowercase hexadecimal")
    return FileEntry(path, size, mtime, digest)


def _snapshot_payload(snapshot: ScanResult) -> dict[str, Any]:
    return {
        "files": [_file_payload(entry) for entry in snapshot.files],
        "directories": list(snapshot.directories),
    }


def _parse_snapshot(payload: dict[str, Any]) -> ScanResult:
    if set(payload) != {"files", "directories"}:
        raise SessionProtocolError("invalid sync.snapshot payload fields")
    files = payload["files"]
    directories = payload["directories"]
    if not isinstance(files, list) or not isinstance(directories, list):
        raise SessionProtocolError("snapshot files and directories must be arrays")
    parsed_directories: list[str] = []
    for path in directories:
        if not isinstance(path, str):
            raise SessionProtocolError("directory path must be a string")
        try:
            _path_parts(path)
        except (TypeError, ValueError) as error:
            raise SessionProtocolError(f"unsafe directory path: {path!r}") from error
        parsed_directories.append(path)
    snapshot = ScanResult(tuple(_parse_file(value) for value in files), tuple(parsed_directories))
    # Reuse the planner's case-insensitive collision validation.
    plan_initial_sync(snapshot, ScanResult((), ()))
    return snapshot


def _message(type_: str, request_id: str, payload: dict[str, Any]) -> ServiceMessage:
    return ServiceMessage(type_, request_id, payload)


def _expect(tcp_socket: socket.socket, type_: str, request_id: str) -> ServiceMessage:
    message = receive_message(tcp_socket)
    if message.request_id != request_id:
        raise SessionProtocolError(
            f"unexpected request_id for {message.type}: {message.request_id!r}"
        )
    if message.type != type_:
        raise SessionProtocolError(f"expected {type_}, received {message.type}")
    return message


def _create_directory(root: Path, relative_path: str) -> None:
    current = Path(root)
    if current.is_symlink():
        raise UnsafePathError(f"Root must not be a symbolic link: {current}")
    for part in _path_parts(relative_path):
        current = current / part
        if current.is_symlink():
            raise UnsafePathError(f"Symbolic link in destination path: {current}")
        if current.exists() and not current.is_dir():
            raise FileExistsError(current)
        current.mkdir(exist_ok=True)


def _plan_conflicts(plan: InitialSyncPlan) -> tuple[str, ...]:
    paths = [change.before.relative_path for change in plan.file_conflicts]
    paths.extend(plan.structure_conflicts)
    return tuple(sorted(set(paths), key=lambda value: (value.casefold(), value)))


def _send_phase(
    tcp_socket: socket.socket,
    request_id: str,
    root: Path,
    directories: tuple[str, ...],
    files: tuple[FileEntry, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    for path in directories:
        send_message(tcp_socket, _message("directory.create", request_id, {"path": path}))
        result = _expect(tcp_socket, "directory.created", request_id)
        if result.payload != {"path": path}:
            raise SessionProtocolError("directory acknowledgement does not match request")

    for entry in files:
        send_message(tcp_socket, _message("file.offer", request_id, _file_payload(entry)))
        ready = _expect(tcp_socket, "file.ready", request_id)
        if ready.payload != {"path": entry.relative_path}:
            raise SessionProtocolError("file readiness does not match offer")
        send_file_bytes(tcp_socket, root, entry)
        received = _expect(tcp_socket, "file.received", request_id)
        if received.payload != {"path": entry.relative_path}:
            raise SessionProtocolError("file acknowledgement does not match offer")

    send_message(tcp_socket, _message("phase.complete", request_id, {}))
    return tuple(entry.relative_path for entry in files), tuple(directories)


def _receive_phase(
    tcp_socket: socket.socket,
    request_id: str,
    root: Path,
    directories: tuple[str, ...],
    files: tuple[FileEntry, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    expected_directories = {path.casefold(): path for path in directories}
    expected_files = {entry.relative_path.casefold(): entry for entry in files}
    received_directories: list[str] = []
    received_files: list[str] = []

    while True:
        message = receive_message(tcp_socket)
        if message.request_id != request_id:
            raise SessionProtocolError("message belongs to another session")
        if message.type == "phase.complete":
            if message.payload:
                raise SessionProtocolError("phase.complete payload must be empty")
            if expected_directories or expected_files:
                raise SessionProtocolError("peer completed a phase before all expected items")
            break
        if message.type == "directory.create":
            if set(message.payload) != {"path"} or not isinstance(message.payload["path"], str):
                raise SessionProtocolError("invalid directory.create payload")
            offered = message.payload["path"]
            expected = expected_directories.pop(offered.casefold(), None)
            if expected is None or expected != offered:
                raise SessionProtocolError(f"unexpected directory: {offered!r}")
            _create_directory(root, offered)
            received_directories.append(offered)
            send_message(tcp_socket, _message("directory.created", request_id, {"path": offered}))
            continue
        if message.type == "file.offer":
            offered = _parse_file(message.payload)
            expected = expected_files.pop(offered.relative_path.casefold(), None)
            if expected is None or expected != offered:
                raise SessionProtocolError(f"unexpected file offer: {offered.relative_path!r}")
            send_message(
                tcp_socket,
                _message("file.ready", request_id, {"path": offered.relative_path}),
            )
            receive_new_file(
                root,
                offered.relative_path,
                receive_file_chunks(tcp_socket, offered.size),
                offered.sha256,
                mtime_ns=offered.mtime_ns,
            )
            received_files.append(offered.relative_path)
            send_message(
                tcp_socket,
                _message("file.received", request_id, {"path": offered.relative_path}),
            )
            continue
        raise SessionProtocolError(f"unexpected command during transfer: {message.type}")

    return tuple(received_files), tuple(received_directories)


def run_client_session(
    tcp_socket: socket.socket,
    root: Path,
    identity: PeerIdentity,
    *,
    expected_peer_id: str | None = None,
) -> SyncResult:
    """Run the initiating side of one deterministic first-sync session."""
    root = Path(root)
    local_snapshot = scan_directory(root)
    request_id = str(uuid4())
    send_message(tcp_socket, _message("session.hello", request_id, _identity_payload(identity)))
    peer = _parse_identity(_expect(tcp_socket, "session.hello", request_id).payload)
    if UUID(peer.device_id) == UUID(identity.device_id):
        raise SessionProtocolError("cannot synchronize a device with itself")
    if expected_peer_id is not None:
        if not isinstance(expected_peer_id, str):
            raise SessionProtocolError("expected peer device_id must be a UUID string")
        try:
            expected_uuid = UUID(expected_peer_id)
        except (ValueError, AttributeError, TypeError) as error:
            raise SessionProtocolError(
                "expected peer device_id must be a UUID string"
            ) from error
        if UUID(peer.device_id) != expected_uuid:
            raise SessionProtocolError(
                "TCP peer device_id does not match the selected discovery device"
            )

    send_message(tcp_socket, _message("sync.snapshot", request_id, _snapshot_payload(local_snapshot)))
    remote_snapshot = _parse_snapshot(_expect(tcp_socket, "sync.snapshot", request_id).payload)
    plan = plan_initial_sync(local_snapshot, remote_snapshot)

    sent_files, sent_directories = _send_phase(
        tcp_socket, request_id, root, plan.create_on_right, plan.copy_to_right
    )
    received_files, received_directories = _receive_phase(
        tcp_socket, request_id, root, plan.create_on_left, plan.copy_to_left
    )
    send_message(tcp_socket, _message("session.complete", request_id, {}))
    completed = _expect(tcp_socket, "session.complete", request_id)
    if completed.payload:
        raise SessionProtocolError("session.complete payload must be empty")
    final_snapshot = scan_directory(root)
    return SyncResult(
        peer,
        received_files,
        sent_files,
        received_directories,
        sent_directories,
        _plan_conflicts(plan),
        final_snapshot,
    )


def run_server_session(
    tcp_socket: socket.socket,
    root: Path,
    identity: PeerIdentity,
) -> SyncResult:
    """Run the accepting side of one deterministic first-sync session."""
    root = Path(root)
    hello = receive_message(tcp_socket)
    if hello.type != "session.hello":
        raise SessionProtocolError(f"expected session.hello, received {hello.type}")
    request_id = hello.request_id
    peer = _parse_identity(hello.payload)
    if UUID(peer.device_id) == UUID(identity.device_id):
        raise SessionProtocolError("cannot synchronize a device with itself")
    send_message(tcp_socket, _message("session.hello", request_id, _identity_payload(identity)))

    remote_snapshot = _parse_snapshot(_expect(tcp_socket, "sync.snapshot", request_id).payload)
    local_snapshot = scan_directory(root)
    send_message(tcp_socket, _message("sync.snapshot", request_id, _snapshot_payload(local_snapshot)))
    plan = plan_initial_sync(local_snapshot, remote_snapshot)

    received_files, received_directories = _receive_phase(
        tcp_socket, request_id, root, plan.create_on_left, plan.copy_to_left
    )
    sent_files, sent_directories = _send_phase(
        tcp_socket, request_id, root, plan.create_on_right, plan.copy_to_right
    )
    completed = _expect(tcp_socket, "session.complete", request_id)
    if completed.payload:
        raise SessionProtocolError("session.complete payload must be empty")
    final_snapshot = scan_directory(root)
    send_message(tcp_socket, _message("session.complete", request_id, {}))
    return SyncResult(
        peer,
        received_files,
        sent_files,
        received_directories,
        sent_directories,
        _plan_conflicts(plan),
        final_snapshot,
    )


__all__ = (
    "PeerIdentity",
    "SESSION_PROTOCOL",
    "SessionProtocolError",
    "SyncResult",
    "run_client_session",
    "run_server_session",
)
