"""Discover, upload, configure, and convert Windows deployment images."""

from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, RLock, Thread
from typing import Any, AsyncIterator
from uuid import uuid4

from app.config import IRONDEPLOY_ROOT
from app.file_names import validate_windows_file_name


IMAGES_DIR = IRONDEPLOY_ROOT / "Share" / "Images"
METADATA_NAME = ".irondeploy-images.json"
METADATA_PATH = IMAGES_DIR / METADATA_NAME
CONVERSION_LOG_DIR = IRONDEPLOY_ROOT / "Logs" / "ImageConversions"
DEPLOY_CONFIG_PATH = IRONDEPLOY_ROOT / "WinPE" / "Runtime" / "deploy.config.ps1"
DEPLOY_CONFIG_EXAMPLE_PATH = (
    IRONDEPLOY_ROOT / "WinPE" / "Runtime" / "deploy.config.example.ps1"
)

_INDEX_PATTERN = re.compile(r"^\s*Index\s*:\s*(\d+)\s*$", re.MULTILINE)
_FIELD_PATTERNS = {
    "name": re.compile(r"^\s*Name\s*:\s*(.*?)\s*$", re.MULTILINE),
    "description": re.compile(
        r"^\s*Description\s*:\s*(.*?)\s*$", re.MULTILINE
    ),
    "architecture": re.compile(
        r"^\s*Architecture\s*:\s*(.*?)\s*$", re.MULTILINE
    ),
}
_conversion_lock = Lock()
_metadata_lock = RLock()
_conversion_cancel = Event()
_conversion_process: subprocess.Popen | None = None
_conversion_state: dict[str, Any] = {
    "status": "idle",
    "source": None,
    "destination": None,
    "startedAt": None,
    "finishedAt": None,
    "message": "No image conversion has been started since IronAPI launched.",
    "logPath": None,
}


class DeploymentImageError(RuntimeError):
    """Raised when an image-management operation cannot be completed."""


class _ConversionCancelled(RuntimeError):
    """Internal signal used to finish a cancelled conversion cleanly."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_image_name(name: str, suffixes: tuple[str, ...] = (".wim", ".esd")) -> str:
    try:
        return validate_windows_file_name(name, suffixes, "Windows image")
    except ValueError as exc:
        raise DeploymentImageError(str(exc)) from exc


def _read_metadata(path: Path = METADATA_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 3, "images": {}}
    except (OSError, json.JSONDecodeError) as exc:
        raise DeploymentImageError(f"Failed to read image metadata: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("images"), dict):
        raise DeploymentImageError("Image metadata has an invalid format.")
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
        raise DeploymentImageError(f"Failed to save image metadata: {exc}") from exc


def _legacy_default_index() -> int:
    # TODO(1.0): remove legacy alpha configuration compatibility.
    pattern = re.compile(r"^\s*\$ImageIndex\s*=\s*(\d+)\s*$", re.MULTILINE)
    for path in (DEPLOY_CONFIG_PATH, DEPLOY_CONFIG_EXAMPLE_PATH):
        try:
            match = pattern.search(path.read_text(encoding="utf-8-sig"))
        except OSError:
            continue
        if match and int(match.group(1)) > 0:
            return int(match.group(1))
    return 1


def _parse_wim_info(output: str) -> list[dict[str, Any]]:
    matches = list(_INDEX_PATTERN.finditer(output))
    indexes: list[dict[str, Any]] = []
    for position, match in enumerate(matches):
        end = matches[position + 1].start() if position + 1 < len(matches) else len(output)
        section = output[match.end() : end]
        item: dict[str, Any] = {"index": int(match.group(1))}
        for field, pattern in _FIELD_PATTERNS.items():
            field_match = pattern.search(section)
            item[field] = field_match.group(1).strip() if field_match else ""
        indexes.append(item)
    return indexes


def _inspect_image(path: Path) -> list[dict[str, Any]]:
    # PowerShell's DISM module keeps image names as Unicode. Calling dism.exe
    # directly through a redirected OEM console can replace Cyrillic (and
    # other non-ASCII edition names) with question marks before Python sees it.
    powershell_script = (
        "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
        "$items = @(Get-WindowsImage "
        "-ImagePath $env:IRONDEPLOY_IMAGE_PATH -ErrorAction Stop | "
        "ForEach-Object { [pscustomobject]@{ "
        "index = [int]$_.ImageIndex; "
        "name = [string]$_.ImageName; "
        "description = [string]$_.ImageDescription; "
        "architecture = [string]$_.Architecture } }); "
        "ConvertTo-Json -InputObject $items -Compress"
    )
    environment = os.environ.copy()
    environment["IRONDEPLOY_IMAGE_PATH"] = str(path)
    powershell_command = [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        powershell_script,
    ]
    try:
        completed = subprocess.run(
            powershell_command,
            capture_output=True,
            timeout=300,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode == 0:
            payload = json.loads(completed.stdout.decode("utf-8-sig"))
            if isinstance(payload, dict):
                payload = [payload]
            indexes = [
                {
                    "index": int(item["index"]),
                    "name": str(item.get("name", "")),
                    "description": str(item.get("description", "")),
                    "architecture": str(item.get("architecture", "")),
                }
                for item in payload
            ]
            if indexes:
                return indexes
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError, TypeError):
        # Retain a dism.exe fallback for minimal Windows installations where
        # the PowerShell DISM module is unavailable.
        pass

    command = [
        "dism.exe",
        "/English",
        "/Get-WimInfo",
        f"/WimFile:{path}",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeploymentImageError(f"DISM could not inspect the image: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise DeploymentImageError(
            f"DISM could not inspect the image"
            + (f": {detail[-600:]}" if detail else ".")
        )
    indexes = _parse_wim_info(completed.stdout)
    if not indexes:
        raise DeploymentImageError("DISM returned no indexes for the image.")
    return indexes


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _list_deployment_images(
    images_dir: Path = IMAGES_DIR,
    metadata_path: Path = METADATA_PATH,
) -> dict[str, Any]:
    images_dir.mkdir(parents=True, exist_ok=True)
    metadata = _read_metadata(metadata_path)
    records = metadata["images"]
    metadata_needs_upgrade = metadata.get("version") != 3
    legacy_index = _legacy_default_index()
    files = sorted(
        (
            item
            for item in images_dir.iterdir()
            if item.is_file() and item.suffix.lower() in {".wim", ".esd"}
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
        record = records.get(path.name, {})
        signature_changed = (
            record.get("size") != stat.st_size
            or record.get("modifiedNs") != stat.st_mtime_ns
        )
        if (
            signature_changed
            or metadata_needs_upgrade
            or not isinstance(record.get("indexes"), list)
            or bool(record.get("inspectionError"))
        ):
            try:
                indexes = _inspect_image(path)
                sha256 = _sha256_file(path)
                inspected_stat = path.stat()
                if (
                    inspected_stat.st_size != stat.st_size
                    or inspected_stat.st_mtime_ns != stat.st_mtime_ns
                ):
                    raise DeploymentImageError(
                        "The image changed while DISM was inspecting it; "
                        "the copy may still be in progress."
                    )
                inspection_error = None
            except DeploymentImageError as exc:
                indexes = []
                sha256 = None
                inspection_error = str(exc)
            except OSError as exc:
                indexes = []
                sha256 = None
                inspection_error = f"Failed to hash the image: {exc}"
            stat = path.stat()
            old_default = record.get("defaultIndex")
            valid_indexes = {item["index"] for item in indexes}
            default_index = (
                old_default
                if old_default in valid_indexes
                else legacy_index
                if legacy_index in valid_indexes
                else indexes[0]["index"]
                if indexes
                else old_default
            )
            record = {
                "defaultIndex": default_index,
                "size": stat.st_size,
                "modifiedNs": stat.st_mtime_ns,
                "indexes": indexes,
                "sha256": sha256,
                "inspectionError": inspection_error,
            }
            records[path.name] = record
            changed = True

        result.append(
            {
                "name": path.name,
                "format": path.suffix[1:].upper(),
                "size": stat.st_size,
                "modifiedAt": datetime.fromtimestamp(
                    stat.st_mtime, timezone.utc
                ).isoformat(),
                "defaultIndex": record.get("defaultIndex"),
                "indexes": record.get("indexes", []),
                "sha256": record.get("sha256"),
                "inspectionError": record.get("inspectionError"),
                "ready": bool(
                    record.get("indexes")
                    and not record.get("inspectionError")
                    and record.get("defaultIndex")
                    in {item["index"] for item in record.get("indexes", [])}
                ),
                "canConvert": path.suffix.lower() == ".esd",
            }
        )

    if changed:
        metadata["version"] = 3
        _write_metadata(metadata, metadata_path)
    return {"images": result, "directory": str(images_dir)}


def list_deployment_images(
    images_dir: Path = IMAGES_DIR,
    metadata_path: Path = METADATA_PATH,
) -> dict[str, Any]:
    with _metadata_lock:
        return _list_deployment_images(images_dir, metadata_path)


def _set_default_image_index(
    name: str,
    index: int,
    images_dir: Path = IMAGES_DIR,
    metadata_path: Path = METADATA_PATH,
) -> dict[str, Any]:
    safe_name = _safe_image_name(name)
    if index <= 0:
        raise DeploymentImageError("Default image index must be positive.")
    listing = list_deployment_images(images_dir, metadata_path)
    image = next((item for item in listing["images"] if item["name"] == safe_name), None)
    if image is None:
        raise DeploymentImageError("Windows image not found.")
    if index not in {item["index"] for item in image["indexes"]}:
        raise DeploymentImageError("The selected index does not exist in this image.")
    metadata = _read_metadata(metadata_path)
    metadata["images"][safe_name]["defaultIndex"] = index
    _write_metadata(metadata, metadata_path)
    image["defaultIndex"] = index
    return image


def set_default_image_index(
    name: str,
    index: int,
    images_dir: Path = IMAGES_DIR,
    metadata_path: Path = METADATA_PATH,
) -> dict[str, Any]:
    with _metadata_lock:
        return _set_default_image_index(name, index, images_dir, metadata_path)


def rename_deployment_image(
    name: str,
    new_name: str,
    images_dir: Path = IMAGES_DIR,
    metadata_path: Path = METADATA_PATH,
) -> dict[str, Any]:
    safe_name = _safe_image_name(name, (".wim",))
    safe_new_name = _safe_image_name(new_name, (".wim",))
    if safe_name == safe_new_name:
        raise DeploymentImageError("The new WIM name is unchanged.")

    with _metadata_lock:
        source = images_dir / safe_name
        destination = images_dir / safe_new_name
        if not source.is_file():
            raise DeploymentImageError("WIM image not found.")

        case_only_rename = safe_name.casefold() == safe_new_name.casefold()
        if destination.exists() and not case_only_rename:
            raise DeploymentImageError(
                f"An image named '{safe_new_name}' already exists."
            )

        metadata = _read_metadata(metadata_path)
        existing_key = next(
            (
                key
                for key in metadata["images"]
                if key.casefold() == safe_name.casefold()
            ),
            None,
        )
        record = metadata["images"].pop(existing_key, None) if existing_key else None
        if record is not None:
            metadata["images"][safe_new_name] = record

        temporary: Path | None = None
        try:
            if case_only_rename:
                temporary = images_dir / f".rename-{uuid4().hex}.tmp"
                os.rename(source, temporary)
                os.rename(temporary, destination)
            else:
                os.rename(source, destination)
            _write_metadata(metadata, metadata_path)
        except (OSError, DeploymentImageError) as exc:
            try:
                if temporary is not None and temporary.exists():
                    os.rename(temporary, source)
                elif destination.exists() and not source.exists():
                    os.rename(destination, source)
            except OSError:
                pass
            if isinstance(exc, DeploymentImageError):
                raise
            raise DeploymentImageError(f"Failed to rename the WIM image: {exc}") from exc

    return {
        "renamed": True,
        "oldName": safe_name,
        "name": safe_new_name,
        "defaultIndex": record.get("defaultIndex") if record else None,
    }


async def save_uploaded_image(
    name: str,
    chunks: AsyncIterator[bytes],
    images_dir: Path = IMAGES_DIR,
) -> dict[str, Any]:
    safe_name = _safe_image_name(name)
    images_dir.mkdir(parents=True, exist_ok=True)
    destination = images_dir / safe_name
    if destination.exists():
        raise DeploymentImageError(f"An image named '{safe_name}' already exists.")
    temporary = images_dir / f".{safe_name}.upload-{uuid4().hex}.tmp"
    size = 0
    try:
        with temporary.open("xb") as handle:
            async for chunk in chunks:
                if chunk:
                    handle.write(chunk)
                    size += len(chunk)
        if size == 0:
            raise DeploymentImageError("The uploaded image is empty.")
        if destination.exists():
            raise DeploymentImageError(f"An image named '{safe_name}' already exists.")
        os.replace(temporary, destination)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise DeploymentImageError(f"Failed to save the uploaded image: {exc}") from exc
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return {
        "uploaded": True,
        "name": safe_name,
        "size": size,
        "canConvert": destination.suffix.lower() == ".esd",
    }


def get_conversion_state() -> dict[str, Any]:
    with _conversion_lock:
        return dict(_conversion_state)


def _finish_conversion(
    source: Path,
    destination: Path,
    working_path: Path,
    indexes: list[int],
    log_path: Path,
) -> None:
    global _conversion_process
    error: str | None = None
    cancelled = False
    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            for index in indexes:
                if _conversion_cancel.is_set():
                    raise _ConversionCancelled()
                command = [
                    "dism.exe",
                    "/English",
                    "/Export-Image",
                    f"/SourceImageFile:{source}",
                    f"/SourceIndex:{index}",
                    f"/DestinationImageFile:{working_path}",
                    "/Compress:max",
                    "/CheckIntegrity",
                ]
                process = subprocess.Popen(
                    command,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                with _conversion_lock:
                    _conversion_process = process
                if _conversion_cancel.is_set():
                    process.terminate()
                exit_code = process.wait(timeout=4 * 60 * 60)
                with _conversion_lock:
                    if _conversion_process is process:
                        _conversion_process = None
                if _conversion_cancel.is_set():
                    raise _ConversionCancelled()
                if exit_code != 0:
                    raise DeploymentImageError(
                        f"DISM failed while exporting index {index} "
                        f"(exit code {exit_code})."
                    )
            if _conversion_cancel.is_set():
                raise _ConversionCancelled()
            if destination.exists():
                raise DeploymentImageError(
                    f"The destination image '{destination.name}' was created "
                    "while conversion was running."
                )
            os.replace(working_path, destination)
    except _ConversionCancelled:
        cancelled = True
    except (OSError, subprocess.SubprocessError, DeploymentImageError) as exc:
        error = str(exc)
    finally:
        with _conversion_lock:
            _conversion_process = None

    if cancelled or error:
        try:
            working_path.unlink(missing_ok=True)
        except OSError:
            pass

    with _conversion_lock:
        _conversion_state.update(
            status="cancelled" if cancelled else "failed" if error else "succeeded",
            finishedAt=_utc_now(),
            message=(
                f"Conversion of {source.name} was cancelled."
                if cancelled
                else f"Conversion failed: {error}"
                if error
                else f"Converted {source.name} to {destination.name}."
            ),
        )


def start_esd_conversion(name: str) -> dict[str, Any]:
    safe_name = _safe_image_name(name, (".esd",))
    source = IMAGES_DIR / safe_name
    destination = source.with_suffix(".wim")
    if not source.is_file():
        raise DeploymentImageError("ESD image not found.")
    if destination.exists():
        raise DeploymentImageError(
            f"The destination image '{destination.name}' already exists."
        )
    indexes = [item["index"] for item in _inspect_image(source)]

    with _conversion_lock:
        if _conversion_state["status"] == "running":
            raise DeploymentImageError("Another image conversion is already running.")
        CONVERSION_LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = CONVERSION_LOG_DIR / f"esd-to-wim-{timestamp}.log"
        working_path = CONVERSION_LOG_DIR / f"conversion-{uuid4().hex}.wim"
        _conversion_cancel.clear()
        relative_log = log_path.relative_to(IRONDEPLOY_ROOT)
        _conversion_state.update(
            status="running",
            source=source.name,
            destination=destination.name,
            startedAt=_utc_now(),
            finishedAt=None,
            message=f"Converting {source.name} to {destination.name}...",
            logPath=str(relative_log),
        )

    Thread(
        target=_finish_conversion,
        args=(source, destination, working_path, indexes, log_path),
        daemon=True,
        name="esd-to-wim-conversion",
    ).start()
    return get_conversion_state()


def cancel_esd_conversion() -> dict[str, Any]:
    with _conversion_lock:
        if _conversion_state["status"] != "running":
            raise DeploymentImageError("No image conversion is running.")
        _conversion_cancel.set()
        process = _conversion_process
        _conversion_state["message"] = "Cancelling image conversion..."

    if process is not None and process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
    return get_conversion_state()
