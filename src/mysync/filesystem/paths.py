"""Portable relative-path validation for synchronized data."""

from pathlib import PurePosixPath


_WINDOWS_FORBIDDEN = frozenset('<>:"|?*')
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


class UnsafePathError(ValueError):
    """A path is unsafe or cannot be represented on every target platform."""


def _validate_portable_part(part: str, relative_path: str) -> None:
    if any(ord(character) < 32 or character in _WINDOWS_FORBIDDEN for character in part):
        raise UnsafePathError(f"Path is not portable to Windows: {relative_path!r}")
    if part.endswith((" ", ".")):
        raise UnsafePathError(f"Path is not portable to Windows: {relative_path!r}")
    base_name = part.split(".", 1)[0].upper()
    if base_name in _WINDOWS_RESERVED:
        raise UnsafePathError(f"Path is not portable to Windows: {relative_path!r}")


def path_parts(relative_path: str) -> tuple[str, ...]:
    """Validate and split one portable POSIX-style relative path."""
    if not isinstance(relative_path, str):
        raise TypeError("relative_path must be a string")
    if not relative_path or "\\" in relative_path:
        raise UnsafePathError(f"Unsafe relative path: {relative_path!r}")
    raw_parts = relative_path.split("/")
    path = PurePosixPath(relative_path)
    if path.is_absolute() or any(part in ("", ".", "..") for part in raw_parts):
        raise UnsafePathError(f"Unsafe relative path: {relative_path!r}")
    for part in raw_parts:
        _validate_portable_part(part, relative_path)
    return tuple(raw_parts)


__all__ = ("UnsafePathError", "path_parts")
