import os

os.environ.setdefault("IRONAPI_DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("IRONAPI_NAME_PREFIX", "pc")
os.environ.setdefault("IRONAPI_NAME_WIDTH", "5")
os.environ.setdefault("IRONAPI_NAME_START", "1")
os.environ.setdefault("IRONAPI_ALLOWED_CLIENT_NETWORKS", "192.0.2.0/24")
os.environ.setdefault("IRONAPI_LDAP_SERVER", "dc01.example.test")
os.environ.setdefault("IRONAPI_LDAP_BASE_DN", "DC=example,DC=test")
os.environ.setdefault("IRONAPI_LDAP_CREDENTIAL_TARGET", "IronDeploy-LDAP")
os.environ.setdefault("IRONAPI_LDAP_USE_SSL", "false")
os.environ.setdefault("IRONAPI_LDAP_CONNECT_TIMEOUT", "5")
os.environ.setdefault("IRONAPI_ODJ_DOMAIN", "example.test")
os.environ.setdefault(
    "IRONAPI_ODJ_MACHINE_OU",
    "OU=Workstations,OU=Clients,DC=example,DC=test",
)
os.environ.setdefault("IRONAPI_ODJ_BLOB_DIR", "{IRONDEPLOY_ROOT}\\ODJ\\pending")
os.environ.setdefault("IRONAPI_ODJ_DJOIN_PATH", "C:\\Windows\\System32\\djoin.exe")
os.environ.setdefault("IRONAPI_ODJ_PROVISION_TIMEOUT", "60")
import unittest
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.deployments import Base, DEPLOYMENT_COMPLETED, Deployment
from app.auth import (
    DeploymentToken,
    create_deployment_token,
    create_user,
    set_user_permissions,
)
from app.main import (
    _browser_permission,
    authorize_browser_request,
    authorize_deployment_client,
    is_client_allowed,
)


def make_http_request(
    path: str,
    authorization: str | None = None,
    method: str = "GET",
) -> Request:
    headers = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode("ascii")))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8000),
        }
    )


class ClientAccessTests(unittest.TestCase):
    def test_loopback_is_always_allowed(self) -> None:
        self.assertTrue(is_client_allowed("127.0.0.1"))
        self.assertTrue(is_client_allowed("::1"))

    def test_configured_network_is_allowed(self) -> None:
        self.assertTrue(is_client_allowed("192.0.2.94"))

    def test_unconfigured_network_is_rejected(self) -> None:
        self.assertFalse(is_client_allowed("203.0.113.10"))

    def test_invalid_or_missing_address_is_rejected(self) -> None:
        self.assertFalse(is_client_allowed(None))
        self.assertFalse(is_client_allowed("not-an-ip"))

    def test_browser_routes_require_exact_permissions(self) -> None:
        self.assertEqual(_browser_permission("/"), "dashboard")
        self.assertEqual(_browser_permission("/dashboard/42"), "dashboard")
        self.assertEqual(_browser_permission("/api/deployments/42"), "dashboard")
        self.assertEqual(_browser_permission("/api/deployment-images"), "images")
        self.assertEqual(_browser_permission("/drivers"), "drivers")
        self.assertEqual(_browser_permission("/api/drivers"), "drivers")
        self.assertEqual(
            _browser_permission("/api/drivers/package-uploads"),
            "drivers",
        )
        self.assertEqual(_browser_permission("/api/admin/users"), "superadmin")

    def test_winpe_deployment_routes_do_not_require_browser_session(self) -> None:
        self.assertIsNone(_browser_permission("/api/deploy/begin"))
        self.assertIsNone(_browser_permission("/api/deploy/42/stages/image_apply/start"))
        self.assertIsNone(_browser_permission("/api/deploy/42/complete"))

    def test_deployment_middleware_requires_unique_token(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        with session_factory() as session:
            user = create_user(session, "winpe-user", "WinPEPassword123")
            set_user_permissions(session, user, {"deploy"})
            bearer, record = create_deployment_token(session, user)

        async def accepted(request):
            self.assertEqual(request.state.deployment_token_id, record.id)
            return JSONResponse({"accepted": True})

        with patch("app.main.SessionLocal", session_factory):
            missing = asyncio.run(
                authorize_deployment_client(
                    make_http_request("/api/deploy/begin"), accepted
                )
            )
            invalid = asyncio.run(
                authorize_deployment_client(
                    make_http_request("/api/deploy/begin", "Bearer wrong-token"),
                    accepted,
                )
            )
            accepted_response = asyncio.run(
                authorize_deployment_client(
                    make_http_request("/api/deploy/begin", f"Bearer {bearer}"),
                    accepted,
                )
            )
            login_response = asyncio.run(
                authorize_deployment_client(
                    make_http_request("/api/deploy/auth/login"),
                    lambda _request: asyncio.sleep(
                        0, result=JSONResponse({"accepted": True})
                    ),
                )
            )
            policy_response = asyncio.run(
                authorize_deployment_client(
                    make_http_request("/api/deploy/auth/policy"),
                    lambda _request: asyncio.sleep(
                        0, result=JSONResponse({"accepted": True})
                    ),
                )
            )
            authorize_response = asyncio.run(
                authorize_deployment_client(
                    make_http_request(
                        "/api/deploy/auth/authorize",
                        method="POST",
                    ),
                    lambda _request: asyncio.sleep(
                        0, result=JSONResponse({"accepted": True})
                    ),
                )
            )
            browser_response = asyncio.run(
                authorize_deployment_client(
                    make_http_request("/api/deployments"),
                    lambda _request: asyncio.sleep(
                        0, result=JSONResponse({"accepted": True})
                    ),
                )
            )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(missing.headers["www-authenticate"], "Bearer")
        self.assertEqual(accepted_response.status_code, 200)
        self.assertEqual(login_response.status_code, 200)
        self.assertEqual(policy_response.status_code, 200)
        self.assertEqual(authorize_response.status_code, 200)
        self.assertEqual(browser_response.status_code, 200)

    def test_completed_token_can_only_replay_its_own_complete_briefly(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        now = datetime.now(timezone.utc)
        with session_factory() as session:
            user = create_user(session, "winpe-user", "WinPEPassword123")
            set_user_permissions(session, user, {"deploy"})
            bearer, record = create_deployment_token(session, user)
            deployment = Deployment(
                computer_name="pc00042",
                serial_number="SERIAL-42",
                mac_address="AA:BB:CC:DD:EE:FF",
                ip_address="192.0.2.42",
                image_name="win11.wim",
                domain_join=False,
                status=DEPLOYMENT_COMPLETED,
                started_at=now - timedelta(minutes=5),
                completed_at=now,
            )
            other = Deployment(
                computer_name="pc00043",
                serial_number="SERIAL-43",
                mac_address="AA:BB:CC:DD:EE:00",
                ip_address="192.0.2.43",
                image_name="win11.wim",
                domain_join=False,
                status=DEPLOYMENT_COMPLETED,
                started_at=now - timedelta(minutes=5),
                completed_at=now,
            )
            session.add_all((deployment, other))
            session.flush()
            record.deployment_id = deployment.id
            record.phase = "completed"
            record.revoked_at = now
            record.expires_at = now + timedelta(minutes=5)
            session.commit()
            record_id = record.id
            deployment_id = deployment.id
            other_id = other.id

        async def receipt(request):
            self.assertEqual(request.state.deployment_token_id, record_id)
            self.assertTrue(request.state.deployment_completion_replay)
            return JSONResponse({"accepted": True})

        async def accepted(_request):
            return JSONResponse({"accepted": True})

        authorization = f"Bearer {bearer}"
        with patch("app.main.SessionLocal", session_factory):
            replay = asyncio.run(
                authorize_deployment_client(
                    make_http_request(
                        f"/api/deploy/{deployment_id}/complete",
                        authorization,
                        method="POST",
                    ),
                    receipt,
                )
            )
            catalog = asyncio.run(
                authorize_deployment_client(
                    make_http_request("/api/deploy/catalog", authorization),
                    accepted,
                )
            )
            other_complete = asyncio.run(
                authorize_deployment_client(
                    make_http_request(
                        f"/api/deploy/{other_id}/complete",
                        authorization,
                        method="POST",
                    ),
                    accepted,
                )
            )
            wrong_method = asyncio.run(
                authorize_deployment_client(
                    make_http_request(
                        f"/api/deploy/{deployment_id}/complete",
                        authorization,
                    ),
                    accepted,
                )
            )

        self.assertEqual(replay.status_code, 200)
        self.assertEqual(catalog.status_code, 401)
        self.assertEqual(other_complete.status_code, 401)
        self.assertEqual(wrong_method.status_code, 401)

        with session_factory() as session:
            expired_record = session.get(DeploymentToken, record_id)
            expired_record.expires_at = now - timedelta(seconds=1)
            session.commit()
        with patch("app.main.SessionLocal", session_factory):
            expired = asyncio.run(
                authorize_deployment_client(
                    make_http_request(
                        f"/api/deploy/{deployment_id}/complete",
                        authorization,
                        method="POST",
                    ),
                    accepted,
                )
            )
        self.assertEqual(expired.status_code, 401)

    def test_browser_middleware_does_not_replace_deployment_bearer_auth(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine, expire_on_commit=False)

        async def accepted(_request):
            return JSONResponse({"accepted": True})

        with patch("app.main.SessionLocal", session_factory):
            browser_response = asyncio.run(
                authorize_browser_request(
                    make_http_request("/api/deployment-images"), accepted
                )
            )
            winpe_response = asyncio.run(
                authorize_browser_request(
                    make_http_request("/api/deploy/begin"), accepted
                )
            )

        self.assertEqual(browser_response.status_code, 401)
        self.assertEqual(winpe_response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
