"""Shared validation for device identity fields."""


MAX_DEVICE_NAME_BYTES = 255


def validate_device_name(value: object) -> str:
    """Return a safe display name or raise ``ValueError``.

    The byte limit keeps every valid name comfortably inside the discovery
    datagram limit. Non-printable characters are rejected so names cannot
    alter terminal output or the line-oriented CLI display.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("device_name must be a non-empty string")
    if any(not character.isprintable() for character in value):
        raise ValueError("device_name must not contain control characters")
    if len(value.encode("utf-8")) > MAX_DEVICE_NAME_BYTES:
        raise ValueError(
            f"device_name must not exceed {MAX_DEVICE_NAME_BYTES} UTF-8 bytes"
        )
    return value


__all__ = ("MAX_DEVICE_NAME_BYTES", "validate_device_name")
