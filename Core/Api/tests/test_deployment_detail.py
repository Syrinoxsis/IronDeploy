import os

os.environ.setdefault("IRONAPI_DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("IRONAPI_NAME_PREFIX", "pc")
os.environ.setdefault("IRONAPI_NAME_WIDTH", "5")
os.environ.setdefault("IRONAPI_NAME_START", "1")
os.environ.setdefault("IRONAPI_ALLOWED_CLIENT_NETWORKS", "192.0.2.0/24")
os.environ.setdefault("IRONAPI_LDAP_SERVER", "dc01.example.test")
os.environ.setdefault("IRONAPI_LDAP_BASE_DN", "DC=example,DC=test")
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.deployments import Base, Deployment, DeploymentProgram, DeploymentStage
from app.main import (
    _browser_permission,
    deployment_dashboard_page,
    deployment_detail,
)


STATIC_ROOT = Path(__file__).resolve().parents[1] / "app" / "static"


class DeploymentDetailApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_detail_returns_deployment_stages_and_programs(self) -> None:
        started_at = datetime.now(timezone.utc) - timedelta(minutes=3)
        completed_at = datetime.now(timezone.utc)
        with Session(self.engine) as session:
            deployment = Deployment(
                computer_name="pc00042",
                serial_number="PF4ABC12",
                model="ThinkPad T14 Gen 2",
                mac_address="AA:BB:CC:DD:EE:FF",
                ip_address="192.0.2.42",
                image_name="win11.wim",
                domain_join=True,
                status="completed",
                started_at=started_at,
                completed_at=completed_at,
            )
            session.add(deployment)
            session.flush()
            session.add(
                DeploymentStage(
                    deployment_id=deployment.id,
                    stage="image_apply",
                    phase="winpe",
                    status="completed",
                    started_at=started_at,
                    completed_at=completed_at,
                )
            )
            session.add(
                DeploymentProgram(
                    deployment_id=deployment.id,
                    position=0,
                    name="Agent.msi",
                    status="installed",
                    exit_code=0,
                    duration_seconds=12,
                )
            )
            session.commit()

            result = deployment_detail(deployment.id, session)

            self.assertEqual(result.deployment_id, deployment.id)
            self.assertEqual(result.model, "ThinkPad T14 Gen 2")
            self.assertEqual(result.stages[0].stage, "image_apply")
            self.assertEqual(result.programs[0].name, "Agent.msi")

    def test_missing_deployment_returns_404(self) -> None:
        with Session(self.engine) as session:
            with self.assertRaises(HTTPException) as raised:
                deployment_detail(404, session)
        self.assertEqual(raised.exception.status_code, 404)


class DeploymentDetailPageTests(unittest.TestCase):
    def test_page_and_api_require_dashboard_permission(self) -> None:
        self.assertEqual(_browser_permission("/dashboard/42"), "dashboard")
        self.assertEqual(_browser_permission("/api/deployments/42"), "dashboard")

    def test_page_serves_the_detail_shell(self) -> None:
        response = deployment_dashboard_page(42)
        self.assertEqual(
            Path(response.path).name,
            "deployment-detail.html",
        )

    def test_dashboard_links_to_detail_and_renders_network_diagnostics(self) -> None:
        dashboard = (STATIC_ROOT / "dashboard.js").read_text(encoding="utf-8")
        detail_html = (STATIC_ROOT / "deployment-detail.html").read_text(
            encoding="utf-8"
        )
        detail_js = (STATIC_ROOT / "deployment-detail.js").read_text(
            encoding="utf-8"
        )
        detail_css = (STATIC_ROOT / "deployment-detail.css").read_text(
            encoding="utf-8"
        )

        self.assertIn("`/dashboard/${deploymentId}`", dashboard)
        self.assertIn('m9 18 6-6-6-6', dashboard)
        self.assertIn('id="stage-list"', detail_html)
        self.assertIn('id="program-list"', detail_html)
        self.assertIn('id="network-content"', detail_html)
        self.assertIn('id="network-stage-list"', detail_html)
        self.assertIn("Average inbound adapter traffic during WinPE", detail_html)
        self.assertIn("fetch(`/api/deployments/${deploymentId}`", detail_js)
        self.assertIn(
            "renderNetworkDiagnostics(deployment.network_diagnostics)",
            detail_js,
        )
        self.assertIn("report.adapters_differ", detail_js)
        self.assertIn('detailText("ICMP unavailable")', detail_js)
        self.assertIn(".detail-loading[hidden]", detail_css)
        self.assertIn("#network-content[hidden]", detail_css)


if __name__ == "__main__":
    unittest.main()
