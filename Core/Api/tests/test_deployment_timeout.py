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
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.database import initialize_database
from app.auth import (
    DeploymentToken,
    create_deployment_token,
    create_user,
    set_user_permissions,
)
from app.deployments import (
    Base,
    DEPLOYMENT_BEGIN,
    DEPLOYMENT_COMPLETED,
    DEPLOYMENT_FAILED,
    STAGE_FAILED,
    STAGE_RUNNING,
    Computer,
    Deployment,
    DeploymentBeginRequest,
    DeploymentCompleteRequest,
    DeploymentErrorRequest,
    DeploymentImageRequest,
    DeploymentManifestRequest,
    DeploymentProgram,
    DeploymentProgramReport,
    DeploymentStage,
    expire_stale_deployments,
)
from app.main import (
    deploy_begin,
    deploy_complete,
    deploy_error,
    deploy_image_selected,
    deploy_manifest,
    deployment_list,
)


DEFAULT_DEPLOYMENT_TIMEOUT = timedelta(minutes=90)


class DeploymentTimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.odj_blob_dir = Path(self.temporary_directory.name) / "pending"
        self.odj_blob_dir.mkdir()

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def odj_settings(self, deployment_timeout_minutes: int = 90) -> SimpleNamespace:
        return SimpleNamespace(
            deployment_timeout_minutes=deployment_timeout_minutes,
            odj_blob_dir=self.odj_blob_dir,
            odj_blob_max_age_minutes=120,
        )

    def write_blob(self, computer_name: str = "pc00042") -> Path:
        blob_path = self.odj_blob_dir / f"{computer_name}.txt"
        blob_path.write_bytes(b"odj-blob")
        return blob_path

    def deployment_request(
        self,
        session: Session,
        deployment_id: int | None = None,
        phase: str = "winpe",
    ) -> SimpleNamespace:
        user = create_user(session, "winpe-operator", "OperatorPassword123")
        set_user_permissions(session, user, {"deploy"})
        _, token = create_deployment_token(session, user)
        if deployment_id is not None:
            token.deployment_id = deployment_id
            token.phase = phase
            session.commit()
        return SimpleNamespace(
            client=SimpleNamespace(host="192.0.2.42"),
            state=SimpleNamespace(deployment_token_id=token.id),
        )

    @staticmethod
    def deployment(
        started_at: datetime,
        status: str = DEPLOYMENT_BEGIN,
    ) -> Deployment:
        return Deployment(
            computer_name="pc00042",
            serial_number="PF4ABC12",
            mac_address="AA:BB:CC:DD:EE:FF",
            ip_address="192.0.2.42",
            image_name="win11.wim",
            domain_join=False,
            status=status,
            started_at=started_at,
            completed_at=started_at if status == DEPLOYMENT_COMPLETED else None,
        )

    def test_stale_deployment_and_running_stage_fail_at_configured_timeout(self) -> None:
        now = datetime(2026, 7, 3, 10, 0, tzinfo=timezone.utc)
        started_at = now - DEFAULT_DEPLOYMENT_TIMEOUT - timedelta(seconds=1)

        with Session(self.engine) as session:
            deployment = self.deployment(started_at)
            session.add(deployment)
            session.flush()
            stage = DeploymentStage(
                deployment_id=deployment.id,
                stage="image_apply",
                phase="winpe",
                status=STAGE_RUNNING,
                started_at=started_at + timedelta(minutes=5),
            )
            session.add(stage)
            session.commit()

            self.assertEqual(expire_stale_deployments(session, now), 1)
            session.refresh(deployment)
            session.refresh(stage)

            expected_failed_at = started_at + DEFAULT_DEPLOYMENT_TIMEOUT
            self.assertEqual(deployment.status, DEPLOYMENT_FAILED)
            self.assertEqual(
                deployment.completed_at.replace(tzinfo=timezone.utc),
                expected_failed_at,
            )
            self.assertEqual(stage.status, STAGE_FAILED)
            self.assertEqual(
                stage.completed_at.replace(tzinfo=timezone.utc),
                expected_failed_at,
            )

    def test_exactly_at_timeout_is_failed(self) -> None:
        now = datetime(2026, 7, 3, 10, 0, tzinfo=timezone.utc)

        with Session(self.engine) as session:
            deployment = self.deployment(now - DEFAULT_DEPLOYMENT_TIMEOUT)
            session.add(deployment)
            session.commit()

            self.assertEqual(expire_stale_deployments(session, now), 1)
            session.refresh(deployment)
            self.assertEqual(deployment.status, DEPLOYMENT_FAILED)
            self.assertEqual(
                deployment.completed_at.replace(tzinfo=timezone.utc),
                now,
            )

    def test_custom_deployment_timeout_is_applied(self) -> None:
        now = datetime(2026, 7, 3, 10, 0, tzinfo=timezone.utc)
        started_at = now - timedelta(minutes=45)

        with Session(self.engine) as session:
            deployment = self.deployment(started_at)
            session.add(deployment)
            session.commit()

            with patch(
                "app.deployments.get_settings",
                return_value=SimpleNamespace(deployment_timeout_minutes=45),
            ):
                self.assertEqual(expire_stale_deployments(session, now), 1)

            session.refresh(deployment)
            self.assertEqual(deployment.status, DEPLOYMENT_FAILED)
            self.assertEqual(
                deployment.completed_at.replace(tzinfo=timezone.utc),
                now,
            )

    def test_late_completion_keeps_failed_terminal_status(self) -> None:
        started_at = datetime.now(timezone.utc) - DEFAULT_DEPLOYMENT_TIMEOUT - timedelta(
            seconds=1
        )

        with Session(self.engine) as session:
            deployment = self.deployment(started_at)
            session.add(deployment)
            session.commit()

            request = self.deployment_request(
                session, deployment.id, phase="postinstall"
            )
            with self.assertRaises(HTTPException) as raised:
                deploy_complete(deployment.id, request, session)

            self.assertEqual(raised.exception.status_code, 401)
            session.refresh(deployment)
            self.assertEqual(deployment.status, DEPLOYMENT_FAILED)
            self.assertEqual(
                deployment.completed_at.replace(tzinfo=timezone.utc),
                started_at + DEFAULT_DEPLOYMENT_TIMEOUT,
            )

    def test_completion_saves_and_lists_postinstall_program_results(self) -> None:
        with Session(self.engine) as session:
            deployment = self.deployment(datetime.now(timezone.utc))
            session.add(deployment)
            session.commit()

            report = DeploymentCompleteRequest(
                programs=[
                    DeploymentProgramReport(
                        name="browser.msi",
                        status="installed",
                        exit_code=0,
                        duration_seconds=41,
                    ),
                    DeploymentProgramReport(
                        name="agent.exe",
                        status="failed",
                        duration_seconds=0,
                        reason="hash_mismatch",
                        error_message="SHA-256 mismatch.",
                    ),
                ]
            )
            request = self.deployment_request(
                session, deployment.id, phase="postinstall"
            )
            response = deploy_complete(deployment.id, request, session, report)
            listed = deployment_list(500, session).items[0]

            self.assertEqual(response.status, DEPLOYMENT_COMPLETED)
            self.assertEqual([item.name for item in listed.programs], [
                "browser.msi",
                "agent.exe",
            ])
            self.assertEqual(listed.programs[1].status, "failed")
            self.assertEqual(listed.programs[1].reason, "hash_mismatch")
            self.assertEqual(listed.programs[1].duration_seconds, 0)

            programs = session.query(DeploymentProgram).all()
            self.assertEqual(len(programs), 2)

    def test_completion_replay_returns_receipt_without_replacing_report(self) -> None:
        with Session(self.engine) as session:
            deployment = self.deployment(datetime.now(timezone.utc))
            session.add(deployment)
            session.commit()
            request = self.deployment_request(
                session, deployment.id, phase="postinstall"
            )
            first_report = DeploymentCompleteRequest(
                programs=[
                    DeploymentProgramReport(
                        name="original.msi",
                        status="installed",
                        exit_code=0,
                        duration_seconds=5,
                    )
                ]
            )
            replacement_report = DeploymentCompleteRequest(
                programs=[
                    DeploymentProgramReport(
                        name="replacement.exe",
                        status="failed",
                        duration_seconds=1,
                        error_message="Must not replace the accepted report.",
                    )
                ]
            )

            first = deploy_complete(
                deployment.id,
                request,
                session,
                first_report,
            )
            request.state.deployment_completion_replay = True
            replay = deploy_complete(
                deployment.id,
                request,
                session,
                replacement_report,
            )

            self.assertEqual(first.status, DEPLOYMENT_COMPLETED)
            self.assertEqual(replay.status, DEPLOYMENT_COMPLETED)
            programs = session.query(DeploymentProgram).all()
            self.assertEqual([program.name for program in programs], ["original.msi"])
            token = session.get(
                DeploymentToken,
                request.state.deployment_token_id,
            )
            self.assertEqual(token.phase, "completed")
            self.assertIsNotNone(token.revoked_at)

    def test_hash_mismatch_reason_requires_failed_status(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires status failed"):
            DeploymentProgramReport(
                name="agent.exe",
                status="installed",
                duration_seconds=0,
                reason="hash_mismatch",
            )

    def test_list_reports_failed_summary_count(self) -> None:
        started_at = datetime.now(timezone.utc) - DEFAULT_DEPLOYMENT_TIMEOUT - timedelta(
            seconds=1
        )

        with Session(self.engine) as session:
            session.add(self.deployment(started_at))
            session.commit()

            response = deployment_list(500, session)

            self.assertEqual(response.total, 1)
            self.assertEqual(response.begin, 0)
            self.assertEqual(response.completed, 0)
            self.assertEqual(response.failed, 1)
            self.assertEqual(response.items[0].status, DEPLOYMENT_FAILED)

    def test_begin_without_image_creates_computer_inventory(self) -> None:
        with Session(self.engine) as session:
            request = self.deployment_request(session, phase="authorized")
            response = deploy_begin(
                DeploymentBeginRequest(
                    computer_name="pc00042",
                    serial_number="PF4ABC12",
                    mac_address="aa-bb-cc-dd-ee-ff",
                    domain_join=True,
                ),
                request,
                session,
            )

            deployment = session.get(Deployment, response.deployment_id)
            computer = session.query(Computer).one()

            self.assertIsNone(deployment.image_name)
            self.assertEqual(deployment.mac_address, "AA:BB:CC:DD:EE:FF")
            self.assertEqual(computer.serial_number, "PF4ABC12")
            self.assertEqual(computer.last_computer_name, "pc00042")
            self.assertEqual(computer.last_deployment_id, deployment.id)

    def test_image_endpoint_updates_deployment_and_computer_inventory(self) -> None:
        with Session(self.engine) as session:
            deployment = self.deployment(datetime.now(timezone.utc))
            deployment.image_name = None
            session.add(deployment)
            session.flush()
            session.add(
                Computer(
                    serial_number=deployment.serial_number,
                    mac_address=deployment.mac_address,
                    last_computer_name=deployment.computer_name,
                    last_ip_address=deployment.ip_address,
                    last_image_name=None,
                    last_domain_join=deployment.domain_join,
                    deployment_count=1,
                    last_deployment_id=deployment.id,
                )
            )
            session.commit()
            request = self.deployment_request(session, deployment.id)

            response = deploy_image_selected(
                deployment.id,
                DeploymentImageRequest(image_name="win11.wim"),
                request,
                session,
            )
            computer = session.query(Computer).one()

            self.assertEqual(response.status, DEPLOYMENT_BEGIN)
            self.assertEqual(
                session.get(Deployment, deployment.id).image_name,
                "win11.wim",
            )
            self.assertEqual(computer.last_image_name, "win11.wim")

    def test_manifest_returns_final_plan_and_updates_inventory(self) -> None:
        catalog = {
            "images": [
                {
                    "name": "Windows 11.esd",
                    "size": 123456,
                    "format": "ESD",
                    "ready": True,
                    "indexes": [{"index": 6, "name": "Windows 11 Pro"}],
                    "defaultIndex": 6,
                }
            ],
            "programs": [
                {
                    "name": "agent.msi",
                    "size": 9876,
                    "type": "MSI",
                    "arguments": "/qn /norestart",
                    "sha256": "a" * 64,
                }
            ],
            "drivers": [
                {
                    "vendor": "Lenovo",
                    "model": "ThinkPad T14",
                    "relativePath": "Lenovo\\ThinkPad T14",
                    "size": 4567,
                    "infCount": 12,
                }
            ],
        }
        image_config = {
            "localAdminName": "localadmin",
            "enableBuiltInAdministrator": True,
            "enableSetupLocalAdmin": False,
        }

        with Session(self.engine) as session:
            deployment = self.deployment(datetime.now(timezone.utc))
            deployment.image_name = None
            session.add(deployment)
            session.flush()
            session.add(
                Computer(
                    serial_number=deployment.serial_number,
                    mac_address=deployment.mac_address,
                    last_computer_name=deployment.computer_name,
                    last_ip_address=deployment.ip_address,
                    last_image_name=None,
                    last_domain_join=deployment.domain_join,
                    deployment_count=1,
                    last_deployment_id=deployment.id,
                )
            )
            session.commit()
            request = self.deployment_request(session, deployment.id)

            with patch("app.main._deployment_catalog", return_value=catalog), patch(
                "app.main.load_image_config", return_value=image_config
            ):
                result = deploy_manifest(
                    deployment.id,
                    DeploymentManifestRequest(
                        image_name="windows 11.ESD",
                        program_names=["AGENT.MSI"],
                        driver_package="lenovo\\THINKPAD T14",
                    ),
                    request,
                    session,
                )

            self.assertEqual(result["image"]["defaultIndex"], 6)
            self.assertEqual(result["programs"][0]["arguments"], "/qn /norestart")
            self.assertEqual(result["programs"][0]["sha256"], "a" * 64)
            self.assertEqual(
                result["driverPackage"]["relativePath"],
                "Lenovo\\ThinkPad T14",
            )
            self.assertFalse(result["postinstall"]["enableSetupLocalAdmin"])
            self.assertEqual(
                session.get(Deployment, deployment.id).image_name,
                "Windows 11.esd",
            )
            self.assertEqual(
                session.query(Computer).one().last_image_name,
                "Windows 11.esd",
            )

    def test_error_endpoint_records_deployment_and_stage_message(self) -> None:
        with Session(self.engine) as session:
            deployment = self.deployment(datetime.now(timezone.utc))
            session.add(deployment)
            session.flush()
            stage = DeploymentStage(
                deployment_id=deployment.id,
                stage="domain_join",
                phase="winpe",
                status=STAGE_RUNNING,
                started_at=datetime.now(timezone.utc),
            )
            session.add(stage)
            session.commit()
            request = self.deployment_request(session, deployment.id)

            response = deploy_error(
                deployment.id,
                DeploymentErrorRequest(
                    stage="domain_join",
                    message="ODJ unattend failed",
                ),
                request,
                session,
            )
            session.refresh(stage)

            self.assertEqual(response.status, DEPLOYMENT_FAILED)
            self.assertEqual(
                session.get(Deployment, deployment.id).last_error_message,
                "ODJ unattend failed",
            )
            self.assertEqual(stage.status, STAGE_FAILED)
            self.assertEqual(stage.error_message, "ODJ unattend failed")

    def test_error_endpoint_deletes_the_domain_join_blob(self) -> None:
        blob_path = self.write_blob()

        with Session(self.engine) as session:
            deployment = self.deployment(datetime.now(timezone.utc))
            deployment.domain_join = True
            session.add(deployment)
            session.commit()
            request = self.deployment_request(session, deployment.id)

            with patch(
                "app.deployments.get_settings",
                return_value=self.odj_settings(),
            ):
                response = deploy_error(
                    deployment.id,
                    DeploymentErrorRequest(message="DISM Apply-Image failed"),
                    request,
                    session,
                )

            self.assertEqual(response.status, DEPLOYMENT_FAILED)
            self.assertFalse(blob_path.exists())

    def test_timed_out_deployment_deletes_the_domain_join_blob(self) -> None:
        now = datetime(2026, 7, 3, 10, 0, tzinfo=timezone.utc)
        blob_path = self.write_blob()

        with Session(self.engine) as session:
            deployment = self.deployment(now - DEFAULT_DEPLOYMENT_TIMEOUT)
            deployment.domain_join = True
            session.add(deployment)
            session.commit()

            with patch(
                "app.deployments.get_settings",
                return_value=self.odj_settings(),
            ):
                self.assertEqual(expire_stale_deployments(session, now), 1)

            session.refresh(deployment)
            self.assertEqual(deployment.status, DEPLOYMENT_FAILED)
            self.assertFalse(blob_path.exists())

    def test_completion_deletes_an_unacknowledged_domain_join_blob(self) -> None:
        blob_path = self.write_blob()

        with Session(self.engine) as session:
            deployment = self.deployment(datetime.now(timezone.utc))
            deployment.domain_join = True
            session.add(deployment)
            session.commit()
            request = self.deployment_request(
                session, deployment.id, phase="postinstall"
            )

            with patch(
                "app.deployments.get_settings",
                return_value=self.odj_settings(),
            ):
                response = deploy_complete(deployment.id, request, session)

            self.assertEqual(response.status, DEPLOYMENT_COMPLETED)
            self.assertFalse(blob_path.exists())


class DeploymentSchemaUpgradeTests(unittest.TestCase):
    def test_legacy_sqlite_constraints_are_upgraded_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "legacy.db"
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        CREATE TABLE deployments (
                            id INTEGER NOT NULL PRIMARY KEY,
                            computer_name VARCHAR(63) NOT NULL,
                            mac_address VARCHAR(17) NOT NULL,
                            ip_address VARCHAR(45) NOT NULL,
                            image_name VARCHAR(255) NOT NULL,
                            domain_join BOOLEAN NOT NULL,
                            status VARCHAR(16) NOT NULL,
                            started_at DATETIME NOT NULL,
                            completed_at DATETIME,
                            CONSTRAINT ck_deployments_status
                                CHECK (status IN ('begin', 'completed')),
                            CONSTRAINT ck_deployments_completion CHECK (
                                (status = 'begin' AND completed_at IS NULL) OR
                                (status = 'completed' AND completed_at IS NOT NULL)
                            )
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO deployments (
                            id, computer_name, mac_address, ip_address,
                            image_name, domain_join, status, started_at
                        ) VALUES (
                            7, 'pc00007', 'AA:BB:CC:DD:EE:FF', '192.0.2.7',
                            'win11.wim', 0, 'begin', '2026-07-03 08:00:00'
                        )
                        """
                    )
                )

            Base.metadata.tables["deployment_stages"].create(engine)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO deployment_stages (
                            id, deployment_id, stage, phase, status, started_at
                        ) VALUES (
                            3, 7, 'image_apply', 'winpe', 'running',
                            '2026-07-03 08:05:00'
                        )
                        """
                    )
                )

            initialize_database(engine)
            initialize_database(engine)

            constraints = inspect(engine).get_check_constraints("deployments")
            constraint_sql = " ".join(
                constraint.get("sqltext") or "" for constraint in constraints
            )
            columns = {
                column["name"]: column
                for column in inspect(engine).get_columns("deployments")
            }
            self.assertIn("'failed'", constraint_sql)
            self.assertTrue(columns["image_name"]["nullable"])
            with engine.connect() as connection:
                row = connection.execute(
                    text(
                        "SELECT id, computer_name, serial_number "
                        "FROM deployments"
                    )
                ).one()
            self.assertEqual(row, (7, "pc00007", None))
            with engine.connect() as connection:
                stage_row = connection.execute(
                    text(
                        "SELECT id, deployment_id, status "
                        "FROM deployment_stages"
                    )
                ).one()
                foreign_key_violations = connection.execute(
                    text("PRAGMA foreign_key_check")
                ).all()
                computer_row = connection.execute(
                    text(
                        "SELECT last_computer_name, mac_address, "
                        "deployment_count, last_deployment_id "
                        "FROM computers"
                    )
                ).one()
            self.assertEqual(stage_row, (3, 7, STAGE_RUNNING))
            self.assertEqual(
                computer_row,
                ("pc00007", "AA:BB:CC:DD:EE:FF", 1, 7),
            )
            self.assertEqual(foreign_key_violations, [])
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
