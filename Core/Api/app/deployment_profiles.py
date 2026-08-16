"""Server-owned deployment profile settings.

Only the default profile is exposed today.  Keeping these settings in a real
profile row avoids another singleton configuration format before profile
selection is added to WinPE.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deployments import DeploymentProfile


DEFAULT_PROFILE_NAME = "Default"
DEFAULT_LOCAL_ADMIN_NAME = "localadmin"
_ADMIN_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,20}$")


class DeploymentProfileError(ValueError):
    """Raised when deployment profile settings are invalid."""


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "y", "on"}


def get_default_profile(session: Session) -> DeploymentProfile:
    profile = session.scalar(
        select(DeploymentProfile)
        .where(DeploymentProfile.is_default.is_(True))
        .order_by(DeploymentProfile.id)
    )
    if profile is not None:
        return profile

    profile = DeploymentProfile(
        name=DEFAULT_PROFILE_NAME,
        description="Default deployment settings",
        is_default=True,
        local_admin_name=DEFAULT_LOCAL_ADMIN_NAME,
        enable_builtin_administrator=True,
        enable_setup_local_admin=True,
    )
    session.add(profile)
    session.flush()
    return profile


def serialize_profile(profile: DeploymentProfile) -> dict[str, Any]:
    return {
        "profileId": profile.id,
        "profileName": profile.name,
        "localAdminName": profile.local_admin_name,
        "enableBuiltInAdministrator": profile.enable_builtin_administrator,
        "enableSetupLocalAdmin": profile.enable_setup_local_admin,
    }


def load_default_profile(session: Session) -> dict[str, Any]:
    return serialize_profile(get_default_profile(session))


def update_default_profile(
    session: Session,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise DeploymentProfileError("Invalid deployment profile payload.")

    profile = get_default_profile(session)
    admin_name = str(payload.get("localAdminName", profile.local_admin_name)).strip()
    if not _ADMIN_NAME_PATTERN.fullmatch(admin_name):
        raise DeploymentProfileError(
            "Local admin name must be 1-20 letters, digits, dot, underscore, or hyphen."
        )

    profile.local_admin_name = admin_name
    if "enableBuiltInAdministrator" in payload:
        profile.enable_builtin_administrator = _as_bool(
            payload["enableBuiltInAdministrator"]
        )
    if "enableSetupLocalAdmin" in payload:
        profile.enable_setup_local_admin = _as_bool(payload["enableSetupLocalAdmin"])
    profile.updated_at = datetime.now(timezone.utc)
    session.flush()
    return serialize_profile(profile)
