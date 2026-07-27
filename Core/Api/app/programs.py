"""Manage post-install programs stored in Share\\Programs.

Operators upload .exe / .msi installers through the Programs page and give
each one optional structured launch arguments. MSI public properties are kept
separate from installer arguments. IronAPI publishes the metadata and SHA-256
values in a deployment manifest; WinPE copies only the selected bytes from SMB
and postinstall.ps1 runs them during Windows SetupComplete.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, AsyncIterator
from uuid import uuid4

from pydantic import BaseModel, Field, StrictStr

from app.config import IRONDEPLOY_ROOT
from app.file_names import validate_windows_file_name


PROGRAMS_DIR = IRONDEPLOY_ROOT / "Share" / "Programs"
METADATA_NAME = ".irondeploy-programs.json"
METADATA_PATH = PROGRAMS_DIR / METADATA_NAME

ALLOWED_SUFFIXES = (".exe", ".msi")
MAX_ARGUMENT_COUNT = 100
MAX_ARGUMENT_LENGTH = 512
MAX_ARGUMENTS_LENGTH = 4096
MAX_MSI_PROPERTY_COUNT = 100
MAX_MSI_PROPERTY_NAME_LENGTH = 72
MAX_MSI_PROPERTY_VALUE_LENGTH = 512
MAX_MSI_PROPERTIES_LENGTH = 4096
MAX_PROGRAM_SIZE_BYTES = 5 * 1024**3

_MSI_PROPERTY_NAME = re.compile(r"^[A-Z_][A-Z0-9_.]*$")
_MSI_PROPERTY_ARGUMENT = re.compile(r"^[A-Z_][A-Z0-9_.]*=", re.IGNORECASE)

_metadata_lock = RLock()


class ProgramError(RuntimeError):
    """Raised when a program-management operation cannot be completed."""


class ProgramConfigurationRequest(BaseModel):
    """Structured installer configuration accepted by the Programs API."""

    arguments: list[StrictStr] = Field(
        default_factory=list,
        max_length=MAX_ARGUMENT_COUNT,
    )
    msi_properties: dict[str, StrictStr] = Field(
        default_factory=dict,
        max_length=MAX_MSI_PROPERTY_COUNT,
    )


class ProgramRecord(BaseModel):
    name: str
    type: str
    size: int
    modifiedAt: str
    arguments: list[str]
    msi_properties: dict[str, str]
    sha256: str


class ProgramListing(BaseModel):
    programs: list[ProgramRecord]
    directory: str


class ProgramUploadResponse(BaseModel):
    uploaded: bool
    name: str
    size: int
    arguments: list[str]
    msi_properties: dict[str, str]
    sha256: str


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


def _validate_command_line_value(
    value: object,
    *,
    label: str,
    maximum_length: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ProgramError(f"{label} must be a string.")
    if not value and not allow_empty:
        raise ProgramError(f"{label} must not be empty.")
    if len(value) > maximum_length:
        raise ProgramError(f"{label} is limited to {maximum_length} characters.")
    if any(character.isspace() for character in value):
        raise ProgramError(f"{label} must not contain whitespace.")
    if "\x00" in value or '"' in value or "'" in value:
        raise ProgramError(f"{label} must not contain NUL or quotes.")
    return value


def validate_program_arguments(
    arguments: object,
    program_type: str = "EXE",
) -> list[str]:
    """Validate a structured argument list or safely migrate a legacy string."""

    if isinstance(arguments, str):
        values: object = arguments.split()
    else:
        values = arguments
    if not isinstance(values, list):
        raise ProgramError("arguments must be an array of strings.")
    if len(values) > MAX_ARGUMENT_COUNT:
        raise ProgramError(
            f"Launch arguments are limited to {MAX_ARGUMENT_COUNT} items."
        )
    normalized: list[str] = []
    total_length = 0
    is_msi = program_type.upper() == "MSI"
    for index, value in enumerate(values):
        argument = _validate_command_line_value(
            value,
            label=f"Launch argument {index + 1}",
            maximum_length=MAX_ARGUMENT_LENGTH,
        )
        if is_msi and _MSI_PROPERTY_ARGUMENT.match(argument):
            raise ProgramError(
                f"MSI property '{argument.split('=', 1)[0]}' must be stored in "
                "msi_properties, not arguments."
            )
        normalized.append(argument)
        total_length += len(argument)
    total_length += max(0, len(normalized) - 1)
    if total_length > MAX_ARGUMENTS_LENGTH:
        raise ProgramError(
            f"Launch arguments are limited to {MAX_ARGUMENTS_LENGTH} characters "
            "in total."
        )
    return normalized


def validate_msi_properties(
    properties: object,
    program_type: str = "MSI",
) -> dict[str, str]:
    if properties is None:
        properties = {}
    if not isinstance(properties, dict):
        raise ProgramError("msi_properties must be an object of string values.")
    if len(properties) > MAX_MSI_PROPERTY_COUNT:
        raise ProgramError(
            f"MSI properties are limited to {MAX_MSI_PROPERTY_COUNT} items."
        )
    if program_type.upper() != "MSI" and properties:
        raise ProgramError("msi_properties are only valid for MSI programs.")

    normalized: dict[str, str] = {}
    total_length = 0
    for raw_name, raw_value in properties.items():
        if not isinstance(raw_name, str):
            raise ProgramError("MSI property names must be strings.")
        name = raw_name.upper()
        if len(name) > MAX_MSI_PROPERTY_NAME_LENGTH:
            raise ProgramError(
                "MSI property names are limited to "
                f"{MAX_MSI_PROPERTY_NAME_LENGTH} characters."
            )
        if not _MSI_PROPERTY_NAME.fullmatch(name):
            raise ProgramError(
                f"Invalid MSI property name '{raw_name}'. Names must match "
                "^[A-Z_][A-Z0-9_.]*$."
            )
        if name in normalized:
            raise ProgramError(
                f"Duplicate MSI property name after uppercase normalization: {name}."
            )
        value = _validate_command_line_value(
            raw_value,
            label=f"MSI property {name} value",
            maximum_length=MAX_MSI_PROPERTY_VALUE_LENGTH,
            allow_empty=True,
        )
        normalized[name] = value
        total_length += len(name) + 1 + len(value)
    total_length += max(0, len(normalized) - 1)
    if total_length > MAX_MSI_PROPERTIES_LENGTH:
        raise ProgramError(
            f"MSI properties are limited to {MAX_MSI_PROPERTIES_LENGTH} "
            "characters in total."
        )
    return normalized


def validate_program_configuration(
    name: str,
    arguments: object,
    msi_properties: object = None,
) -> tuple[list[str], dict[str, str]]:
    program_type = Path(name).suffix[1:].upper()
    return (
        validate_program_arguments(arguments, program_type),
        validate_msi_properties(msi_properties, program_type),
    )


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
            record = {"arguments": [], "msi_properties": {}}
            changed = True
        arguments, msi_properties = validate_program_configuration(
            path.name,
            record.get("arguments", []),
            record.get("msi_properties", {}),
        )
        # Mutate the in-memory record so any subsequent metadata write also
        # migrates legacy string arguments to the v3 structured format.
        record["arguments"] = arguments
        record["msi_properties"] = msi_properties
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
                "arguments": arguments,
                "msi_properties": msi_properties,
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
    arguments: object,
    programs_dir: Path = PROGRAMS_DIR,
    metadata_path: Path = METADATA_PATH,
    *,
    msi_properties: object = None,
) -> dict[str, Any]:
    safe_name = _safe_program_name(name)
    normalized_arguments, normalized_properties = validate_program_configuration(
        safe_name,
        arguments,
        msi_properties,
    )

    with _metadata_lock:
        listing = _list_programs(programs_dir, metadata_path)
        program = next(
            (item for item in listing["programs"] if item["name"] == safe_name), None
        )
        if program is None:
            raise ProgramError("Program not found.")
        metadata = _read_metadata(metadata_path)
        metadata["version"] = 3
        metadata["programs"][safe_name]["arguments"] = normalized_arguments
        metadata["programs"][safe_name]["msi_properties"] = normalized_properties
        _write_metadata(metadata, metadata_path)
        program["arguments"] = normalized_arguments
        program["msi_properties"] = normalized_properties
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
                record = {"arguments": [], "msi_properties": {}}
            arguments, msi_properties = validate_program_configuration(
                safe_new_name,
                record.get("arguments", []),
                record.get("msi_properties", {}),
            )
            record["arguments"] = arguments
            record["msi_properties"] = msi_properties
            record["size"] = stat.st_size
            record["modifiedNs"] = stat.st_mtime_ns
            metadata["version"] = 3
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
        "arguments": arguments,
        "msi_properties": msi_properties,
    }


async def save_uploaded_program(
    name: str,
    chunks: AsyncIterator[bytes],
    arguments: object = None,
    msi_properties: object = None,
    programs_dir: Path = PROGRAMS_DIR,
    metadata_path: Path = METADATA_PATH,
) -> dict[str, Any]:
    safe_name = _safe_program_name(name)
    normalized_arguments, normalized_properties = validate_program_configuration(
        safe_name,
        [] if arguments is None else arguments,
        msi_properties,
    )
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
            metadata["version"] = 3
            metadata["programs"][safe_name] = {
                "arguments": normalized_arguments,
                "msi_properties": normalized_properties,
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
        "msi_properties": normalized_properties,
        "sha256": digest.hexdigest(),
    }
