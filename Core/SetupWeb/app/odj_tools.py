from __future__ import annotations

import getpass
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .config_store import IronDeployPaths


QUALIFIED_WINDOWS_ACCOUNT_PATTERN = re.compile(r"^[^\\/@\r\n]+\\[^\\/@\r\n]+$")


class OdjToolError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def validate_windows_account(value: str) -> str:
    account = str(value or "").strip()
    if (
        len(account) > 256
        or not QUALIFIED_WINDOWS_ACCOUNT_PATTERN.fullmatch(account)
    ):
        raise OdjToolError(
            "invalid_account",
            "IronAPI process account must use DOMAIN\\user or COMPUTER\\user format.",
        )
    authority, username = account.split("\\", 1)
    if authority != authority.strip() or username != username.strip():
        raise OdjToolError(
            "invalid_account",
            "IronAPI process account must not contain spaces around its parts.",
        )
    return account


def get_current_process_account() -> str:
    authority = str(
        os.environ.get("USERDOMAIN")
        or os.environ.get("COMPUTERNAME")
        or ""
    ).strip()
    username = str(os.environ.get("USERNAME") or getpass.getuser()).strip()
    if authority and username:
        return f"{authority}\\{username}"
    return username


def get_local_odj_info(paths: IronDeployPaths) -> dict[str, str]:
    return {
        "path": str((paths.root / "ODJ").resolve()),
        "processAccount": get_current_process_account(),
    }


def secure_odj_acl(paths: IronDeployPaths, account: str) -> dict[str, Any]:
    script_path = paths.root / "Tools" / "Set-IronDeployOdjAcl.ps1"
    if not script_path.is_file():
        raise OdjToolError(
            "script_missing",
            f"ODJ ACL helper script not found: {script_path}",
            500,
        )

    payload = {"account": validate_windows_account(account)}
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
            ],
            cwd=str(paths.root),
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise OdjToolError(
            "timeout",
            "Updating the ODJ folder permissions timed out.",
            504,
        ) from exc

    output = completed.stdout.strip()
    try:
        result = json.loads(output)
    except json.JSONDecodeError as exc:
        detail = completed.stderr.strip() or output or "PowerShell returned no output."
        raise OdjToolError(
            "invalid_script_response",
            f"ODJ ACL helper returned an invalid response: {detail}",
            500,
        ) from exc

    if not isinstance(result, dict):
        raise OdjToolError(
            "invalid_script_response",
            "ODJ ACL helper returned an invalid response.",
            500,
        )

    if completed.returncode != 0 or result.get("ok") is not True:
        code = str(result.get("code") or "operation_failed")
        message = str(result.get("message") or "Updating ODJ permissions failed.")
        status_code = 500 if code in {"not_elevated", "operation_failed"} else 400
        raise OdjToolError(code, message, status_code)

    return result
