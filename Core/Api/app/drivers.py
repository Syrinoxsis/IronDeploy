"""Manage vendor/model driver packages stored in ``Share\Drivers``."""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, AsyncIterator
from uuid import uuid4

from app.config import IRONDEPLOY_ROOT


DRIVERS_DIR = IRONDEPLOY_ROOT / "Share" / "Drivers"
UPLOADS_DIRECTORY_NAME = ".irondeploy-uploads"
UPLOAD_METADATA_NAME = "upload.json"
UPLOAD_FILES_DIRECTORY_NAME = "files"
MAX_DRIVER_FILE_SIZE_BYTES = 5 * 1024**3
MAX_DRIVER_PACKAGE_SIZE_BYTES = 20 * 1024**3

_INVALID_WINDOWS_CHARACTERS = set('<>:"/\\|?*')
_RESERVED_WINDOWS_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{number}"
    for prefix in ("COM", "LPT")
    for number in range(1, 10)
}
_UPLOAD_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_driver_lock = RLock()


@dataclass(frozen=True)
class DriverUploadLimits:
    max_files: int = 25000
    max_depth: int = 16
    max_full_path: int = 240
    upload_ttl_hours: int = 24
    max_active_uploads: int = 3
    min_free_space_gib: int = 25


DEFAULT_DRIVER_UPLOAD_LIMITS = DriverUploadLimits()


class DriverError(RuntimeError):
    """Raised when a driver-management operation cannot be completed."""


def _safe_directory_name(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 255
        or value in {".", ".."}
        or value[-1] in {".", " "}
        or any(
            ord(character) < 32 or character in _INVALID_WINDOWS_CHARACTERS
            for character in value
        )
    ):
        raise DriverError(f"Invalid {label} name.")
    device_stem = value.split(".", 1)[0].rstrip(" .").upper()
    if device_stem in _RESERVED_WINDOWS_NAMES:
        raise DriverError(f"Invalid {label} name: reserved Windows device name.")
    return value


def _safe_vendor_name(value: str) -> str:
    return _safe_directory_name(value, "vendor")


def _safe_model_name(value: str) -> str:
    return _safe_directory_name(value, "model")


def _safe_relative_path(value: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise DriverError("Invalid driver file path.")
    parts = value.split("/")
    if value.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise DriverError("Invalid driver file path.")
    safe_parts = [
        _safe_directory_name(part, "driver file or directory")
        for part in parts
    ]
    return Path(*safe_parts)


def _find_child_casefold(parent: Path, name: str) -> Path | None:
    if not parent.is_dir():
        return None
    folded = name.casefold()
    try:
        return next(
            (child for child in parent.iterdir() if child.name.casefold() == folded),
            None,
        )
    except OSError as exc:
        raise DriverError(f"Failed to inspect '{parent}': {exc}") from exc


def _require_directory(parent: Path, name: str, label: str) -> Path:
    match = _find_child_casefold(parent, name)
    if match is None or not match.is_dir():
        raise DriverError(f"{label} not found.")
    return match


def _rename_directory(source: Path, destination: Path) -> None:
    case_only_rename = source.name.casefold() == destination.name.casefold()
    temporary: Path | None = None
    try:
        if case_only_rename:
            temporary = source.parent / f".rename-{uuid4().hex}.tmp"
            os.rename(source, temporary)
            os.rename(temporary, destination)
        else:
            os.rename(source, destination)
    except OSError as exc:
        try:
            if temporary is not None and temporary.exists():
                os.rename(temporary, source)
            elif destination.exists() and not source.exists():
                os.rename(destination, source)
        except OSError:
            pass
        raise DriverError(f"Failed to rename the directory: {exc}") from exc


def _package_details(vendor: str, package_path: Path) -> dict[str, Any]:
    total_size = 0
    inf_count = 0
    file_count = 0
    modified_at = package_path.stat().st_mtime
    try:
        for path in package_path.rglob("*"):
            if not path.is_file():
                continue
            stat = path.stat()
            total_size += stat.st_size
            file_count += 1
            modified_at = max(modified_at, stat.st_mtime)
            if path.suffix.casefold() == ".inf":
                inf_count += 1
    except OSError as exc:
        raise DriverError(
            f"Failed to inspect driver package '{vendor}/{package_path.name}': {exc}"
        ) from exc
    return {
        "vendor": vendor,
        "model": package_path.name,
        "relativePath": f"{vendor}\\{package_path.name}",
        "size": total_size,
        "infCount": inf_count,
        "fileCount": file_count,
        "modifiedAt": datetime.fromtimestamp(
            modified_at, timezone.utc
        ).isoformat(),
    }


def list_driver_packages(drivers_dir: Path = DRIVERS_DIR) -> dict[str, Any]:
    with _driver_lock:
        try:
            drivers_dir.mkdir(parents=True, exist_ok=True)
            vendor_paths = sorted(
                (
                    path
                    for path in drivers_dir.iterdir()
                    if path.is_dir() and not path.name.startswith(".")
                ),
                key=lambda path: path.name.casefold(),
            )
            vendors = []
            packages = []
            for vendor_path in vendor_paths:
                vendor_packages = [
                    _package_details(vendor_path.name, package_path)
                    for package_path in sorted(
                        (
                            path
                            for path in vendor_path.iterdir()
                            if path.is_dir() and not path.name.startswith(".")
                        ),
                        key=lambda path: path.name.casefold(),
                    )
                ]
                vendors.append(
                    {
                        "name": vendor_path.name,
                        "packageCount": len(vendor_packages),
                    }
                )
                packages.extend(vendor_packages)
        except DriverError:
            raise
        except OSError as exc:
            raise DriverError(f"Failed to list driver packages: {exc}") from exc
    return {
        "vendors": vendors,
        "packages": packages,
        "directory": str(drivers_dir),
    }


def create_vendor(name: str, drivers_dir: Path = DRIVERS_DIR) -> dict[str, Any]:
    safe_name = _safe_vendor_name(name)
    with _driver_lock:
        drivers_dir.mkdir(parents=True, exist_ok=True)
        if _find_child_casefold(drivers_dir, safe_name) is not None:
            raise DriverError(f"A vendor named '{safe_name}' already exists.")
        try:
            (drivers_dir / safe_name).mkdir()
        except OSError as exc:
            raise DriverError(f"Failed to create the vendor: {exc}") from exc
    return {"created": True, "name": safe_name}


def rename_vendor(
    name: str,
    new_name: str,
    drivers_dir: Path = DRIVERS_DIR,
) -> dict[str, Any]:
    safe_name = _safe_vendor_name(name)
    safe_new_name = _safe_vendor_name(new_name)
    if safe_name == safe_new_name:
        raise DriverError("The new vendor name is unchanged.")
    with _driver_lock:
        source = _require_directory(drivers_dir, safe_name, "Vendor")
        conflict = _find_child_casefold(drivers_dir, safe_new_name)
        if conflict is not None and conflict != source:
            raise DriverError(f"A vendor named '{safe_new_name}' already exists.")
        _rename_directory(source, drivers_dir / safe_new_name)
    return {"renamed": True, "oldName": source.name, "name": safe_new_name}


def delete_vendor(name: str, drivers_dir: Path = DRIVERS_DIR) -> dict[str, Any]:
    safe_name = _safe_vendor_name(name)
    with _driver_lock:
        target = _require_directory(drivers_dir, safe_name, "Vendor")
        try:
            shutil.rmtree(target)
        except OSError as exc:
            raise DriverError(f"Failed to delete the vendor: {exc}") from exc
    return {"deleted": True, "name": target.name}


def rename_driver_package(
    vendor: str,
    model: str,
    new_model: str,
    drivers_dir: Path = DRIVERS_DIR,
) -> dict[str, Any]:
    safe_vendor = _safe_vendor_name(vendor)
    safe_model = _safe_model_name(model)
    safe_new_model = _safe_model_name(new_model)
    if safe_model == safe_new_model:
        raise DriverError("The new model name is unchanged.")
    with _driver_lock:
        vendor_path = _require_directory(drivers_dir, safe_vendor, "Vendor")
        source = _require_directory(vendor_path, safe_model, "Driver package")
        conflict = _find_child_casefold(vendor_path, safe_new_model)
        if conflict is not None and conflict != source:
            raise DriverError(
                f"A package named '{safe_new_model}' already exists for this vendor."
            )
        _rename_directory(source, vendor_path / safe_new_model)
    return {
        "renamed": True,
        "vendor": vendor_path.name,
        "oldModel": source.name,
        "model": safe_new_model,
    }


def delete_driver_package(
    vendor: str,
    model: str,
    drivers_dir: Path = DRIVERS_DIR,
) -> dict[str, Any]:
    safe_vendor = _safe_vendor_name(vendor)
    safe_model = _safe_model_name(model)
    with _driver_lock:
        vendor_path = _require_directory(drivers_dir, safe_vendor, "Vendor")
        target = _require_directory(vendor_path, safe_model, "Driver package")
        try:
            shutil.rmtree(target)
        except OSError as exc:
            raise DriverError(f"Failed to delete the driver package: {exc}") from exc
    return {
        "deleted": True,
        "vendor": vendor_path.name,
        "model": target.name,
    }


def _uploads_directory(drivers_dir: Path) -> Path:
    return drivers_dir / UPLOADS_DIRECTORY_NAME


def _upload_directories(drivers_dir: Path) -> list[Path]:
    uploads_directory = _uploads_directory(drivers_dir)
    if not uploads_directory.is_dir():
        return []
    try:
        return [
            path
            for path in uploads_directory.iterdir()
            if path.is_dir() and _UPLOAD_ID_PATTERN.fullmatch(path.name)
        ]
    except OSError as exc:
        raise DriverError(f"Failed to inspect unfinished driver uploads: {exc}") from exc


def _upload_directory(upload_id: str, drivers_dir: Path) -> Path:
    if not isinstance(upload_id, str) or not _UPLOAD_ID_PATTERN.fullmatch(upload_id):
        raise DriverError("Invalid driver upload identifier.")
    return _uploads_directory(drivers_dir) / upload_id


def _read_upload(upload_directory: Path) -> dict[str, Any]:
    try:
        payload = json.loads(
            (upload_directory / UPLOAD_METADATA_NAME).read_text(encoding="utf-8")
        )
    except FileNotFoundError as exc:
        raise DriverError("Driver upload not found.") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise DriverError(f"Failed to read driver upload state: {exc}") from exc
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("vendor"), str)
        or not isinstance(payload.get("model"), str)
        or not isinstance(payload.get("files"), dict)
    ):
        raise DriverError("Driver upload state has an invalid format.")
    return payload


def _write_upload(upload_directory: Path, payload: dict[str, Any]) -> None:
    metadata_path = upload_directory / UPLOAD_METADATA_NAME
    temporary = upload_directory / f".{UPLOAD_METADATA_NAME}.{uuid4().hex}.tmp"
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, metadata_path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise DriverError(f"Failed to save driver upload state: {exc}") from exc


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_final_driver_path(
    path: Path,
    limits: DriverUploadLimits,
) -> None:
    full_path = os.path.abspath(path)
    if len(full_path) > limits.max_full_path:
        raise DriverError(
            "Driver path exceeds the maximum full path length of "
            f"{limits.max_full_path} characters."
        )


def _validate_uploaded_file_path(
    safe_path: Path,
    payload: dict[str, Any],
    drivers_dir: Path,
    limits: DriverUploadLimits,
) -> None:
    directory_depth = max(len(safe_path.parts) - 1, 0)
    if directory_depth > limits.max_depth:
        raise DriverError(
            "Driver path exceeds the maximum directory depth of "
            f"{limits.max_depth}."
        )
    final_path = (
        drivers_dir
        / _safe_vendor_name(payload["vendor"])
        / _safe_model_name(payload["model"])
        / safe_path
    )
    _validate_final_driver_path(final_path, limits)


def begin_driver_package_upload(
    vendor: str,
    model: str,
    drivers_dir: Path = DRIVERS_DIR,
    limits: DriverUploadLimits = DEFAULT_DRIVER_UPLOAD_LIMITS,
) -> dict[str, Any]:
    safe_vendor = _safe_vendor_name(vendor)
    safe_model = _safe_model_name(model)
    upload_id = uuid4().hex
    with _driver_lock:
        vendor_path = _require_directory(drivers_dir, safe_vendor, "Vendor")
        _validate_final_driver_path(vendor_path / safe_model, limits)
        if _find_child_casefold(vendor_path, safe_model) is not None:
            raise DriverError(
                f"A package named '{safe_model}' already exists for this vendor."
            )
        unfinished_uploads = _upload_directories(drivers_dir)
        if len(unfinished_uploads) >= limits.max_active_uploads:
            raise DriverError(
                "The server already has the maximum number of unfinished driver "
                f"uploads ({limits.max_active_uploads}). Remove an abandoned upload "
                "from the Info page and try again."
            )
        try:
            free_bytes = shutil.disk_usage(drivers_dir).free
        except OSError as exc:
            raise DriverError(f"Failed to check free disk space: {exc}") from exc
        required_bytes = limits.min_free_space_gib * 1024**3
        if free_bytes < required_bytes:
            raise DriverError(
                f"Free space: {free_bytes / 1024**3:.1f} GiB. "
                f"Required minimum: {limits.min_free_space_gib} GiB. "
                "Upload was not started."
            )
        upload_directory = _uploads_directory(drivers_dir) / upload_id
        now = _utc_now_iso()
        try:
            (upload_directory / UPLOAD_FILES_DIRECTORY_NAME).mkdir(
                parents=True, exist_ok=False
            )
            _write_upload(
                upload_directory,
                {
                    "version": 2,
                    "vendor": vendor_path.name,
                    "model": safe_model,
                    "files": {},
                    "size": 0,
                    "infCount": 0,
                    "createdAt": now,
                    "updatedAt": now,
                },
            )
        except Exception:
            shutil.rmtree(upload_directory, ignore_errors=True)
            raise
    return {
        "uploadId": upload_id,
        "vendor": vendor_path.name,
        "model": safe_model,
    }


async def save_uploaded_driver_file(
    upload_id: str,
    relative_path: str,
    chunks: AsyncIterator[bytes],
    drivers_dir: Path = DRIVERS_DIR,
    limits: DriverUploadLimits = DEFAULT_DRIVER_UPLOAD_LIMITS,
) -> dict[str, Any]:
    safe_path = _safe_relative_path(relative_path)
    upload_directory = _upload_directory(upload_id, drivers_dir)
    destination = upload_directory / UPLOAD_FILES_DIRECTORY_NAME / safe_path
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    size = 0
    published = False

    with _driver_lock:
        payload = _read_upload(upload_directory)
        _validate_uploaded_file_path(safe_path, payload, drivers_dir, limits)
        folded_path = safe_path.as_posix().casefold()
        if folded_path in payload["files"]:
            raise DriverError(f"Driver file '{safe_path.as_posix()}' was uploaded twice.")
        received_file_count = len(payload["files"]) + 1
        if received_file_count > limits.max_files:
            raise DriverError(
                "Driver package contains too many files. "
                f"Maximum: {limits.max_files}. Received: {received_file_count}."
            )
        current_size = int(payload.get("size", 0))
        payload["updatedAt"] = _utc_now_iso()
        _write_upload(upload_directory, payload)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DriverError(f"Failed to create driver directories: {exc}") from exc

    try:
        with temporary.open("xb") as handle:
            async for chunk in chunks:
                if not chunk:
                    continue
                if size + len(chunk) > MAX_DRIVER_FILE_SIZE_BYTES:
                    raise DriverError("Individual driver files are limited to 5 GiB.")
                if current_size + size + len(chunk) > MAX_DRIVER_PACKAGE_SIZE_BYTES:
                    raise DriverError("Driver packages are limited to 20 GiB.")
                handle.write(chunk)
                size += len(chunk)

        with _driver_lock:
            payload = _read_upload(upload_directory)
            folded_path = safe_path.as_posix().casefold()
            if folded_path in payload["files"] or destination.exists():
                raise DriverError(
                    f"Driver file '{safe_path.as_posix()}' was uploaded twice."
                )
            received_file_count = len(payload["files"]) + 1
            if received_file_count > limits.max_files:
                raise DriverError(
                    "Driver package contains too many files. "
                    f"Maximum: {limits.max_files}. Received: {received_file_count}."
                )
            if int(payload.get("size", 0)) + size > MAX_DRIVER_PACKAGE_SIZE_BYTES:
                raise DriverError("Driver packages are limited to 20 GiB.")
            os.replace(temporary, destination)
            published = True
            payload["files"][folded_path] = {
                "path": safe_path.as_posix(),
                "size": size,
            }
            payload["size"] = int(payload.get("size", 0)) + size
            if safe_path.suffix.casefold() == ".inf":
                payload["infCount"] = int(payload.get("infCount", 0)) + 1
            payload["updatedAt"] = _utc_now_iso()
            _write_upload(upload_directory, payload)
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
        raise DriverError(f"Failed to save the driver file: {exc}") from exc
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
        "path": safe_path.as_posix(),
        "size": size,
    }


def finalize_driver_package_upload(
    upload_id: str,
    drivers_dir: Path = DRIVERS_DIR,
) -> dict[str, Any]:
    upload_directory = _upload_directory(upload_id, drivers_dir)
    with _driver_lock:
        payload = _read_upload(upload_directory)
        if not payload["files"]:
            raise DriverError("The selected driver folder contains no files.")
        if int(payload.get("infCount", 0)) <= 0:
            raise DriverError("The selected driver folder contains no INF files.")

        vendor_path = _require_directory(
            drivers_dir,
            _safe_vendor_name(payload["vendor"]),
            "Vendor",
        )
        model = _safe_model_name(payload["model"])
        if _find_child_casefold(vendor_path, model) is not None:
            raise DriverError(
                f"A package named '{model}' already exists for this vendor."
            )
        source = upload_directory / UPLOAD_FILES_DIRECTORY_NAME
        destination = vendor_path / model
        try:
            os.rename(source, destination)
            shutil.rmtree(upload_directory)
        except OSError as exc:
            if destination.exists() and not source.exists():
                try:
                    os.rename(destination, source)
                except OSError:
                    pass
            raise DriverError(f"Failed to publish the driver package: {exc}") from exc

        package = _package_details(vendor_path.name, destination)
    return {"uploaded": True, "package": package}


def cancel_driver_package_upload(
    upload_id: str,
    drivers_dir: Path = DRIVERS_DIR,
) -> dict[str, Any]:
    upload_directory = _upload_directory(upload_id, drivers_dir)
    with _driver_lock:
        if not upload_directory.is_dir():
            raise DriverError("Driver upload not found.")
        try:
            shutil.rmtree(upload_directory)
        except OSError as exc:
            raise DriverError(f"Failed to cancel the driver upload: {exc}") from exc
    return {"cancelled": True, "uploadId": upload_id}


def _parse_upload_time(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    return fallback


def _upload_info(
    upload_directory: Path,
    limits: DriverUploadLimits,
    now: datetime,
) -> dict[str, Any]:
    try:
        modified = datetime.fromtimestamp(
            (upload_directory / UPLOAD_METADATA_NAME).stat().st_mtime,
            timezone.utc,
        )
    except OSError:
        modified = datetime.fromtimestamp(upload_directory.stat().st_mtime, timezone.utc)
    try:
        payload = _read_upload(upload_directory)
        size = int(payload.get("size", 0))
        file_count = len(payload["files"])
    except (DriverError, TypeError, ValueError):
        return {
            "uploadId": upload_directory.name,
            "vendor": "",
            "model": "",
            "size": 0,
            "fileCount": 0,
            "createdAt": modified.isoformat(),
            "updatedAt": modified.isoformat(),
            "status": "invalid",
        }

    created_at = _parse_upload_time(payload.get("createdAt"), modified)
    updated_at = _parse_upload_time(payload.get("updatedAt"), modified)
    abandoned = now - updated_at > timedelta(hours=limits.upload_ttl_hours)
    return {
        "uploadId": upload_directory.name,
        "vendor": payload["vendor"],
        "model": payload["model"],
        "size": size,
        "fileCount": file_count,
        "createdAt": created_at.isoformat(),
        "updatedAt": updated_at.isoformat(),
        "status": "abandoned" if abandoned else "active",
    }


def get_driver_upload_info(
    limits: DriverUploadLimits = DEFAULT_DRIVER_UPLOAD_LIMITS,
    drivers_dir: Path = DRIVERS_DIR,
) -> dict[str, Any]:
    with _driver_lock:
        try:
            drivers_dir.mkdir(parents=True, exist_ok=True)
            usage = shutil.disk_usage(drivers_dir)
            now = datetime.now(timezone.utc)
            uploads = [
                _upload_info(path, limits, now)
                for path in _upload_directories(drivers_dir)
            ]
        except DriverError:
            raise
        except OSError as exc:
            raise DriverError(f"Failed to inspect driver upload storage: {exc}") from exc

    uploads.sort(
        key=lambda item: (
            {"abandoned": 0, "invalid": 1, "active": 2}[item["status"]],
            item["updatedAt"],
        )
    )
    counts = {
        "total": len(uploads),
        "active": sum(item["status"] == "active" for item in uploads),
        "abandoned": sum(item["status"] == "abandoned" for item in uploads),
        "invalid": sum(item["status"] == "invalid" for item in uploads),
    }
    minimum_free_bytes = limits.min_free_space_gib * 1024**3
    return {
        "directory": str(drivers_dir),
        "storage": {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "minimumFree": minimum_free_bytes,
            "hasMinimumFreeSpace": usage.free >= minimum_free_bytes,
        },
        "limits": {
            "maxFiles": limits.max_files,
            "maxDepth": limits.max_depth,
            "maxFullPath": limits.max_full_path,
            "uploadTtlHours": limits.upload_ttl_hours,
            "maxActiveUploads": limits.max_active_uploads,
            "minFreeSpaceGiB": limits.min_free_space_gib,
        },
        "counts": counts,
        "uploads": uploads,
    }


def delete_abandoned_driver_upload(
    upload_id: str,
    limits: DriverUploadLimits = DEFAULT_DRIVER_UPLOAD_LIMITS,
    drivers_dir: Path = DRIVERS_DIR,
) -> dict[str, Any]:
    upload_directory = _upload_directory(upload_id, drivers_dir)
    with _driver_lock:
        if not upload_directory.is_dir():
            raise DriverError("Driver upload not found.")
        info = _upload_info(upload_directory, limits, datetime.now(timezone.utc))
        if info["status"] not in {"abandoned", "invalid"}:
            raise DriverError(
                "Only abandoned or invalid driver uploads can be deleted here."
            )
        try:
            shutil.rmtree(upload_directory)
        except OSError as exc:
            raise DriverError(f"Failed to delete the abandoned upload: {exc}") from exc
    return {"deleted": True, "uploadId": upload_id}


def delete_all_abandoned_driver_uploads(
    limits: DriverUploadLimits = DEFAULT_DRIVER_UPLOAD_LIMITS,
    drivers_dir: Path = DRIVERS_DIR,
) -> dict[str, Any]:
    deleted: list[str] = []
    with _driver_lock:
        now = datetime.now(timezone.utc)
        for upload_directory in _upload_directories(drivers_dir):
            info = _upload_info(upload_directory, limits, now)
            if info["status"] != "abandoned":
                continue
            try:
                shutil.rmtree(upload_directory)
            except OSError as exc:
                raise DriverError(
                    f"Failed to delete abandoned upload '{upload_directory.name}': {exc}"
                ) from exc
            deleted.append(upload_directory.name)
    return {"deleted": len(deleted), "uploadIds": deleted}
