"""Browser authentication, sessions, and per-page authorization for IronAPI."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Request
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    delete,
    select,
    update,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.config import IRONDEPLOY_ROOT, get_settings
from app.deployments import Base


SESSION_COOKIE = "irondeploy_session"
PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 600_000
SESSION_IDLE = timedelta(hours=8)
SESSION_ABSOLUTE = timedelta(days=7)
LOGIN_LOCK_THRESHOLD = 5
LOGIN_LOCK_DURATION = timedelta(minutes=15)
BOOTSTRAP_PATH = IRONDEPLOY_ROOT / "Data" / "auth-bootstrap.json"
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{3,64}$")
DEPLOY_PERMISSION = "deploy"
DEPLOYMENT_COMPLETION_RECEIPT_TTL = timedelta(minutes=5)

PERMISSIONS: dict[str, str] = {
    "dashboard": "Dashboard",
    "images": "Windows images",
    "programs": "Post-install software",
    "drivers": "Drivers",
    "image_config": "Image settings",
    DEPLOY_PERMISSION: "WinPE deployment only",
}


class AuthError(ValueError):
    """Safe authentication or account-management error."""


class AuthUser(Base):
    __tablename__ = "auth_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    username_normalized: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_superadmin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class AuthPermission(Base):
    __tablename__ = "auth_permissions"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), primary_key=True
    )
    permission: Mapped[str] = mapped_column(String(32), primary_key=True)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DeploymentToken(Base):
    """One login, one deployment, one bearer token."""

    __tablename__ = "deployment_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    deployment_id: Mapped[int | None] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"),
        nullable=True,
        unique=True,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    phase: Mapped[str] = mapped_column(
        String(16), nullable=False, default="authorized"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def normalize_username(value: str) -> str:
    username = value.strip()
    if not USERNAME_PATTERN.fullmatch(username):
        raise AuthError(
            "Username must be 3-64 letters, digits, dots, underscores, or hyphens."
        )
    return username.lower()


def validate_password(password: str) -> None:
    if len(password) < 12:
        raise AuthError("Password must contain at least 12 characters.")
    if len(password) > 512:
        raise AuthError("Password is too long.")


def hash_password(password: str, salt: bytes | None = None) -> str:
    validate_password(password)
    salt = salt or secrets.token_bytes(24)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return "$".join(
        (
            PASSWORD_ALGORITHM,
            str(PASSWORD_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != PASSWORD_ALGORITHM:
            return False
        iterations = int(iterations_text)
        if iterations < 100_000 or iterations > 2_000_000:
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iterations
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def is_deployment_operator(session: Session, user: AuthUser) -> bool:
    if user.is_superadmin or not user.is_active:
        return False
    permissions = set(
        session.scalars(
            select(AuthPermission.permission).where(AuthPermission.user_id == user.id)
        ).all()
    )
    return permissions == {DEPLOY_PERMISSION}


def create_deployment_token(session: Session, user: AuthUser) -> tuple[str, DeploymentToken]:
    if not is_deployment_operator(session, user):
        raise AuthError("This account is not allowed to deploy from WinPE.")
    token = secrets.token_urlsafe(32)
    now = _utc_now()
    record = DeploymentToken(
        user_id=user.id,
        token_hash=_token_hash(token),
        phase="authorized",
        created_at=now,
        last_seen_at=now,
        expires_at=now
        + timedelta(
            minutes=get_settings().deployment_authorization_timeout_minutes
        ),
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return token, record


def deployment_token_from_authorization(
    session: Session,
    authorization: str | None,
) -> DeploymentToken | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:]
    if not token:
        return None
    try:
        token_hash = _token_hash(token)
    except UnicodeEncodeError:
        return None
    record = session.scalar(
        select(DeploymentToken).where(DeploymentToken.token_hash == token_hash)
    )
    if record is None or record.revoked_at is not None:
        return None
    now = _utc_now()
    if record.deployment_id is not None:
        # Imported lazily to avoid the auth/Base import cycle at module load.
        from app.deployments import (
            DEPLOYMENT_BEGIN,
            Deployment,
            expire_stale_deployments,
        )

        expire_stale_deployments(session, now)
        session.refresh(record)
        if record.revoked_at is not None:
            return None
        deployment = session.get(Deployment, record.deployment_id)
        if (
            record.revoked_at is not None
            or deployment is None
            or deployment.status != DEPLOYMENT_BEGIN
        ):
            if record.revoked_at is None:
                record.revoked_at = now
                session.commit()
            return None
    if _as_utc(record.expires_at) <= now:
        record.revoked_at = now
        session.commit()
        return None
    user = session.get(AuthUser, record.user_id)
    if user is None or not is_deployment_operator(session, user):
        record.revoked_at = now
        session.commit()
        return None
    record.last_seen_at = now
    session.commit()
    return record


def deployment_completion_receipt_from_authorization(
    session: Session,
    authorization: str | None,
    deployment_id: int,
) -> DeploymentToken | None:
    """Validate a terminal token only for acknowledging its own completion."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:]
    if not token:
        return None
    try:
        token_hash = _token_hash(token)
    except UnicodeEncodeError:
        return None
    record = session.scalar(
        select(DeploymentToken).where(DeploymentToken.token_hash == token_hash)
    )
    now = _utc_now()
    if (
        record is None
        or record.deployment_id != deployment_id
        or record.phase != "completed"
        or record.revoked_at is None
        or _as_utc(record.expires_at) <= now
    ):
        return None

    # Imported lazily to avoid the auth/Base import cycle at module load.
    from app.deployments import DEPLOYMENT_COMPLETED, Deployment

    deployment = session.get(Deployment, deployment_id)
    if deployment is None or deployment.status != DEPLOYMENT_COMPLETED:
        return None
    return record


def bind_deployment_token(
    session: Session,
    record: DeploymentToken,
    deployment_id: int,
) -> None:
    from app.deployments import Deployment, as_utc

    deployment = session.get(Deployment, deployment_id)
    if deployment is None:
        raise AuthError("Deployment not found while binding its token.")
    now = _utc_now()
    expires_at = as_utc(deployment.started_at) + timedelta(
        minutes=get_settings().deployment_timeout_minutes
    )
    result = session.execute(
        update(DeploymentToken)
        .where(
            DeploymentToken.id == record.id,
            DeploymentToken.deployment_id.is_(None),
            DeploymentToken.phase == "authorized",
            DeploymentToken.revoked_at.is_(None),
        )
        .values(
            deployment_id=deployment_id,
            phase="winpe",
            last_seen_at=now,
            expires_at=expires_at,
        )
    )
    if result.rowcount != 1:
        session.rollback()
        raise AuthError("This WinPE login has already been used for a deployment.")
    session.commit()
    session.refresh(record)


def set_deployment_token_phase(
    session: Session,
    record: DeploymentToken,
    phase: str,
) -> None:
    if phase not in {"winpe", "postinstall"}:
        raise ValueError("Unsupported deployment token phase.")
    now = _utc_now()
    record.phase = phase
    record.last_seen_at = now
    session.commit()


def revoke_deployment_token(session: Session, record: DeploymentToken) -> None:
    if record.revoked_at is None:
        record.revoked_at = _utc_now()
        session.commit()


def revoke_user_deployment_tokens(session: Session, user_id: int) -> None:
    session.execute(
        update(DeploymentToken)
        .where(
            DeploymentToken.user_id == user_id,
            DeploymentToken.revoked_at.is_(None),
        )
        .values(revoked_at=_utc_now())
    )


def bootstrap_superadmin(session: Session, path: Path = BOOTSTRAP_PATH) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Failed to read auth bootstrap: {exc}") from exc

    username = str(payload.get("username", "")).strip()
    normalized = normalize_username(username)
    password_hash = str(payload.get("passwordHash", ""))
    if not password_hash.startswith(f"{PASSWORD_ALGORITHM}$"):
        raise RuntimeError("Auth bootstrap password hash is invalid.")

    superadmin = session.scalar(select(AuthUser).where(AuthUser.is_superadmin.is_(True)))
    changed = False
    if superadmin is None:
        conflict = session.scalar(
            select(AuthUser).where(AuthUser.username_normalized == normalized)
        )
        if conflict is not None:
            raise RuntimeError("Bootstrap superadmin username belongs to another user.")
        superadmin = AuthUser(
            username=username,
            username_normalized=normalized,
            password_hash=password_hash,
            is_active=True,
            is_superadmin=True,
        )
        session.add(superadmin)
        changed = True
    else:
        conflict = session.scalar(
            select(AuthUser).where(
                AuthUser.username_normalized == normalized,
                AuthUser.id != superadmin.id,
            )
        )
        if conflict is not None:
            raise RuntimeError("Bootstrap superadmin username belongs to another user.")
        if (
            superadmin.username != username
            or superadmin.username_normalized != normalized
            or superadmin.password_hash != password_hash
            or not superadmin.is_active
        ):
            password_changed = superadmin.password_hash != password_hash
            superadmin.username = username
            superadmin.username_normalized = normalized
            superadmin.password_hash = password_hash
            superadmin.is_active = True
            superadmin.updated_at = _utc_now()
            if password_changed:
                session.execute(
                    delete(AuthSession).where(AuthSession.user_id == superadmin.id)
                )
            changed = True
    if changed:
        session.commit()
    return True


def authenticate_user(session: Session, username: str, password: str) -> AuthUser:
    normalized = username.strip().lower()
    user = session.scalar(
        select(AuthUser).where(AuthUser.username_normalized == normalized)
    )
    now = _utc_now()
    if user is None or not user.is_active:
        # Keep timing closer to a real password check without revealing the account.
        hash_password("dummy-invalid-password")
        raise AuthError("Invalid username or password.")
    if user.locked_until and _as_utc(user.locked_until) > now:
        raise AuthError("Account is temporarily locked. Try again later.")
    if not verify_password(password, user.password_hash):
        user.failed_login_count += 1
        if user.failed_login_count >= LOGIN_LOCK_THRESHOLD:
            user.failed_login_count = 0
            user.locked_until = now + LOGIN_LOCK_DURATION
        session.commit()
        raise AuthError("Invalid username or password.")
    user.failed_login_count = 0
    user.locked_until = None
    session.commit()
    return user


def create_session(session: Session, user: AuthUser) -> str:
    token = secrets.token_urlsafe(32)
    now = _utc_now()
    session.add(
        AuthSession(
            user_id=user.id,
            token_hash=_token_hash(token),
            created_at=now,
            last_seen_at=now,
            expires_at=now + SESSION_ABSOLUTE,
        )
    )
    session.commit()
    return token


def destroy_session(session: Session, token: str | None) -> None:
    if token:
        session.execute(
            delete(AuthSession).where(AuthSession.token_hash == _token_hash(token))
        )
        session.commit()


def current_user(session: Session, request: Request) -> AuthUser | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    auth_session = session.scalar(
        select(AuthSession).where(AuthSession.token_hash == _token_hash(token))
    )
    if auth_session is None:
        return None
    now = _utc_now()
    if (
        _as_utc(auth_session.expires_at) <= now
        or _as_utc(auth_session.last_seen_at) + SESSION_IDLE <= now
    ):
        session.delete(auth_session)
        session.commit()
        return None
    user = session.get(AuthUser, auth_session.user_id)
    if user is None or not user.is_active:
        session.delete(auth_session)
        session.commit()
        return None
    auth_session.last_seen_at = now
    session.commit()
    return user


def user_permissions(session: Session, user: AuthUser) -> set[str]:
    if user.is_superadmin:
        return set(PERMISSIONS) - {DEPLOY_PERMISSION}
    return set(
        session.scalars(
            select(AuthPermission.permission).where(AuthPermission.user_id == user.id)
        ).all()
    )


def serialize_user(session: Session, user: AuthUser) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "active": user.is_active,
        "superadmin": user.is_superadmin,
        "permissions": sorted(user_permissions(session, user)),
        "createdAt": user.created_at.isoformat(),
    }


def create_user(session: Session, username: str, password: str) -> AuthUser:
    normalized = normalize_username(username)
    if normalized == "__winpe_policy__":
        raise AuthError("This username is reserved.")
    validate_password(password)
    if session.scalar(
        select(AuthUser).where(AuthUser.username_normalized == normalized)
    ):
        raise AuthError("A user with this username already exists.")
    user = AuthUser(
        username=username.strip(),
        username_normalized=normalized,
        password_hash=hash_password(password),
        is_active=True,
        is_superadmin=False,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def set_user_password(session: Session, user: AuthUser, password: str) -> None:
    if user.is_superadmin:
        raise AuthError("The superadmin password is managed through SetupWeb.")
    user.password_hash = hash_password(password)
    user.updated_at = _utc_now()
    session.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
    revoke_user_deployment_tokens(session, user.id)
    session.commit()


def set_user_active(session: Session, user: AuthUser, active: bool) -> None:
    if user.is_superadmin:
        raise AuthError("The bootstrap superadmin cannot be disabled.")
    user.is_active = active
    user.updated_at = _utc_now()
    if not active:
        session.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
        revoke_user_deployment_tokens(session, user.id)
    session.commit()


def set_user_permissions(
    session: Session, user: AuthUser, permissions: set[str]
) -> None:
    if user.is_superadmin:
        raise AuthError("Superadmin permissions cannot be changed.")
    unknown = permissions - set(PERMISSIONS)
    if unknown:
        raise AuthError(f"Unknown permissions: {', '.join(sorted(unknown))}.")
    if DEPLOY_PERMISSION in permissions and permissions != {DEPLOY_PERMISSION}:
        raise AuthError(
            "WinPE deployment access cannot be combined with other permissions."
        )
    session.execute(delete(AuthPermission).where(AuthPermission.user_id == user.id))
    session.add_all(
        AuthPermission(user_id=user.id, permission=permission)
        for permission in sorted(permissions)
    )
    session.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
    revoke_user_deployment_tokens(session, user.id)
    user.updated_at = _utc_now()
    session.commit()


def cookie_secure() -> bool:
    return os.getenv("IRONAPI_COOKIE_SECURE", "false").lower() in {
        "1", "true", "yes", "on"
    }
