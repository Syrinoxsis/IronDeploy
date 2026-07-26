"""Launch and track IronDeploy WinPE artifact builds."""

from __future__ import annotations

import ctypes
import locale
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Thread
from typing import Any

from app.config import IRONDEPLOY_ROOT


BUILD_SCRIPT = IRONDEPLOY_ROOT / "Tools" / "Build-IronDeployWinPE.ps1"
BUILD_LOG_DIR = IRONDEPLOY_ROOT / "Logs" / "WinPEBuilds"
_TARGETS = {"wim": "Wim", "iso": "Iso"}
_state_lock = Lock()
_state: dict[str, Any] = {
    "status": "idle",
    "target": None,
    "startedAt": None,
    "finishedAt": None,
    "exitCode": None,
    "logPath": None,
    "message": "No WinPE build has been started since IronAPI launched.",
}


class WinPEBuildError(RuntimeError):
    """Raised when a WinPE build cannot be started."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def get_winpe_build_state() -> dict[str, Any]:
    with _state_lock:
        return dict(_state)


def _raise_start_error(target: str | None, message: str) -> None:
    now = _utc_now()
    with _state_lock:
        if _state["status"] != "running":
            _state.update(
                status="failed",
                target=target,
                startedAt=now,
                finishedAt=now,
                exitCode=None,
                logPath=None,
                message=message,
            )
    raise WinPEBuildError(message)


def _build_failure_message(target: str, exit_code: int, log_path: Path) -> str:
    try:
        content = log_path.read_bytes().decode(
            locale.getpreferredencoding(False),
            errors="replace",
        )
        for line in reversed(content.replace("\r", "\n").splitlines()):
            line = line.strip()
            if line.startswith("REBUILD FAILED:"):
                return f"{target} build failed: {line.removeprefix('REBUILD FAILED:').strip()}"
    except OSError:
        pass
    return f"{target} build failed with exit code {exit_code}."


def _finish_build(
    process: subprocess.Popen,
    log_handle,
    target: str,
    log_path: Path,
) -> None:
    exit_code = process.wait()
    log_handle.close()
    succeeded = exit_code == 0

    with _state_lock:
        _state.update(
            status="succeeded" if succeeded else "failed",
            finishedAt=_utc_now(),
            exitCode=exit_code,
            message=(
                f"{target} build completed successfully."
                if succeeded
                else _build_failure_message(target, exit_code, log_path)
            ),
        )


def start_winpe_build(target: str) -> dict[str, Any]:
    normalized_target = target.lower()
    powershell_target = _TARGETS.get(normalized_target)
    if powershell_target is None:
        _raise_start_error(None, "Build target must be 'wim' or 'iso'.")
    if not BUILD_SCRIPT.is_file():
        _raise_start_error(
            powershell_target,
            f"WinPE build wrapper is missing: {BUILD_SCRIPT}",
        )
    if not _is_elevated():
        _raise_start_error(
            powershell_target,
            "IronAPI must be started as Administrator to rebuild WinPE artifacts.",
        )

    with _state_lock:
        if _state["status"] == "running":
            raise WinPEBuildError(
                f"A {_state['target']} build is already running."
            )

        BUILD_LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = BUILD_LOG_DIR / f"winpe-{normalized_target}-{timestamp}.log"
        log_handle = log_path.open("wb")
        command = [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(BUILD_SCRIPT),
            "-Target",
            powershell_target,
        ]

        try:
            process = subprocess.Popen(
                command,
                cwd=IRONDEPLOY_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            log_handle.close()
            message = f"Failed to launch PowerShell: {exc}"
            now = _utc_now()
            _state.update(
                status="failed",
                target=powershell_target,
                startedAt=now,
                finishedAt=now,
                exitCode=None,
                logPath=None,
                message=message,
            )
            raise WinPEBuildError(message) from exc

        relative_log_path = log_path.relative_to(IRONDEPLOY_ROOT)
        _state.update(
            status="running",
            target=powershell_target,
            startedAt=_utc_now(),
            finishedAt=None,
            exitCode=None,
            logPath=str(relative_log_path),
            message=f"{powershell_target} build is running.",
        )

    Thread(
        target=_finish_build,
        args=(process, log_handle, powershell_target, log_path),
        daemon=True,
        name=f"winpe-{normalized_target}-build",
    ).start()
    return get_winpe_build_state()
