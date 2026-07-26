import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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

from fastapi import HTTPException

from app.main import (
    _header_hostname,
    begin_winpe_build,
    require_image_config_write,
)
from app.winpe_build import (
    WinPEBuildError,
    _build_failure_message,
    get_winpe_build_state,
    start_winpe_build,
)


def make_request(host="127.0.0.1", headers=None):
    header_map = {"host": "127.0.0.1:8000"}
    if headers is not None:
        header_map.update(headers)
    return SimpleNamespace(
        client=SimpleNamespace(host=host) if host is not None else None,
        headers=header_map,
    )


class HeaderHostnameTests(unittest.TestCase):
    def test_extracts_host_from_various_forms(self) -> None:
        self.assertEqual(_header_hostname("127.0.0.1:8000"), "127.0.0.1")
        self.assertEqual(_header_hostname("localhost"), "localhost")
        self.assertEqual(_header_hostname("http://127.0.0.1:8000"), "127.0.0.1")
        self.assertEqual(_header_hostname("http://[::1]:8000"), "[::1]")
        self.assertEqual(_header_hostname("ATTACKER.EXAMPLE:8000"), "attacker.example")
        self.assertIsNone(_header_hostname(None))


class RequireImageConfigWriteTests(unittest.TestCase):
    def valid_headers(self, **extra):
        headers = {
            "host": "192.0.2.10:8000",
            "origin": "http://192.0.2.10:8000",
            "x-requested-with": "IronDeploy",
        }
        headers.update(extra)
        return headers

    def test_same_origin_write_with_header_is_allowed(self) -> None:
        require_image_config_write(make_request("192.0.2.20", self.valid_headers()))

    def test_missing_request_header_is_rejected(self) -> None:
        headers = self.valid_headers()
        del headers["x-requested-with"]
        with self.assertRaises(HTTPException):
            require_image_config_write(make_request("192.0.2.20", headers))

    def test_cross_origin_write_is_rejected(self) -> None:
        headers = self.valid_headers(origin="http://attacker.example")
        with self.assertRaises(HTTPException):
            require_image_config_write(make_request("192.0.2.20", headers))

    def test_absent_origin_still_needs_header(self) -> None:
        # Non-browser clients (no Origin) still need the custom header.
        headers = self.valid_headers()
        del headers["origin"]
        require_image_config_write(make_request("192.0.2.20", headers))


class WinPEBuildEndpointTests(unittest.TestCase):
    def valid_request(self):
        return make_request(
            "192.0.2.20",
            {
                "host": "192.0.2.10:8000",
                "origin": "http://192.0.2.10:8000",
                "x-requested-with": "IronDeploy",
            },
        )

    @patch("app.main.start_winpe_build")
    def test_wim_build_is_accepted(self, start_build) -> None:
        start_build.return_value = {"status": "running", "target": "Wim"}

        response = begin_winpe_build("wim", self.valid_request())

        self.assertEqual(response.status_code, 202)
        start_build.assert_called_once_with("wim")

    @patch("app.main.start_winpe_build")
    def test_iso_build_is_accepted(self, start_build) -> None:
        start_build.return_value = {"status": "running", "target": "Iso"}

        response = begin_winpe_build("iso", self.valid_request())

        self.assertEqual(response.status_code, 202)
        start_build.assert_called_once_with("iso")


class WinPEBuildStateTests(unittest.TestCase):
    def test_missing_elevation_is_persisted_as_failed_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            build_script = Path(temporary_directory) / "build.ps1"
            build_script.touch()
            with (
                patch("app.winpe_build.BUILD_SCRIPT", build_script),
                patch("app.winpe_build._is_elevated", return_value=False),
            ):
                with self.assertRaises(WinPEBuildError):
                    start_winpe_build("iso")

        state = get_winpe_build_state()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["target"], "Iso")
        self.assertIn("Administrator", state["message"])

    def test_powershell_failure_is_extracted_from_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            log_path = Path(temporary_directory) / "build.log"
            log_path.write_text(
                "Progress\nREBUILD FAILED: publication failed\n",
                encoding="utf-8",
            )

            message = _build_failure_message("Iso", 1, log_path)

        self.assertEqual(message, "Iso build failed: publication failed")


if __name__ == "__main__":
    unittest.main()
