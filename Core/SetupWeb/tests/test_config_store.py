import sys
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


SETUPWEB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SETUPWEB_ROOT))

from app.config_store import (
    IronDeployPaths,
    ensure_config_files,
    load_config,
    normalize_api,
    normalize_winpe,
    save_config,
)


class AccessModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.paths = IronDeployPaths.from_setupweb(SETUPWEB_ROOT)

    def test_image_apply_mode_defaults_to_direct_and_accepts_staged(self) -> None:
        self.assertEqual(
            normalize_api({})["IRONAPI_IMAGE_APPLY_MODE"],
            "direct",
        )
        self.assertEqual(
            normalize_api({"IRONAPI_IMAGE_APPLY_MODE": "STAGED"})[
                "IRONAPI_IMAGE_APPLY_MODE"
            ],
            "staged",
        )
        with self.assertRaisesRegex(ValueError, "direct or staged"):
            normalize_api({"IRONAPI_IMAGE_APPLY_MODE": "auto"})

    def test_empty_allowed_client_networks_are_preserved(self) -> None:
        result = normalize_api({"IRONAPI_ALLOWED_CLIENT_NETWORKS": ""})
        self.assertEqual(result["IRONAPI_ALLOWED_CLIENT_NETWORKS"], "")

        result = normalize_api(
            {
                "IRONAPI_ALLOWED_CLIENT_NETWORKS": (
                    " 192.0.2.0/24, 2001:db8::/32 "
                )
            }
        )
        self.assertEqual(
            result["IRONAPI_ALLOWED_CLIENT_NETWORKS"],
            "192.0.2.0/24,2001:db8::/32",
        )

        with self.assertRaises(ValueError):
            normalize_api({"IRONAPI_ALLOWED_CLIENT_NETWORKS": "not-a-cidr"})

    def test_http_direct_uses_selected_network_bind_and_insecure_cookie(self) -> None:
        result = normalize_api(
            {
                "IRONAPI_ACCESS_MODE": "http_direct",
                "IRONAPI_BIND_HOST": "198.51.100.5",
                "IRONAPI_PORT": "9000",
            }
        )

        self.assertEqual(result["IRONAPI_BIND_HOST"], "198.51.100.5")
        self.assertEqual(result["IRONAPI_PORT"], "9000")
        self.assertEqual(result["IRONAPI_COOKIE_SECURE"], "false")

    def test_http_direct_rejects_loopback_bind(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-loopback"):
            normalize_api(
                {
                    "IRONAPI_ACCESS_MODE": "http_direct",
                    "IRONAPI_BIND_HOST": "127.0.0.1",
                }
            )

    def test_deployment_lifecycle_timeouts_are_normalized(self) -> None:
        result = normalize_api(
            {
                "IRONAPI_DEPLOYMENT_AUTHORIZATION_TIMEOUT_MINUTES": "10",
                "IRONAPI_DEPLOYMENT_TIMEOUT_MINUTES": "90",
            }
        )
        self.assertEqual(
            result["IRONAPI_DEPLOYMENT_AUTHORIZATION_TIMEOUT_MINUTES"], "10"
        )
        self.assertEqual(result["IRONAPI_DEPLOYMENT_TIMEOUT_MINUTES"], "90")
        self.assertEqual(result["IRONAPI_ODJ_BLOB_MAX_AGE_MINUTES"], "5")

        with self.assertRaisesRegex(ValueError, "must be from 5 to 30"):
            normalize_api(
                {"IRONAPI_DEPLOYMENT_AUTHORIZATION_TIMEOUT_MINUTES": "4"}
            )
        with self.assertRaisesRegex(ValueError, "must be from 30 to 240"):
            normalize_api({"IRONAPI_DEPLOYMENT_TIMEOUT_MINUTES": "241"})
        with self.assertRaisesRegex(ValueError, "must be from 5 to 1440"):
            normalize_api(
                {
                    "IRONAPI_DEPLOYMENT_TIMEOUT_MINUTES": "120",
                    "IRONAPI_ODJ_BLOB_MAX_AGE_MINUTES": "4",
                }
            )

    def test_driver_upload_limits_are_normalized(self) -> None:
        result = normalize_api(
            {
                "IRONAPI_DRIVER_MAX_FILES": "25000",
                "IRONAPI_DRIVER_MAX_DEPTH": "16",
                "IRONAPI_DRIVER_MAX_FULL_PATH": "240",
                "IRONAPI_DRIVER_UPLOAD_TTL_HOURS": "24",
                "IRONAPI_DRIVER_MAX_ACTIVE_UPLOADS": "3",
                "IRONAPI_DRIVER_MIN_FREE_SPACE_GIB": "25",
            }
        )
        self.assertEqual(result["IRONAPI_DRIVER_MAX_FILES"], "25000")
        self.assertEqual(result["IRONAPI_DRIVER_MAX_DEPTH"], "16")
        self.assertEqual(result["IRONAPI_DRIVER_MAX_FULL_PATH"], "240")
        self.assertEqual(result["IRONAPI_DRIVER_UPLOAD_TTL_HOURS"], "24")
        self.assertEqual(result["IRONAPI_DRIVER_MAX_ACTIVE_UPLOADS"], "3")
        self.assertEqual(result["IRONAPI_DRIVER_MIN_FREE_SPACE_GIB"], "25")

        with self.assertRaisesRegex(ValueError, "IRONAPI_DRIVER_MAX_FILES"):
            normalize_api({"IRONAPI_DRIVER_MAX_FILES": "0"})
        with self.assertRaisesRegex(ValueError, "IRONAPI_DRIVER_MAX_FULL_PATH"):
            normalize_api({"IRONAPI_DRIVER_MAX_FULL_PATH": "63"})

    def test_https_proxy_forces_loopback_port_and_secure_cookie(self) -> None:
        result = normalize_api(
            {
                "IRONAPI_ACCESS_MODE": "https_proxy",
                "IRONAPI_BIND_HOST": "0.0.0.0",
                "IRONAPI_PORT": "9443",
            }
        )

        self.assertEqual(result["IRONAPI_BIND_HOST"], "127.0.0.1")
        self.assertEqual(result["IRONAPI_PORT"], "8000")
        self.assertEqual(result["IRONAPI_COOKIE_SECURE"], "true")

    def test_winpe_url_scheme_must_match_access_mode(self) -> None:
        values = {
            "SharePath": r"\\server\IronDeploy",
            "ShareDrive": "Z:",
            "ShareUser": r"server\iron_ro",
            "SharePassword": "not-a-placeholder-password",
            "ApiBaseUrl": "http://198.51.100.5:8000",
            "ValidateApiServerCertificate": "false",
        }

        direct = normalize_winpe(self.paths, values, "http_direct")
        self.assertEqual(direct["ApiBaseUrl"], values["ApiBaseUrl"])

        values["ApiBaseUrl"] = "https://deploy.example.test"
        proxy = normalize_winpe(self.paths, values, "https_proxy")
        self.assertEqual(proxy["ApiBaseUrl"], values["ApiBaseUrl"])

        values["ApiBaseUrl"] = "http://198.51.100.5:8000"
        with self.assertRaisesRegex(ValueError, "requires an https://"):
            normalize_winpe(self.paths, values, "https_proxy")

    def test_certificate_validation_requires_https_and_certificate(self) -> None:
        values = {
            "SharePath": r"\\server\IronDeploy",
            "ShareDrive": "Z:",
            "ShareUser": r"server\iron_ro",
            "SharePassword": "not-a-placeholder-password",
            "ApiBaseUrl": "http://198.51.100.5:8000",
            "ValidateApiServerCertificate": "true",
            "ApiServerCertificateType": "self_signed",
        }

        with self.assertRaisesRegex(ValueError, "requires an https://"):
            normalize_winpe(self.paths, values, "http_direct")

        values["ApiBaseUrl"] = "https://deploy.example.test"
        with patch("app.config_store.read_ps_config", return_value={}):
            with self.assertRaisesRegex(ValueError, "certificate is required"):
                normalize_winpe(self.paths, values, "https_proxy")

    @patch("app.config_store.inspect_certificate_der")
    def test_self_signed_certificate_is_normalized(self, inspect_certificate) -> None:
        now = datetime.now(timezone.utc)
        inspect_certificate.return_value = {
            "subject": ((('commonName', 'deploy.example.test'),),),
            "issuer": ((('commonName', 'deploy.example.test'),),),
            "not_before": now - timedelta(days=1),
            "not_after": now + timedelta(days=30),
        }
        values = {
            "SharePath": r"\\server\IronDeploy",
            "ShareDrive": "Z:",
            "ShareUser": r"server\iron_ro",
            "SharePassword": "not-a-placeholder-password",
            "ApiBaseUrl": "https://deploy.example.test",
            "ValidateApiServerCertificate": "true",
            "ApiServerCertificateType": "self_signed",
            "ApiServerCertificateBase64": "AQID",
        }

        result = normalize_winpe(self.paths, values, "https_proxy")

        self.assertEqual(result["ValidateApiServerCertificate"], "true")
        self.assertEqual(result["ApiServerCertificateType"], "self_signed")
        self.assertEqual(result["ApiServerCertificateBase64"], "AQID")


class CredentialMigrationTests(unittest.TestCase):
    def test_legacy_unattend_is_moved_out_of_the_smb_share(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = (
                root
                / "Share"
                / "Unattend"
                / "unattend-win11-template.xml"
            )
            target = (
                root
                / "ServerTemplates"
                / "Unattend"
                / "unattend-win11-template.xml"
            )
            legacy.parent.mkdir(parents=True)
            legacy.write_text("<Value>secret</Value>", encoding="utf-8")
            api_env = root / "Api" / ".env"
            winpe_config = root / "WinPE" / "Runtime" / "deploy.config.ps1"
            api_env.parent.mkdir(parents=True)
            winpe_config.parent.mkdir(parents=True)
            api_env.write_text("", encoding="utf-8")
            winpe_config.write_text("", encoding="utf-8")
            paths = IronDeployPaths(
                root=root,
                api_env=api_env,
                api_env_example=root / "Api" / ".env.example",
                winpe_config=winpe_config,
                winpe_config_example=(
                    root / "WinPE" / "Runtime" / "deploy.config.example.ps1"
                ),
                unattend=target,
                unattend_example=(
                    root
                    / "ServerTemplates"
                    / "Unattend"
                    / "unattend-win11-template.example.xml"
                ),
                backup_dir=root / "Logs" / "ConfigBackups",
                validation_script=root / "Tools" / "Test-IronDeploy.ps1",
                auth_bootstrap=root / "Data" / "auth-bootstrap.json",
            )

            ensure_config_files(paths)

            self.assertFalse(legacy.exists())
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "<Value>secret</Value>",
            )
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_text("<Value>stale-secret</Value>", encoding="utf-8")
            with self.assertRaisesRegex(
                RuntimeError,
                "Both legacy SMB-exposed and server-only",
            ):
                ensure_config_files(paths)

    def test_load_prefers_legacy_share_over_api_sample_during_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = IronDeployPaths(
                root=root,
                api_env=root / "Api" / ".env",
                api_env_example=root / "Api" / ".env.example",
                winpe_config=root / "WinPE" / "Runtime" / "deploy.config.ps1",
                winpe_config_example=root
                / "WinPE"
                / "Runtime"
                / "deploy.config.example.ps1",
                unattend=root
                / "ServerTemplates"
                / "Unattend"
                / "unattend-win11-template.xml",
                unattend_example=root
                / "ServerTemplates"
                / "Unattend"
                / "unattend-win11-template.example.xml",
                backup_dir=root / "Logs" / "ConfigBackups",
                validation_script=root / "Tools" / "Test-IronDeploy.ps1",
                auth_bootstrap=root / "Data" / "auth-bootstrap.json",
            )

            current_api = {"IRONAPI_BIND_HOST": "192.0.2.10"}

            def fake_dotenv(path: Path) -> dict[str, str]:
                if path == paths.api_env_example:
                    return {
                        "IRONAPI_SMB_SHARE_PATH": r"\\DEPLOY-SERVER\IronDeploy",
                        "IRONAPI_SMB_USER": r"DEPLOY-SERVER\sample",
                        "IRONAPI_SMB_PASSWORD": "sample-password",
                    }
                return current_api

            def fake_ps_config(path: Path) -> dict[str, str]:
                if path == paths.winpe_config:
                    return {
                        "SharePath": r"\\real-server.example.test\IronDeploy",
                        "ShareUser": r"REAL-SERVER\iron_ro",
                        "SharePassword": "working-password",
                    }
                return {"ShareDrive": "Z:", "ApiBaseUrl": "http://192.0.2.10:8000"}

            with (
                patch("app.config_store.ensure_config_files", return_value={"apiEnv": False, "winpeConfig": False}),
                patch("app.config_store.read_dotenv", side_effect=fake_dotenv),
                patch("app.config_store.read_ps_config", side_effect=fake_ps_config),
                patch("app.config_store.read_unattend_settings", return_value={"TimeZone": "UTC"}),
                patch("app.config_store.read_auth_bootstrap", return_value={}),
            ):
                payload = load_config(paths)

            self.assertEqual(
                payload["winpe"]["SharePath"],
                r"\\real-server.example.test\IronDeploy",
            )
            self.assertEqual(payload["winpe"]["ShareUser"], r"REAL-SERVER\iron_ro")
            self.assertTrue(payload["secrets"]["hasWinpeSharePassword"])

            current_api.update(
                {
                    "IRONAPI_SMB_SHARE_PATH": r"\\api-server.example.test\IronDeploy",
                    "IRONAPI_SMB_USER": r"API-SERVER\iron_ro",
                    "IRONAPI_SMB_PASSWORD": "api-password",
                }
            )
            with (
                patch("app.config_store.ensure_config_files", return_value={"apiEnv": False, "winpeConfig": False}),
                patch("app.config_store.read_dotenv", side_effect=fake_dotenv),
                patch("app.config_store.read_ps_config", side_effect=fake_ps_config),
                patch("app.config_store.read_unattend_settings", return_value={"TimeZone": "UTC"}),
                patch("app.config_store.read_auth_bootstrap", return_value={}),
            ):
                payload = load_config(paths)

            self.assertEqual(
                payload["winpe"]["SharePath"],
                r"\\api-server.example.test\IronDeploy",
            )

    def test_save_moves_smb_credentials_out_of_winpe_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Api").mkdir()
            (root / "WinPE" / "Runtime").mkdir(parents=True)
            (root / "ServerTemplates" / "Unattend").mkdir(parents=True)
            shutil.copy2(
                SETUPWEB_ROOT.parent / "Api" / ".env.example",
                root / "Api" / ".env.example",
            )
            shutil.copy2(
                SETUPWEB_ROOT.parent
                / "WinPE"
                / "Runtime"
                / "deploy.config.example.ps1",
                root / "WinPE" / "Runtime" / "deploy.config.example.ps1",
            )
            shutil.copy2(
                SETUPWEB_ROOT.parent
                / "ServerTemplates"
                / "Unattend"
                / "unattend-win11-template.example.xml",
                root
                / "ServerTemplates"
                / "Unattend"
                / "unattend-win11-template.example.xml",
            )
            paths = IronDeployPaths(
                root=root,
                api_env=root / "Api" / ".env",
                api_env_example=root / "Api" / ".env.example",
                winpe_config=root / "WinPE" / "Runtime" / "deploy.config.ps1",
                winpe_config_example=root
                / "WinPE"
                / "Runtime"
                / "deploy.config.example.ps1",
                unattend=root
                / "ServerTemplates"
                / "Unattend"
                / "unattend-win11-template.xml",
                unattend_example=root
                / "ServerTemplates"
                / "Unattend"
                / "unattend-win11-template.example.xml",
                backup_dir=root / "Logs" / "ConfigBackups",
                validation_script=root / "Tools" / "Test-IronDeploy.ps1",
                auth_bootstrap=root / "Data" / "auth-bootstrap.json",
            )
            payload = load_config(paths)
            paths.api_env.write_text(
                paths.api_env.read_text(encoding="utf-8").replace(
                    "IRONAPI_IMAGE_APPLY_MODE=direct",
                    "IRONAPI_IMAGE_APPLY_MODE=staged",
                ),
                encoding="utf-8",
            )
            payload["api"].pop("IRONAPI_IMAGE_APPLY_MODE")
            payload["api"]["IRONAPI_BIND_HOST"] = "198.51.100.5"
            payload["winpe"].update(
                {
                    "SharePath": r"\\server\IronDeploy",
                    "ShareUser": r"server\iron_ro",
                    "SharePassword": "server-side-password",
                    "ApiBaseUrl": "http://198.51.100.5:8000",
                }
            )
            payload["auth"] = {
                "username": "root-admin",
                "password": "SuperadminPassword123",
            }

            save_config(paths, payload)

            api_env = paths.api_env.read_text(encoding="utf-8")
            winpe_config = paths.winpe_config.read_text(encoding="utf-8")
            self.assertIn("IRONAPI_SMB_PASSWORD=server-side-password", api_env)
            self.assertIn(
                "IRONAPI_DEPLOYMENT_AUTHORIZATION_TIMEOUT_MINUTES=10", api_env
            )
            self.assertIn("IRONAPI_DEPLOYMENT_TIMEOUT_MINUTES=90", api_env)
            self.assertIn("IRONAPI_IMAGE_APPLY_MODE=staged", api_env)
            self.assertIn("IRONAPI_ODJ_BLOB_MAX_AGE_MINUTES=5", api_env)
            self.assertNotIn("server-side-password", winpe_config)
            self.assertNotIn("$SharePassword", winpe_config)
            self.assertNotIn("$ShareUser", winpe_config)


if __name__ == "__main__":
    unittest.main()
