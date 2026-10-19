"""Run discovery and TCP synchronization services from validated config."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import socket
import threading
from typing import Callable

from mysync.application.session import (
    PeerIdentity,
    SyncResult,
    run_client_session,
    run_server_session,
)
from mysync.network.discovery import (
    DiscoveryAnnouncement,
    DiscoveryResponder,
    DiscoveryProtocolError,
    discover_devices,
)
from mysync.network.transport import TcpListener, connect_tcp
from mysync.storage.config import AppConfig


@dataclass(frozen=True)
class DiscoveredDevice:
    device_id: str
    device_name: str
    host: str
    tcp_port: int


def _announcement(config: AppConfig) -> DiscoveryAnnouncement:
    return DiscoveryAnnouncement(config.device_id, config.device_name, config.tcp_port)


def _identity(config: AppConfig) -> PeerIdentity:
    return PeerIdentity(config.device_id, config.device_name)


def find_devices(
    config: AppConfig,
    *,
    timeout: float = 2.0,
    host: str = "255.255.255.255",
) -> tuple[DiscoveredDevice, ...]:
    """Discover serving peers using the configured shared UDP port."""
    return tuple(
        DiscoveredDevice(item.device_id, item.device_name, address, item.tcp_port)
        for item, address in discover_devices(
            _announcement(config), udp_port=config.udp_port, timeout=timeout, host=host
        )
    )


def sync_with_device(config: AppConfig, device: DiscoveredDevice) -> SyncResult:
    """Connect to one discovered device and run the initiating session."""
    with connect_tcp(device.host, device.tcp_port) as connection:
        return run_client_session(
            connection,
            Path(config.root_path),
            _identity(config),
            expected_peer_id=device.device_id,
        )


def serve(
    config: AppConfig,
    *,
    once: bool = False,
    bind_host: str = "0.0.0.0",
    on_result: Callable[[SyncResult], None] | None = None,
    on_error: Callable[[Exception], None] | None = None,
) -> None:
    """Serve discovery replies and sequential TCP sessions until interrupted."""
    stop = threading.Event()
    responder_error: list[BaseException] = []
    responder = DiscoveryResponder(
        _announcement(config), bind_host=bind_host, udp_port=config.udp_port
    )

    def reply_loop() -> None:
        while not stop.is_set():
            try:
                responder.respond_once()
            except DiscoveryProtocolError:
                continue
            except OSError as error:
                if not stop.is_set():
                    responder_error.append(error)
                    stop.set()
                return

    thread = threading.Thread(target=reply_loop, name="mysync-discovery", daemon=True)
    try:
        with TcpListener(bind_host, config.tcp_port) as listener:
            thread.start()
            while not stop.is_set():
                if responder_error:
                    raise responder_error[0]
                try:
                    connection, _address = listener.accept()
                except socket.timeout:
                    continue
                try:
                    with connection:
                        result = run_server_session(
                            connection, Path(config.root_path), _identity(config)
                        )
                except Exception as error:
                    if on_error is not None:
                        on_error(error)
                    if once:
                        raise
                    continue
                if on_result is not None:
                    on_result(result)
                if once:
                    return
    finally:
        stop.set()
        responder.close()
        if thread.ident is not None:
            thread.join(timeout=2.0)


__all__ = ("DiscoveredDevice", "find_devices", "serve", "sync_with_device")
