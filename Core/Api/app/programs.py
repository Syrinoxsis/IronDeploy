"""Manage post-install programs stored in Share\\Programs.

Operators upload .exe / .msi installers through the Programs page and give
each one an optional raw launch-argument string (such as /S or ALLUSERS=1).
IronAPI publishes the metadata and SHA-256 values in a deployment manifest;
WinPE copies only the selected bytes from SMB and postinstall.ps1 runs them
during Windows SetupComplete.
"""

from __future__ import annotations

import json
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, AsyncIterator
from uuid import uuid4

from app.config import IRONDEPLOY_ROOT
from app.file_names import validate_windows_file_name


PROGRAMS_DIR = IRONDEPLOY_ROOT / "Share" / "Programs"
METADATA_NAME = ".irondeploy-programs.json"
METADATA_PATH = PROGRAMS_DIR / METADATA_NAME

ALLOWED_SUFFIXES = (".exe", ".msi")
MAX_ARGUMENTS_LENGTH = 500
MAX_PROGRAM_SIZE_BYTES = 5 * 1024**3

_metadata_lock = RLock()


class ProgramError(RuntimeError):
    """Raised when a program-management operation cannot be completed."""


def _safe_program_name(name: str) -> str:
    try:
        return validate_windows_file_name(name, ALLOWED_SUFFIXES, "program")
    except ValueError as exc:
        raise ProgramError(str(exc)) from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ProgramError(f"Failed to hash program '{path.name}': {exc}") from exc
    return digest.hexdigest()


def validate_program_arguments(arguments: str) -> str:
    if not isinstance(arguments, str):
        raise ProgramError("arguments must be a string.")
    value = arguments
    if any(character in value for character in ("\0", "\r", "\n")):
        raise ProgramError(
            "Launch arguments must not contain NUL, CR, or LF characters."
        )
    normalized = value.strip(" ")
    if len(normalized) > MAX_ARGUMENTS_LENGTH:
        raise ProgramError(
            f"Launch arguments are limited to {MAX_ARGUMENTS_LENGTH} characters."
        )
    return normalized


def _read_metadata(path: Path = METADATA_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 3, "programs": {}}
    except (OSError, json.JSONDecodeError) as exc:
        raise ProgramError(f"Failed to read program metadata: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("programs"), dict):
        raise ProgramError("Program metadata has an invalid format.")
    return payload


def _write_metadata(payload: dict[str, Any], path: Path = METADATA_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{uuid4().hex}")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ProgramError(f"Failed to save program metadata: {exc}") from exc


def _list_programs(
    programs_dir: Path = PROGRAMS_DIR,
    metadata_path: Path = METADATA_PATH,
) -> dict[str, Any]:
    programs_dir.mkdir(parents=True, exist_ok=True)
    metadata = _read_metadata(metadata_path)
    records = metadata["programs"]
    files = sorted(
        (
            item
            for item in programs_dir.iterdir()
            if item.is_file()
            and item.suffix.lower() in ALLOWED_SUFFIXES
            and not item.name.startswith(".")
        ),
        key=lambda item: item.name.lower(),
    )
    found_names = {item.name for item in files}
    changed = False

    for stale_name in [name for name in records if name not in found_names]:
        del records[stale_name]
        changed = True

    result = []
    for path in files:
        stat = path.stat()
        record = records.get(path.name)
        if not isinstance(record, dict):
            record = {}
            changed = True
        if not isinstance(record.get("arguments"), str):
            record["arguments"] = ""
            changed = True
        if type(record.get("enabled")) is not bool:
            record["enabled"] = True
            changed = True
        signature_changed = (
            record.get("size") != stat.st_size
            or record.get("modifiedNs") != stat.st_mtime_ns
        )
        if signature_changed or not isinstance(record.get("sha256"), str):
            sha256 = _sha256_file(path)
            hashed_stat = path.stat()
            if (
                hashed_stat.st_size != stat.st_size
                or hashed_stat.st_mtime_ns != stat.st_mtime_ns
            ):
                raise ProgramError(
                    f"Program '{path.name}' changed while SHA-256 was calculated."
                )
            record["size"] = stat.st_size
            record["modifiedNs"] = stat.st_mtime_ns
            record["sha256"] = sha256
            changed = True
        records[path.name] = record

        result.append(
            {
                "name": path.name,
                "type": path.suffix[1:].upper(),
                "size": stat.st_size,
                "modifiedAt": datetime.fromtimestamp(
                    stat.st_mtime, timezone.utc
                ).isoformat(),
                "arguments": record["arguments"],
                "enabled": record["enabled"],
                "sha256": record["sha256"],
            }
        )

    if changed:
        metadata["version"] = 3
        _write_metadata(metadata, metadata_path)
    return {"programs": result, "directory": str(programs_dir)}


def list_programs(
    programs_dir: Path = PROGRAMS_DIR,
    metadata_path: Path = METADATA_PATH,
) -> dict[str, Any]:
    with _metadata_lock:
        return _list_programs(programs_dir, metadata_path)


def set_program_arguments(
    name: str,
    arguments: str,
    programs_dir: Path = PROGRAMS_DIR,
    metadata_path: Path = METADATA_PATH,
) -> dict[str, Any]:
    safe_name = _safe_program_name(name)
    normalized = validate_program_arguments(arguments)

    with _metadata_lock:
        listing = _list_programs(programs_dir, metadata_path)
        program = next(
            (item for item in listing["programs"] if item["name"] == safe_name), None
        )
        if program is None:
            raise ProgramError("Program not found.")
        metadata = _read_metadata(metadata_path)
        metadata["programs"][safe_name]["arguments"] = normalized
        _write_metadata(metadata, metadata_path)
        program["arguments"] = normalized
        return program


def set_program_enabled(
    name: str,
    enabled: bool,
    programs_dir: Path = PROGRAMS_DIR,
    metadata_path: Path = METADATA_PATH,
) -> dict[str, Any]:
    safe_name = _safe_program_name(name)
    if type(enabled) is not bool:
        raise ProgramError("enabled must be a boolean.")

    with _metadata_lock:
        listing = _list_programs(programs_dir, metadata_path)
        program = next(
            (item for item in listing["programs"] if item["name"] == safe_name), None
        )
        if program is None:
            raise ProgramError("Program not found.")
        metadata = _read_metadata(metadata_path)
        metadata["programs"][safe_name]["enabled"] = enabled
        metadata["version"] = 3
        _write_metadata(metadata, metadata_path)
        program["enabled"] = enabled
        return program


def delete_program(
    name: str,
    programs_dir: Path = PROGRAMS_DIR,
    metadata_path: Path = METADATA_PATH,
) -> dict[str, Any]:
    safe_name = _safe_program_name(name)

    with _metadata_lock:
        target = programs_dir / safe_name
        if not target.is_file():
            raise ProgramError("Program not found.")
        try:
            target.unlink()
        except OSError as exc:
            raise ProgramError(f"Failed to delete the program: {exc}") from exc
        metadata = _read_metadata(metadata_path)
        if safe_name in metadata["programs"]:
            del metadata["programs"][safe_name]
            _write_metadata(metadata, metadata_path)

    return {"deleted": True, "name": safe_name}


def rename_program(
    name: str,
    new_name: str,
    programs_dir: Path = PROGRAMS_DIR,
    metadata_path: Path = METADATA_PATH,
) -> dict[str, Any]:
    safe_name = _safe_program_name(name)
    safe_new_name = _safe_program_name(new_name)
    if safe_name == safe_new_name:
        raise ProgramError("The new program name is unchanged.")
    if Path(safe_name).suffix.lower() != Path(safe_new_name).suffix.lower():
        raise ProgramError("The program file extension cannot be changed.")

    with _metadata_lock:
        source = programs_dir / safe_name
        destination = programs_dir / safe_new_name
        if not source.is_file():
            raise ProgramError("Program not found.")

        case_only_rename = safe_name.casefold() == safe_new_name.casefold()
        if destination.exists() and not case_only_rename:
            raise ProgramError(f"A program named '{safe_new_name}' already exists.")

        metadata = _read_metadata(metadata_path)
        existing_key = next(
            (
                key
                for key in metadata["programs"]
                if key.casefold() == safe_name.casefold()
            ),
            None,
        )
        record = metadata["programs"].pop(existing_key, None) if existing_key else None
        temporary: Path | None = None
        try:
            if case_only_rename:
                temporary = programs_dir / f".rename-{uuid4().hex}.tmp"
                os.rename(source, temporary)
                os.rename(temporary, destination)
            else:
                os.rename(source, destination)

            stat = destination.stat()
            if not isinstance(record, dict):
                record = {"arguments": ""}
            record["size"] = stat.st_size
            record["modifiedNs"] = stat.st_mtime_ns
            metadata["programs"][safe_new_name] = record
            _write_metadata(metadata, metadata_path)
        except (OSError, ProgramError) as exc:
            try:
                if temporary is not None and temporary.exists():
                    os.rename(temporary, source)
                elif destination.exists() and not source.exists():
                    os.rename(destination, source)
            except OSError:
                pass
            if isinstance(exc, ProgramError):
                raise
            raise ProgramError(f"Failed to rename the program: {exc}") from exc

    return {
        "renamed": True,
        "oldName": safe_name,
        "name": safe_new_name,
        "arguments": str(record.get("arguments", "")),
        "enabled": bool(record.get("enabled", True)),
    }


async def save_uploaded_program(
    name: str,
    chunks: AsyncIterator[bytes],
    arguments: str = "",
    programs_dir: Path = PROGRAMS_DIR,
    metadata_path: Path = METADATA_PATH,
) -> dict[str, Any]:
    safe_name = _safe_program_name(name)
    normalized_arguments = validate_program_arguments(arguments)
    programs_dir.mkdir(parents=True, exist_ok=True)
    destination = programs_dir / safe_name
    if destination.exists():
        raise ProgramError(f"A program named '{safe_name}' already exists.")
    temporary = programs_dir / f".{safe_name}.upload-{uuid4().hex}.tmp"
    size = 0
    digest = hashlib.sha256()
    published = False
    try:
        with temporary.open("xb") as handle:
            async for chunk in chunks:
                if chunk:
                    if size + len(chunk) > MAX_PROGRAM_SIZE_BYTES:
                        raise ProgramError(
                            "Program files are limited to 5 GiB."
                        )
                    handle.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
        if size == 0:
            raise ProgramError("The uploaded program file is empty.")
        with _metadata_lock:
            if destination.exists():
                raise ProgramError(f"A program named '{safe_name}' already exists.")
            os.replace(temporary, destination)
            published = True
            stat = destination.stat()
            metadata = _read_metadata(metadata_path)
            metadata["programs"][safe_name] = {
                "arguments": normalized_arguments,
                "enabled": True,
                "size": stat.st_size,
                "modifiedNs": stat.st_mtime_ns,
                "sha256": digest.hexdigest(),
            }
            _write_metadata(metadata, metadata_path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        if published:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
        raise ProgramError(f"Failed to save the uploaded program: {exc}") from exc
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        if published:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return {
        "uploaded": True,
        "name": safe_name,
        "size": size,
        "arguments": normalized_arguments,
        "enabled": True,
        "sha256": digest.hexdigest(),
    }
