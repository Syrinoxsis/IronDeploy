import json
import shutil
import tempfile
import unittest
import sys
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.auth import (
    AuthError,
    AuthSession,
    AuthUser,
    authenticate_user,
    bind_deployment_token,
    bootstrap_superadmin,
    create_deployment_token,
    create_session,
    create_user,
    current_user,
    deployment_token_from_authorization,
    hash_password,
    is_deployment_operator,
    set_user_active,
    set_deployment_token_phase,
    set_user_password,
    set_user_permissions,
    user_permissions,
    verify_password,
)
from app.deployments import Base, Deployment
from app.config import IRONDEPLOY_ROOT

sys.path.insert(0, str(IRONDEPLOY_ROOT))
from SetupWeb.app.config_store import hash_auth_password


class AuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine, expire_on_commit=False)
        self.addCleanup(self.session.close)

    def bootstrap(self, username="root-admin", password="LongPassword123") -> AuthUser:
        path = self.root / "auth-bootstrap.json"
        path.write_text(
            json.dumps(
                {"username": username, "passwordHash": hash_password(password)}
            ),
            encoding="utf-8",
        )
        self.assertTrue(bootstrap_superadmin(self.session, path))
        return self.session.scalar(select(AuthUser).where(AuthUser.is_superadmin.is_(True)))

    def test_password_hash_is_salted_and_verifiable(self) -> None:
        first = hash_password("Очень-Длинный-Пароль-123")
        second = hash_password("Очень-Длинный-Пароль-123")

        self.assertNotEqual(first, second)
        self.assertTrue(verify_password("Очень-Длинный-Пароль-123", first))
        self.assertFalse(verify_password("wrong-password", first))

    def test_setupweb_password_hash_is_accepted_by_api(self) -> None:
        encoded = hash_auth_password("SetupWebPassword123")

        self.assertTrue(verify_password("SetupWebPassword123", encoded))

    def test_bootstrap_creates_only_superadmin_and_authenticates(self) -> None:
        user = self.bootstrap()

        self.assertTrue(user.is_superadmin)
        self.assertTrue(user.is_active)
        self.assertEqual(
            authenticate_user(self.session, "ROOT-ADMIN", "LongPassword123").id,
            user.id,
        )

    def test_regular_user_permissions_are_exact(self) -> None:
        self.bootstrap()
        user = create_user(self.session, "operator", "OperatorPassword123")

        set_user_permissions(self.session, user, {"dashboard", "images"})

        self.assertEqual(user_permissions(self.session, user), {"dashboard", "images"})
        with self.assertRaises(AuthError):
            set_user_permissions(self.session, user, {"dashboard", "unknown"})

    def test_deployment_permission_is_backend_enforced_and_exclusive(self) -> None:
        user = create_user(self.session, "winpe-user", "WinPEPassword123")

        with self.assertRaisesRegex(AuthError, "cannot be combined"):
            set_user_permissions(self.session, user, {"deploy", "dashboard"})

        set_user_permissions(self.session, user, {"deploy"})
        self.assertTrue(is_deployment_operator(self.session, user))
        self.assertEqual(user_permissions(self.session, user), {"deploy"})

    def test_one_deployment_token_binds_once_and_is_revoked_on_role_change(self) -> None:
        user = create_user(self.session, "winpe-user", "WinPEPassword123")
        set_user_permissions(self.session, user, {"deploy"})
        bearer, token = create_deployment_token(self.session, user)
        self.assertEqual(token.expires_at - token.created_at, timedelta(minutes=10))
        deployment = Deployment(
            computer_name="pc00042",
            serial_number="PF4ABC12",
            mac_address="AA:BB:CC:DD:EE:FF",
            ip_address="192.0.2.42",
            image_name=None,
            domain_join=False,
        )
        self.session.add(deployment)
        self.session.flush()

        bind_deployment_token(self.session, token, deployment.id)
        self.assertEqual(token.deployment_id, deployment.id)
        self.assertEqual(token.phase, "winpe")
        expected_deadline = deployment.started_at + timedelta(minutes=90)
        self.assertEqual(
            token.expires_at.replace(tzinfo=timezone.utc),
            expected_deadline,
        )
        set_deployment_token_phase(self.session, token, "postinstall")
        self.assertEqual(
            token.expires_at.replace(tzinfo=timezone.utc),
            expected_deadline,
        )
        set_deployment_token_phase(self.session, token, "winpe")
        with self.assertRaisesRegex(AuthError, "already been used"):
            bind_deployment_token(self.session, token, deployment.id)
        self.assertEqual(
            deployment_token_from_authorization(
                self.session, f"Bearer {bearer}"
            ).id,
            token.id,
        )

        set_user_permissions(self.session, user, {"dashboard"})
        self.assertIsNone(
            deployment_token_from_authorization(
                self.session, f"Bearer {bearer}"
            )
        )

    def test_password_change_and_blocking_revoke_sessions(self) -> None:
        self.bootstrap()
        user = create_user(self.session, "operator", "OperatorPassword123")
        token = create_session(self.session, user)
        request = SimpleNamespace(cookies={"irondeploy_session": token})
        self.assertEqual(current_user(self.session, request).id, user.id)

        set_user_password(self.session, user, "ReplacementPassword123")
        self.assertIsNone(current_user(self.session, request))

        token = create_session(self.session, user)
        request.cookies["irondeploy_session"] = token
        set_user_active(self.session, user, False)
        self.assertIsNone(current_user(self.session, request))
        self.assertEqual(
            self.session.scalars(
                select(AuthSession).where(AuthSession.user_id == user.id)
            ).all(),
            [],
        )

    def test_superadmin_cannot_be_disabled_or_changed_from_web(self) -> None:
        user = self.bootstrap()

        with self.assertRaises(AuthError):
            set_user_active(self.session, user, False)
        with self.assertRaises(AuthError):
            set_user_password(self.session, user, "AnotherPassword123")
        with self.assertRaises(AuthError):
            set_user_permissions(self.session, user, {"dashboard"})


if __name__ == "__main__":
    unittest.main()
