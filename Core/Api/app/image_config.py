"""Read and write file-backed Windows image settings surfaced in IronAPI.

WinPE bootstrap and UI behavior remain in ``deploy.config.ps1``. Regional
settings and the existing plaintext account passwords remain in the server-only
unattend template. Post-install account policy belongs to the default deployment
profile in SQLite and is no longer read from the WinPE runtime source.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from app.config import IRONDEPLOY_ROOT

# The plain-text placeholder shipped in the example templates. A value equal to
# this (or any ``CHANGE_ME`` marker) is treated as "not configured yet".
PASSWORD_PLACEHOLDER = "CHANGE_ME_USE_A_UNIQUE_PASSWORD"

# Windows time zone ids, offset, and a short label. Kept in sync with the list
# SetupWeb offers so both surfaces present the same choices.
TIME_ZONES: tuple[tuple[str, str, str], ...] = (
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
DEFAULT_TIME_ZONE = "Central Asia Standard Time"
DEFAULT_LOCAL_ADMIN_NAME = "localadmin"

_ADMIN_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,20}$")
UI_LANGUAGES: tuple[tuple[str, str], ...] = (
    ("ar-SA", "Arabic"),
    ("az-Latn-AZ", "Azerbaijani (Latin)"),
    ("bg-BG", "Bulgarian"),
    ("cs-CZ", "Czech"),
    ("da-DK", "Danish"),
    ("de-DE", "German"),
    ("el-GR", "Greek"),
    ("en-GB", "English (United Kingdom)"),
    ("en-US", "English (United States)"),
    ("es-ES", "Spanish"),
    ("et-EE", "Estonian"),
    ("fi-FI", "Finnish"),
    ("fr-FR", "French"),
    ("he-IL", "Hebrew"),
    ("hu-HU", "Hungarian"),
    ("it-IT", "Italian"),
    ("ja-JP", "Japanese"),
    ("kk-KZ", "Kazakh"),
    ("ko-KR", "Korean"),
    ("lt-LT", "Lithuanian"),
    ("lv-LV", "Latvian"),
    ("nb-NO", "Norwegian"),
    ("nl-NL", "Dutch"),
    ("pl-PL", "Polish"),
    ("pt-BR", "Portuguese (Brazil)"),
    ("pt-PT", "Portuguese (Portugal)"),
    ("ro-RO", "Romanian"),
    ("ru-RU", "Russian"),
    ("sk-SK", "Slovak"),
    ("sl-SI", "Slovenian"),
    ("sr-Latn-RS", "Serbian (Latin)"),
    ("sv-SE", "Swedish"),
    ("th-TH", "Thai"),
    ("tr-TR", "Turkish"),
    ("uk-UA", "Ukrainian"),
    ("vi-VN", "Vietnamese"),
    ("zh-CN", "Chinese (Simplified)"),
    ("zh-TW", "Chinese (Traditional)"),
)

LOCALES: tuple[tuple[str, str], ...] = UI_LANGUAGES + (
    ("az-Cyrl-AZ", "Azerbaijani (Cyrillic)"),
    ("be-BY", "Belarusian"),
    ("en-AU", "English (Australia)"),
    ("en-CA", "English (Canada)"),
    ("es-MX", "Spanish (Mexico)"),
    ("fa-IR", "Persian"),
    ("fr-CA", "French (Canada)"),
    ("hi-IN", "Hindi"),
    ("hy-AM", "Armenian"),
    ("id-ID", "Indonesian"),
    ("ka-GE", "Georgian"),
    ("ky-KG", "Kyrgyz"),
    ("ru-KZ", "Russian (Kazakhstan)"),
    ("sr-Cyrl-RS", "Serbian (Cyrillic)"),
    ("tg-Cyrl-TJ", "Tajik (Cyrillic)"),
    ("tk-TM", "Turkmen"),
    ("ur-PK", "Urdu"),
    ("uz-Cyrl-UZ", "Uzbek (Cyrillic)"),
    ("uz-Latn-UZ", "Uzbek (Latin)"),
)

KEYBOARD_LAYOUTS: tuple[tuple[str, str], ...] = LOCALES
UI_LANGUAGE_IDS = {item[0] for item in UI_LANGUAGES}
LOCALE_IDS = {item[0] for item in LOCALES}
KEYBOARD_LAYOUT_IDS = {item[0] for item in KEYBOARD_LAYOUTS}


class ImageConfigError(ValueError):
    """Raised when the submitted image settings are invalid."""


@dataclass(frozen=True)
class ImagePaths:
    winpe_config: Path
    winpe_config_example: Path
    unattend: Path
    unattend_example: Path
    backup_dir: Path
    api_env: Path | None = None
    api_env_example: Path | None = None

    @staticmethod
    def default() -> "ImagePaths":
        root = IRONDEPLOY_ROOT
        return ImagePaths(
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
        )


# ---------------------------------------------------------------------------
# Low level file helpers
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.urandom(8).hex()}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _backup_file(paths: ImagePaths, path: Path) -> str | None:
    if not path.is_file():
        return None
    paths.backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    backup = paths.backup_dir / f"{path.name}.{stamp}.bak"
    shutil.copy2(path, backup)
    return str(backup)


def _read_ps_config(path: Path) -> dict[str, str]:
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


def _normalize_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "y", "on"}


def _read_image_apply_mode(paths: ImagePaths) -> str:
    mode = "direct"
    for path in (paths.api_env_example, paths.api_env):
        if path is None or not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            match = re.match(
                r"^\s*IRONAPI_IMAGE_APPLY_MODE\s*=\s*(.*?)\s*$",
                line,
            )
            if match:
                mode = match.group(1).strip().strip("'\"").lower()
    return mode if mode in {"direct", "staged"} else "direct"


def _save_image_apply_mode(paths: ImagePaths, mode: str) -> str | None:
    if paths.api_env is None or paths.api_env_example is None:
        raise ImageConfigError("IronAPI config paths are unavailable.")
    if not paths.api_env.is_file():
        if not paths.api_env_example.is_file():
            raise ImageConfigError(
                f"Missing IronAPI config template: {paths.api_env_example}"
            )
        paths.api_env.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(paths.api_env_example, paths.api_env)
        backup = None
    else:
        backup = _backup_file(paths, paths.api_env)

    lines = paths.api_env.read_text(encoding="utf-8-sig").splitlines()
    replacement = f"IRONAPI_IMAGE_APPLY_MODE={mode}"
    updated: list[str] = []
    found = False
    for line in lines:
        if re.match(r"^\s*IRONAPI_IMAGE_APPLY_MODE\s*=", line):
            updated.append(replacement)
            found = True
        else:
            updated.append(line)
    if not found:
        if updated and updated[-1] != "":
            updated.append("")
        updated.append(replacement)
    _atomic_write(paths.api_env, "\n".join(updated) + "\n")
    return backup


# ---------------------------------------------------------------------------
# Unattend XML helpers
# ---------------------------------------------------------------------------


def _unattend_source(paths: ImagePaths) -> Path:
    return paths.unattend if paths.unattend.is_file() else paths.unattend_example


def _local_account_block(content: str) -> str | None:
    match = re.search(
        r"<LocalAccount\b[^>]*>.*?</LocalAccount>",
        content,
        re.DOTALL,
    )
    return match.group(0) if match else None


def _has_real_password(value: str | None) -> bool:
    if value is None:
        return False
    stripped = value.strip()
    return bool(stripped) and "CHANGE_ME" not in stripped


def _read_unattend(paths: ImagePaths) -> dict[str, Any]:
    source = _unattend_source(paths)
    time_zone = DEFAULT_TIME_ZONE
    regional = {
        "inputLocale": "ru-RU",
        "systemLocale": "ru-RU",
        "uiLanguage": "ru-RU",
        "userLocale": "ru-RU",
    }
    admin_name = DEFAULT_LOCAL_ADMIN_NAME
    has_password = False
    has_builtin_password = False
    if source.is_file():
        content = source.read_text(encoding="utf-8-sig")
        tz_match = re.search(r"<TimeZone>(.*?)</TimeZone>", content, re.DOTALL)
        if tz_match:
            time_zone = tz_match.group(1).strip()
        for element_name, field_name in (
            ("InputLocale", "inputLocale"),
            ("SystemLocale", "systemLocale"),
            ("UILanguage", "uiLanguage"),
            ("UserLocale", "userLocale"),
        ):
            match = re.search(
                rf"<{element_name}>(.*?)</{element_name}>",
                content,
                re.DOTALL,
            )
            if match and match.group(1).strip():
                regional[field_name] = match.group(1).strip()
        block = _local_account_block(content)
        if block:
            name_match = re.search(r"<Name>(.*?)</Name>", block, re.DOTALL)
            if name_match and name_match.group(1).strip():
                admin_name = name_match.group(1).strip()
            value_match = re.search(
                r"<Password>.*?<Value>(.*?)</Value>",
                block,
                re.DOTALL,
            )
            if value_match:
                has_password = _has_real_password(value_match.group(1))
        builtin_match = re.search(
            r"<AdministratorPassword\b[^>]*>.*?<Value>(.*?)</Value>",
            content,
            re.DOTALL,
        )
        if builtin_match:
            has_builtin_password = _has_real_password(builtin_match.group(1))
    return {
        "timeZone": time_zone,
        **regional,
        "localAdminName": admin_name,
        "hasLocalAdminPassword": has_password,
        "hasBuiltInAdministratorPassword": has_builtin_password,
    }


def _patch_unattend_block(block: str, admin_name: str, password: str | None) -> str:
    updated = re.sub(
        r"(<Name>)(.*?)(</Name>)",
        lambda m: f"{m.group(1)}{escape(admin_name)}{m.group(3)}",
        block,
        count=1,
        flags=re.DOTALL,
    )
    updated = re.sub(
        r"(<DisplayName>)(.*?)(</DisplayName>)",
        lambda m: f"{m.group(1)}{escape(admin_name)}{m.group(3)}",
        updated,
        count=1,
        flags=re.DOTALL,
    )
    if password is not None:
        if not re.search(r"<Password>.*?<Value>.*?</Value>", updated, re.DOTALL):
            raise ImageConfigError(
                "Unattend template does not contain a localadmin <Password><Value>."
            )
        updated = re.sub(
            r"(<Password>.*?<Value>)(.*?)(</Value>)",
            lambda m: f"{m.group(1)}{escape(password)}{m.group(3)}",
            updated,
            count=1,
            flags=re.DOTALL,
        )
    return updated


def _set_builtin_administrator_password(content: str, password: str) -> str:
    """Set the built-in Administrator password in the oobeSystem UserAccounts.

    The element is created only on demand so that leaving the field empty never
    writes a password (a placeholder here would become the real password Windows
    applies). Windows scrubs this value from the deployed C:\\Windows\\Panther
    unattend after it is processed.
    """

    value = escape(password)
    if re.search(r"<AdministratorPassword\b", content):
        if not re.search(
            r"<AdministratorPassword\b[^>]*>.*?<Value>.*?</Value>",
            content,
            re.DOTALL,
        ):
            raise ImageConfigError("Unattend <AdministratorPassword> is malformed.")
        return re.sub(
            r"(<AdministratorPassword\b[^>]*>.*?<Value>)(.*?)(</Value>)",
            lambda m: f"{m.group(1)}{value}{m.group(3)}",
            content,
            count=1,
            flags=re.DOTALL,
        )

    marker = re.search(r"(?m)^([ \t]*)<LocalAccounts>", content)
    if marker is None:
        raise ImageConfigError(
            "Unattend template does not contain a <LocalAccounts> element."
        )
    indent = marker.group(1)
    block = (
        f"{indent}<AdministratorPassword>\n"
        f"{indent}  <Value>{value}</Value>\n"
        f"{indent}  <PlainText>true</PlainText>\n"
        f"{indent}</AdministratorPassword>\n"
    )
    return content[: marker.start()] + block + content[marker.start() :]


def _save_unattend(
    paths: ImagePaths,
    time_zone: str,
    input_locale: str,
    system_locale: str,
    ui_language: str,
    user_locale: str,
    admin_name: str,
    password: str | None,
    builtin_password: str | None,
) -> str | None:
    if not paths.unattend.is_file():
        if not paths.unattend_example.is_file():
            raise ImageConfigError(
                f"Missing unattend template: {paths.unattend_example}"
            )
        paths.unattend.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(paths.unattend_example, paths.unattend)
        backup = None
    else:
        backup = _backup_file(paths, paths.unattend)

    content = paths.unattend.read_text(encoding="utf-8-sig")
    if not re.search(r"<TimeZone>.*?</TimeZone>", content, re.DOTALL):
        raise ImageConfigError("Unattend template does not contain a TimeZone element.")
    content = re.sub(
        r"<TimeZone>.*?</TimeZone>",
        f"<TimeZone>{escape(time_zone)}</TimeZone>",
        content,
        flags=re.DOTALL,
    )
    for element_name, value in (
        ("InputLocale", input_locale),
        ("SystemLocale", system_locale),
        ("UILanguage", ui_language),
        ("UserLocale", user_locale),
    ):
        pattern = rf"<{element_name}>.*?</{element_name}>"
        if not re.search(pattern, content, re.DOTALL):
            raise ImageConfigError(
                f"Unattend template does not contain a {element_name} element."
            )
        content = re.sub(
            pattern,
            f"<{element_name}>{escape(value)}</{element_name}>",
            content,
            count=1,
            flags=re.DOTALL,
        )

    block = _local_account_block(content)
    if block is None:
        raise ImageConfigError(
            "Unattend template does not contain a localadmin <LocalAccount>."
        )
    content = content.replace(block, _patch_unattend_block(block, admin_name, password))

    if builtin_password is not None:
        content = _set_builtin_administrator_password(content, builtin_password)

    _atomic_write(paths.unattend, content)
    return backup


# ---------------------------------------------------------------------------
# deploy.config.ps1 helpers
# ---------------------------------------------------------------------------

_REMOVED_WINPE_FIELDS = {
    "ImageIndex",
    "SetupLocalAdminName",
    "EnableBuiltInAdministrator",
    "EnableSetupLocalAdmin",
    "DisableSetupLocalAdmin",
}


def _save_winpe_config(
    paths: ImagePaths,
    enable_gui_image_apply_progress: bool,
) -> str | None:
    if not paths.winpe_config.is_file():
        if not paths.winpe_config_example.is_file():
            raise ImageConfigError(
                f"Missing WinPE config template: {paths.winpe_config_example}"
            )
        paths.winpe_config.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(paths.winpe_config_example, paths.winpe_config)
        backup = None
    else:
        backup = _backup_file(paths, paths.winpe_config)

    replacement = (
        "$EnableGuiImageApplyProgress = "
        f"${'true' if enable_gui_image_apply_progress else 'false'}"
    )

    lines = paths.winpe_config.read_text(encoding="utf-8-sig").splitlines()
    seen = False
    updated: list[str] = []
    for line in lines:
        match = re.match(r"^\s*\$([A-Za-z][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1) == "EnableGuiImageApplyProgress":
            updated.append(replacement)
            seen = True
        elif match and match.group(1) in _REMOVED_WINPE_FIELDS:
            continue
        else:
            updated.append(line)
    if not seen:
        updated.append(replacement)

    _atomic_write(paths.winpe_config, "\n".join(updated) + "\n")
    return backup


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _migrate_legacy_unattend(paths: ImagePaths) -> None:
    root = paths.unattend.parents[2]
    legacy = root / "Share" / "Unattend" / "unattend-win11-template.xml"
    if not legacy.is_file():
        return
    if paths.unattend.is_file():
        raise ImageConfigError(
            "Both legacy SMB-exposed and server-only unattend templates exist. "
            "Remove the legacy Share\\Unattend copy after verifying which "
            "configuration is current."
        )
    paths.unattend.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(legacy, paths.unattend)


def load_image_config(paths: ImagePaths | None = None) -> dict[str, Any]:
    paths = paths or ImagePaths.default()
    _migrate_legacy_unattend(paths)
    winpe = _read_ps_config(paths.winpe_config_example)
    winpe.update(_read_ps_config(paths.winpe_config))
    unattend = _read_unattend(paths)

    return {
        "imageApplyMode": _read_image_apply_mode(paths),
        "enableGuiImageApplyProgress": _normalize_bool(
            winpe.get("EnableGuiImageApplyProgress", "true")
        ),
        "timeZone": unattend["timeZone"],
        "inputLocale": unattend["inputLocale"],
        "systemLocale": unattend["systemLocale"],
        "uiLanguage": unattend["uiLanguage"],
        "userLocale": unattend["userLocale"],
        "hasLocalAdminPassword": unattend["hasLocalAdminPassword"],
        "hasBuiltInAdministratorPassword": unattend[
            "hasBuiltInAdministratorPassword"
        ],
        "timeZones": [
            {"id": item[0], "offset": item[1], "label": item[2]}
            for item in TIME_ZONES
        ],
        "uiLanguages": [
            {"id": item[0], "label": item[1]}
            for item in sorted(UI_LANGUAGES, key=lambda item: item[1])
        ],
        "locales": [
            {"id": item[0], "label": item[1]}
            for item in sorted(LOCALES, key=lambda item: item[1])
        ],
        "keyboardLayouts": [
            {"id": item[0], "label": item[1]}
            for item in sorted(KEYBOARD_LAYOUTS, key=lambda item: item[1])
        ],
        "files": {
            "winpeConfig": str(paths.winpe_config),
            "winpeConfigExists": paths.winpe_config.is_file(),
            "unattend": str(paths.unattend),
            "unattendExists": paths.unattend.is_file(),
        },
    }


def save_image_config(
    payload: dict[str, Any],
    paths: ImagePaths | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ImageConfigError("Invalid payload.")
    paths = paths or ImagePaths.default()
    _migrate_legacy_unattend(paths)

    admin_name = str(payload.get("localAdminName", "")).strip()
    if not _ADMIN_NAME_PATTERN.match(admin_name):
        raise ImageConfigError(
            "Local admin name must be 1-20 letters, digits, dot, underscore, or hyphen."
        )

    time_zone = str(payload.get("timeZone", "")).strip()
    if time_zone not in TIME_ZONE_IDS:
        raise ImageConfigError(
            "Time zone must be selected from the supported Windows time zone list."
        )

    current_unattend = _read_unattend(paths)
    input_locale = _validate_input_locale(
        payload.get("inputLocale", current_unattend["inputLocale"])
    )
    system_locale = _validate_locale(
        payload.get("systemLocale", current_unattend["systemLocale"]),
        "System locale",
    )
    ui_language = _validate_locale(
        payload.get("uiLanguage", current_unattend["uiLanguage"]),
        "UI language",
    )
    user_locale = _validate_locale(
        payload.get("userLocale", current_unattend["userLocale"]),
        "User locale",
    )

    current_winpe = _read_ps_config(paths.winpe_config_example)
    current_winpe.update(_read_ps_config(paths.winpe_config))
    enable_gui_image_apply_progress = _normalize_bool(
        payload.get(
            "enableGuiImageApplyProgress",
            current_winpe.get("EnableGuiImageApplyProgress", "true"),
        )
    )
    image_apply_mode = str(
        payload.get("imageApplyMode", _read_image_apply_mode(paths))
    ).strip().lower()
    if image_apply_mode not in {"direct", "staged"}:
        raise ImageConfigError("Image apply mode must be direct or staged.")

    password = _validate_optional_password(
        payload.get("localAdminPassword"), "Local admin"
    )
    builtin_password = _validate_optional_password(
        payload.get("builtInAdministratorPassword"), "Built-in Administrator"
    )

    backups: list[str] = []
    if "imageApplyMode" in payload:
        backup = _save_image_apply_mode(paths, image_apply_mode)
        if backup:
            backups.append(backup)
    backup = _save_winpe_config(
        paths,
        enable_gui_image_apply_progress,
    )
    if backup:
        backups.append(backup)
    backup = _save_unattend(
        paths,
        time_zone,
        input_locale,
        system_locale,
        ui_language,
        user_locale,
        admin_name,
        password,
        builtin_password,
    )
    if backup:
        backups.append(backup)

    return {"saved": True, "backups": backups, "config": load_image_config(paths)}


def _validate_optional_password(raw: Any, label: str) -> str | None:
    """Return a validated password string, or None to keep the existing value."""

    if raw in (None, ""):
        return None
    password = str(raw)
    if "CHANGE_ME" in password:
        raise ImageConfigError(
            f"Choose a real {label} password, not the CHANGE_ME placeholder."
        )
    if len(password) < 8:
        raise ImageConfigError(f"{label} password must be at least 8 characters.")
    return password


def _validate_locale(raw: Any, label: str) -> str:
    value = str(raw).strip()
    allowed = UI_LANGUAGE_IDS if label == "UI language" else LOCALE_IDS
    if value not in allowed:
        raise ImageConfigError(f"{label} must be selected from the supported list.")
    return value


def _validate_input_locale(raw: Any) -> str:
    value = str(raw).strip()
    layouts = [item.strip() for item in value.split(";") if item.strip()]
    if not layouts:
        raise ImageConfigError("Select at least one keyboard layout.")
    if len(layouts) > 8:
        raise ImageConfigError("No more than 8 keyboard layouts can be selected.")
    if len(layouts) != len(set(layouts)):
        raise ImageConfigError("The same keyboard layout cannot be selected twice.")
    if any(item not in KEYBOARD_LAYOUT_IDS for item in layouts):
        raise ImageConfigError(
            "Every keyboard layout must be selected from the supported list."
        )
    return ";".join(layouts)
