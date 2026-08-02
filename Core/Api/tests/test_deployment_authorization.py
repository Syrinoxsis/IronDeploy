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
from app.config import IRONDEPLOY_ROOT
from app.deployments import Base, Deployment, DeploymentBeginRequest
from app.main import (
    deploy_begin,
    deploy_enter_postinstall,
    deployment_postinstall_script,
    deployment_setup_complete,
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
                target_disk_number=1,
                target_disk_model="Samsung SSD 990 PRO 2TB",
                target_disk_size_bytes=2_000_398_934_016,
                domain_join=False,
            )

            first = deploy_begin(payload, request, session)
            self.assertGreater(first.deployment_id, 0)
            stored = session.get(Deployment, first.deployment_id)
            self.assertEqual(stored.target_disk_number, 1)
            self.assertEqual(stored.target_disk_model, "Samsung SSD 990 PRO 2TB")
            self.assertEqual(stored.target_disk_size_bytes, 2_000_398_934_016)
            with self.assertRaises(HTTPException) as raised:
                deploy_begin(payload, request, session)
            self.assertEqual(raised.exception.status_code, 409)

    def test_target_disk_snapshot_must_be_complete(self) -> None:
        with self.assertRaises(ValueError):
            DeploymentBeginRequest(
                computer_name="pc00042",
                serial_number="SERIAL-42",
                mac_address="AA:BB:CC:DD:EE:FF",
                target_disk_number=1,
                domain_join=False,
            )

    def test_domain_join_is_refused_when_offline_domain_join_is_unconfigured(
        self,
    ) -> None:
        # WinPE erases the selected disk after /begin succeeds, so an
        # impossible domain join has to be rejected here rather than
        # mid-deployment.
        with Session(self.engine) as session:
            user = create_user(session, "winpe-user", "WinPEPassword123")
            set_user_permissions(session, user, {"deploy"})
            _, token = create_deployment_token(session, user)
            request = self.request(token.id)
            payload = DeploymentBeginRequest(
                computer_name="pc00042",
                serial_number="SERIAL-42",
                mac_address="AA:BB:CC:DD:EE:FF",
                domain_join=True,
            )

            with patch(
                "app.main.get_settings",
                return_value=SimpleNamespace(odj_enabled=False),
            ):
                with self.assertRaises(HTTPException) as raised:
                    deploy_begin(payload, request, session)
            self.assertEqual(raised.exception.status_code, 409)

            # The same request succeeds once ODJ is configured.
            with patch(
                "app.main.get_settings",
                return_value=SimpleNamespace(odj_enabled=True),
            ):
                response = deploy_begin(payload, request, session)
            self.assertGreater(response.deployment_id, 0)

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
            template_dir = root / "Unattend"
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

            with patch("app.main.SERVER_TEMPLATES_ROOT", root):
                response = deployment_unattend(
                    deployment.id,
                    self.request(token.id),
                    session,
                )

            content = response.body.decode("utf-8")
            self.assertIn("<ComputerName>pc00042</ComputerName>", content)
            self.assertNotIn("COMPUTER_NAME", content)
            self.assertEqual(response.headers["cache-control"], "no-store")

    def test_postinstall_files_require_owner_and_winpe_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, Session(self.engine) as session:
            root = Path(temporary)
            template_dir = root / "PostInstall"
            template_dir.mkdir(parents=True)
            setup_complete = template_dir / "SetupComplete.cmd"
            postinstall = template_dir / "postinstall.ps1"
            setup_complete.write_text("@echo off", encoding="utf-8")
            postinstall.write_text("Write-Host ready", encoding="utf-8")

            first = self.deployment("pc00042")
            second = self.deployment("pc00043")
            second.mac_address = "AA:BB:CC:DD:EE:00"
            session.add_all((first, second))
            session.commit()
            token = self.create_bound_token(session, first)
            request = self.request(token.id)

            with patch("app.main.SERVER_TEMPLATES_ROOT", root):
                setup_response = deployment_setup_complete(
                    first.id,
                    request,
                    session,
                )
                script_response = deployment_postinstall_script(
                    first.id,
                    request,
                    session,
                )
                self.assertEqual(Path(setup_response.path), setup_complete)
                self.assertEqual(Path(script_response.path), postinstall)
                self.assertEqual(
                    setup_response.headers["cache-control"],
                    "no-store",
                )

                with self.assertRaises(HTTPException) as wrong_owner:
                    deployment_postinstall_script(
                        second.id,
                        request,
                        session,
                    )
                self.assertEqual(wrong_owner.exception.status_code, 403)

                set_deployment_token_phase(session, token, "postinstall")
                with self.assertRaises(HTTPException) as wrong_phase:
                    deployment_setup_complete(first.id, request, session)
                self.assertEqual(wrong_phase.exception.status_code, 409)

    def test_winpe_downloads_postinstall_files_from_the_api(self) -> None:
        engine = (
            IRONDEPLOY_ROOT
            / "WinPE"
            / "Runtime"
            / "IronDeploy.Engine.ps1"
        ).read_text(encoding="utf-8-sig")

        self.assertIn('"$PostInstallBaseUrl/setup-complete"', engine)
        self.assertIn('"$PostInstallBaseUrl/script"', engine)
        self.assertNotIn("Join-Path $PostInstallPath", engine)


if __name__ == "__main__":
    unittest.main()
