import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.deployments import (
    Base,
    Deployment,
    DeploymentPowerShellResult,
    DeploymentProfileScript,
)
from app.main import deploy_post_powershell_report
from app.post_powershell import (
    PostPowerShellError,
    list_scripts,
    resolve_profile_scripts,
    save_uploaded_script,
    update_script_settings,
)


async def chunks(*values: bytes):
    for value in values:
        yield value


class ReportRequest:
    def __init__(self, body: bytes, **headers: str) -> None:
        self._body = body
        self.headers = headers

    async def stream(self):
        yield self._body


class PostPowerShellTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.directory_patch = patch(
            "app.post_powershell.POST_POWERSHELL_DIR", self.directory
        )
        self.directory_patch.start()

    def tearDown(self) -> None:
        self.directory_patch.stop()
        self.session.close()
        self.engine.dispose()
        self.temporary.cleanup()

    def upload(self, name: str = "configure.ps1") -> dict:
        content = b"Write-Output 'ready'\r\n"
        result = asyncio.run(
            save_uploaded_script(
                self.session,
                name,
                chunks(content),
                arguments="-Mode Silent",
                selection_mode="operator",
                run_phase="before_software",
                timeout_seconds=45,
            )
        )
        self.session.commit()
        self.assertEqual(result["sha256"], hashlib.sha256(content).hexdigest())
        return result

    def test_upload_binds_script_to_default_profile(self) -> None:
        uploaded = self.upload()
        listing = list_scripts(self.session)
        self.assertEqual(listing["profileName"], "Default")
        self.assertEqual(len(listing["scripts"]), 1)
        script = listing["scripts"][0]
        self.assertEqual(script["id"], uploaded["id"])
        self.assertEqual(script["selectionMode"], "operator")
        self.assertEqual(script["runPhase"], "before_software")
        self.assertEqual(script["timeoutSeconds"], 45)
        self.assertTrue(script["available"])

    def test_profile_resolver_merges_automatic_and_operator_selection(self) -> None:
        first = self.upload("automatic.ps1")
        second = self.upload("optional.ps1")
        update_script_settings(
            self.session,
            first["id"],
            {
                "selectionMode": "automatic",
                "runPhase": "before_software",
                "timeoutSeconds": 30,
                "arguments": "",
            },
        )
        self.session.commit()
        plan = resolve_profile_scripts(self.session, [second["name"]])
        self.assertEqual([item["name"] for item in plan], [first["name"], second["name"]])

    def test_invalid_settings_are_rejected(self) -> None:
        script = self.upload()
        for timeout_seconds in (0, 10801):
            with self.subTest(timeout_seconds=timeout_seconds):
                with self.assertRaises(PostPowerShellError):
                    update_script_settings(
                        self.session,
                        script["id"],
                        {
                            "selectionMode": "operator",
                            "runPhase": "after_software",
                            "timeoutSeconds": timeout_seconds,
                            "arguments": "",
                        },
                    )

    def test_missing_selected_file_is_retained_for_nonfatal_reporting(self) -> None:
        script = self.upload()
        (self.directory / script["name"]).unlink()
        plan = resolve_profile_scripts(self.session, [script["name"]])
        self.assertEqual(len(plan), 1)
        self.assertFalse(plan[0]["available"])

    def test_profile_positions_are_unique_and_ordered(self) -> None:
        first = self.upload("first.ps1")
        second = self.upload("second.ps1")
        rows = self.session.query(DeploymentProfileScript).order_by(
            DeploymentProfileScript.position
        ).all()
        self.assertEqual([row.script_id for row in rows], [first["id"], second["id"]])
        self.assertEqual([row.position for row in rows], [0, 1])

    def test_report_stores_raw_output_and_updates_snapshot(self) -> None:
        deployment = Deployment(
            computer_name="pc00001",
            mac_address="00:11:22:33:44:55",
            ip_address="192.0.2.10",
            domain_join=False,
            status="begin",
        )
        self.session.add(deployment)
        self.session.flush()
        result = DeploymentPowerShellResult(
            deployment_id=deployment.id,
            position=0,
            name="configure.ps1",
            selection_mode="operator",
            run_phase="after_software",
            arguments="",
            timeout_seconds=60,
            size_bytes=10,
            sha256="a" * 64,
            status="pending",
            output_bytes=0,
            output_total_bytes=0,
            output_truncated=False,
        )
        self.session.add(result)
        self.session.commit()
        output_path = self.directory / "result.log"
        request = ReportRequest(
            "first\nsecond\n".encode(),
            **{
                "x-irondeploy-status": "succeeded",
                "x-irondeploy-duration-seconds": "3",
                "x-irondeploy-output-total-bytes": "13",
                "x-irondeploy-output-truncated": "false",
                "x-irondeploy-exit-code": "0",
            },
        )
        with (
            patch("app.main.require_owned_deployment", return_value=(deployment, None)),
            patch("app.main.result_log_path", return_value=output_path),
        ):
            response = asyncio.run(
                deploy_post_powershell_report(
                    deployment.id,
                    0,
                    request,
                    self.session,
                )
            )
        self.assertTrue(response["accepted"])
        self.assertEqual(output_path.read_bytes(), b"first\nsecond\n")
        self.session.refresh(result)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.output_bytes, 13)
        self.assertIsNotNone(result.reported_at)

    def test_postinstall_runner_inherits_utf8_console_without_visible_window(
        self,
    ) -> None:
        postinstall = (
            Path(__file__).resolve().parents[2]
            / "ServerTemplates"
            / "PostInstall"
            / "postinstall.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("$StartInfo.CreateNoWindow = $false", postinstall)
        self.assertIn(
            "$StartInfo.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden",
            postinstall,
        )
        self.assertIn(
            "$StartInfo.StandardOutputEncoding = $Utf8OutputEncoding",
            postinstall,
        )
        self.assertIn(
            "$StartInfo.StandardErrorEncoding = $Utf8OutputEncoding",
            postinstall,
        )


if __name__ == "__main__":
    unittest.main()
