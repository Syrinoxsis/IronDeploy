import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SETUPWEB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SETUPWEB_ROOT))

from app.config_store import IronDeployPaths
from app.smb_tools import (
    SmbToolError,
    configure_local_share,
    get_local_smb_info,
    test_local_share_access as run_smb_access_test,
    validate_qualified_account,
    validate_server_address,
    validate_share_name,
)


def make_paths(root: Path) -> IronDeployPaths:
    return IronDeployPaths(
        root=root,
        api_env=root / "Api" / ".env",
        api_env_example=root / "Api" / ".env.example",
        winpe_config=root / "WinPE" / "Runtime" / "deploy.config.ps1",
        winpe_config_example=(
            root / "WinPE" / "Runtime" / "deploy.config.example.ps1"
        ),
        unattend=root / "ServerTemplates" / "Unattend" / "unattend.xml",
        unattend_example=(
            root / "ServerTemplates" / "Unattend" / "unattend.example.xml"
        ),
        backup_dir=root / "Logs" / "ConfigBackups",
        validation_script=root / "Tools" / "Test-IronDeploy.ps1",
        auth_bootstrap=root / "Data" / "auth-bootstrap.json",
    )


class SmbValidationTests(unittest.TestCase):
    def test_share_name_is_restricted(self) -> None:
        self.assertEqual(validate_share_name("IronDeploy-01"), "IronDeploy-01")
        for invalid in ("", "name with spaces", "bad/share", "share$"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(SmbToolError):
                    validate_share_name(invalid)

    def test_account_must_use_authority_backslash_user(self) -> None:
        self.assertEqual(
            validate_qualified_account(r"SERVER\iron_ro"),
            r"SERVER\iron_ro",
        )
        self.assertEqual(
            validate_qualified_account(r"DOMAIN\iron_ro"),
            r"DOMAIN\iron_ro",
        )
        for invalid in ("iron_ro", "iron_ro@example.test", r"SERVER\\iron_ro"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(SmbToolError):
                    validate_qualified_account(invalid)

    def test_server_address_accepts_hostname_fqdn_or_ipv4(self) -> None:
        for valid in ("DEPLOY01", "deploy01.example.test", "192.168.1.10"):
            with self.subTest(valid=valid):
                self.assertEqual(validate_server_address(valid), valid)
        for invalid in (
            "",
            r"server\share",
            "https://server",
            "bad server",
            "999.1.1.1",
            "-server",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(SmbToolError):
                    validate_server_address(invalid)

    def test_local_info_uses_fixed_share_directory(self) -> None:
        paths = make_paths(Path("C:/IronDeploy"))
        with patch.dict(os.environ, {"COMPUTERNAME": "DEPLOY01"}):
            result = get_local_smb_info(paths)
        self.assertEqual(result["serverName"], "DEPLOY01")
        self.assertEqual(result["localPath"], str((paths.root / "Share").resolve()))


class SmbPowerShellRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path("C:/IronDeploy")
        self.paths = make_paths(self.root)

    @patch("app.smb_tools.Path.is_file", return_value=True)
    @patch("app.smb_tools.subprocess.run")
    def test_password_is_sent_through_stdin_not_arguments(
        self,
        run,
        _is_file,
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"ok": True, "status": "accessible"}),
            stderr="",
        )

        result = run_smb_access_test(
            self.paths,
            "192.168.1.10",
            "IronDeploy",
            r"SERVER\iron_ro",
            "new-secret",
        )

        self.assertTrue(result["ok"])
        command = run.call_args.args[0]
        self.assertNotIn("new-secret", command)
        payload = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(payload["serverAddress"], "192.168.1.10")
        self.assertEqual(payload["password"], "new-secret")
        self.assertTrue(run.call_args.kwargs["check"] is False)

    @patch("app.smb_tools.Path.is_file", return_value=True)
    @patch("app.smb_tools.get_saved_smb_password", return_value="saved-secret")
    @patch("app.smb_tools.subprocess.run")
    def test_saved_password_is_used_when_input_is_empty(
        self,
        run,
        _saved_password,
        _is_file,
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"ok": True, "status": "accessible"}),
            stderr="",
        )

        run_smb_access_test(
            self.paths,
            "deploy01.example.test",
            "IronDeploy",
            r"SERVER\iron_ro",
            "",
        )

        payload = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(payload["password"], "saved-secret")

    @patch("app.smb_tools.Path.is_file", return_value=True)
    @patch("app.smb_tools.subprocess.run")
    def test_share_conflict_is_reported_as_conflict(
        self,
        run,
        _is_file,
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=json.dumps(
                {
                    "ok": False,
                    "code": "name_conflict",
                    "message": "Share name is already in use.",
                }
            ),
            stderr="",
        )

        with self.assertRaises(SmbToolError) as raised:
            configure_local_share(
                self.paths,
                "deploy01.example.test",
                "IronDeploy",
                r"SERVER\iron_ro",
            )

        self.assertEqual(raised.exception.code, "name_conflict")
        self.assertEqual(raised.exception.status_code, 409)
        payload = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(payload["serverAddress"], "deploy01.example.test")


if __name__ == "__main__":
    unittest.main()
