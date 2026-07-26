"""Strict Windows file-name validation for deployment artifacts."""

from __future__ import annotations

from pathlib import Path


_INVALID_WINDOWS_CHARACTERS = set('<>:"/\\|?*')
_RESERVED_WINDOWS_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{number}"
    for prefix in ("COM", "LPT")
    for number in range(1, 10)
}


def validate_windows_file_name(
    name: str,
    allowed_suffixes: tuple[str, ...],
    label: str,
) -> str:
    """Return an unchanged valid file name or raise ``ValueError``.

    Validation deliberately never trims or rewrites input. Reserved device
    names are checked case-insensitively against the part before the first
    dot, so names such as ``COM1.msi`` and ``nul.anything.wim`` are rejected.
    """

    if not isinstance(name, str) or not name or name != name.strip():
        raise ValueError(f"Invalid {label} file name.")
    if len(name) > 255 or name in {".", ".."} or name[-1] in {".", " "}:
        raise ValueError(f"Invalid {label} file name.")
    if any(
        ord(character) < 32 or character in _INVALID_WINDOWS_CHARACTERS
        for character in name
    ):
        raise ValueError(f"Invalid {label} file name.")
    if Path(name).suffix.lower() not in allowed_suffixes:
        suffixes = "/".join(suffix.lstrip(".") for suffix in allowed_suffixes)
        raise ValueError(f"Invalid {label} file name. Only {suffixes} are allowed.")
    device_stem = name.split(".", 1)[0].rstrip(" .").upper()
    if device_stem in _RESERVED_WINDOWS_NAMES:
        raise ValueError(f"Invalid {label} file name: reserved Windows device name.")
    return name
