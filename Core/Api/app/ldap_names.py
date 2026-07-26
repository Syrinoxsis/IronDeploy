import re
from dataclasses import dataclass

from ldap3 import ALL, NTLM, Connection, Server
from ldap3.utils.conv import escape_filter_chars

from app.deployments import KnownComputerName
from app.config import Settings
from app.windows_credentials import read_generic_credential


class DirectoryLookupError(RuntimeError):
    pass


@dataclass
class NameSuggestion:
    last_domain_name: str | None
    suggested_name: str
    max_existing_number: int | None
    source: str
    ldap_enabled: bool
    ldap_error: str | None = None
    known_computer_names: list[KnownComputerName] | None = None


def _compile_name_pattern(prefix: str, width: int) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(prefix)}(\d{{{width}}})\$?$", re.IGNORECASE)


def _extract_number(value: str, pattern: re.Pattern[str]) -> int | None:
    match = pattern.match(value.strip())
    if not match:
        return None

    return int(match.group(1))


def _next_name(prefix: str, width: int, number: int) -> str:
    return f"{prefix}{number:0{width}d}"


def suggest_computer_name(settings: Settings) -> NameSuggestion:
    pattern = _compile_name_pattern(settings.name_prefix, settings.name_width)

    if not settings.ldap_enabled:
        raise DirectoryLookupError(
            "LDAP server and base DN are not configured"
        )

    try:
        max_number = _find_max_number_in_ldap(settings, pattern)
    except Exception as exc:
        raise DirectoryLookupError(f"LDAP lookup failed: {exc}") from exc

    next_number = settings.name_start if max_number is None else max_number + 1
    return NameSuggestion(
        last_domain_name=(
            None
            if max_number is None
            else _next_name(settings.name_prefix, settings.name_width, max_number)
        ),
        suggested_name=_next_name(settings.name_prefix, settings.name_width, next_number),
        max_existing_number=max_number,
        source="ldap",
        ldap_enabled=True,
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


def _create_connection(settings: Settings) -> Connection:
    server = Server(
        settings.ldap_server,
        get_info=ALL,
        use_ssl=settings.ldap_use_ssl,
        connect_timeout=settings.ldap_connect_timeout,
    )

    credential = read_generic_credential(settings.ldap_credential_target)
    return Connection(
        server=server,
        auto_bind=True,
        authentication=NTLM,
        receive_timeout=settings.ldap_connect_timeout,
        user=credential.username,
        password=credential.password,
    )


def _computer_exists_in_ldap(
    settings: Settings,
    computer_name: str,
) -> bool:
    account_name = escape_filter_chars(f"{computer_name}$")
    with _create_connection(settings) as conn:
        search_succeeded = conn.search(
            search_base=settings.ldap_base_dn,
            search_filter=(
                "(&(objectCategory=computer)"
                f"(sAMAccountName={account_name}))"
            ),
            attributes=["distinguishedName"],
            size_limit=1,
        )
        if not search_succeeded:
            message = conn.result.get("message") or conn.result.get("description")
            raise RuntimeError(f"LDAP search failed: {message or 'unknown error'}")
        return bool(conn.entries)


def _find_max_number_in_ldap(
    settings: Settings,
    pattern: re.Pattern[str],
) -> int | None:
    with _create_connection(settings) as conn:
        ldap_prefix = settings.name_prefix
        search_succeeded = conn.search(
            search_base=settings.ldap_base_dn,
            search_filter=(
                "(&(objectCategory=computer)"
                f"(|(cn={ldap_prefix}*)(sAMAccountName={ldap_prefix}*)))"
            ),
            attributes=["cn", "sAMAccountName"],
        )
        if not search_succeeded:
            message = conn.result.get("message") or conn.result.get("description")
            raise RuntimeError(f"LDAP search failed: {message or 'unknown error'}")

        numbers: list[int] = []
        for entry in conn.entries:
            for attr_name in ("cn", "sAMAccountName"):
                attr = getattr(entry, attr_name, None)
                if not attr:
                    continue

                values = attr.values if hasattr(attr, "values") else [str(attr)]
                for value in values:
                    number = _extract_number(str(value), pattern)
                    if number is not None:
                        numbers.append(number)

        return max(numbers) if numbers else None
