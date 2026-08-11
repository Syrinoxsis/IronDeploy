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
from app.odj_tools import (
    OdjToolError,
    get_local_odj_info,
    secure_odj_acl,
    validate_windows_account,
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


class OdjAccountValidationTests(unittest.TestCase):
    def test_process_account_must_be_qualified(self) -> None:
        for valid in (
            r"DOMAIN\irondeploy_api",
            r"SERVER\svc_irondeploy$",
            r"NT AUTHORITY\SYSTEM",
        ):
            with self.subTest(valid=valid):
                self.assertEqual(validate_windows_account(valid), valid)

        for invalid in ("", "irondeploy_api", "user@example.test", "DOMAIN/user"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(OdjToolError):
                    validate_windows_account(invalid)

    def test_local_info_uses_fixed_odj_path_and_current_account(self) -> None:
        paths = make_paths(Path("C:/IronDeploy"))
        with patch.dict(
            os.environ,
            {"USERDOMAIN": "DOMAIN", "USERNAME": "irondeploy_api"},
        ):
            result = get_local_odj_info(paths)

        self.assertEqual(result["path"], str((paths.root / "ODJ").resolve()))
        self.assertEqual(result["processAccount"], r"DOMAIN\irondeploy_api")


class OdjPowerShellRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path("C:/IronDeploy")
        self.paths = make_paths(self.root)

    @patch("app.odj_tools.Path.is_file", return_value=True)
    @patch("app.odj_tools.subprocess.run")
    def test_account_is_sent_through_stdin_to_fixed_helper(
        self,
        run,
        _is_file,
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "status": "secured",
                    "aclAccount": r"DOMAIN\irondeploy_api",
                }
            ),
            stderr="",
        )

        result = secure_odj_acl(self.paths, r"DOMAIN\irondeploy_api")

        self.assertTrue(result["ok"])
        command = run.call_args.args[0]
        self.assertEqual(command[-2], "-File")
        self.assertEqual(
            Path(command[-1]),
            self.root / "Tools" / "Set-IronDeployOdjAcl.ps1",
        )
        payload = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(payload, {"account": r"DOMAIN\irondeploy_api"})
        self.assertTrue(run.call_args.kwargs["check"] is False)

    @patch("app.odj_tools.Path.is_file", return_value=True)
    @patch("app.odj_tools.subprocess.run")
    def test_unresolvable_account_is_reported(
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
                    "code": "account_not_found",
                    "message": "Windows cannot resolve the account.",
                }
            ),
            stderr="",
        )

        with self.assertRaises(OdjToolError) as raised:
            secure_odj_acl(self.paths, r"DOMAIN\missing")

        self.assertEqual(raised.exception.code, "account_not_found")
        self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
