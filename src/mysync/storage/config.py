"""Validated JSON configuration for one MySync device."""

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile
from uuid import UUID, uuid4


class ConfigError(ValueError):
    """Configuration data is unsupported or invalid."""


@dataclass(frozen=True)
class AppConfig:
    root_path: str
    device_id: str
    device_name: str
    tcp_port: int
    udp_port: int
    backup_retention_days: int = 7
    auto_sync: bool = True

    def __post_init__(self) -> None:
        for name in ("root_path", "device_id", "device_name"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"{name} must be a non-empty string")
        try:
            UUID(self.device_id)
        except (ValueError, AttributeError) as error:
            raise ConfigError("device_id must be a UUID") from error
        for name in ("tcp_port", "udp_port"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
                raise ConfigError(f"{name} must be an integer from 1 to 65535")
        days = self.backup_retention_days
        if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
            raise ConfigError("backup_retention_days must be a positive integer")
        if not isinstance(self.auto_sync, bool):
            raise ConfigError("auto_sync must be a boolean")


def new_device_id() -> str:
    """Return a random canonical UUID string for a new device."""
    return str(uuid4())


def save_config(path: Path, config: AppConfig) -> None:
    """Atomically save validated configuration as UTF-8 JSON."""
    path = Path(path)
    if not path.parent.is_dir():
        raise FileNotFoundError(path.parent)
    if path.is_symlink():
        raise ConfigError(f"Configuration path must not be a symbolic link: {path}")
    payload = {"schema_version": 1, **asdict(config)}
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(payload, temporary, ensure_ascii=False, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def load_config(path: Path) -> AppConfig:
    """Load and validate a version-1 configuration file."""
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except json.JSONDecodeError as error:
        raise ConfigError(f"Invalid JSON configuration: {error}") from error
    if not isinstance(payload, dict):
        raise ConfigError("Configuration must be a JSON object")

    expected = {
        "schema_version", "root_path", "device_id", "device_name",
        "tcp_port", "udp_port", "backup_retention_days", "auto_sync",
    }
    actual = set(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ConfigError(f"Configuration fields differ; missing={missing}, extra={extra}")
    if payload["schema_version"] != 1:
        raise ConfigError(f"Unsupported schema version: {payload['schema_version']!r}")
    values = {key: value for key, value in payload.items() if key != "schema_version"}
    try:
        return AppConfig(**values)
    except TypeError as error:
        raise ConfigError(f"Invalid configuration fields: {error}") from error
