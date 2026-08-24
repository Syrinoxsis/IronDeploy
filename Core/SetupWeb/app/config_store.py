from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import ssl
import tempfile
from html import escape
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


API_NAMES = (
    "IRONAPI_ACCESS_MODE",
    "IRONAPI_BIND_HOST",
    "IRONAPI_PORT",
    "IRONAPI_ACCESS_LOG",
    "IRONAPI_COOKIE_SECURE",
    "IRONAPI_DEPLOYMENT_AUTHORIZATION_TIMEOUT_MINUTES",
    "IRONAPI_DEPLOYMENT_TIMEOUT_MINUTES",
    "IRONAPI_IMAGE_APPLY_MODE",
    "IRONAPI_DRIVER_APPLY_MODE",
    "IRONAPI_DRIVER_MAX_FILES",
    "IRONAPI_DRIVER_MAX_DEPTH",
    "IRONAPI_DRIVER_MAX_FULL_PATH",
    "IRONAPI_DRIVER_UPLOAD_TTL_HOURS",
    "IRONAPI_DRIVER_MAX_ACTIVE_UPLOADS",
    "IRONAPI_DRIVER_MIN_FREE_SPACE_GIB",
    "IRONAPI_SMB_SHARE_PATH",
    "IRONAPI_SMB_USER",
    "IRONAPI_SMB_PASSWORD",
    "IRONAPI_ALLOWED_CLIENT_NETWORKS",
    "IRONAPI_DATABASE_URL",
    "IRONAPI_NAME_PREFIX",
    "IRONAPI_NAME_WIDTH",
    "IRONAPI_NAME_START",
    "IRONAPI_LDAP_SERVER",
    "IRONAPI_LDAP_BASE_DN",
    "IRONAPI_LDAP_USE_SSL",
    "IRONAPI_LDAP_CONNECT_TIMEOUT",
    "IRONAPI_ODJ_DOMAIN",
    "IRONAPI_ODJ_MACHINE_OU",
    "IRONAPI_ODJ_BLOB_DIR",
    "IRONAPI_ODJ_DJOIN_PATH",
    "IRONAPI_ODJ_PROVISION_TIMEOUT",
    "IRONAPI_ODJ_BLOB_MAX_AGE_MINUTES",
)

# Active Directory integration is optional. Leaving these empty disables LDAP
# name checks and Offline Domain Join, which is what a deployment without a
# domain needs. An empty client-network list explicitly allows every network;
# every other setting still has to be present.
OPTIONAL_API_NAMES = frozenset(
    {
        "IRONAPI_ALLOWED_CLIENT_NETWORKS",
        "IRONAPI_LDAP_SERVER",
        "IRONAPI_LDAP_BASE_DN",
        "IRONAPI_ODJ_DOMAIN",
        "IRONAPI_ODJ_MACHINE_OU",
    }
)

WINPE_NAMES = (
    "SharePath",
    "ShareDrive",
    "ShareUser",
    "SharePassword",
    "ApiBaseUrl",
    "ValidateApiServerCertificate",
    "ApiServerCertificateType",
    "ImagesPath",
    "DriversPath",
    "EnableGuiImageApplyProgress",
)

CERTIFICATE_MAX_BYTES = 64 * 1024
CERTIFICATE_TYPES = {"self_signed", "ca"}

TIME_ZONES = (
    ("Dateline Standard Time", "UTC-12", "International Date Line West"),
    ("UTC-11", "UTC-11", "Coordinated Universal Time-11"),
    ("Aleutian Standard Time", "UTC-10", "Aleutian Islands"),
    ("Hawaiian Standard Time", "UTC-10", "Hawaii"),
    ("Marquesas Standard Time", "UTC-09:30", "Marquesas Islands"),
    ("Alaskan Standard Time", "UTC-09", "Alaska"),
    ("UTC-09", "UTC-09", "Coordinated Universal Time-09"),
    ("Pacific Standard Time", "UTC-08", "Pacific Time"),
    ("UTC-08", "UTC-08", "Coordinated Universal Time-08"),
    ("Mountain Standard Time", "UTC-07", "Mountain Time"),
    ("US Mountain Standard Time", "UTC-07", "Arizona"),
    ("Central Standard Time", "UTC-06", "Central Time"),
    ("Canada Central Standard Time", "UTC-06", "Saskatchewan"),
    ("Eastern Standard Time", "UTC-05", "Eastern Time"),
    ("US Eastern Standard Time", "UTC-05", "Indiana East"),
    ("SA Pacific Standard Time", "UTC-05", "Bogota, Lima, Quito"),
    ("Atlantic Standard Time", "UTC-04", "Atlantic Time"),
    ("SA Western Standard Time", "UTC-04", "Georgetown, La Paz, Manaus"),
    ("Newfoundland Standard Time", "UTC-03:30", "Newfoundland"),
    ("E. South America Standard Time", "UTC-03", "Brasilia"),
    ("SA Eastern Standard Time", "UTC-03", "Cayenne, Fortaleza"),
    ("UTC-02", "UTC-02", "Coordinated Universal Time-02"),
    ("Azores Standard Time", "UTC-01", "Azores"),
    ("UTC", "UTC+00", "Coordinated Universal Time"),
    ("GMT Standard Time", "UTC+00", "Dublin, Edinburgh, Lisbon, London"),
    ("W. Europe Standard Time", "UTC+01", "Amsterdam, Berlin, Rome"),
    ("Central Europe Standard Time", "UTC+01", "Budapest, Prague, Warsaw"),
    ("Romance Standard Time", "UTC+01", "Brussels, Copenhagen, Madrid, Paris"),
    ("E. Europe Standard Time", "UTC+02", "Chisinau"),
    ("FLE Standard Time", "UTC+02", "Helsinki, Kyiv, Riga, Sofia, Tallinn, Vilnius"),
    ("GTB Standard Time", "UTC+02", "Athens, Bucharest"),
    ("South Africa Standard Time", "UTC+02", "Harare, Pretoria"),
    ("Turkey Standard Time", "UTC+03", "Istanbul"),
    ("Russian Standard Time", "UTC+03", "Moscow, St. Petersburg"),
    ("Arab Standard Time", "UTC+03", "Kuwait, Riyadh"),
    ("Iran Standard Time", "UTC+03:30", "Tehran"),
    ("Arabian Standard Time", "UTC+04", "Abu Dhabi, Muscat"),
    ("Astrakhan Standard Time", "UTC+04", "Astrakhan, Ulyanovsk"),
    ("Afghanistan Standard Time", "UTC+04:30", "Kabul"),
    ("West Asia Standard Time", "UTC+05", "Ashgabat, Tashkent"),
    ("Qyzylorda Standard Time", "UTC+05", "Qyzylorda"),
    ("Pakistan Standard Time", "UTC+05", "Islamabad, Karachi"),
    ("India Standard Time", "UTC+05:30", "Chennai, Kolkata, Mumbai, New Delhi"),
    ("Nepal Standard Time", "UTC+05:45", "Kathmandu"),
    ("Central Asia Standard Time", "UTC+06", "Astana"),
    ("Bangladesh Standard Time", "UTC+06", "Dhaka"),
    ("Myanmar Standard Time", "UTC+06:30", "Yangon"),
    ("SE Asia Standard Time", "UTC+07", "Bangkok, Hanoi, Jakarta"),
    ("North Asia Standard Time", "UTC+07", "Krasnoyarsk"),
    ("N. Central Asia Standard Time", "UTC+07", "Novosibirsk"),
    ("China Standard Time", "UTC+08", "Beijing, Chongqing, Hong Kong"),
    ("Singapore Standard Time", "UTC+08", "Kuala Lumpur, Singapore"),
    ("Taipei Standard Time", "UTC+08", "Taipei"),
    ("North Asia East Standard Time", "UTC+08", "Irkutsk"),
    ("Tokyo Standard Time", "UTC+09", "Osaka, Sapporo, Tokyo"),
    ("Korea Standard Time", "UTC+09", "Seoul"),
    ("AUS Central Standard Time", "UTC+09:30", "Darwin"),
    ("E. Australia Standard Time", "UTC+10", "Brisbane"),
    ("AUS Eastern Standard Time", "UTC+10", "Canberra, Melbourne, Sydney"),
    ("West Pacific Standard Time", "UTC+10", "Guam, Port Moresby"),
    ("Lord Howe Standard Time", "UTC+10:30", "Lord Howe Island"),
    ("Central Pacific Standard Time", "UTC+11", "Solomon Islands, New Caledonia"),
    ("New Zealand Standard Time", "UTC+12", "Auckland, Wellington"),
    ("UTC+12", "UTC+12", "Coordinated Universal Time+12"),
    ("Tonga Standard Time", "UTC+13", "Nuku'alofa"),
    ("Line Islands Standard Time", "UTC+14", "Kiritimati Island"),
)

TIME_ZONE_IDS = {item[0] for item in TIME_ZONES}
AUTH_PASSWORD_ITERATIONS = 600_000
AUTH_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{3,64}$")
SMB_SERVER_ADDRESS_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class IronDeployPaths:
    root: Path
    api_env: Path
    api_env_example: Path
    winpe_config: Path
    winpe_config_example: Path
    unattend: Path
    unattend_example: Path
    backup_dir: Path
    validation_script: Path
    auth_bootstrap: Path

    @staticmethod
    def from_setupweb(setupweb_root: Path) -> "IronDeployPaths":
        root = setupweb_root.parent
        return IronDeployPaths(
            root=root,
            api_env=root / "Api" / ".env",
            api_env_example=root / "Api" / ".env.example",
            winpe_config=root / "WinPE" / "Runtime" / "deploy.config.ps1",
            winpe_config_example=root
            / "WinPE"
            / "Runtime"
            / "deploy.config.example.ps1",
            unattend=root
            / "ServerTemplates"
            / "Unattend"
            / "unattend-win11-template.xml",
            unattend_example=root
            / "ServerTemplates"
            / "Unattend"
            / "unattend-win11-template.example.xml",
            backup_dir=root / "Logs" / "ConfigBackups",
            validation_script=root / "Tools" / "Test-IronDeploy.ps1",
            auth_bootstrap=root / "Data" / "auth-bootstrap.json",
        )


def hash_auth_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Superadmin password must contain at least 12 characters.")
    if len(password) > 512:
        raise ValueError("Superadmin password is too long.")
    salt = secrets.token_bytes(24)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, AUTH_PASSWORD_ITERATIONS
    )
    return "$".join(
        (
            "pbkdf2_sha256",
            str(AUTH_PASSWORD_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def read_auth_bootstrap(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        "username": str(payload.get("username", "")),
        "passwordHash": str(payload.get("passwordHash", "")),
    }


def normalize_auth(paths: IronDeployPaths, values: dict[str, Any]) -> dict[str, str]:
    current = read_auth_bootstrap(paths.auth_bootstrap)
    username = str(values.get("username", current.get("username", ""))).strip()
    if not AUTH_USERNAME_PATTERN.fullmatch(username):
        raise ValueError(
            "Superadmin username must be 3-64 letters, digits, dots, "
            "underscores, or hyphens."
        )
    password = str(values.get("password", ""))
    password_hash = (
        hash_auth_password(password) if password else current.get("passwordHash", "")
    )
    if not password_hash.startswith("pbkdf2_sha256$"):
        raise ValueError("Set the initial superadmin password (at least 12 characters).")
    return {"username": username, "passwordHash": password_hash}


def save_auth_bootstrap(path: Path, values: dict[str, str]) -> None:
    atomic_write(
        path,
        json.dumps(values, ensure_ascii=False, indent=2) + "\n",
    )


def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"^\s*(IRONAPI_[A-Z0-9_]+)\s*=(.*)$", line)
        if match:
            values[match.group(1)] = decode_dotenv_value(match.group(2))
    return values


def decode_dotenv_value(value: str) -> str:
    result = value.strip()
    if len(result) >= 2 and result[0] == result[-1] == "'":
        return result[1:-1].replace("\\'", "'")
    if len(result) >= 2 and result[0] == result[-1] == '"':
        return result[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return result


def encode_dotenv_value(value: str) -> str:
    if value == "":
        return ""
    if not re.search(r"[\s#'\"]", value):
        return value
    return "'" + value.replace("'", "\\'") + "'"


def read_ps_config(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        string_match = re.match(
            r"^\s*\$([A-Za-z][A-Za-z0-9_]*)\s*=\s*(['\"])(.*?)\2\s*$",
            line,
        )
        if string_match:
            values[string_match.group(1)] = string_match.group(3).replace("''", "'")
            continue
        bool_match = re.match(
            r"^\s*\$([A-Za-z][A-Za-z0-9_]*)\s*=\s*\$(true|false)\s*$",
            line,
            re.IGNORECASE,
        )
        if bool_match:
            values[bool_match.group(1)] = bool_match.group(2).lower()
    return values


def ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def backup_file(paths: IronDeployPaths, path: Path) -> str | None:
    if not path.is_file():
        return None
    paths.backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    backup = paths.backup_dir / f"{path.name}.{stamp}.bak"
    shutil.copy2(path, backup)
    return str(backup)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.urandom(8).hex()}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)



def ensure_config_files(paths: IronDeployPaths) -> dict[str, bool]:
    created = {"apiEnv": False, "winpeConfig": False}
    legacy_unattend = (
        paths.root / "Share" / "Unattend" / "unattend-win11-template.xml"
    )
    if legacy_unattend.is_file():
        if paths.unattend.is_file():
            raise RuntimeError(
                "Both legacy SMB-exposed and server-only unattend templates "
                "exist. Remove the legacy Share\\Unattend copy after verifying "
                "which configuration is current."
            )
        paths.unattend.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(legacy_unattend, paths.unattend)

    if not paths.api_env.is_file():
        if not paths.api_env_example.is_file():
            raise FileNotFoundError(f"Missing template: {paths.api_env_example}")
        paths.api_env.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(paths.api_env_example, paths.api_env)
        created["apiEnv"] = True

    if not paths.winpe_config.is_file():
        if not paths.winpe_config_example.is_file():
            raise FileNotFoundError(f"Missing template: {paths.winpe_config_example}")
        paths.winpe_config.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(paths.winpe_config_example, paths.winpe_config)
        created["winpeConfig"] = True

    return created
def load_config(paths: IronDeployPaths) -> dict[str, Any]:
    created = ensure_config_files(paths)
    api_values = read_dotenv(paths.api_env_example)
    current_api_values = read_dotenv(paths.api_env)
    for name, value in current_api_values.items():
        if value != "" or name in OPTIONAL_API_NAMES:
            api_values[name] = value

    winpe_values = read_ps_config(paths.winpe_config_example)
    winpe_values.update(read_ps_config(paths.winpe_config))

    # Use only values explicitly stored in Api/.env here. Falling back to the
    # example would make sample SMB settings look authoritative and overwrite
    # working credentials from a legacy WinPE config during migration.
    server_side_share = {
        "SharePath": current_api_values.get("IRONAPI_SMB_SHARE_PATH", ""),
        "ShareUser": current_api_values.get("IRONAPI_SMB_USER", ""),
        "SharePassword": current_api_values.get("IRONAPI_SMB_PASSWORD", ""),
    }
    for name, value in server_side_share.items():
        if value and "CHANGE_ME" not in value:
            winpe_values[name] = value

    has_password = bool(
        winpe_values.get("SharePassword")
        and "CHANGE_ME" not in winpe_values.get("SharePassword", "")
    )
    public_winpe = {name: winpe_values.get(name, "") for name in WINPE_NAMES}
    public_winpe["SharePassword"] = ""
    public_api = {name: api_values.get(name, "") for name in API_NAMES}
    public_api["IRONAPI_SMB_PASSWORD"] = ""
    unattend_values = read_unattend_settings(paths)
    auth_values = read_auth_bootstrap(paths.auth_bootstrap)

    return {
        "root": str(paths.root),
        "files": {
            "apiEnv": str(paths.api_env),
            "apiEnvExists": paths.api_env.is_file(),
            "apiEnvCreated": created["apiEnv"],
            "winpeConfig": str(paths.winpe_config),
            "winpeConfigExists": paths.winpe_config.is_file(),
            "winpeConfigCreated": created["winpeConfig"],
            "unattend": str(paths.unattend),
            "unattendExists": paths.unattend.is_file(),
        },
        "api": public_api,
        "winpe": public_winpe,
        "unattend": unattend_values,
        "auth": {"username": auth_values.get("username", ""), "password": ""},
        "timeZones": [
            {"id": item[0], "offset": item[1], "label": item[2]} for item in TIME_ZONES
        ],
        "secrets": {
            "hasWinpeSharePassword": has_password,
            "hasSuperadminPassword": bool(auth_values.get("passwordHash")),
            "hasApiServerCertificate": bool(
                winpe_values.get("ApiServerCertificateBase64")
            ),
        },
    }


def save_config(paths: IronDeployPaths, payload: dict[str, Any]) -> dict[str, Any]:
    api_updates = payload.get("api", {})
    winpe_updates = payload.get("winpe", {})
    unattend_updates = payload.get("unattend", {})
    auth_updates = payload.get("auth", {})
    if (
        not isinstance(api_updates, dict)
        or not isinstance(winpe_updates, dict)
        or not isinstance(unattend_updates, dict)
        or not isinstance(auth_updates, dict)
    ):
        raise ValueError("Invalid payload.")

    smb_names = {
        "SharePath": "IRONAPI_SMB_SHARE_PATH",
        "ShareUser": "IRONAPI_SMB_USER",
        "SharePassword": "IRONAPI_SMB_PASSWORD",
    }
    current_api = read_dotenv(paths.api_env)

    normalized_api: dict[str, str] | None = None
    normalized_api_updates: dict[str, str] = {}
    if api_updates:
        effective_api_updates = dict(current_api)
        effective_api_updates.update(api_updates)
        normalized_api = normalize_api(effective_api_updates)
        normalized_api_updates = {
            name: normalized_api[name]
            for name in api_updates
            if name in normalized_api
        }
        if "IRONAPI_ACCESS_MODE" in api_updates:
            normalized_api_updates["IRONAPI_COOKIE_SECURE"] = normalized_api[
                "IRONAPI_COOKIE_SECURE"
            ]

    access_mode = (
        normalized_api["IRONAPI_ACCESS_MODE"]
        if normalized_api is not None
        else current_api.get("IRONAPI_ACCESS_MODE", "")
        or read_dotenv(paths.api_env_example).get(
            "IRONAPI_ACCESS_MODE", "http_direct"
        )
    )

    normalized_winpe: dict[str, str] | None = None
    smb_api_updates: dict[str, str] = {}
    if winpe_updates:
        effective_winpe_updates = dict(winpe_updates)
        for winpe_name, api_name in smb_names.items():
            if not str(effective_winpe_updates.get(winpe_name, "")).strip():
                existing = current_api.get(api_name, "")
                if existing and "CHANGE_ME" not in existing:
                    effective_winpe_updates[winpe_name] = existing

        smb_is_in_scope = any(name in winpe_updates for name in smb_names)
        normalized_winpe = normalize_winpe(
            paths,
            effective_winpe_updates,
            access_mode,
            require_smb_configuration=smb_is_in_scope,
        )
        if smb_is_in_scope:
            smb_api_updates = {
                api_name: normalized_winpe[winpe_name]
                for winpe_name, api_name in smb_names.items()
            }
    normalized_unattend = (
        normalize_unattend(paths, unattend_updates) if unattend_updates else None
    )
    normalized_auth = normalize_auth(paths, auth_updates) if auth_updates else None

    backups: list[str] = []
    api_values_to_save = dict(normalized_api_updates)
    api_values_to_save.update(smb_api_updates)
    if api_values_to_save:
        backup = save_api_env(paths, api_values_to_save)
        if backup:
            backups.append(backup)
    if normalized_winpe is not None and any(
        name not in smb_names for name in winpe_updates
    ):
        backup = save_winpe_config(paths, normalized_winpe)
        if backup:
            backups.append(backup)
    if normalized_unattend is not None:
        backup = save_unattend_settings(paths, normalized_unattend)
        if backup:
            backups.append(backup)
    if normalized_auth is not None:
        save_auth_bootstrap(paths.auth_bootstrap, normalized_auth)
    return {"saved": True, "backups": backups, "config": load_config(paths)}


def read_unattend_settings(paths: IronDeployPaths) -> dict[str, str]:
    source = paths.unattend if paths.unattend.is_file() else paths.unattend_example
    time_zone = "Central Asia Standard Time"
    if source.is_file():
        content = source.read_text(encoding="utf-8-sig")
        match = re.search(r"<TimeZone>(.*?)</TimeZone>", content, re.DOTALL)
        if match:
            time_zone = match.group(1).strip()
    return {"TimeZone": time_zone}


def save_unattend_settings(paths: IronDeployPaths, values: dict[str, str]) -> str | None:
    created = False
    if not paths.unattend.is_file():
        if values["TimeZone"] == read_unattend_settings(paths)["TimeZone"]:
            return None
        if not paths.unattend_example.is_file():
            raise FileNotFoundError(f"Missing template: {paths.unattend_example}")
        paths.unattend.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(paths.unattend_example, paths.unattend)
        created = True

    backup = None if created else backup_file(paths, paths.unattend)
    content = paths.unattend.read_text(encoding="utf-8-sig")
    escaped_time_zone = escape(values["TimeZone"], quote=False)
    if not re.search(r"<TimeZone>.*?</TimeZone>", content, re.DOTALL):
        raise ValueError("Unattend template does not contain a TimeZone element.")
    content = re.sub(
        r"<TimeZone>.*?</TimeZone>",
        f"<TimeZone>{escaped_time_zone}</TimeZone>",
        content,
        flags=re.DOTALL,
    )
    atomic_write(paths.unattend, content)
    return backup


def save_api_env(paths: IronDeployPaths, updates: dict[str, str]) -> str | None:
    if not paths.api_env.is_file():
        shutil.copy2(paths.api_env_example, paths.api_env)

    backup = backup_file(paths, paths.api_env)
    lines = paths.api_env.read_text(encoding="utf-8-sig").splitlines()
    seen: set[str] = set()
    updated_lines: list[str] = []
    for line in lines:
        match = re.match(r"^\s*(IRONAPI_[A-Z0-9_]+)\s*=", line)
        if match and match.group(1) in updates:
            name = match.group(1)
            updated_lines.append(f"{name}={encode_dotenv_value(updates[name])}")
            seen.add(name)
        else:
            updated_lines.append(line)
    for name in API_NAMES:
        if name in updates and name not in seen:
            if updated_lines and updated_lines[-1] != "":
                updated_lines.append("")
            updated_lines.append(f"{name}={encode_dotenv_value(updates[name])}")
    atomic_write(paths.api_env, "\n".join(updated_lines) + "\n")
    return backup


def save_winpe_config(paths: IronDeployPaths, values: dict[str, str]) -> str | None:
    backup = backup_file(paths, paths.winpe_config)
    lines = [
        "# Generated by SetupWeb.",
        "# Contains no deployment credentials; secrets stay server-side in Api\\.env.",
        "",
        f"$ShareDrive = {ps_literal(values['ShareDrive'])}",
        f"$ApiBaseUrl = {ps_literal(values['ApiBaseUrl'])}",
        f"$ValidateApiServerCertificate = ${values['ValidateApiServerCertificate']}",
        f"$ApiServerCertificateType = {ps_literal(values['ApiServerCertificateType'])}",
        f"$ApiServerCertificateBase64 = {ps_literal(values['ApiServerCertificateBase64'])}",
        f"$ImagesPath = {ps_literal(values['ImagesPath'])}",
        f"$DriversPath = {ps_literal(values['DriversPath'])}",
        f"$EnableGuiImageApplyProgress = ${values['EnableGuiImageApplyProgress']}",
    ]
    atomic_write(paths.winpe_config, "\n".join(lines) + "\n")
    return backup


def normalize_api(values: dict[str, Any]) -> dict[str, str]:
    defaults = read_dotenv(Path(__file__).resolve().parents[2] / "Api" / ".env.example")
    result: dict[str, str] = {}
    for name in API_NAMES:
        candidate = str(values.get(name, "")).strip()
        if candidate == "" and name not in OPTIONAL_API_NAMES:
            # Optional settings must stay empty when the operator clears them.
            # Falling back here would silently write the example placeholders
            # into a live configuration and make the feature look configured.
            candidate = defaults.get(name, "")
            if candidate == "":
                raise ValueError(f"{name} is required in Api\\.env.example.")
        result[name] = candidate

    access_mode = result["IRONAPI_ACCESS_MODE"].lower()
    if access_mode not in {"http_direct", "https_proxy"}:
        raise ValueError(
            "IRONAPI_ACCESS_MODE must be http_direct or https_proxy."
        )
    result["IRONAPI_ACCESS_MODE"] = access_mode
    if access_mode == "https_proxy":
        result["IRONAPI_BIND_HOST"] = "127.0.0.1"
        result["IRONAPI_COOKIE_SECURE"] = "true"
    else:
        result["IRONAPI_COOKIE_SECURE"] = "false"

    bind_host = result["IRONAPI_BIND_HOST"]
    if bind_host != "0.0.0.0":
        parse_ip(bind_host, "IRONAPI_BIND_HOST")
    if access_mode == "http_direct" and ipaddress.ip_address(bind_host).is_loopback:
        raise ValueError(
            "HTTP direct mode requires a non-loopback IRONAPI_BIND_HOST."
        )

    port = parse_int(result["IRONAPI_PORT"], "IRONAPI_PORT")
    if port < 1 or port > 65535:
        raise ValueError("IRONAPI_PORT must be from 1 to 65535.")
    result["IRONAPI_PORT"] = str(port)

    result["IRONAPI_ACCESS_LOG"] = normalize_bool(result["IRONAPI_ACCESS_LOG"])
    result["IRONAPI_COOKIE_SECURE"] = normalize_bool(result["IRONAPI_COOKIE_SECURE"])
    result["IRONAPI_LDAP_USE_SSL"] = normalize_bool(result["IRONAPI_LDAP_USE_SSL"])

    authorization_timeout = parse_int(
        result["IRONAPI_DEPLOYMENT_AUTHORIZATION_TIMEOUT_MINUTES"],
        "IRONAPI_DEPLOYMENT_AUTHORIZATION_TIMEOUT_MINUTES",
    )
    if authorization_timeout < 5 or authorization_timeout > 30:
        raise ValueError(
            "IRONAPI_DEPLOYMENT_AUTHORIZATION_TIMEOUT_MINUTES must be from 5 to 30."
        )
    result["IRONAPI_DEPLOYMENT_AUTHORIZATION_TIMEOUT_MINUTES"] = str(
        authorization_timeout
    )

    deployment_timeout = parse_int(
        result["IRONAPI_DEPLOYMENT_TIMEOUT_MINUTES"],
        "IRONAPI_DEPLOYMENT_TIMEOUT_MINUTES",
    )
    if deployment_timeout < 30 or deployment_timeout > 240:
        raise ValueError(
            "IRONAPI_DEPLOYMENT_TIMEOUT_MINUTES must be from 30 to 240."
        )
    result["IRONAPI_DEPLOYMENT_TIMEOUT_MINUTES"] = str(deployment_timeout)

    image_apply_mode = result["IRONAPI_IMAGE_APPLY_MODE"].lower()
    if image_apply_mode not in {"direct", "staged"}:
        raise ValueError("IRONAPI_IMAGE_APPLY_MODE must be direct or staged.")
    result["IRONAPI_IMAGE_APPLY_MODE"] = image_apply_mode

    driver_apply_mode = result["IRONAPI_DRIVER_APPLY_MODE"].lower()
    if driver_apply_mode not in {"direct", "staged"}:
        raise ValueError("IRONAPI_DRIVER_APPLY_MODE must be direct or staged.")
    result["IRONAPI_DRIVER_APPLY_MODE"] = driver_apply_mode

    driver_integer_ranges = {
        "IRONAPI_DRIVER_MAX_FILES": (1, 1_000_000),
        "IRONAPI_DRIVER_MAX_DEPTH": (1, 100),
        "IRONAPI_DRIVER_MAX_FULL_PATH": (64, 32767),
        "IRONAPI_DRIVER_UPLOAD_TTL_HOURS": (1, 8760),
        "IRONAPI_DRIVER_MAX_ACTIVE_UPLOADS": (1, 100),
        "IRONAPI_DRIVER_MIN_FREE_SPACE_GIB": (1, 10240),
    }
    for name, (minimum, maximum) in driver_integer_ranges.items():
        value = parse_int(result[name], name)
        if value < minimum or value > maximum:
            raise ValueError(f"{name} must be from {minimum} to {maximum}.")
        result[name] = str(value)

    cleaned = []
    for item in result["IRONAPI_ALLOWED_CLIENT_NETWORKS"].split(","):
        network = item.strip()
        if network:
            ipaddress.ip_network(network, strict=False)
            cleaned.append(network)
    result["IRONAPI_ALLOWED_CLIENT_NETWORKS"] = ",".join(cleaned)

    width = parse_int(result["IRONAPI_NAME_WIDTH"], "IRONAPI_NAME_WIDTH")
    if width < 1 or width > 20:
        raise ValueError("IRONAPI_NAME_WIDTH must be from 1 to 20.")
    result["IRONAPI_NAME_WIDTH"] = str(width)

    start = parse_int(result["IRONAPI_NAME_START"], "IRONAPI_NAME_START")
    if start < 0:
        raise ValueError("IRONAPI_NAME_START must be non-negative.")
    result["IRONAPI_NAME_START"] = str(start)

    timeout = parse_int(result["IRONAPI_LDAP_CONNECT_TIMEOUT"], "IRONAPI_LDAP_CONNECT_TIMEOUT")
    if timeout < 1 or timeout > 60:
        raise ValueError("IRONAPI_LDAP_CONNECT_TIMEOUT must be from 1 to 60.")
    result["IRONAPI_LDAP_CONNECT_TIMEOUT"] = str(timeout)

    odj_timeout = parse_int(result["IRONAPI_ODJ_PROVISION_TIMEOUT"], "IRONAPI_ODJ_PROVISION_TIMEOUT")
    if odj_timeout < 1 or odj_timeout > 300:
        raise ValueError("IRONAPI_ODJ_PROVISION_TIMEOUT must be from 1 to 300.")
    result["IRONAPI_ODJ_PROVISION_TIMEOUT"] = str(odj_timeout)

    blob_max_age = parse_int(
        result["IRONAPI_ODJ_BLOB_MAX_AGE_MINUTES"],
        "IRONAPI_ODJ_BLOB_MAX_AGE_MINUTES",
    )
    if blob_max_age < 5 or blob_max_age > 1440:
        raise ValueError(
            "IRONAPI_ODJ_BLOB_MAX_AGE_MINUTES must be from 5 to 1440."
        )
    result["IRONAPI_ODJ_BLOB_MAX_AGE_MINUTES"] = str(blob_max_age)

    prefix = result["IRONAPI_NAME_PREFIX"]
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9-]{0,14}$", prefix):
        raise ValueError("IRONAPI_NAME_PREFIX is invalid.")
    result["IRONAPI_NAME_PREFIX"] = prefix.lower()
    return result


def validate_certificate(
    paths: IronDeployPaths,
    encoded: str,
    certificate_type: str,
) -> dict[str, Any]:
    candidate = str(encoded or "").strip()
    if not candidate:
        candidate = read_ps_config(paths.winpe_config).get(
            "ApiServerCertificateBase64", ""
        )
    if not candidate:
        raise ValueError("Select a certificate file before checking it.")

    certificate_type = str(certificate_type or "").strip().lower()
    if certificate_type not in CERTIFICATE_TYPES:
        raise ValueError("Certificate type must be self_signed or ca.")

    _, certificate = normalize_certificate_base64(candidate)
    self_signed = certificate["subject"] == certificate["issuer"]
    if certificate_type == "self_signed" and not self_signed:
        raise ValueError(
            "Self-signed mode requires a certificate whose subject and issuer "
            "are the same."
        )

    return {
        "valid": True,
        "selfSigned": self_signed,
        "notAfter": certificate["not_after"].isoformat(),
    }


def normalize_smb_server_address(value: str) -> str:
    server_address = str(value or "").strip()
    if (
        not server_address
        or len(server_address) > 253
        or not SMB_SERVER_ADDRESS_PATTERN.fullmatch(server_address)
    ):
        raise ValueError("SMB address must be a hostname, FQDN, or IPv4 address.")

    if re.fullmatch(r"[0-9.]+", server_address) and "." in server_address:
        try:
            ipaddress.IPv4Address(server_address)
        except ipaddress.AddressValueError as exc:
            raise ValueError("SMB IPv4 address is invalid.") from exc
        return server_address

    labels = server_address.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        for label in labels
    ):
        raise ValueError(
            "SMB address must be a valid hostname, FQDN, or IPv4 address."
        )
    return server_address


def normalize_winpe(
    paths: IronDeployPaths,
    values: dict[str, Any],
    access_mode: str | None = None,
    require_smb_configuration: bool = True,
) -> dict[str, str]:
    current = read_ps_config(paths.winpe_config_example)
    configured = read_ps_config(paths.winpe_config)
    current.update(configured)
    result: dict[str, str] = {}
    for name in WINPE_NAMES:
        candidate = str(values.get(name, current.get(name, ""))).strip()
        if candidate == "":
            candidate = current.get(name, "")
        result[name] = candidate

    if require_smb_configuration:
        share_path_match = re.fullmatch(r"\\\\([^\\]+)\\([^\\]+)", result["SharePath"])
        if share_path_match is None:
            raise ValueError("SharePath must be a UNC path such as \\\\SERVER\\IronDeploy.")
        normalize_smb_server_address(share_path_match.group(1))
    if not re.match(r"^[A-Za-z]:$", result["ShareDrive"]):
        raise ValueError("ShareDrive must be one drive letter followed by a colon.")
    if require_smb_configuration:
        account_match = re.fullmatch(
            r"([^\\/@\r\n]+)\\([^\\/@\r\n]+)",
            result["ShareUser"],
        )
        if (
            account_match is None
            or account_match.group(1) != account_match.group(1).strip()
            or account_match.group(2) != account_match.group(2).strip()
        ):
            raise ValueError("ShareUser must use SERVER\\user or DOMAIN\\user format.")
        if not result["SharePassword"]:
            result["SharePassword"] = current.get("SharePassword", "")
        if not result["SharePassword"] or "CHANGE_ME" in result["SharePassword"]:
            raise ValueError("SharePassword is required for the IronAPI SMB settings.")
    if not re.match(r"^https?://[^/]+(?::\d+)?$", result["ApiBaseUrl"]):
        raise ValueError("ApiBaseUrl must look like http://198.51.100.10:8000.")

    access_mode = str(access_mode or "").lower()
    if access_mode == "http_direct" and not result["ApiBaseUrl"].lower().startswith("http://"):
        raise ValueError("HTTP direct mode requires an http:// ApiBaseUrl.")
    if access_mode == "https_proxy" and not result["ApiBaseUrl"].lower().startswith("https://"):
        raise ValueError("HTTPS reverse proxy mode requires an https:// ApiBaseUrl.")

    result["ValidateApiServerCertificate"] = normalize_bool(
        result["ValidateApiServerCertificate"]
    )
    certificate_type = result["ApiServerCertificateType"].lower()
    if certificate_type not in CERTIFICATE_TYPES:
        raise ValueError("ApiServerCertificateType must be self_signed or ca.")
    result["ApiServerCertificateType"] = certificate_type

    uploaded_certificate = str(
        values.get("ApiServerCertificateBase64", "")
    ).strip()
    certificate_base64 = uploaded_certificate or current.get(
        "ApiServerCertificateBase64", ""
    )
    if result["ValidateApiServerCertificate"] == "true":
        if not result["ApiBaseUrl"].lower().startswith("https://"):
            raise ValueError(
                "Certificate validation requires an https:// ApiBaseUrl."
            )
        if not certificate_base64:
            raise ValueError(
                "A server or CA certificate is required when certificate "
                "validation is enabled."
            )
        certificate_base64, certificate = normalize_certificate_base64(
            certificate_base64
        )
        if (
            certificate_type == "self_signed"
            and certificate["subject"] != certificate["issuer"]
        ):
            raise ValueError(
                "Self-signed mode requires a certificate whose subject and "
                "issuer are the same."
            )
    elif uploaded_certificate:
        certificate_base64, _ = normalize_certificate_base64(
            uploaded_certificate
        )
    result["ApiServerCertificateBase64"] = certificate_base64

    result["EnableGuiImageApplyProgress"] = normalize_bool(
        result["EnableGuiImageApplyProgress"]
    )

    drive = result["ShareDrive"]
    result["ImagesPath"] = f"{drive}\\Images"
    result["DriversPath"] = f"{drive}\\Drivers"
    return result


def normalize_unattend(paths: IronDeployPaths, values: dict[str, Any]) -> dict[str, str]:
    current = read_unattend_settings(paths)
    time_zone = str(values.get("TimeZone", current["TimeZone"])).strip()
    if time_zone not in TIME_ZONE_IDS:
        raise ValueError("TimeZone must be selected from the supported Windows time zone list.")
    return {"TimeZone": time_zone}


def normalize_bool(value: str) -> str:
    if str(value).strip().lower() in {"true", "1", "yes", "y", "on"}:
        return "true"
    return "false"


def normalize_certificate_base64(value: str) -> tuple[str, dict[str, Any]]:
    encoded = str(value).strip()
    if encoded.lower().startswith("data:"):
        marker = encoded.find(",")
        if marker < 0:
            raise ValueError("The certificate data URL is invalid.")
        encoded = encoded[marker + 1 :]

    try:
        uploaded = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("The certificate upload is not valid base64.") from exc

    if not uploaded or len(uploaded) > CERTIFICATE_MAX_BYTES:
        raise ValueError("The certificate must be between 1 byte and 64 KiB.")

    if b"-----BEGIN CERTIFICATE-----" in uploaded:
        try:
            pem = uploaded.decode("ascii")
            der = base64.b64decode(
                "".join(
                    line.strip()
                    for line in pem.splitlines()
                    if line.strip()
                    and not line.startswith("-----BEGIN")
                    and not line.startswith("-----END")
                ),
                validate=True,
            )
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("The PEM certificate is invalid.") from exc
    else:
        der = uploaded

    certificate = inspect_certificate_der(der)
    now = datetime.now(timezone.utc)
    if certificate["not_before"] > now:
        raise ValueError("The certificate is not valid yet.")
    if certificate["not_after"] <= now:
        raise ValueError("The certificate has expired.")
    return base64.b64encode(der).decode("ascii"), certificate


def inspect_certificate_der(der: bytes) -> dict[str, Any]:
    try:
        pem = ssl.DER_cert_to_PEM_cert(der)
    except (ValueError, ssl.SSLError) as exc:
        raise ValueError("The uploaded file is not an X.509 certificate.") from exc

    temporary_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="ascii", suffix=".pem", delete=False
        ) as temporary:
            temporary.write(pem)
            temporary_path = temporary.name
        decoded = ssl._ssl._test_decode_cert(temporary_path)
    except (OSError, ValueError, ssl.SSLError) as exc:
        raise ValueError("The uploaded file is not an X.509 certificate.") from exc
    finally:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass

    try:
        not_before = datetime.fromtimestamp(
            ssl.cert_time_to_seconds(decoded["notBefore"]), timezone.utc
        )
        not_after = datetime.fromtimestamp(
            ssl.cert_time_to_seconds(decoded["notAfter"]), timezone.utc
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("The X.509 certificate validity period is invalid.") from exc

    return {
        "subject": decoded.get("subject", ()),
        "issuer": decoded.get("issuer", ()),
        "not_before": not_before,
        "not_after": not_after,
    }


def parse_int(value: str, field_name: str) -> int:
    try:
        return int(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc


def parse_ip(value: str, field_name: str) -> None:
    try:
        ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an IP address.") from exc
