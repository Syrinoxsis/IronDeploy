from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from pathlib import Path
from typing import Any

from .config_store import IronDeployPaths, normalize_smb_server_address, read_dotenv


SHARE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
QUALIFIED_ACCOUNT_PATTERN = re.compile(r"^[^\\/@\r\n]+\\[^\\/@\r\n]+$")


class SmbToolError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def validate_share_name(value: str) -> str:
    share_name = str(value or "").strip()
    if not SHARE_NAME_PATTERN.fullmatch(share_name):
        raise SmbToolError(
            "invalid_share_name",
            "Share name must be 1-80 letters, digits, dots, underscores, or hyphens.",
        )
    return share_name


def validate_qualified_account(value: str) -> str:
    account = str(value or "").strip()
    if not QUALIFIED_ACCOUNT_PATTERN.fullmatch(account):
        raise SmbToolError(
            "invalid_account",
            "SMB account must use SERVER\\user or DOMAIN\\user format.",
        )
    authority, username = account.split("\\", 1)
    if authority != authority.strip() or username != username.strip():
        raise SmbToolError(
            "invalid_account",
            "SMB account must not contain spaces around the authority or username.",
        )
    return account


def validate_server_address(value: str) -> str:
    try:
        return normalize_smb_server_address(value)
    except ValueError as exc:
        raise SmbToolError("invalid_server_address", str(exc)) from exc


def get_local_smb_info(paths: IronDeployPaths) -> dict[str, str]:
    server_name = str(os.environ.get("COMPUTERNAME") or socket.gethostname()).strip()
    if not server_name:
        raise SmbToolError(
            "server_name_unavailable",
            "Windows computer name is unavailable.",
            500,
        )
    return {
        "serverName": server_name,
        "localPath": str((paths.root / "Share").resolve()),
    }


def get_saved_smb_password(paths: IronDeployPaths) -> str:
    return read_dotenv(paths.api_env).get("IRONAPI_SMB_PASSWORD", "")


def _run_smb_tool(
    paths: IronDeployPaths,
    script_name: str,
    payload: dict[str, str],
    timeout: int,
) -> dict[str, Any]:
    script_path = paths.root / "Tools" / script_name
    if not script_path.is_file():
        raise SmbToolError(
            "script_missing",
            f"SMB helper script not found: {script_path}",
            500,
        )

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
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SmbToolError(
            "timeout",
            "The SMB operation timed out.",
            504,
        ) from exc

    output = completed.stdout.strip()
    try:
        result = json.loads(output)
    except json.JSONDecodeError as exc:
        detail = completed.stderr.strip() or output or "PowerShell returned no output."
        raise SmbToolError(
            "invalid_script_response",
            f"SMB helper returned an invalid response: {detail}",
            500,
        ) from exc

    if not isinstance(result, dict):
        raise SmbToolError(
            "invalid_script_response",
            "SMB helper returned an invalid response.",
            500,
        )

    if completed.returncode != 0 or result.get("ok") is not True:
        code = str(result.get("code") or "operation_failed")
        message = str(result.get("message") or "The SMB operation failed.")
        conflict_codes = {"name_conflict", "path_shared_as_other_name"}
        status_code = 409 if code in conflict_codes else 400
        if code in {"not_elevated", "operation_failed"}:
            status_code = 500
        raise SmbToolError(code, message, status_code)

    return result


def configure_local_share(
    paths: IronDeployPaths,
    server_address: str,
    share_name: str,
    account: str,
) -> dict[str, Any]:
    return _run_smb_tool(
        paths,
        "Set-IronDeploySmbShare.ps1",
        {
            "serverAddress": validate_server_address(server_address),
            "shareName": validate_share_name(share_name),
            "account": validate_qualified_account(account),
        },
        timeout=45,
    )


def test_local_share_access(
    paths: IronDeployPaths,
    server_address: str,
    share_name: str,
    account: str,
    password: str,
) -> dict[str, Any]:
    candidate_password = str(password or "")
    if not candidate_password:
        candidate_password = get_saved_smb_password(paths)
    if not candidate_password:
        raise SmbToolError(
            "password_required",
            "Enter an SMB password or save one before testing access.",
        )
    if len(candidate_password) > 512:
        raise SmbToolError(
            "password_too_long",
            "SMB password is too long.",
        )

    return _run_smb_tool(
        paths,
        "Test-IronDeploySmbAccess.ps1",
        {
            "serverAddress": validate_server_address(server_address),
            "shareName": validate_share_name(share_name),
            "account": validate_qualified_account(account),
            "password": candidate_password,
        },
        timeout=30,
    )
