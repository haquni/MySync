"""Command-line interface for the first MySync network MVP."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import socket
import sys
from typing import Sequence

from mysync.application.service import (
    DiscoveredDevice,
    find_devices,
    serve,
    sync_with_device,
)
from mysync.application.session import SyncResult
from mysync.filesystem.scanner import scan_directory
from mysync.network.discovery import DEFAULT_DISCOVERY_PORT
from mysync.network.transport import choose_free_tcp_port
from mysync.storage.config import AppConfig, ConfigError, load_config, new_device_id, save_config


class CliError(RuntimeError):
    """A short user-facing command error."""


def default_config_path() -> Path:
    """Return an OS-local config path that is outside synchronized data."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "MySync" / "config.json"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "mysync" / "config.json"
    return Path.home() / ".config" / "mysync" / "config.json"


def _extract_config(arguments: list[str]) -> tuple[list[str], Path]:
    cleaned: list[str] = []
    config: str | None = None
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--config":
            if index + 1 >= len(arguments):
                raise CliError("--config requires a path")
            config = arguments[index + 1]
            index += 2
            continue
        if argument.startswith("--config="):
            config = argument.split("=", 1)[1]
            index += 1
            continue
        cleaned.append(argument)
        index += 1
    if config == "":
        raise CliError("--config requires a non-empty path")
    return cleaned, Path(config) if config is not None else default_config_path()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="MySync", description="Synchronize folders on a trusted local network.")
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="local configuration path (accepted before or after the command)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("init", help="create local device configuration")
    initialize.add_argument("root", help="folder to synchronize")
    initialize.add_argument("--name", required=True, help="device name shown to peers")

    serving = subparsers.add_parser("serve", help="serve discovery and synchronization")
    serving.add_argument("--bind", default="0.0.0.0", help=argparse.SUPPRESS)
    serving.add_argument("--once", action="store_true", help=argparse.SUPPRESS)

    devices = subparsers.add_parser("devices", help="list serving devices on the LAN")
    devices.add_argument("--timeout", type=float, default=2.0, help="discovery time in seconds")
    devices.add_argument("--host", default="255.255.255.255", help=argparse.SUPPRESS)

    synchronize = subparsers.add_parser("sync", help="run one first-sync session")
    synchronize.add_argument("--device", help="exact device UUID or name")
    synchronize.add_argument("--timeout", type=float, default=2.0, help="discovery time in seconds")
    synchronize.add_argument(
        "--host",
        default="255.255.255.255",
        help="diagnostic discovery destination (default: LAN broadcast)",
    )

    subparsers.add_parser("status", help="show local configuration and folder state")
    return parser


def _load(path: Path) -> AppConfig:
    if not path.exists():
        raise CliError(f"configuration not found: {path}; run 'MySync init' first")
    config = load_config(path)
    _ensure_config_outside_root(path, Path(config.root_path))
    return config


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _ensure_config_outside_root(config_path: Path, root: Path) -> None:
    resolved_config = config_path.expanduser().resolve(strict=False)
    resolved_root = root.expanduser().resolve(strict=False)
    if _is_within(resolved_config, resolved_root):
        raise CliError("configuration path must be outside the synchronization root")


def _initialize(args: argparse.Namespace, config_path: Path) -> None:
    if config_path.exists() or config_path.is_symlink():
        raise CliError(f"configuration already exists: {config_path}")
    root = Path(os.path.abspath(Path(args.root).expanduser()))
    if root.is_symlink():
        raise CliError(f"root must not be a symbolic link: {root}")
    if root.exists() and not root.is_dir():
        raise CliError(f"root is not a directory: {root}")
    root.mkdir(parents=True, exist_ok=True)
    _ensure_config_outside_root(config_path, root)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config = AppConfig(
        root_path=str(root),
        device_id=new_device_id(),
        device_name=args.name,
        tcp_port=choose_free_tcp_port(),
        udp_port=DEFAULT_DISCOVERY_PORT,
        backup_retention_days=7,
        auto_sync=False,
    )
    save_config(config_path, config)
    print(f"Initialized {config.device_name}")
    print(f"Root: {config.root_path}")
    print(f"Config: {config_path}")
    print(f"Device ID: {config.device_id}")
    print(f"TCP port: {config.tcp_port}")


def _print_devices(devices: tuple[DiscoveredDevice, ...]) -> None:
    if not devices:
        print("No devices found.")
        return
    for device in devices:
        print(f"{device.device_name}\t{device.device_id}\t{device.host}:{device.tcp_port}")


def _select_device(
    devices: tuple[DiscoveredDevice, ...], selector: str | None
) -> DiscoveredDevice:
    if selector is None:
        if not devices:
            raise CliError("no serving devices found")
        if len(devices) != 1:
            names = ", ".join(device.device_name for device in devices)
            raise CliError(f"multiple devices found ({names}); use --device")
        return devices[0]
    matches = tuple(
        device
        for device in devices
        if device.device_id == selector or device.device_name.casefold() == selector.casefold()
    )
    if not matches:
        raise CliError(f"device not found: {selector}")
    if len(matches) != 1:
        raise CliError(f"device name is ambiguous: {selector}; use its UUID")
    return matches[0]


def _print_result(result: SyncResult) -> None:
    print(f"Synchronized with {result.peer.device_name} ({result.peer.device_id})")
    for path in result.created_on_local:
        print(f"Created directory: {path}")
    for path in result.copied_to_local:
        print(f"Received file: {path}")
    for path in result.created_on_remote:
        print(f"Remote directory created: {path}")
    for path in result.copied_to_remote:
        print(f"Sent file: {path}")
    if result.conflicts:
        print("Conflicts (unchanged):")
        for path in result.conflicts:
            print(f"  {path}")
    else:
        print("Conflicts: none")


def _print_session_error(error: Exception) -> None:
    print(f"Rejected session: {error}", file=sys.stderr)


def _run(arguments: list[str]) -> None:
    cleaned, config_path = _extract_config(arguments)
    args = _parser().parse_args(cleaned)
    if args.command == "init":
        _initialize(args, config_path)
        return

    config = _load(config_path)
    if args.command == "status":
        snapshot = scan_directory(Path(config.root_path))
        print(f"Device: {config.device_name} ({config.device_id})")
        print(f"Root: {config.root_path}")
        print(f"TCP port: {config.tcp_port}")
        print(f"UDP discovery port: {config.udp_port}")
        print(f"Files: {len(snapshot.files)}")
        print(f"Directories: {len(snapshot.directories)}")
        return
    if args.command == "devices":
        _print_devices(find_devices(config, timeout=args.timeout, host=args.host))
        return
    if args.command == "sync":
        devices = find_devices(config, timeout=args.timeout, host=args.host)
        selected = _select_device(devices, args.device)
        _print_result(sync_with_device(config, selected))
        return
    if args.command == "serve":
        print(f"Serving {config.device_name} on TCP port {config.tcp_port}; press Ctrl+C to stop.")
        serve(
            config,
            once=args.once,
            bind_host=args.bind,
            on_result=_print_result,
            on_error=_print_session_error,
        )
        return
    raise CliError(f"unsupported command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code without a traceback."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        _run(arguments)
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        return 130
    except (CliError, ConfigError, EOFError, OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
