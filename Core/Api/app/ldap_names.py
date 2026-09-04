import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import pythoncom
import win32com.client

from app.config import Settings


class DirectoryLookupError(RuntimeError):
    pass


ADS_SECURE_AUTHENTICATION = 0x1
ADS_USE_SSL = 0x2


@dataclass
class DirectoryNameSuggestion:
    last_domain_name: str | None
    suggested_name: str
    max_existing_number: int | None


def _compile_name_pattern(prefix: str, width: int) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(prefix)}(\d{{{width}}})\$?$", re.IGNORECASE)


def _extract_number(value: str, pattern: re.Pattern[str]) -> int | None:
    match = pattern.match(value.strip())
    if not match:
        return None

    return int(match.group(1))


def _next_name(prefix: str, width: int, number: int) -> str:
    return f"{prefix}{number:0{width}d}"


def suggest_computer_name(
    settings: Settings,
    prefix: str,
    width: int,
    start: int,
) -> DirectoryNameSuggestion:
    pattern = _compile_name_pattern(prefix, width)

    if not settings.ldap_enabled:
        raise DirectoryLookupError(
            "LDAP server and base DN are not configured"
        )

    try:
        max_number = _find_max_number_in_ldap(settings, pattern, prefix)
    except Exception as exc:
        raise DirectoryLookupError(f"LDAP lookup failed: {exc}") from exc

    next_number = start if max_number is None else max_number + 1
    suggested_name = "" if next_number >= 10**width else _next_name(
        prefix,
        width,
        next_number,
    )
    return DirectoryNameSuggestion(
        last_domain_name=(
            None
            if max_number is None
            else _next_name(prefix, width, max_number)
        ),
        suggested_name=suggested_name,
        max_existing_number=max_number,
    )


def computer_exists(settings: Settings, computer_name: str) -> bool:
    if not settings.ldap_enabled:
        raise DirectoryLookupError(
            "LDAP server and base DN are required for ODJ account detection"
        )

    try:
        return _computer_exists_in_ldap(settings, computer_name)
    except Exception as exc:
        raise DirectoryLookupError(
            f"LDAP computer lookup failed: {exc}"
        ) from exc


def _escape_filter_value(value: str) -> str:
    escaped: list[str] = []
    special = {
        "\x00": r"\00",
        "(": r"\28",
        ")": r"\29",
        "*": r"\2a",
        "\\": r"\5c",
    }
    for character in value:
        replacement = special.get(character)
        if replacement is not None:
            escaped.append(replacement)
            continue
        if ord(character) < 0x20 or ord(character) >= 0x7F:
            escaped.extend(f"\\{byte:02x}" for byte in character.encode("utf-8"))
            continue
        escaped.append(character)
    return "".join(escaped)


def _search_root(settings: Settings) -> str:
    server = settings.ldap_server
    if server is None or settings.ldap_base_dn is None:
        raise RuntimeError("LDAP server and base DN are required")
    if settings.ldap_use_ssl:
        server = f"{server}:636"
    return f"LDAP://{server}/{settings.ldap_base_dn}"


@contextmanager
def _adsi_connection(settings: Settings) -> Iterator[object]:
    """Open ADSI under the calling process/thread Windows identity."""
    pythoncom.CoInitialize()
    connection = None
    try:
        connection = win32com.client.Dispatch("ADODB.Connection")
        connection.Provider = "ADsDSOObject"
        flags = ADS_SECURE_AUTHENTICATION
        if settings.ldap_use_ssl:
            flags |= ADS_USE_SSL
        connection.Properties("ADSI Flag").Value = flags
        # User ID and Password are intentionally omitted. ADSI therefore uses
        # the Windows security context of the IronAPI process.
        connection.Open("Active Directory Provider")
        yield connection
    finally:
        if connection is not None:
            try:
                connection.Close()
            except Exception:
                pass
        pythoncom.CoUninitialize()


def _field_values(recordset: object, name: str) -> list[str]:
    value = recordset.Fields.Item(name).Value
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def _run_adsi_search(
    settings: Settings,
    search_filter: str,
    attributes: tuple[str, ...],
    *,
    size_limit: int = 0,
) -> list[dict[str, list[str]]]:
    command = None
    recordset = None
    with _adsi_connection(settings) as connection:
        try:
            command = win32com.client.Dispatch("ADODB.Command")
            command.ActiveConnection = connection
            command.Properties("Page Size").Value = 1000
            command.Properties("Timeout").Value = settings.ldap_connect_timeout
            if size_limit:
                command.Properties("Size Limit").Value = size_limit
            command.CommandText = (
                f"<{_search_root(settings)}>;"
                f"{search_filter};"
                f"{','.join(attributes)};"
                "subtree"
            )
            recordset, _ = command.Execute()

            rows: list[dict[str, list[str]]] = []
            while not recordset.EOF:
                rows.append({
                    name: _field_values(recordset, name)
                    for name in attributes
                })
                recordset.MoveNext()
            return rows
        finally:
            if recordset is not None:
                try:
                    recordset.Close()
                except Exception:
                    pass


def _computer_exists_in_ldap(
    settings: Settings,
    computer_name: str,
) -> bool:
    account_name = _escape_filter_value(f"{computer_name}$")
    rows = _run_adsi_search(
        settings,
        (
            "(&(objectCategory=computer)"
            f"(sAMAccountName={account_name}))"
        ),
        ("distinguishedName",),
        size_limit=1,
    )
    return bool(rows)


def _find_max_number_in_ldap(
    settings: Settings,
    pattern: re.Pattern[str],
    prefix: str,
) -> int | None:
    ldap_prefix = _escape_filter_value(prefix)
    rows = _run_adsi_search(
        settings,
        (
            "(&(objectCategory=computer)"
            f"(|(cn={ldap_prefix}*)(sAMAccountName={ldap_prefix}*)))"
        ),
        ("cn", "sAMAccountName"),
    )

    numbers: list[int] = []
    for row in rows:
        for attr_name in ("cn", "sAMAccountName"):
            for value in row.get(attr_name, []):
                number = _extract_number(value, pattern)
                if number is not None:
                    numbers.append(number)

    return max(numbers) if numbers else None
