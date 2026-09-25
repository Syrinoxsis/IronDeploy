"""Temporary upload storage for drivers embedded into the next WinPE build.

TEMPORARY WINPE DRIVER UPLOAD: this module is intentionally self-contained so
the stop-gap feature can be removed without touching normal deployment drivers.
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, RLock
from typing import Any, AsyncIterator
from uuid import uuid4

from app.config import IRONDEPLOY_ROOT
from app.drivers import DriverUploadLimits


WINPE_DRIVERS_DIR = IRONDEPLOY_ROOT / "WinPE" / "Runtime" / "WinPEDrivers"
UPLOADS_DIR = IRONDEPLOY_ROOT / "WinPE" / "Runtime" / ".winpe-driver-uploads"
MAX_DRIVER_FILE_SIZE_BYTES = 5 * 1024**3
MAX_DRIVER_SET_SIZE_BYTES = 20 * 1024**3

_UPLOAD_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_INVALID_WINDOWS_CHARACTERS = set('<>:"/\\|?*')
_RESERVED_WINDOWS_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{number}"
    for prefix in ("COM", "LPT")
    for number in range(1, 10)
}
_lock = RLock()
_active_uploads: dict[str, Event] = {}


class WinPEDriverUploadError(RuntimeError):
    """Raised when the temporary WinPE-driver upload cannot be completed."""


def _safe_part(value: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or value[-1] in {".", " "}
        or len(value) > 255
        or any(
            ord(character) < 32 or character in _INVALID_WINDOWS_CHARACTERS
            for character in value
        )
    ):
        raise WinPEDriverUploadError("Invalid WinPE driver file path.")
    stem = value.split(".", 1)[0].rstrip(" .").upper()
    if stem in _RESERVED_WINDOWS_NAMES:
        raise WinPEDriverUploadError("Invalid WinPE driver file path.")
    return value


def _safe_relative_path(value: str) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or value.startswith("/")
    ):
        raise WinPEDriverUploadError("Invalid WinPE driver file path.")
    parts = value.split("/")
    if any(not part for part in parts):
        raise WinPEDriverUploadError("Invalid WinPE driver file path.")
    return Path(*(_safe_part(part) for part in parts))


def _upload_directory(upload_id: str) -> Path:
    if not isinstance(upload_id, str) or not _UPLOAD_ID_PATTERN.fullmatch(upload_id):
        raise WinPEDriverUploadError("Invalid WinPE driver upload identifier.")
    path = UPLOADS_DIR / upload_id
    if not path.is_dir():
        raise WinPEDriverUploadError("WinPE driver upload not found.")
    return path


def _scan(directory: Path) -> dict[str, Any]:
    file_count = 0
    inf_count = 0
    size = 0
    modified_at: float | None = None
    if directory.is_dir():
        try:
            for path in directory.rglob("*"):
                if not path.is_file():
                    continue
                stat = path.stat()
                file_count += 1
                size += stat.st_size
                modified_at = max(modified_at or stat.st_mtime, stat.st_mtime)
                if path.suffix.casefold() == ".inf":
                    inf_count += 1
        except OSError as exc:
            raise WinPEDriverUploadError(
                f"Failed to inspect WinPE drivers: {exc}"
            ) from exc
    return {
        "available": file_count > 0,
        "fileCount": file_count,
        "infCount": inf_count,
        "size": size,
        "modifiedAt": (
            datetime.fromtimestamp(modified_at, timezone.utc).isoformat()
            if modified_at is not None
            else None
        ),
        "directory": str(directory),
    }


def get_winpe_drivers() -> dict[str, Any]:
    with _lock:
        return _scan(WINPE_DRIVERS_DIR)


def begin_winpe_driver_upload(limits: DriverUploadLimits) -> dict[str, str]:
    with _lock:
        active = [
            key
            for key, cancellation in _active_uploads.items()
            if not cancellation.is_set()
        ]
        if active:
            raise WinPEDriverUploadError("A WinPE driver upload is already running.")
        try:
            UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
            required_free_bytes = limits.min_free_space_gib * 1024**3
            if shutil.disk_usage(UPLOADS_DIR).free < required_free_bytes:
                raise WinPEDriverUploadError(
                    f"At least {limits.min_free_space_gib} GiB of free space is required."
                )
            upload_id = uuid4().hex
            (UPLOADS_DIR / upload_id / "files").mkdir(parents=True)
        except WinPEDriverUploadError:
            raise
        except OSError as exc:
            raise WinPEDriverUploadError(
                f"Failed to start WinPE driver upload: {exc}"
            ) from exc
        _active_uploads[upload_id] = Event()
        return {"uploadId": upload_id}


def _validate_path(path: Path, limits: DriverUploadLimits) -> None:
    if len(path.parts) > limits.max_depth:
        raise WinPEDriverUploadError(
            f"WinPE driver path exceeds the maximum depth of {limits.max_depth}."
        )
    embedded_path = Path("X:/IronDeploy/WinPEDrivers") / path
    if len(str(embedded_path)) > limits.max_full_path:
        raise WinPEDriverUploadError(
            f"WinPE driver path exceeds {limits.max_full_path} characters."
        )


async def save_winpe_driver_file(
    upload_id: str,
    relative_path: str,
    chunks: AsyncIterator[bytes],
    limits: DriverUploadLimits,
) -> dict[str, Any]:
    safe_path = _safe_relative_path(relative_path)
    _validate_path(safe_path, limits)
    with _lock:
        cancellation = _active_uploads.get(upload_id)
        if cancellation is None or cancellation.is_set():
            raise WinPEDriverUploadError("WinPE driver upload was interrupted.")
        upload_directory = _upload_directory(upload_id)
        files_directory = upload_directory / "files"
        existing = _scan(files_directory)
        if existing["fileCount"] >= limits.max_files:
            raise WinPEDriverUploadError(
                f"WinPE driver folder is limited to {limits.max_files} files."
            )
        destination = files_directory / safe_path
        if destination.exists():
            raise WinPEDriverUploadError(
                f"WinPE driver file '{safe_path.as_posix()}' was uploaded twice."
            )
        temporary = upload_directory / f".{uuid4().hex}.part"
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WinPEDriverUploadError(
                f"Failed to create driver path: {exc}"
            ) from exc

    size = 0
    try:
        with temporary.open("xb") as handle:
            async for chunk in chunks:
                if cancellation.is_set():
                    raise WinPEDriverUploadError("WinPE driver upload was cancelled.")
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_DRIVER_FILE_SIZE_BYTES:
                    raise WinPEDriverUploadError(
                        "Individual driver files are limited to 5 GiB."
                    )
                if existing["size"] + size > MAX_DRIVER_SET_SIZE_BYTES:
                    raise WinPEDriverUploadError("WinPE drivers are limited to 20 GiB.")
                handle.write(chunk)
        with _lock:
            if cancellation.is_set():
                raise WinPEDriverUploadError("WinPE driver upload was cancelled.")
            if destination.exists():
                raise WinPEDriverUploadError(
                    f"WinPE driver file '{safe_path.as_posix()}' was uploaded twice."
                )
            os.replace(temporary, destination)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        if cancellation.is_set():
            shutil.rmtree(upload_directory, ignore_errors=True)
        raise WinPEDriverUploadError(
            f"Failed to save WinPE driver file: {exc}"
        ) from exc
    except Exception:
        temporary.unlink(missing_ok=True)
        if cancellation.is_set():
            shutil.rmtree(upload_directory, ignore_errors=True)
        raise
    return {"uploaded": True, "path": safe_path.as_posix(), "size": size}


def finalize_winpe_driver_upload(upload_id: str) -> dict[str, Any]:
    with _lock:
        cancellation = _active_uploads.get(upload_id)
        if cancellation is None or cancellation.is_set():
            raise WinPEDriverUploadError("WinPE driver upload was interrupted.")
        upload_directory = _upload_directory(upload_id)
        source = upload_directory / "files"
        details = _scan(source)
        if not details["available"]:
            raise WinPEDriverUploadError("The selected WinPE driver folder is empty.")
        if details["infCount"] <= 0:
            raise WinPEDriverUploadError("The selected folder contains no INF files.")

        backup = WINPE_DRIVERS_DIR.with_name(f".WinPEDrivers-backup-{uuid4().hex}")
        moved_current = False
        published = False
        try:
            if WINPE_DRIVERS_DIR.exists():
                os.rename(WINPE_DRIVERS_DIR, backup)
                moved_current = True
            os.rename(source, WINPE_DRIVERS_DIR)
            published = True
            shutil.rmtree(upload_directory)
            if moved_current:
                shutil.rmtree(backup)
        except OSError as exc:
            try:
                if WINPE_DRIVERS_DIR.exists() and published:
                    shutil.rmtree(WINPE_DRIVERS_DIR)
                if moved_current and backup.exists():
                    os.rename(backup, WINPE_DRIVERS_DIR)
            except OSError:
                pass
            raise WinPEDriverUploadError(
                f"Failed to publish WinPE drivers: {exc}"
            ) from exc
        finally:
            _active_uploads.pop(upload_id, None)
        return _scan(WINPE_DRIVERS_DIR)


def cancel_winpe_driver_upload(upload_id: str) -> dict[str, bool]:
    with _lock:
        upload_directory = _upload_directory(upload_id)
        cancellation = _active_uploads.pop(upload_id, None)
        if cancellation is not None:
            cancellation.set()
        try:
            shutil.rmtree(upload_directory)
        except OSError as exc:
            raise WinPEDriverUploadError(
                f"Failed to cancel WinPE driver upload: {exc}"
            ) from exc
        return {"cancelled": True}


def delete_winpe_drivers() -> dict[str, bool]:
    with _lock:
        if not WINPE_DRIVERS_DIR.exists():
            return {"deleted": False}
        try:
            shutil.rmtree(WINPE_DRIVERS_DIR)
        except OSError as exc:
            raise WinPEDriverUploadError(
                f"Failed to delete WinPE drivers: {exc}"
            ) from exc
        return {"deleted": True}
