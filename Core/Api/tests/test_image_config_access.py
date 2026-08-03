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
    _last_build_starts,
    get_winpe_build_state,
    start_winpe_build,
)

STATIC_ROOT = Path(__file__).resolve().parents[1] / "app" / "static"


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
    def test_last_build_starts_are_restored_from_log_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            log_directory = Path(temporary_directory)
            for name in (
                "winpe-wim-20260728T090000Z.log",
                "winpe-wim-20260729T110500Z.log",
                "winpe-iso-20260727T081500Z.log",
                "winpe-iso-invalid.log",
            ):
                (log_directory / name).touch()

            with patch("app.winpe_build.BUILD_LOG_DIR", log_directory):
                starts = _last_build_starts()

        self.assertEqual(starts["wim"], "2026-07-29T11:05:00+00:00")
        self.assertEqual(starts["iso"], "2026-07-27T08:15:00+00:00")

    def test_last_build_starts_are_empty_without_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch(
                "app.winpe_build.BUILD_LOG_DIR", Path(temporary_directory)
            ):
                self.assertEqual(
                    _last_build_starts(),
                    {"wim": None, "iso": None},
                )

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


class ImageConfigPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (STATIC_ROOT / "image-config.html").read_text(encoding="utf-8")
        cls.css = (STATIC_ROOT / "image-config.css").read_text(encoding="utf-8")
        cls.javascript = (STATIC_ROOT / "image-config.js").read_text(encoding="utf-8")
        cls.i18n = (STATIC_ROOT / "i18n.js").read_text(encoding="utf-8")

    def test_uses_nested_settings_workspace(self) -> None:
        self.assertIn('class="image-config-page"', self.html)
        for view in (
            "setup-account",
            "builtin-account",
            "language",
            "keyboard-layouts",
            "locale-time",
            "technical-settings",
        ):
            self.assertIn(f'data-settings-view="{view}"', self.html)
            self.assertIn(f'data-settings-view-panel="{view}"', self.html)
        self.assertIn("grid-template-columns:", self.css)
        self.assertIn(".settings-workspace", self.css)
        self.assertIn(".settings-actions", self.css)

    def test_action_rail_keeps_only_primary_operations_visible(self) -> None:
        self.assertIn('id="save-button"', self.html)
        self.assertIn('id="build-wim-button"', self.html)
        self.assertIn('id="build-iso-button"', self.html)
        self.assertIn('id="last-wim-build"', self.html)
        self.assertIn('id="last-iso-build"', self.html)
        self.assertNotIn("SID -500", self.html)
        self.assertNotIn("settings-category", self.html)
        self.assertNotIn(r"WinPE\Runtime", self.html)

    def test_navigation_and_dirty_state_are_interactive(self) -> None:
        self.assertIn("function showSettingsView(view)", self.javascript)
        self.assertIn("function setDirty(dirty)", self.javascript)
        self.assertIn("function refreshAll()", self.javascript)
        self.assertNotIn("settingsCategory", self.javascript)

    def test_winpe_technical_settings_offer_both_image_apply_modes(self) -> None:
        self.assertIn('name="imageApplyMode" type="radio" value="staged"', self.html)
        self.assertIn('name="imageApplyMode" type="radio" value="direct"', self.html)
        self.assertIn("config.imageApplyMode || \"direct\"", self.javascript)
        self.assertIn("imageApplyMode:", self.javascript)

    def test_redesigned_workspace_has_russian_localization(self) -> None:
        for text in (
            '"Locale & time zone": "Регион и часовой пояс"',
            '"Image apply progress": "Применение образа"',
            '"All changes saved": "Все изменения сохранены"',
            '"Available layout": "Доступная раскладка"',
            '"No WinPE build has been started since IronAPI launched.": '
            '"После запуска IronAPI сборка WinPE ещё не запускалась."',
        ):
            self.assertIn(text, self.i18n)
        self.assertIn("function localizedLocaleLabel(choice)", self.javascript)
        self.assertIn("translated(zone.label)", self.javascript)


if __name__ == "__main__":
    unittest.main()
