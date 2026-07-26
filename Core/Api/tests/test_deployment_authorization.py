import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.auth import (
    bind_deployment_token,
    create_deployment_token,
    create_user,
    set_deployment_token_phase,
    set_user_permissions,
)
from app.deployments import Base, Deployment, DeploymentBeginRequest
from app.main import (
    deploy_begin,
    deploy_enter_postinstall,
    deployment_smb_credentials,
    deployment_unattend,
)


class DeploymentAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    @staticmethod
    def deployment(name: str = "pc00042") -> Deployment:
        return Deployment(
            computer_name=name,
            serial_number=f"SERIAL-{name}",
            mac_address="AA:BB:CC:DD:EE:FF",
            ip_address="192.0.2.42",
            image_name=None,
            domain_join=False,
        )

    @staticmethod
    def request(token_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            client=SimpleNamespace(host="192.0.2.42"),
            state=SimpleNamespace(deployment_token_id=token_id),
        )

    def create_bound_token(
        self,
        session: Session,
        deployment: Deployment,
    ):
        user = create_user(session, "winpe-user", "WinPEPassword123")
        set_user_permissions(session, user, {"deploy"})
        _, token = create_deployment_token(session, user)
        bind_deployment_token(session, token, deployment.id)
        return token

    def test_one_login_can_begin_only_one_deployment(self) -> None:
        with Session(self.engine) as session:
            user = create_user(session, "winpe-user", "WinPEPassword123")
            set_user_permissions(session, user, {"deploy"})
            _, token = create_deployment_token(session, user)
            request = self.request(token.id)
            payload = DeploymentBeginRequest(
                computer_name="pc00042",
                serial_number="SERIAL-42",
                mac_address="AA:BB:CC:DD:EE:FF",
                domain_join=False,
            )

            first = deploy_begin(payload, request, session)
            self.assertGreater(first.deployment_id, 0)
            with self.assertRaises(HTTPException) as raised:
                deploy_begin(payload, request, session)
            self.assertEqual(raised.exception.status_code, 409)

    def test_smb_credentials_require_owner_and_winpe_phase(self) -> None:
        with Session(self.engine) as session:
            first = self.deployment("pc00042")
            second = self.deployment("pc00043")
            second.mac_address = "AA:BB:CC:DD:EE:00"
            session.add_all((first, second))
            session.commit()
            token = self.create_bound_token(session, first)
            request = self.request(token.id)
            settings = SimpleNamespace(
                smb_share_path=r"\\server\IronDeploy",
                smb_user=r"server\iron_ro",
                smb_password="server-side-password",
            )

            with patch("app.main.get_settings", return_value=settings):
                response = deployment_smb_credentials(first.id, request, session)
                payload = json.loads(response.body)
                self.assertEqual(payload["password"], "server-side-password")
                self.assertEqual(response.headers["cache-control"], "no-store")

                with self.assertRaises(HTTPException) as wrong_owner:
                    deployment_smb_credentials(second.id, request, session)
                self.assertEqual(wrong_owner.exception.status_code, 403)

                set_deployment_token_phase(session, token, "postinstall")
                with self.assertRaises(HTTPException) as wrong_phase:
                    deployment_smb_credentials(first.id, request, session)
                self.assertEqual(wrong_phase.exception.status_code, 409)

    def test_postinstall_transition_is_idempotent_without_restoring_winpe_access(
        self,
    ) -> None:
        with Session(self.engine) as session:
            deployment = self.deployment()
            session.add(deployment)
            session.commit()
            token = self.create_bound_token(session, deployment)
            request = self.request(token.id)

            first = deploy_enter_postinstall(deployment.id, request, session)
            second = deploy_enter_postinstall(deployment.id, request, session)

            self.assertEqual(first, {"status": "postinstall"})
            self.assertEqual(second, {"status": "postinstall"})
            session.refresh(token)
            self.assertEqual(token.phase, "postinstall")
            with self.assertRaises(HTTPException) as wrong_phase:
                deployment_smb_credentials(deployment.id, request, session)
            self.assertEqual(wrong_phase.exception.status_code, 409)

    def test_unattend_is_generated_only_for_owned_deployment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, Session(self.engine) as session:
            root = Path(temporary)
            template_dir = root / "Share" / "Unattend"
            template_dir.mkdir(parents=True)
            (template_dir / "unattend-win11-template.xml").write_text(
                "<unattend><ComputerName>COMPUTER_NAME</ComputerName>"
                "<Value>local-secret</Value></unattend>",
                encoding="utf-8",
            )
            deployment = self.deployment()
            session.add(deployment)
            session.commit()
            token = self.create_bound_token(session, deployment)

            with patch("app.main.IRONDEPLOY_ROOT", root):
                response = deployment_unattend(
                    deployment.id,
                    self.request(token.id),
                    session,
                )

            content = response.body.decode("utf-8")
            self.assertIn("<ComputerName>pc00042</ComputerName>", content)
            self.assertNotIn("COMPUTER_NAME", content)
            self.assertEqual(response.headers["cache-control"], "no-store")


if __name__ == "__main__":
    unittest.main()
