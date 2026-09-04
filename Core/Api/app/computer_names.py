"""Server-owned computer-name formats and suggestions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.deployments import ComputerNameFormat, Deployment, KnownComputerName
from app.ldap_names import DirectoryLookupError, suggest_computer_name


MAX_NAME_FORMATS = 20
MAX_WINDOWS_COMPUTER_NAME_LENGTH = 15
_PREFIX_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,13}$")


class ComputerNameFormatError(ValueError):
    """Raised when computer-name format configuration is invalid."""


@dataclass(frozen=True)
class FormatSuggestion:
    id: int | None
    prefix: str
    number_width: int
    start_number: int
    pattern: str
    domain_linked: bool
    last_name: str | None
    suggested_name: str | None
    source: str
    directory_available: bool | None
    domain_join_available: bool
    error: str | None = None
    history_last_name: str | None = None


@dataclass
class NameSuggestion:
    formats: list[FormatSuggestion]
    known_computer_names: list[KnownComputerName]


@dataclass(frozen=True)
class FormatValues:
    id: int | None
    prefix: str
    number_width: int
    start_number: int
    domain_linked: bool
    position: int


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "y", "on"}


def _normalize_format(raw: Any, position: int) -> FormatValues:
    if not isinstance(raw, dict):
        raise ComputerNameFormatError("Every computer-name format must be an object.")

    prefix = str(raw.get("prefix", "")).strip().lower()
    if _PREFIX_PATTERN.fullmatch(prefix) is None:
        raise ComputerNameFormatError(
            "A computer-name prefix must start with a letter and contain only "
            "letters, digits, or hyphens."
        )
    try:
        number_width = int(raw.get("numberWidth"))
        start_number = int(raw.get("startNumber", 1))
    except (TypeError, ValueError) as exc:
        raise ComputerNameFormatError(
            "Number width and starting number must be integers."
        ) from exc
    if number_width < 1 or number_width > 14:
        raise ComputerNameFormatError("Number width must be from 1 to 14.")
    if len(prefix) + number_width > MAX_WINDOWS_COMPUTER_NAME_LENGTH:
        raise ComputerNameFormatError(
            "A generated Windows computer name cannot exceed 15 characters."
        )
    if start_number < 0 or start_number >= 10**number_width:
        raise ComputerNameFormatError(
            "Starting number must fit inside the configured number width."
        )
    return FormatValues(
        id=None,
        prefix=prefix,
        number_width=number_width,
        start_number=start_number,
        domain_linked=_as_bool(raw.get("domainLinked", False)),
        position=position,
    )


def _formats_overlap(first: FormatValues, second: FormatValues) -> bool:
    first_mask = [*first.prefix, *([None] * first.number_width)]
    second_mask = [*second.prefix, *([None] * second.number_width)]
    if len(first_mask) != len(second_mask):
        return False
    for first_character, second_character in zip(first_mask, second_mask):
        if first_character is None and second_character is None:
            continue
        if first_character is None:
            if not str(second_character).isdigit():
                return False
            continue
        if second_character is None:
            if not str(first_character).isdigit():
                return False
            continue
        if first_character != second_character:
            return False
    return True


def validate_name_formats(raw_formats: Any) -> list[FormatValues]:
    if not isinstance(raw_formats, list):
        raise ComputerNameFormatError("computerNameFormats must be an array.")
    if not raw_formats:
        raise ComputerNameFormatError("Configure at least one computer-name format.")
    if len(raw_formats) > MAX_NAME_FORMATS:
        raise ComputerNameFormatError(
            f"No more than {MAX_NAME_FORMATS} computer-name formats are allowed."
        )

    formats = [
        _normalize_format(raw_format, position)
        for position, raw_format in enumerate(raw_formats)
    ]
    for index, first in enumerate(formats):
        for second in formats[index + 1 :]:
            if _formats_overlap(first, second):
                raise ComputerNameFormatError(
                    f"Computer-name formats {format_pattern(first)} and "
                    f"{format_pattern(second)} overlap."
                )
    return formats


def format_pattern(name_format: FormatValues | ComputerNameFormat) -> str:
    return f"{name_format.prefix}{'#' * name_format.number_width}"


def serialize_name_format(name_format: ComputerNameFormat) -> dict[str, Any]:
    return {
        "id": name_format.id,
        "prefix": name_format.prefix,
        "numberWidth": name_format.number_width,
        "startNumber": name_format.start_number,
        "domainLinked": name_format.domain_linked,
        "pattern": format_pattern(name_format),
    }


def get_name_formats(session: Session) -> list[ComputerNameFormat]:
    return list(
        session.scalars(
            select(ComputerNameFormat).order_by(
                ComputerNameFormat.position,
                ComputerNameFormat.id,
            )
        )
    )


def update_name_formats(session: Session, raw_formats: Any) -> list[ComputerNameFormat]:
    formats = validate_name_formats(raw_formats)
    session.execute(delete(ComputerNameFormat))
    session.flush()
    rows = [
        ComputerNameFormat(
            prefix=name_format.prefix,
            number_width=name_format.number_width,
            start_number=name_format.start_number,
            domain_linked=name_format.domain_linked,
            position=name_format.position,
        )
        for name_format in formats
    ]
    session.add_all(rows)
    session.flush()
    return rows


def _compiled_pattern(name_format: FormatValues | ComputerNameFormat) -> re.Pattern[str]:
    return re.compile(
        rf"^{re.escape(name_format.prefix)}\d{{{name_format.number_width}}}$",
        re.IGNORECASE,
    )


def match_name_format(
    name_formats: list[ComputerNameFormat],
    computer_name: str,
) -> ComputerNameFormat | None:
    candidate = computer_name.strip()
    for name_format in name_formats:
        if _compiled_pattern(name_format).fullmatch(candidate):
            return name_format
    return None


def _extract_number(
    value: str,
    name_format: FormatValues | ComputerNameFormat,
) -> int | None:
    match = _compiled_pattern(name_format).fullmatch(value.strip())
    if match is None:
        return None
    return int(value.strip()[len(name_format.prefix) :])


def _format_name(
    name_format: FormatValues | ComputerNameFormat,
    number: int,
) -> str | None:
    if number < 0 or number >= 10**name_format.number_width:
        return None
    return f"{name_format.prefix}{number:0{name_format.number_width}d}"


def last_deployed_name(
    session: Session,
    name_format: FormatValues | ComputerNameFormat,
) -> str | None:
    names = session.scalars(
        select(Deployment.computer_name).where(
            func.lower(Deployment.computer_name).like(
                f"{name_format.prefix.lower()}%"
            )
        )
    ).all()
    numbers = [
        number
        for value in names
        if (number := _extract_number(str(value), name_format)) is not None
    ]
    if not numbers:
        return None
    return _format_name(name_format, max(numbers))


def _local_suggestion(
    session: Session,
    name_format: FormatValues | ComputerNameFormat,
) -> tuple[str | None, str | None, str | None]:
    last_name = last_deployed_name(session, name_format)
    if last_name is None:
        next_number = name_format.start_number
    else:
        next_number = int(last_name[len(name_format.prefix) :]) + 1
    suggested_name = _format_name(name_format, next_number)
    error = None if suggested_name is not None else "The numeric range is exhausted."
    return last_name, suggested_name, error


def evaluate_name_format(
    session: Session,
    settings: Settings,
    name_format: FormatValues | ComputerNameFormat,
) -> FormatSuggestion:
    history_last_name = last_deployed_name(session, name_format)
    if not name_format.domain_linked:
        last_name, suggested_name, error = _local_suggestion(session, name_format)
        return FormatSuggestion(
            id=name_format.id,
            prefix=name_format.prefix,
            number_width=name_format.number_width,
            start_number=name_format.start_number,
            pattern=format_pattern(name_format),
            domain_linked=False,
            last_name=last_name,
            suggested_name=suggested_name,
            source="irondeploy",
            directory_available=None,
            domain_join_available=False,
            error=error,
        )

    try:
        directory = suggest_computer_name(
            settings,
            name_format.prefix,
            name_format.number_width,
            name_format.start_number,
        )
    except DirectoryLookupError as exc:
        return FormatSuggestion(
            id=name_format.id,
            prefix=name_format.prefix,
            number_width=name_format.number_width,
            start_number=name_format.start_number,
            pattern=format_pattern(name_format),
            domain_linked=True,
            last_name=None,
            suggested_name=None,
            source="active_directory",
            directory_available=False,
            domain_join_available=False,
            error=str(exc),
            history_last_name=history_last_name,
        )

    domain_error = None
    if not settings.odj_enabled:
        domain_error = "Offline Domain Join is not configured."
    return FormatSuggestion(
        id=name_format.id,
        prefix=name_format.prefix,
        number_width=name_format.number_width,
        start_number=name_format.start_number,
        pattern=format_pattern(name_format),
        domain_linked=True,
        last_name=directory.last_domain_name,
        suggested_name=directory.suggested_name or None,
        source="active_directory",
        directory_available=True,
        domain_join_available=settings.odj_enabled,
        error=domain_error,
        history_last_name=history_last_name,
    )


def build_name_suggestion(
    session: Session,
    settings: Settings,
    known_computer_names: list[KnownComputerName],
) -> NameSuggestion:
    return NameSuggestion(
        formats=[
            evaluate_name_format(session, settings, name_format)
            for name_format in get_name_formats(session)
        ],
        known_computer_names=known_computer_names,
    )


def check_unsaved_domain_format(
    session: Session,
    settings: Settings,
    raw_format: Any,
) -> FormatSuggestion:
    name_format = _normalize_format(raw_format, 0)
    if not name_format.domain_linked:
        raise ComputerNameFormatError(
            "Enable domain linkage before checking Active Directory."
        )
    return evaluate_name_format(session, settings, name_format)
