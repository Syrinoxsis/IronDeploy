"""Manage profile-aware PowerShell scripts run during Windows SetupComplete."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, AsyncIterator
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import IRONDEPLOY_ROOT
from app.deployment_profiles import get_default_profile
from app.deployments import (
    DeploymentProfileScript,
    PostPowerShellScript,
)
from app.file_names import validate_windows_file_name


POST_POWERSHELL_DIR = IRONDEPLOY_ROOT / "Library" / "PostPowerShell"
POST_POWERSHELL_LOG_DIR = IRONDEPLOY_ROOT / "Logs" / "PostPowerShell"
MAX_SCRIPT_SIZE_BYTES = 50 * 1024**2
MAX_OUTPUT_SIZE_BYTES = 20 * 1024**2
MAX_ARGUMENTS_LENGTH = 500
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 3 * 60 * 60
DEFAULT_TIMEOUT_SECONDS = 10 * 60
ALLOWED_SELECTION_MODES = {"automatic", "operator"}
ALLOWED_RUN_PHASES = {"before_software", "after_software"}

_file_lock = RLock()


class PostPowerShellError(RuntimeError):
    """Raised when a managed PowerShell script operation is invalid."""


def _safe_name(name: str) -> str:
    try:
        return validate_windows_file_name(name, (".ps1",), "PowerShell script")
    except ValueError as exc:
        raise PostPowerShellError(str(exc)) from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise PostPowerShellError(f"Failed to hash script '{path.name}': {exc}") from exc
    return digest.hexdigest()


def validate_arguments(arguments: Any) -> str:
    if not isinstance(arguments, str):
        raise PostPowerShellError("arguments must be a string.")
    if any(character in arguments for character in ("\0", "\r", "\n")):
        raise PostPowerShellError("Arguments must not contain NUL, CR, or LF characters.")
    value = arguments.strip(" ")
    if len(value) > MAX_ARGUMENTS_LENGTH:
        raise PostPowerShellError(
            f"Arguments are limited to {MAX_ARGUMENTS_LENGTH} characters."
        )
    return value


def validate_settings(
    *,
    arguments: Any,
    selection_mode: Any,
    run_phase: Any,
    timeout_seconds: Any,
) -> tuple[str, str, str, int]:
    normalized_arguments = validate_arguments(arguments)
    normalized_mode = str(selection_mode).strip().lower()
    normalized_phase = str(run_phase).strip().lower()
    if normalized_mode not in ALLOWED_SELECTION_MODES:
        raise PostPowerShellError("selectionMode must be automatic or operator.")
    if normalized_phase not in ALLOWED_RUN_PHASES:
        raise PostPowerShellError(
            "runPhase must be before_software or after_software."
        )
    try:
        normalized_timeout = int(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise PostPowerShellError("timeoutSeconds must be an integer.") from exc
    if not MIN_TIMEOUT_SECONDS <= normalized_timeout <= MAX_TIMEOUT_SECONDS:
        raise PostPowerShellError("timeoutSeconds must be between 1 and 10800.")
    return (
        normalized_arguments,
        normalized_mode,
        normalized_phase,
        normalized_timeout,
    )


def _refresh_signature(script: PostPowerShellScript) -> bool:
    path = POST_POWERSHELL_DIR / script.name
    if not path.is_file():
        return False
    stat = path.stat()
    if stat.st_size > MAX_SCRIPT_SIZE_BYTES:
        return False
    if script.size_bytes == stat.st_size and script.modified_ns == stat.st_mtime_ns:
        return True
    digest = _sha256_file(path)
    verified_stat = path.stat()
    if (
        verified_stat.st_size != stat.st_size
        or verified_stat.st_mtime_ns != stat.st_mtime_ns
    ):
        raise PostPowerShellError(
            f"Script '{script.name}' changed while SHA-256 was calculated."
        )
    script.size_bytes = stat.st_size
    script.modified_ns = stat.st_mtime_ns
    script.sha256 = digest
    script.updated_at = datetime.now(timezone.utc)
    return True


def _serialize(
    script: PostPowerShellScript,
    assignment: DeploymentProfileScript,
    *,
    available: bool,
) -> dict[str, Any]:
    return {
        "id": script.id,
        "name": script.name,
        "size": script.size_bytes,
        "sha256": script.sha256,
        "modifiedAt": datetime.fromtimestamp(
            script.modified_ns / 1_000_000_000, timezone.utc
        ).isoformat(),
        "available": available,
        "position": assignment.position,
        "selectionMode": assignment.selection_mode,
        "runPhase": assignment.run_phase,
        "arguments": assignment.arguments,
        "timeoutSeconds": assignment.timeout_seconds,
    }


def list_scripts(session: Session) -> dict[str, Any]:
    profile = get_default_profile(session)
    rows = session.execute(
        select(PostPowerShellScript, DeploymentProfileScript)
        .join(
            DeploymentProfileScript,
            DeploymentProfileScript.script_id == PostPowerShellScript.id,
        )
        .where(DeploymentProfileScript.profile_id == profile.id)
        .order_by(DeploymentProfileScript.position, PostPowerShellScript.id)
    ).all()
    scripts = []
    with _file_lock:
        for script, assignment in rows:
            available = _refresh_signature(script)
            scripts.append(_serialize(script, assignment, available=available))
    session.flush()
    return {
        "profileId": profile.id,
        "profileName": profile.name,
        "directory": str(POST_POWERSHELL_DIR),
        "maxFileSizeBytes": MAX_SCRIPT_SIZE_BYTES,
        "maxOutputSizeBytes": MAX_OUTPUT_SIZE_BYTES,
        "scripts": scripts,
    }


async def save_uploaded_script(
    session: Session,
    name: str,
    chunks: AsyncIterator[bytes],
    *,
    arguments: Any = "",
    selection_mode: Any = "operator",
    run_phase: Any = "after_software",
    timeout_seconds: Any = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    safe_name = _safe_name(name)
    settings = validate_settings(
        arguments=arguments,
        selection_mode=selection_mode,
        run_phase=run_phase,
        timeout_seconds=timeout_seconds,
    )
    profile = get_default_profile(session)
    POST_POWERSHELL_DIR.mkdir(parents=True, exist_ok=True)
    destination = POST_POWERSHELL_DIR / safe_name
    temporary = POST_POWERSHELL_DIR / f".{safe_name}.upload-{uuid4().hex}.tmp"
    if destination.exists() or session.scalar(
        select(PostPowerShellScript).where(PostPowerShellScript.name == safe_name)
    ):
        raise PostPowerShellError(f"A script named '{safe_name}' already exists.")

    size = 0
    digest = hashlib.sha256()
    published = False
    try:
        with temporary.open("xb") as handle:
            async for chunk in chunks:
                if not chunk:
                    continue
                if size + len(chunk) > MAX_SCRIPT_SIZE_BYTES:
                    raise PostPowerShellError("PowerShell scripts are limited to 50 MiB.")
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        if size == 0:
            raise PostPowerShellError("The uploaded PowerShell script is empty.")
        with _file_lock:
            os.replace(temporary, destination)
            published = True
            stat = destination.stat()
        script = PostPowerShellScript(
            name=safe_name,
            size_bytes=stat.st_size,
            modified_ns=stat.st_mtime_ns,
            sha256=digest.hexdigest(),
        )
        session.add(script)
        session.flush()
        current_max_position = session.scalar(
            select(func.coalesce(func.max(DeploymentProfileScript.position), -1)).where(
                DeploymentProfileScript.profile_id == profile.id
            )
        )
        assignment = DeploymentProfileScript(
            profile_id=profile.id,
            script_id=script.id,
            position=int(current_max_position if current_max_position is not None else -1)
            + 1,
            arguments=settings[0],
            selection_mode=settings[1],
            run_phase=settings[2],
            timeout_seconds=settings[3],
        )
        session.add(assignment)
        session.flush()
        return _serialize(script, assignment, available=True)
    except Exception:
        temporary.unlink(missing_ok=True)
        if published:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def update_script_settings(
    session: Session,
    script_id: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    profile = get_default_profile(session)
    script = session.get(PostPowerShellScript, script_id)
    assignment = session.get(DeploymentProfileScript, (profile.id, script_id))
    if script is None or assignment is None:
        raise PostPowerShellError("PowerShell script not found.")
    settings = validate_settings(
        arguments=payload.get("arguments", assignment.arguments),
        selection_mode=payload.get("selectionMode", assignment.selection_mode),
        run_phase=payload.get("runPhase", assignment.run_phase),
        timeout_seconds=payload.get("timeoutSeconds", assignment.timeout_seconds),
    )
    assignment.arguments = settings[0]
    assignment.selection_mode = settings[1]
    assignment.run_phase = settings[2]
    assignment.timeout_seconds = settings[3]
    script.updated_at = datetime.now(timezone.utc)
    available = _refresh_signature(script)
    session.flush()
    return _serialize(script, assignment, available=available)


def delete_script(session: Session, script_id: int) -> dict[str, Any]:
    script = session.get(PostPowerShellScript, script_id)
    if script is None:
        raise PostPowerShellError("PowerShell script not found.")
    path = POST_POWERSHELL_DIR / script.name
    with _file_lock:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise PostPowerShellError(f"Failed to delete the script: {exc}") from exc
    name = script.name
    session.delete(script)
    session.flush()
    return {"deleted": True, "id": script_id, "name": name}


def resolve_profile_scripts(
    session: Session,
    requested_names: list[str],
) -> list[dict[str, Any]]:
    listing = list_scripts(session)
    by_name = {item["name"].casefold(): item for item in listing["scripts"]}
    requested = {name.casefold() for name in requested_names}
    unavailable = sorted(name for name in requested if name not in by_name)
    if unavailable:
        raise PostPowerShellError(
            "Selected PowerShell script is unavailable: " + unavailable[0]
        )
    selected: list[dict[str, Any]] = []
    for item in listing["scripts"]:
        if (
            item["selectionMode"] == "automatic"
            or item["name"].casefold() in requested
        ):
            selected.append(item)
    return selected


def script_path(name: str) -> Path:
    return POST_POWERSHELL_DIR / _safe_name(name)


def verified_script_path(name: str, size_bytes: int, sha256: str) -> Path:
    path = script_path(name)
    if not path.is_file():
        raise PostPowerShellError("PowerShell script file is unavailable.")
    stat = path.stat()
    if stat.st_size != size_bytes or _sha256_file(path).lower() != sha256.lower():
        raise PostPowerShellError(
            "PowerShell script changed after the deployment manifest was issued."
        )
    return path


def result_log_path(deployment_id: int, position: int) -> Path:
    return POST_POWERSHELL_LOG_DIR / str(deployment_id) / f"{position:04d}.log"
