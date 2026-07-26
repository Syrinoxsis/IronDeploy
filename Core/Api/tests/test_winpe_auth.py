import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.auth import AuthUser, deployment_token_from_authorization
from app.deployments import Base
from app.main import admin_user_list, deployment_authorize
from app.winpe_auth import (
    PIN_LOCK_THRESHOLD,
    WINPE_AUTH_ACCOUNT,
    WINPE_AUTH_NONE,
    WINPE_AUTH_PIN,
    WINPE_SYSTEM_USERNAME,
    WinPEAuthError,
    WinPEPinAttempt,
    authorize_with_pin,
    authorize_without_credentials,
    get_winpe_auth_policy,
    serialize_winpe_auth_policy,
    set_winpe_auth_policy,
)


class WinPEAuthPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_default_policy_keeps_existing_account_login(self) -> None:
        with Session(self.engine) as session:
            policy = get_winpe_auth_policy(session)
            self.assertEqual(policy.mode, WINPE_AUTH_ACCOUNT)
            self.assertFalse(serialize_winpe_auth_policy(policy)["pinConfigured"])

    def test_pin_is_hashed_and_issues_a_scoped_token(self) -> None:
        with Session(self.engine) as session:
            policy = set_winpe_auth_policy(session, WINPE_AUTH_PIN, "123456")
            self.assertNotIn("123456", policy.pin_hash)

            bearer, record = authorize_with_pin(session, "123456", "192.0.2.42")
            validated = deployment_token_from_authorization(
                session, f"Bearer {bearer}"
            )
            self.assertEqual(validated.id, record.id)
            system_user = session.get(AuthUser, record.user_id)
            self.assertEqual(system_user.username_normalized, WINPE_SYSTEM_USERNAME)

    def test_pin_attempts_are_limited_per_client(self) -> None:
        with Session(self.engine) as session:
            set_winpe_auth_policy(session, WINPE_AUTH_PIN, "123456")
            for _ in range(PIN_LOCK_THRESHOLD):
                with self.assertRaises(WinPEAuthError):
                    authorize_with_pin(session, "654321", "192.0.2.42")

            with self.assertRaisesRegex(WinPEAuthError, "Too many"):
                authorize_with_pin(session, "123456", "192.0.2.42")

            attempt = session.scalar(select(WinPEPinAttempt))
            attempt.locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)
            session.commit()
            bearer, _ = authorize_with_pin(session, "123456", "192.0.2.42")
            self.assertTrue(bearer)
            self.assertIsNone(session.scalar(select(WinPEPinAttempt)))

    def test_no_auth_still_issues_a_deployment_bearer_token(self) -> None:
        with Session(self.engine) as session:
            set_winpe_auth_policy(session, WINPE_AUTH_NONE)
            bearer, record = authorize_without_credentials(session)
            self.assertIsNotNone(
                deployment_token_from_authorization(session, f"Bearer {bearer}")
            )
            self.assertEqual(record.phase, "authorized")
            self.assertEqual(admin_user_list(session)["users"], [])

    def test_mode_must_match_authorization_method(self) -> None:
        with Session(self.engine) as session:
            with self.assertRaises(WinPEAuthError):
                authorize_without_credentials(session)
            with self.assertRaises(WinPEAuthError):
                authorize_with_pin(session, "123456", "192.0.2.42")

    def test_pin_mode_requires_a_configured_pin(self) -> None:
        with Session(self.engine) as session:
            with self.assertRaisesRegex(WinPEAuthError, "Set a PIN"):
                set_winpe_auth_policy(session, WINPE_AUTH_PIN)

    def test_public_pin_endpoint_returns_a_bearer_without_exposing_pin(self) -> None:
        session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        with session_factory() as session:
            set_winpe_auth_policy(session, WINPE_AUTH_PIN, "123456")

        class JsonRequest:
            client = SimpleNamespace(host="192.0.2.42")

            async def json(self):
                return {"mode": "pin", "pin": "123456"}

        with patch("app.main.SessionLocal", session_factory):
            response = asyncio.run(deployment_authorize(JsonRequest()))
        payload = json.loads(response.body)
        self.assertTrue(payload["access_token"])
        self.assertNotIn("123456", response.body.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
