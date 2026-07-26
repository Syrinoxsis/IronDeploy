"""Server-owned WinPE authorization policy and PIN verification."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, delete, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.auth import (
    DEPLOY_PERMISSION,
    AuthPermission,
    AuthUser,
    DeploymentToken,
    create_deployment_token,
    hash_password,
    verify_password,
)
from app.deployments import Base


WINPE_AUTH_ACCOUNT = "account"
WINPE_AUTH_PIN = "pin"
WINPE_AUTH_NONE = "none"
WINPE_AUTH_MODES = {WINPE_AUTH_ACCOUNT, WINPE_AUTH_PIN, WINPE_AUTH_NONE}
WINPE_SYSTEM_USERNAME = "__winpe_policy__"
PIN_MIN_LENGTH = 6
PIN_MAX_LENGTH = 10
PIN_LOCK_THRESHOLD = 5
PIN_LOCK_DURATION = timedelta(minutes=15)


class WinPEAuthError(ValueError):
    """Safe WinPE policy or PIN validation error."""


class WinPEAuthPolicy(Base):
    __tablename__ = "winpe_auth_policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default=WINPE_AUTH_ACCOUNT
    )
    pin_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class WinPEPinAttempt(Base):
    __tablename__ = "winpe_pin_attempts"

    client_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _pin_secret(pin: str) -> str:
    return f"irondeploy-winpe-pin:{pin}"


def _client_key(client_address: str | None) -> str:
    normalized = (client_address or "unknown").strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def validate_pin(pin: str) -> str:
    value = pin.strip()
    if (
        len(value) < PIN_MIN_LENGTH
        or len(value) > PIN_MAX_LENGTH
        or not value.isascii()
        or not value.isdigit()
    ):
        raise WinPEAuthError(
            f"PIN must contain {PIN_MIN_LENGTH}-{PIN_MAX_LENGTH} digits."
        )
    return value


def get_winpe_auth_policy(session: Session) -> WinPEAuthPolicy:
    policy = session.get(WinPEAuthPolicy, 1)
    if policy is None:
        policy = WinPEAuthPolicy(id=1, mode=WINPE_AUTH_ACCOUNT)
        session.add(policy)
        session.commit()
        session.refresh(policy)
    return policy


def serialize_winpe_auth_policy(policy: WinPEAuthPolicy) -> dict[str, Any]:
    return {
        "mode": policy.mode,
        "pinConfigured": bool(policy.pin_hash),
        "pinMinLength": PIN_MIN_LENGTH,
        "pinMaxLength": PIN_MAX_LENGTH,
        "updatedAt": policy.updated_at.isoformat(),
    }


def _ensure_system_user(session: Session) -> AuthUser:
    user = session.scalar(
        select(AuthUser).where(
            AuthUser.username_normalized == WINPE_SYSTEM_USERNAME
        )
    )
    now = _utc_now()
    if user is None:
        user = AuthUser(
            username=WINPE_SYSTEM_USERNAME,
            username_normalized=WINPE_SYSTEM_USERNAME,
            password_hash=hash_password(secrets.token_urlsafe(48)),
            is_active=True,
            is_superadmin=False,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        session.flush()
    elif not user.is_active:
        user.is_active = True
        user.updated_at = now

    permissions = set(
        session.scalars(
            select(AuthPermission.permission).where(
                AuthPermission.user_id == user.id
            )
        ).all()
    )
    if permissions != {DEPLOY_PERMISSION}:
        session.execute(
            delete(AuthPermission).where(AuthPermission.user_id == user.id)
        )
        session.add(AuthPermission(user_id=user.id, permission=DEPLOY_PERMISSION))
    session.commit()
    session.refresh(user)
    return user


def set_winpe_auth_policy(
    session: Session,
    mode: str,
    pin: str | None = None,
) -> WinPEAuthPolicy:
    normalized_mode = mode.strip().lower()
    if normalized_mode not in WINPE_AUTH_MODES:
        raise WinPEAuthError("Unsupported WinPE authorization mode.")

    policy = get_winpe_auth_policy(session)
    if pin is not None and pin.strip():
        normalized_pin = validate_pin(pin)
        policy.pin_hash = hash_password(_pin_secret(normalized_pin))
    if normalized_mode == WINPE_AUTH_PIN and not policy.pin_hash:
        raise WinPEAuthError("Set a PIN before enabling PIN authorization.")

    policy.mode = normalized_mode
    policy.updated_at = _utc_now()
    if normalized_mode != WINPE_AUTH_ACCOUNT:
        _ensure_system_user(session)
    session.commit()
    session.refresh(policy)
    return policy


def _system_deployment_token(session: Session) -> tuple[str, DeploymentToken]:
    return create_deployment_token(session, _ensure_system_user(session))


def authorize_without_credentials(
    session: Session,
) -> tuple[str, DeploymentToken]:
    policy = get_winpe_auth_policy(session)
    if policy.mode != WINPE_AUTH_NONE:
        raise WinPEAuthError("Credential-free authorization is disabled.")
    return _system_deployment_token(session)


def authorize_with_pin(
    session: Session,
    pin: str,
    client_address: str | None,
) -> tuple[str, DeploymentToken]:
    policy = get_winpe_auth_policy(session)
    if policy.mode != WINPE_AUTH_PIN:
        raise WinPEAuthError("PIN authorization is disabled.")

    now = _utc_now()
    key = _client_key(client_address)
    attempt = session.get(WinPEPinAttempt, key)
    if (
        attempt is not None
        and attempt.locked_until is not None
        and _as_utc(attempt.locked_until) > now
    ):
        raise WinPEAuthError("Too many incorrect PIN attempts. Try again later.")

    candidate = pin.strip()
    valid_format = (
        PIN_MIN_LENGTH <= len(candidate) <= PIN_MAX_LENGTH
        and candidate.isascii()
        and candidate.isdigit()
    )
    encoded = policy.pin_hash or hash_password(_pin_secret("000000"))
    valid = valid_format and policy.pin_hash is not None and verify_password(
        _pin_secret(candidate), encoded
    )
    if not valid:
        if attempt is None:
            attempt = WinPEPinAttempt(client_key=key, failed_count=0, updated_at=now)
            session.add(attempt)
        if attempt.locked_until is not None and _as_utc(attempt.locked_until) <= now:
            attempt.failed_count = 0
            attempt.locked_until = None
        attempt.failed_count += 1
        attempt.updated_at = now
        if attempt.failed_count >= PIN_LOCK_THRESHOLD:
            attempt.failed_count = 0
            attempt.locked_until = now + PIN_LOCK_DURATION
        session.commit()
        raise WinPEAuthError("Invalid PIN.")

    if attempt is not None:
        session.delete(attempt)
        session.commit()
    return _system_deployment_token(session)


def is_winpe_system_user(user: AuthUser) -> bool:
    return user.username_normalized == WINPE_SYSTEM_USERNAME
