r"""Manage post-install software packages stored in ``Share\Programs``.

Every program is a directory. The directory contains one configured EXE/MSI
entrypoint plus any supporting files it needs. Uploads are staged outside the
published package tree and become visible only after every declared file has
been received and verified.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from threading import RLock
from typing import Any, AsyncIterator
from uuid import uuid4

from app.config import IRONDEPLOY_ROOT


PROGRAMS_DIR = IRONDEPLOY_ROOT / "Share" / "Programs"
METADATA_NAME = ".irondeploy-programs.json"
METADATA_PATH = PROGRAMS_DIR / METADATA_NAME
UPLOADS_NAME = ".irondeploy-program-uploads"

ALLOWED_SUFFIXES = (".exe", ".msi")
MAX_ARGUMENTS_LENGTH = 500
MAX_PROGRAM_SIZE_BYTES = 5 * 1024**3
MAX_PROGRAM_FILES = 10_000
MAX_RELATIVE_PATH_LENGTH = 500

_INVALID_WINDOWS_CHARACTERS = set('<>:"/\\|?*')
_RESERVED_WINDOWS_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{number}"
    for prefix in ("COM", "LPT")
    for number in range(1, 10)
}
_metadata_lock = RLock()
_pending_files: set[str] = set()


def _file_conflict() -> ProgramError:
    return ProgramError("This file already exists in the folder or is being uploaded. Rename your file.")


class ProgramError(RuntimeError):
    """Raised when a software package operation cannot be completed."""


def _safe_windows_component(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProgramError(f"Invalid {label}.")
    if len(value) > 255 or value in {".", ".."} or value[-1] in {".", " "}:
        raise ProgramError(f"Invalid {label}.")
    if any(
        ord(character) < 32 or character in _INVALID_WINDOWS_CHARACTERS
        for character in value
    ):
        raise ProgramError(f"Invalid {label}.")
    device_stem = value.split(".", 1)[0].rstrip(" .").upper()
    if device_stem in _RESERVED_WINDOWS_NAMES:
        raise ProgramError(f"Invalid {label}: reserved Windows device name.")
    return value


def _safe_program_name(name: str) -> str:
    """Validate a package name (kept for callers using the historic helper)."""

    value = _safe_windows_component(name, "program package name")
    if value.startswith(".") or value.casefold() in {
        METADATA_NAME.casefold(),
        UPLOADS_NAME.casefold(),
    }:
        raise ProgramError("Reserved program package name.")
    return value


def _safe_relative_path(value: str, *, entrypoint: bool = False) -> str:
    if not isinstance(value, str):
        raise ProgramError("Invalid package file path.")
    normalized = value.replace("/", "\\")
    if (
        not normalized
        or normalized != normalized.strip()
        or len(normalized) > MAX_RELATIVE_PATH_LENGTH
        or normalized.startswith("\\")
        or normalized.endswith("\\")
    ):
        raise ProgramError("Invalid package file path.")
    parts = normalized.split("\\")
    if any(not part for part in parts):
        raise ProgramError("Invalid package file path.")
    for part in parts:
        _safe_windows_component(part, "package file path")
        if part.casefold().startswith(".irondeploy-"):
            raise ProgramError("Reserved package file path.")
    if entrypoint and PureWindowsPath(normalized).suffix.lower() not in ALLOWED_SUFFIXES:
        raise ProgramError("The package entrypoint must be an .exe or .msi file.")
    return normalized


def _native_path(root: Path, relative_path: str) -> Path:
    return root.joinpath(*PureWindowsPath(relative_path).parts)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ProgramError(f"Failed to hash package file '{path.name}': {exc}") from exc
    return digest.hexdigest()


def _package_sha256(files: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda value: value["path"].casefold()):
        digest.update(item["path"].replace("/", "\\").encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item["size"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(item["sha256"].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def validate_program_arguments(arguments: str) -> str:
    if not isinstance(arguments, str):
        raise ProgramError("arguments must be a string.")
    if any(character in arguments for character in ("\0", "\r", "\n")):
        raise ProgramError(
            "Launch arguments must not contain NUL, CR, or LF characters."
        )
    normalized = arguments.strip(" ")
    if len(normalized) > MAX_ARGUMENTS_LENGTH:
        raise ProgramError(
            f"Launch arguments are limited to {MAX_ARGUMENTS_LENGTH} characters."
        )
    return normalized


def _read_metadata(path: Path = METADATA_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 4, "programs": {}}
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


def _unique_package_name(preferred: str, programs_dir: Path) -> str:
    candidate = _safe_program_name(preferred)
    occupied = {item.name.casefold() for item in programs_dir.iterdir()}
    if candidate.casefold() not in occupied:
        return candidate
    for number in range(2, 10_000):
        candidate = _safe_program_name(f"{preferred} ({number})")
        if candidate.casefold() not in occupied:
            return candidate
    raise ProgramError("Could not create a unique package name.")


def _migrate_flat_programs(programs_dir: Path, metadata: dict[str, Any]) -> bool:
    """Move legacy root EXE/MSI files into package directories."""

    changed = False
    records = metadata["programs"]
    legacy_files = sorted(
        (
            item
            for item in programs_dir.iterdir()
            if item.is_file()
            and item.suffix.lower() in ALLOWED_SUFFIXES
            and not item.name.startswith(".")
        ),
        key=lambda item: item.name.casefold(),
    )
    for source in legacy_files:
        package_name = _unique_package_name(source.stem, programs_dir)
        temporary = programs_dir / f".irondeploy-migrate-{uuid4().hex}"
        destination = programs_dir / package_name
        old_record = records.pop(source.name, {})
        try:
            temporary.mkdir()
            os.replace(source, temporary / source.name)
            os.replace(temporary, destination)
        except OSError as exc:
            try:
                if (temporary / source.name).is_file() and not source.exists():
                    os.replace(temporary / source.name, source)
                temporary.rmdir()
            except OSError:
                pass
            raise ProgramError(
                f"Failed to migrate legacy program '{source.name}': {exc}"
            ) from exc
        records[package_name] = {
            "arguments": (
                old_record.get("arguments", "") if isinstance(old_record, dict) else ""
            ),
            "enabled": (
                old_record.get("enabled", True) if isinstance(old_record, dict) else True
            ),
            "entrypoint": source.name,
        }
        changed = True
    return changed


def _scan_package(
    package_dir: Path, record: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    changed = False
    if package_dir.is_symlink() or (
        hasattr(package_dir, "is_junction") and package_dir.is_junction()
    ):
        raise ProgramError(f"Package '{package_dir.name}' must not be a link or junction.")
    entries = list(package_dir.rglob("*"))
    for item in entries:
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ProgramError(
                f"Package '{package_dir.name}' contains a link or junction."
            )
    paths = sorted(
        (item for item in entries if item.is_file()),
        key=lambda item: str(item.relative_to(package_dir)).casefold(),
    )
    if len(paths) > MAX_PROGRAM_FILES:
        raise ProgramError(
            f"Package '{package_dir.name}' contains more than {MAX_PROGRAM_FILES} files."
        )

    cached_files = record.get("files")
    if not isinstance(cached_files, dict):
        cached_files = {}
        changed = True
    files: list[dict[str, Any]] = []
    total_size = 0
    latest_mtime_ns = 0
    for path in paths:
        relative = str(path.relative_to(package_dir)).replace(os.sep, "\\")
        relative = _safe_relative_path(relative)
        stat = path.stat()
        total_size += stat.st_size
        latest_mtime_ns = max(latest_mtime_ns, stat.st_mtime_ns)
        if total_size > MAX_PROGRAM_SIZE_BYTES:
            raise ProgramError(f"Package '{package_dir.name}' exceeds 5 GiB.")
        cached = cached_files.get(relative)
        if (
            not isinstance(cached, dict)
            or cached.get("size") != stat.st_size
            or cached.get("modifiedNs") != stat.st_mtime_ns
            or not isinstance(cached.get("sha256"), str)
        ):
            sha256 = _sha256_file(path)
            hashed_stat = path.stat()
            if (
                hashed_stat.st_size != stat.st_size
                or hashed_stat.st_mtime_ns != stat.st_mtime_ns
            ):
                raise ProgramError(
                    f"Package file '{relative}' changed while SHA-256 was calculated."
                )
            cached = {
                "size": stat.st_size,
                "modifiedNs": stat.st_mtime_ns,
                "sha256": sha256,
            }
            changed = True
        files.append(
            {
                "path": relative,
                "size": stat.st_size,
                "modifiedAt": datetime.fromtimestamp(
                    stat.st_mtime, timezone.utc
                ).isoformat(),
                "sha256": cached["sha256"],
            }
        )

    current_cache = {
        item["path"]: {
            "size": item["size"],
            "modifiedNs": _native_path(package_dir, item["path"]).stat().st_mtime_ns,
            "sha256": item["sha256"],
        }
        for item in files
    }
    if set(cached_files) != set(current_cache):
        changed = True
    record["files"] = current_cache

    entrypoint = record.get("entrypoint")
    entrypoint_names = {
        item["path"].casefold(): item["path"]
        for item in files
        if PureWindowsPath(item["path"]).suffix.lower() in ALLOWED_SUFFIXES
    }
    if not isinstance(entrypoint, str) or entrypoint.casefold() not in entrypoint_names:
        entrypoint = entrypoint_names[sorted(entrypoint_names)[0]] if entrypoint_names else ""
        record["entrypoint"] = entrypoint
        changed = True
    else:
        entrypoint = entrypoint_names[entrypoint.casefold()]
        if record.get("entrypoint") != entrypoint:
            record["entrypoint"] = entrypoint
            changed = True

    if not isinstance(record.get("arguments"), str):
        record["arguments"] = ""
        changed = True
    if type(record.get("enabled")) is not bool:
        record["enabled"] = True
        changed = True
    package_hash = _package_sha256(files)
    if record.get("sha256") != package_hash:
        record["sha256"] = package_hash
        changed = True
    record["size"] = total_size
    record["modifiedNs"] = latest_mtime_ns

    return (
        {
            "name": package_dir.name,
            "entrypoint": entrypoint,
            "ready": bool(entrypoint),
            "warning": "" if entrypoint else "No .exe or .msi found. This package cannot be selected for installation.",
            "type": PureWindowsPath(entrypoint).suffix[1:].upper(),
            "size": total_size,
            "fileCount": len(files),
            "modifiedAt": datetime.fromtimestamp(
                (
                    latest_mtime_ns / 1_000_000_000
                    if latest_mtime_ns
                    else package_dir.stat().st_mtime
                ),
                timezone.utc,
            ).isoformat(),
            "arguments": record["arguments"],
            "enabled": record["enabled"],
            "sha256": package_hash,
            "files": files,
        },
        changed,
    )


def _list_programs(
    programs_dir: Path = PROGRAMS_DIR,
    metadata_path: Path = METADATA_PATH,
) -> dict[str, Any]:
    programs_dir.mkdir(parents=True, exist_ok=True)
    metadata = _read_metadata(metadata_path)
    records = metadata["programs"]
    changed = _migrate_flat_programs(programs_dir, metadata)
    package_dirs = sorted(
        (
            item
            for item in programs_dir.iterdir()
            if item.is_dir() and not item.name.startswith(".")
        ),
        key=lambda item: item.name.casefold(),
    )
    found_names = {item.name for item in package_dirs}
    for stale_name in [name for name in records if name not in found_names]:
        del records[stale_name]
        changed = True

    result = []
    for package_dir in package_dirs:
        record = records.get(package_dir.name)
        if not isinstance(record, dict):
            record = {}
            records[package_dir.name] = record
            changed = True
        package, package_changed = _scan_package(package_dir, record)
        result.append(package)
        changed = changed or package_changed

    if changed or metadata.get("version") != 4:
        metadata["version"] = 4
        _write_metadata(metadata, metadata_path)
    return {"programs": result, "directory": str(programs_dir)}


def list_programs(
    programs_dir: Path = PROGRAMS_DIR,
    metadata_path: Path = METADATA_PATH,
) -> dict[str, Any]:
    with _metadata_lock:
        return _list_programs(programs_dir, metadata_path)


def _program_record(
    name: str,
    programs_dir: Path,
    metadata_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    safe_name = _safe_program_name(name)
    listing = _list_programs(programs_dir, metadata_path)
    program = next(
        (
            item
            for item in listing["programs"]
            if item["name"].casefold() == safe_name.casefold()
        ),
        None,
    )
    if program is None:
        raise ProgramError("Program package not found.")
    metadata = _read_metadata(metadata_path)
    return program, metadata


def set_program_arguments(
    name: str,
    arguments: str,
    programs_dir: Path = PROGRAMS_DIR,
    metadata_path: Path = METADATA_PATH,
) -> dict[str, Any]:
    normalized = validate_program_arguments(arguments)
    with _metadata_lock:
        program, metadata = _program_record(name, programs_dir, metadata_path)
        metadata["programs"][program["name"]]["arguments"] = normalized
        metadata["version"] = 4
        _write_metadata(metadata, metadata_path)
        program["arguments"] = normalized
        return program


def set_program_enabled(
    name: str,
    enabled: bool,
    programs_dir: Path = PROGRAMS_DIR,
    metadata_path: Path = METADATA_PATH,
) -> dict[str, Any]:
    if type(enabled) is not bool:
        raise ProgramError("enabled must be a boolean.")
    with _metadata_lock:
        program, metadata = _program_record(name, programs_dir, metadata_path)
        metadata["programs"][program["name"]]["enabled"] = enabled
        metadata["version"] = 4
        _write_metadata(metadata, metadata_path)
        program["enabled"] = enabled
        return program


def set_program_entrypoint(
    name: str,
    entrypoint: str,
    programs_dir: Path = PROGRAMS_DIR,
    metadata_path: Path = METADATA_PATH,
) -> dict[str, Any]:
    safe_entrypoint = _safe_relative_path(entrypoint, entrypoint=True)
    with _metadata_lock:
        program, metadata = _program_record(name, programs_dir, metadata_path)
        candidates = {
            item["path"].casefold(): item["path"] for item in program["files"]
        }
        if safe_entrypoint.casefold() not in candidates:
            raise ProgramError("Package entrypoint file not found.")
        actual_entrypoint = candidates[safe_entrypoint.casefold()]
        metadata["programs"][program["name"]]["entrypoint"] = actual_entrypoint
        metadata["version"] = 4
        _write_metadata(metadata, metadata_path)
        program["entrypoint"] = actual_entrypoint
        program["type"] = PureWindowsPath(actual_entrypoint).suffix[1:].upper()
        return program


def delete_program(
    name: str,
    programs_dir: Path = PROGRAMS_DIR,
    metadata_path: Path = METADATA_PATH,
) -> dict[str, Any]:
    safe_name = _safe_program_name(name)
    with _metadata_lock:
        program, metadata = _program_record(safe_name, programs_dir, metadata_path)
        target = programs_dir / program["name"]
        try:
            shutil.rmtree(target)
        except OSError as exc:
            raise ProgramError(f"Failed to delete the program package: {exc}") from exc
        metadata["programs"].pop(program["name"], None)
        _write_metadata(metadata, metadata_path)
    return {"deleted": True, "name": program["name"]}


def rename_program(
    name: str,
    new_name: str,
    programs_dir: Path = PROGRAMS_DIR,
    metadata_path: Path = METADATA_PATH,
) -> dict[str, Any]:
    safe_name = _safe_program_name(name)
    safe_new_name = _safe_program_name(new_name)
    if safe_name == safe_new_name:
        raise ProgramError("The new package name is unchanged.")
    with _metadata_lock:
        program, metadata = _program_record(safe_name, programs_dir, metadata_path)
        source = programs_dir / program["name"]
        destination = programs_dir / safe_new_name
        case_only = program["name"].casefold() == safe_new_name.casefold()
        if destination.exists() and not case_only:
            raise ProgramError(f"A package named '{safe_new_name}' already exists.")
        record = metadata["programs"].pop(program["name"])
        temporary: Path | None = None
        try:
            if case_only:
                temporary = programs_dir / f".irondeploy-rename-{uuid4().hex}"
                os.rename(source, temporary)
                os.rename(temporary, destination)
            else:
                os.rename(source, destination)
            metadata["programs"][safe_new_name] = record
            metadata["version"] = 4
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
            raise ProgramError(f"Failed to rename the program package: {exc}") from exc
    return {
        "renamed": True,
        "oldName": program["name"],
        "name": safe_new_name,
        "arguments": str(record.get("arguments", "")),
        "enabled": bool(record.get("enabled", True)),
    }


def _uploads_dir(programs_dir: Path) -> Path:
    return programs_dir / UPLOADS_NAME


def _upload_root(upload_id: str, programs_dir: Path) -> Path:
    if (
        not isinstance(upload_id, str)
        or len(upload_id) != 32
        or any(character not in "0123456789abcdef" for character in upload_id)
    ):
        raise ProgramError("Invalid package upload ID.")
    return _uploads_dir(programs_dir) / upload_id


def _upload_state(
    upload_id: str, programs_dir: Path
) -> tuple[Path, dict[str, Any]]:
    root = _upload_root(upload_id, programs_dir)
    try:
        state = json.loads((root / "upload.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProgramError("Package upload not found or invalid.") from exc
    if not isinstance(state, dict):
        raise ProgramError("Package upload not found or invalid.")
    return root, state


def _write_upload_state(root: Path, state: dict[str, Any]) -> None:
    temporary = root / f"upload.json.tmp.{uuid4().hex}"
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, root / "upload.json")


def start_program_upload(
    name: str,
    entrypoint: str,
    files: list[dict[str, Any]],
    arguments: str = "",
    programs_dir: Path = PROGRAMS_DIR,
) -> dict[str, Any]:
    safe_name = _safe_program_name(name)
    safe_entrypoint = _safe_relative_path(entrypoint, entrypoint=True)
    normalized_arguments = validate_program_arguments(arguments)
    if not isinstance(files, list) or not files or len(files) > MAX_PROGRAM_FILES:
        raise ProgramError(f"A package must contain 1 to {MAX_PROGRAM_FILES} files.")
    expected: dict[str, int] = {}
    expected_casefold: set[str] = set()
    total_size = 0
    for item in files:
        if not isinstance(item, dict) or type(item.get("size")) is not int:
            raise ProgramError("Invalid package file manifest.")
        relative = _safe_relative_path(item.get("path", ""))
        size = item["size"]
        if size < 0:
            raise ProgramError("Invalid package file size.")
        key = relative.casefold()
        if key in expected_casefold:
            raise ProgramError(f"Duplicate package file path: {relative}")
        expected_casefold.add(key)
        expected[relative] = size
        total_size += size
        if total_size > MAX_PROGRAM_SIZE_BYTES:
            raise ProgramError("Program packages are limited to 5 GiB.")
    entrypoint_match = next(
        (path for path in expected if path.casefold() == safe_entrypoint.casefold()),
        None,
    )
    if entrypoint_match is None:
        raise ProgramError("The package entrypoint is not present in the file manifest.")
    if expected[entrypoint_match] == 0:
        raise ProgramError("The package entrypoint file is empty.")

    programs_dir.mkdir(parents=True, exist_ok=True)
    if any(
        item.name.casefold() == safe_name.casefold() for item in programs_dir.iterdir()
    ):
        raise ProgramError(f"A package named '{safe_name}' already exists.")
    upload_id = uuid4().hex
    root = _uploads_dir(programs_dir) / upload_id
    (root / "files").mkdir(parents=True)
    state = {
        "version": 1,
        "name": safe_name,
        "entrypoint": entrypoint_match,
        "arguments": normalized_arguments,
        "expected": expected,
        "uploaded": {},
        "totalSize": total_size,
    }
    _write_upload_state(root, state)
    return {
        "uploadId": upload_id,
        "name": safe_name,
        "entrypoint": entrypoint_match,
        "fileCount": len(expected),
        "size": total_size,
    }


async def save_program_upload_file(
    upload_id: str,
    relative_path: str,
    chunks: AsyncIterator[bytes],
    programs_dir: Path = PROGRAMS_DIR,
) -> dict[str, Any]:
    safe_relative = _safe_relative_path(relative_path)
    with _metadata_lock:
        root, state = _upload_state(upload_id, programs_dir)
        actual_relative = next(
            (
                path
                for path in state["expected"]
                if path.casefold() == safe_relative.casefold()
            ),
            None,
        )
        if actual_relative is None:
            raise ProgramError("File is not part of this package upload.")
        if actual_relative in state["uploaded"]:
            raise ProgramError("Package file was already uploaded.")
        expected_size = int(state["expected"][actual_relative])
        state["uploaded"][actual_relative] = {"status": "uploading"}
        _write_upload_state(root, state)
    destination = _native_path(root / "files", actual_relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = _uploads_dir(programs_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    temporary = staging_dir / f"file-{uuid4().hex}.tmp"
    size = 0
    digest = hashlib.sha256()
    try:
        with temporary.open("xb") as handle:
            async for chunk in chunks:
                if chunk:
                    size += len(chunk)
                    if size > expected_size:
                        raise ProgramError(
                            "Uploaded package file is larger than declared."
                        )
                    handle.write(chunk)
                    digest.update(chunk)
        if size != expected_size:
            raise ProgramError(
                "Uploaded package file size does not match its manifest."
            )
        os.replace(temporary, destination)
        with _metadata_lock:
            root, state = _upload_state(upload_id, programs_dir)
            state["uploaded"][actual_relative] = {
                "status": "complete",
                "size": size,
                "sha256": digest.hexdigest(),
            }
            _write_upload_state(root, state)
    except Exception:
        temporary.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        try:
            with _metadata_lock:
                root, state = _upload_state(upload_id, programs_dir)
                uploaded = state.get("uploaded", {}).get(actual_relative)
                if isinstance(uploaded, dict) and uploaded.get("status") == "uploading":
                    state["uploaded"].pop(actual_relative, None)
                    _write_upload_state(root, state)
        except ProgramError:
            pass
        raise
    return {"uploaded": True, "path": actual_relative, "size": size}


def finalize_program_upload(
    upload_id: str,
    programs_dir: Path = PROGRAMS_DIR,
    metadata_path: Path = METADATA_PATH,
) -> dict[str, Any]:
    with _metadata_lock:
        root, state = _upload_state(upload_id, programs_dir)
        missing = []
        for path, expected_size in state["expected"].items():
            uploaded = state["uploaded"].get(path)
            if (
                not isinstance(uploaded, dict)
                or uploaded.get("status") != "complete"
                or uploaded.get("size") != expected_size
                or not isinstance(uploaded.get("sha256"), str)
            ):
                missing.append(path)
        if missing:
            raise ProgramError(
                f"Package upload is incomplete; {len(missing)} file(s) are missing."
            )
        destination = programs_dir / state["name"]
        if destination.exists():
            raise ProgramError(f"A package named '{state['name']}' already exists.")
        published = False
        previous_metadata: dict[str, Any] | None = None
        try:
            os.replace(root / "files", destination)
            published = True
            metadata = _read_metadata(metadata_path)
            previous_metadata = json.loads(json.dumps(metadata))
            metadata["programs"][state["name"]] = {
                "arguments": state["arguments"],
                "enabled": True,
                "entrypoint": state["entrypoint"],
            }
            metadata["version"] = 4
            _write_metadata(metadata, metadata_path)
            program = next(
                item
                for item in _list_programs(programs_dir, metadata_path)["programs"]
                if item["name"] == state["name"]
            )
        except Exception:
            if published and destination.exists() and not (root / "files").exists():
                os.replace(destination, root / "files")
            if previous_metadata is not None:
                try:
                    _write_metadata(previous_metadata, metadata_path)
                except ProgramError:
                    pass
            raise
        finally:
            if destination.exists():
                shutil.rmtree(root, ignore_errors=True)
    return {"uploaded": True, **program}


def cancel_program_upload(
    upload_id: str, programs_dir: Path = PROGRAMS_DIR
) -> dict[str, Any]:
    root = _upload_root(upload_id, programs_dir)
    if not root.exists():
        return {"deleted": False, "uploadId": upload_id}
    shutil.rmtree(root, ignore_errors=False)
    return {"deleted": True, "uploadId": upload_id}


async def save_uploaded_program(
    name: str,
    chunks: AsyncIterator[bytes],
    arguments: str = "",
    programs_dir: Path = PROGRAMS_DIR,
    metadata_path: Path = METADATA_PATH,
) -> dict[str, Any]:
    """Compatibility helper: wrap one uploaded installer in a package folder."""

    installer_name = _safe_relative_path(name, entrypoint=True)
    if "\\" in installer_name:
        raise ProgramError("The installer file name must not contain directories.")
    package_name = _safe_program_name(PureWindowsPath(installer_name).stem)
    normalized_arguments = validate_program_arguments(arguments)
    programs_dir.mkdir(parents=True, exist_ok=True)
    if any(
        item.name.casefold() == package_name.casefold()
        for item in programs_dir.iterdir()
    ):
        raise ProgramError(f"A package named '{package_name}' already exists.")
    temporary_root = _uploads_dir(programs_dir) / uuid4().hex
    temporary_root.mkdir(parents=True)
    temporary_file = temporary_root / installer_name
    size = 0
    try:
        with temporary_file.open("xb") as handle:
            async for chunk in chunks:
                if chunk:
                    size += len(chunk)
                    if size > MAX_PROGRAM_SIZE_BYTES:
                        raise ProgramError("Program packages are limited to 5 GiB.")
                    handle.write(chunk)
        if size == 0:
            raise ProgramError("The uploaded program file is empty.")
        destination = programs_dir / package_name
        with _metadata_lock:
            if destination.exists():
                raise ProgramError(f"A package named '{package_name}' already exists.")
            os.replace(temporary_root, destination)
            metadata = _read_metadata(metadata_path)
            metadata["programs"][package_name] = {
                "arguments": normalized_arguments,
                "enabled": True,
                "entrypoint": installer_name,
            }
            metadata["version"] = 4
            try:
                _write_metadata(metadata, metadata_path)
            except Exception:
                os.replace(destination, temporary_root)
                raise
            program = next(
                item
                for item in _list_programs(programs_dir, metadata_path)["programs"]
                if item["name"] == package_name
            )
    finally:
        if temporary_root.exists():
            shutil.rmtree(temporary_root, ignore_errors=True)
    return {"uploaded": True, **program}


async def add_program_file(
    name: str,
    relative_path: str,
    chunks: AsyncIterator[bytes],
    programs_dir: Path = PROGRAMS_DIR,
    metadata_path: Path = METADATA_PATH,
) -> dict[str, Any]:
    safe_relative = _safe_relative_path(relative_path)
    with _metadata_lock:
        program, _ = _program_record(name, programs_dir, metadata_path)
        if program["fileCount"] >= MAX_PROGRAM_FILES:
            raise ProgramError(f"Packages are limited to {MAX_PROGRAM_FILES} files.")
        package_dir = programs_dir / program["name"]
        destination = _native_path(package_dir, safe_relative)
        reservation = str(destination.absolute()).casefold()
        if destination.exists() or reservation in _pending_files:
            raise _file_conflict()
        _pending_files.add(reservation)
    temporary = _uploads_dir(programs_dir) / f"file-{uuid4().hex}.tmp"
    size = 0
    published = False
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("xb") as handle:
            async for chunk in chunks:
                if chunk:
                    size += len(chunk)
                    if program["size"] + size > MAX_PROGRAM_SIZE_BYTES:
                        raise ProgramError("Program packages are limited to 5 GiB.")
                    handle.write(chunk)
        with _metadata_lock:
            if destination.exists():
                raise _file_conflict()
            # A hard link publishes atomically without overwriting a file that
            # appeared during the upload (including from another process).
            try:
                os.link(temporary, destination)
            except FileExistsError:
                raise _file_conflict() from None
            published = True
            updated, metadata = _program_record(
                program["name"], programs_dir, metadata_path
            )
            metadata["version"] = 4
            _write_metadata(metadata, metadata_path)
    except BaseException:
        if published:
            destination.unlink(missing_ok=True)
        raise
    finally:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            with _metadata_lock:
                _pending_files.discard(reservation)
    return {
        "uploaded": True,
        "path": safe_relative,
        "size": size,
        "program": updated,
    }


def delete_program_file(
    name: str,
    relative_path: str,
    programs_dir: Path = PROGRAMS_DIR,
    metadata_path: Path = METADATA_PATH,
) -> dict[str, Any]:
    safe_relative = _safe_relative_path(relative_path)
    with _metadata_lock:
        program, _ = _program_record(name, programs_dir, metadata_path)
        actual = next(
            (
                item["path"]
                for item in program["files"]
                if item["path"].casefold() == safe_relative.casefold()
            ),
            None,
        )
        if actual is None:
            raise ProgramError("Package file not found.")
        target = _native_path(programs_dir / program["name"], actual)
        target.unlink()
        parent = target.parent
        package_root = programs_dir / program["name"]
        while parent != package_root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        updated = _list_programs(programs_dir, metadata_path)
    return {"deleted": True, "path": actual, "programs": updated["programs"]}
