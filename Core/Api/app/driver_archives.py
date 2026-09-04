"""Prepare deployment-scoped, uncompressed driver TAR files for WinPE."""

from __future__ import annotations

import json
import locale
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, RLock, Thread, current_thread
from typing import Any

from app.config import IRONDEPLOY_ROOT, Settings, get_settings
from app.drivers import DRIVERS_DIR


ARCHIVE_DIRECTORY_NAME = ".irondeploy-archives"
ARCHIVE_ROOT = DRIVERS_DIR / ARCHIVE_DIRECTORY_NAME
SEVEN_ZIP_EXE = (
    IRONDEPLOY_ROOT
    / "WinPE"
    / "Runtime"
    / "Tools"
    / "7-Zip"
    / "7za.exe"
)
ARCHIVE_NAME = "drivers.tar"
PARTIAL_ARCHIVE_NAME = "drivers.tar.partial"
METADATA_NAME = "archive.json"
_TAR_ENTRY_RESERVE_BYTES = 4096
_TAR_FIXED_RESERVE_BYTES = 1024 * 1024


class DriverArchiveError(RuntimeError):
    """Raised when a deployment driver archive cannot be prepared."""


@dataclass
class _RunningArchive:
    reservation_bytes: int
    cancel: Event
    thread: Thread | None = None
    process: subprocess.Popen[bytes] | None = None


_archive_lock = RLock()
_running: dict[int, _RunningArchive] = {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_directory(deployment_id: int) -> Path:
    if deployment_id <= 0:
        raise DriverArchiveError("Deployment ID is invalid.")
    return ARCHIVE_ROOT / str(deployment_id)


def _metadata_path(deployment_id: int) -> Path:
    return _job_directory(deployment_id) / METADATA_NAME


def _write_metadata(deployment_id: int, payload: dict[str, Any]) -> None:
    with _archive_lock:
        path = _metadata_path(deployment_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)


def _read_metadata(deployment_id: int) -> dict[str, Any]:
    with _archive_lock:
        path = _metadata_path(deployment_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DriverArchiveError(
                "Driver archive preparation metadata is unavailable."
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("deploymentId") != deployment_id
        ):
            raise DriverArchiveError(
                "Driver archive preparation metadata is invalid."
            )
        return payload


def _find_child_casefold(parent: Path, name: str) -> Path | None:
    folded = name.casefold()
    try:
        return next(
            (child for child in parent.iterdir() if child.name.casefold() == folded),
            None,
        )
    except OSError as exc:
        raise DriverArchiveError(f"Failed to inspect driver storage: {exc}") from exc


def _resolve_package(relative_path: str) -> Path:
    parts = relative_path.split("\\")
    if (
        len(parts) != 2
        or any(not part or part in {".", ".."} for part in parts)
        or any(character in '<>:"/|?*' for part in parts for character in part)
    ):
        raise DriverArchiveError("Driver package path is invalid.")
    vendor = _find_child_casefold(DRIVERS_DIR, parts[0])
    package = _find_child_casefold(vendor, parts[1]) if vendor is not None else None
    if vendor is None or not vendor.is_dir() or package is None or not package.is_dir():
        raise DriverArchiveError("Selected driver package is unavailable.")
    return package


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.stat(), "st_file_attributes", 0)
    except OSError as exc:
        raise DriverArchiveError(f"Failed to inspect '{path}': {exc}") from exc
    reparse_attribute = getattr(
        os.stat_result,
        "FILE_ATTRIBUTE_REPARSE_POINT",
        0x400,
    )
    return bool(attributes & reparse_attribute)


def _snapshot_package(
    package_path: Path,
    cancel: Event | None = None,
) -> dict[str, Any]:
    entries: list[tuple[str, str, int, int]] = []
    total_size = 0
    file_count = 0
    inf_count = 0
    entry_count = 0
    try:
        paths = sorted(
            package_path.rglob("*"),
            key=lambda item: str(item).casefold(),
        )
        for path in paths:
            if cancel is not None and cancel.is_set():
                raise DriverArchiveError("Driver archive preparation was cancelled.")
            if path.is_symlink() or _is_reparse_point(path):
                raise DriverArchiveError(
                    f"Driver package contains an unsupported reparse point: {path}"
                )
            relative = path.relative_to(package_path).as_posix()
            stat = path.stat()
            if path.is_dir():
                entries.append(("d", relative, 0, stat.st_mtime_ns))
                entry_count += 1
                continue
            if not path.is_file():
                raise DriverArchiveError(
                    f"Driver package contains an unsupported filesystem entry: {path}"
                )
            entries.append(("f", relative, stat.st_size, stat.st_mtime_ns))
            total_size += stat.st_size
            file_count += 1
            entry_count += 1
            if path.suffix.casefold() == ".inf":
                inf_count += 1
    except DriverArchiveError:
        raise
    except OSError as exc:
        raise DriverArchiveError(
            f"Failed to inspect driver package '{package_path}': {exc}"
        ) from exc
    return {
        "entries": entries,
        "size": total_size,
        "fileCount": file_count,
        "infCount": inf_count,
        "entryCount": entry_count,
    }


def _assert_expected_package(
    snapshot: dict[str, Any],
    metadata: dict[str, Any],
) -> None:
    actual = (
        int(snapshot["size"]),
        int(snapshot["fileCount"]),
        int(snapshot["infCount"]),
    )
    expected = (
        int(metadata["sourceSize"]),
        int(metadata["sourceFileCount"]),
        int(metadata["sourceInfCount"]),
    )
    if actual != expected or actual[1] <= 0 or actual[2] <= 0:
        raise DriverArchiveError(
            "Selected driver package changed after the deployment manifest was issued."
        )


def _estimate_archive_bytes(snapshot: dict[str, Any]) -> int:
    return (
        int(snapshot["size"])
        + int(snapshot["entryCount"]) * _TAR_ENTRY_RESERVE_BYTES
        + _TAR_FIXED_RESERVE_BYTES
    )


def _archive_file_usage() -> int:
    total = 0
    if not ARCHIVE_ROOT.is_dir():
        return 0
    try:
        for path in ARCHIVE_ROOT.rglob("*"):
            if path.is_file() and path.name in {ARCHIVE_NAME, PARTIAL_ARCHIVE_NAME}:
                total += path.stat().st_size
    except OSError as exc:
        raise DriverArchiveError(
            f"Failed to inspect driver archive storage: {exc}"
        ) from exc
    return total


def _reserved_remaining_bytes() -> int:
    remaining = 0
    for deployment_id, job in _running.items():
        partial = _job_directory(deployment_id) / PARTIAL_ARCHIVE_NAME
        try:
            actual = partial.stat().st_size if partial.is_file() else 0
        except FileNotFoundError:
            actual = 0
        remaining += max(0, job.reservation_bytes - actual)
    return remaining


def _assert_free_space(required_bytes: int) -> None:
    try:
        free_bytes = shutil.disk_usage(DRIVERS_DIR).free
    except OSError as exc:
        raise DriverArchiveError(
            f"Failed to check free space for the driver TAR file: {exc}"
        ) from exc
    if free_bytes < required_bytes:
        raise DriverArchiveError(
            "Insufficient free space to prepare the driver TAR file."
        )


def _archive_relative_path(deployment_id: int) -> str:
    return f"{ARCHIVE_DIRECTORY_NAME}\\{deployment_id}\\{ARCHIVE_NAME}"


def _stop_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _run_7za(
    deployment_id: int,
    package_path: Path,
    partial_path: Path,
    timeout_seconds: int,
    expected_job: _RunningArchive,
) -> None:
    command = [
        str(SEVEN_ZIP_EXE),
        "a",
        "-ttar",
        "-y",
        str(partial_path),
        ".\\*",
    ]
    with _archive_lock:
        job = _running.get(deployment_id)
        if job is not expected_job or job.cancel.is_set():
            raise DriverArchiveError("Driver archive preparation was cancelled.")
        process = subprocess.Popen(
            command,
            cwd=package_path,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        job.process = process
    try:
        output, _ = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _stop_process(process)
        raise DriverArchiveError("Driver archive preparation timed out.") from exc
    finally:
        with _archive_lock:
            job = _running.get(deployment_id)
            if job is not None and job.process is process:
                job.process = None
    if process.returncode != 0:
        detail = output.decode(
            locale.getpreferredencoding(False), errors="replace"
        ).strip()
        if len(detail) > 2000:
            detail = detail[-2000:]
        raise DriverArchiveError(
            f"7-Zip failed with exit code {process.returncode}: {detail}"
        )


def _build_archive(
    deployment_id: int,
    settings: Settings,
    expected_job: _RunningArchive,
) -> None:
    directory = _job_directory(deployment_id)
    partial_path = directory / PARTIAL_ARCHIVE_NAME
    archive_path = directory / ARCHIVE_NAME
    try:
        metadata = _read_metadata(deployment_id)
        package_path = _resolve_package(str(metadata["sourceRelativePath"]))
        for attempt in range(1, 3):
            with _archive_lock:
                job = _running.get(deployment_id)
                if job is not expected_job or job.cancel.is_set():
                    raise DriverArchiveError(
                        "Driver archive preparation was cancelled."
                    )
                partial_path.unlink(missing_ok=True)
                archive_path.unlink(missing_ok=True)
            before = _snapshot_package(package_path, expected_job.cancel)
            _assert_expected_package(before, metadata)
            _run_7za(
                deployment_id,
                package_path,
                partial_path,
                settings.deployment_timeout_minutes * 60,
                expected_job,
            )
            after = _snapshot_package(package_path, expected_job.cancel)
            if before["entries"] == after["entries"]:
                _assert_expected_package(after, metadata)
                break
            partial_path.unlink(missing_ok=True)
            if attempt == 2:
                raise DriverArchiveError(
                    "Driver package changed repeatedly during archive preparation."
                )
            metadata.update(
                sourceSize=int(after["size"]),
                sourceFileCount=int(after["fileCount"]),
                sourceInfCount=int(after["infCount"]),
            )
            if metadata["sourceFileCount"] <= 0 or metadata["sourceInfCount"] <= 0:
                raise DriverArchiveError(
                    "Driver package became invalid during archive preparation."
                )
            revised_estimate = _estimate_archive_bytes(after)
            with _archive_lock:
                job = _running.get(deployment_id)
                if job is not expected_job or job.cancel.is_set():
                    raise DriverArchiveError(
                        "Driver archive preparation was cancelled."
                    )
                job.reservation_bytes = revised_estimate
                maximum = settings.driver_archive_max_gib * 1024**3
                _assert_free_space(_reserved_remaining_bytes())
                if _archive_file_usage() + _reserved_remaining_bytes() > maximum:
                    raise DriverArchiveError(
                        "Changed driver package exceeds the driver archive "
                        "storage limit."
                    )
                _write_metadata(deployment_id, metadata)

        if not partial_path.is_file() or partial_path.stat().st_size <= 0:
            raise DriverArchiveError("7-Zip did not create the driver TAR file.")
        archive_size = partial_path.stat().st_size
        with _archive_lock:
            job = _running.get(deployment_id)
            if job is not expected_job or job.cancel.is_set():
                raise DriverArchiveError("Driver archive preparation was cancelled.")
            maximum = settings.driver_archive_max_gib * 1024**3
            if _archive_file_usage() + _reserved_remaining_bytes() > maximum:
                raise DriverArchiveError(
                    "Driver archive storage limit was exceeded while creating "
                    "the TAR file."
                )
            os.replace(partial_path, archive_path)
            metadata.update(
                status="ready",
                archiveRelativePath=_archive_relative_path(deployment_id),
                archiveSize=archive_size,
                completedAt=_utc_now(),
                error=None,
            )
            _write_metadata(deployment_id, metadata)
    except Exception as exc:  # noqa: BLE001 - worker failures are persisted for WinPE
        with _archive_lock:
            job = _running.get(deployment_id)
            cancelled = job is not expected_job or job.cancel.is_set()
            if not cancelled:
                partial_path.unlink(missing_ok=True)
                archive_path.unlink(missing_ok=True)
                try:
                    metadata = _read_metadata(deployment_id)
                    metadata.update(
                        status="failed",
                        error=str(exc),
                        completedAt=_utc_now(),
                    )
                    _write_metadata(deployment_id, metadata)
                except Exception:
                    pass
    finally:
        with _archive_lock:
            if _running.get(deployment_id) is expected_job:
                _running.pop(deployment_id, None)


def prepare_driver_archive(
    deployment_id: int,
    driver_package: dict[str, Any],
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Start one fresh TAR preparation task for a deployment."""
    resolved_settings = settings or get_settings()
    relative_path = str(driver_package.get("relativePath") or "")
    try:
        source_metadata = {
            "sourceRelativePath": relative_path,
            "sourceSize": int(driver_package.get("size", -1)),
            "sourceFileCount": int(driver_package.get("fileCount", -1)),
            "sourceInfCount": int(driver_package.get("infCount", -1)),
        }
    except (TypeError, ValueError) as exc:
        raise DriverArchiveError("Driver package metadata is invalid.") from exc
    package_path = _resolve_package(relative_path)
    snapshot = _snapshot_package(package_path)
    _assert_expected_package(snapshot, source_metadata)
    if not SEVEN_ZIP_EXE.is_file():
        raise DriverArchiveError(f"Bundled x64 7za.exe is missing: {SEVEN_ZIP_EXE}")
    estimate = _estimate_archive_bytes(snapshot)
    maximum = resolved_settings.driver_archive_max_gib * 1024**3
    if estimate > maximum:
        raise DriverArchiveError(
            "Selected driver package is larger than the configured driver "
            "archive limit."
        )
    with _archive_lock:
        existing = _running.get(deployment_id)
        if existing is not None:
            metadata = _read_metadata(deployment_id)
            if metadata.get("sourceRelativePath") == relative_path:
                return metadata
            raise DriverArchiveError(
                "Another driver archive is already being prepared for this deployment."
            )
        try:
            existing_metadata = _read_metadata(deployment_id)
        except DriverArchiveError:
            existing_metadata = None
        if (
            existing_metadata is not None
            and existing_metadata.get("sourceRelativePath") == relative_path
            and existing_metadata.get("status") == "ready"
            and (_job_directory(deployment_id) / ARCHIVE_NAME).is_file()
        ):
            return existing_metadata

        cleanup_driver_archive(deployment_id)
        current_usage = _archive_file_usage()
        reserved = _reserved_remaining_bytes()
        # Physical free space already excludes bytes written to partial/ready
        # archives. Reserve only the bytes that active jobs still need to write.
        _assert_free_space(reserved + estimate)
        if current_usage + reserved + estimate > maximum:
            raise DriverArchiveError(
                "Driver archive storage limit is currently in use by other deployments."
            )
        directory = _job_directory(deployment_id)
        directory.mkdir(parents=True, exist_ok=True)
        metadata = {
            "deploymentId": deployment_id,
            **source_metadata,
            "status": "preparing",
            "archiveRelativePath": None,
            "archiveSize": None,
            "estimatedArchiveSize": estimate,
            "startedAt": _utc_now(),
            "completedAt": None,
            "error": None,
        }
        _write_metadata(deployment_id, metadata)
        job = _RunningArchive(reservation_bytes=estimate, cancel=Event())
        job.thread = Thread(
            target=_build_archive,
            args=(deployment_id, resolved_settings, job),
            daemon=True,
            name=f"driver-archive-{deployment_id}",
        )
        _running[deployment_id] = job
        job.thread.start()
        return metadata


def get_driver_archive_status(
    deployment_id: int,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Return status, restarting an interrupted preparation after an API restart."""
    metadata = _read_metadata(deployment_id)
    if metadata.get("status") == "preparing":
        with _archive_lock:
            running = deployment_id in _running
        if not running:
            package = {
                "relativePath": metadata.get("sourceRelativePath"),
                "size": metadata.get("sourceSize"),
                "fileCount": metadata.get("sourceFileCount"),
                "infCount": metadata.get("sourceInfCount"),
            }
            shutil.rmtree(_job_directory(deployment_id), ignore_errors=True)
            prepare_driver_archive(deployment_id, package, settings)
            metadata = _read_metadata(deployment_id)
    if metadata.get("status") == "ready":
        archive = _job_directory(deployment_id) / ARCHIVE_NAME
        if (
            not archive.is_file()
            or archive.stat().st_size != metadata.get("archiveSize")
        ):
            raise DriverArchiveError("Prepared driver TAR file is unavailable.")
    return metadata


def cleanup_driver_archive(deployment_id: int) -> None:
    """Cancel preparation and remove every server-side TAR for a deployment."""
    with _archive_lock:
        job = _running.get(deployment_id)
        if job is not None:
            job.cancel.set()
            process = job.process
        else:
            process = None
    _stop_process(process)
    if (
        job is not None
        and job.thread is not None
        and job.thread is not current_thread()
        and job.thread.is_alive()
    ):
        job.thread.join(timeout=10)
    with _archive_lock:
        if _running.get(deployment_id) is job:
            _running.pop(deployment_id, None)
        shutil.rmtree(_job_directory(deployment_id), ignore_errors=True)


def cleanup_driver_archives(deployment_ids: list[int]) -> None:
    for deployment_id in deployment_ids:
        cleanup_driver_archive(int(deployment_id))


def shutdown_driver_archive_workers() -> None:
    """Stop active 7-Zip children while preserving restartable metadata."""
    with _archive_lock:
        jobs = list(_running.items())
        for _, job in jobs:
            job.cancel.set()
    for _, job in jobs:
        _stop_process(job.process)
    for _, job in jobs:
        if (
            job.thread is not None
            and job.thread is not current_thread()
            and job.thread.is_alive()
        ):
            job.thread.join(timeout=10)
    with _archive_lock:
        for deployment_id, job in jobs:
            if _running.get(deployment_id) is job:
                _running.pop(deployment_id, None)
            directory = _job_directory(deployment_id)
            (directory / PARTIAL_ARCHIVE_NAME).unlink(missing_ok=True)
            (directory / f"{METADATA_NAME}.tmp").unlink(missing_ok=True)
