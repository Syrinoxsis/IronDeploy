import shutil
import tempfile
import unittest
from pathlib import Path

from app.config import IRONDEPLOY_ROOT
from app.image_config import (
    ImageConfigError,
    ImagePaths,
    load_image_config,
    save_image_config,
)

DEPLOY_EXAMPLE = IRONDEPLOY_ROOT / "WinPE" / "Runtime" / "deploy.config.example.ps1"
UNATTEND_EXAMPLE = (
    IRONDEPLOY_ROOT
    / "ServerTemplates"
    / "Unattend"
    / "unattend-win11-template.example.xml"
)


class ImageConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        winpe_dir = self.tmp / "WinPE" / "Runtime"
        unattend_dir = self.tmp / "ServerTemplates" / "Unattend"
        winpe_dir.mkdir(parents=True)
        unattend_dir.mkdir(parents=True)
        self.paths = ImagePaths(
            winpe_config=winpe_dir / "deploy.config.ps1",
            winpe_config_example=winpe_dir / "deploy.config.example.ps1",
            unattend=unattend_dir / "unattend-win11-template.xml",
            unattend_example=unattend_dir / "unattend-win11-template.example.xml",
            backup_dir=self.tmp / "Logs" / "ConfigBackups",
        )
        shutil.copy2(DEPLOY_EXAMPLE, self.paths.winpe_config_example)
        shutil.copy2(UNATTEND_EXAMPLE, self.paths.unattend_example)
        shutil.copy2(DEPLOY_EXAMPLE, self.paths.winpe_config)
        shutil.copy2(UNATTEND_EXAMPLE, self.paths.unattend)

    def test_load_reads_defaults_from_templates(self) -> None:
        config = load_image_config(self.paths)
        self.assertEqual(config["localAdminName"], "localadmin")
        self.assertTrue(config["enableBuiltInAdministrator"])
        self.assertTrue(config["enableSetupLocalAdmin"])
        self.assertTrue(config["enableGuiImageApplyProgress"])
        self.assertEqual(config["timeZone"], "Central Asia Standard Time")
        self.assertFalse(config["hasLocalAdminPassword"])
        self.assertFalse(config["hasBuiltInAdministratorPassword"])
        self.assertTrue(len(config["timeZones"]) > 50)

    def test_builtin_administrator_password_is_injected_and_reread(self) -> None:
        # No AdministratorPassword element exists in the template by default.
        unattend = self.paths.unattend.read_text(encoding="utf-8-sig")
        self.assertNotIn("<AdministratorPassword>", unattend)

        result = save_image_config(
            {
                "localAdminName": "localadmin",
                "builtInAdministratorPassword": "BuiltinSecret1",
                "timeZone": "UTC",
            },
            self.paths,
        )
        unattend = self.paths.unattend.read_text(encoding="utf-8-sig")
        self.assertIn("<AdministratorPassword>", unattend)
        self.assertIn("<Value>BuiltinSecret1</Value>", unattend)
        # The element belongs to UserAccounts, before LocalAccounts.
        self.assertLess(
            unattend.index("<AdministratorPassword>"),
            unattend.index("<LocalAccounts>"),
        )
        self.assertTrue(result["config"]["hasBuiltInAdministratorPassword"])

        # A second save updates in place rather than injecting a duplicate.
        save_image_config(
            {
                "localAdminName": "localadmin",
                "builtInAdministratorPassword": "BuiltinSecret2",
                "timeZone": "UTC",
            },
            self.paths,
        )
        unattend = self.paths.unattend.read_text(encoding="utf-8-sig")
        self.assertEqual(unattend.count("<AdministratorPassword>"), 1)
        self.assertIn("<Value>BuiltinSecret2</Value>", unattend)

    def test_blank_builtin_password_does_not_write_placeholder(self) -> None:
        save_image_config(
            {"localAdminName": "localadmin", "timeZone": "UTC"},
            self.paths,
        )
        unattend = self.paths.unattend.read_text(encoding="utf-8-sig")
        # Never create the element (or a placeholder password) when left blank.
        self.assertNotIn("<AdministratorPassword>", unattend)
        self.assertFalse(
            load_image_config(self.paths)["hasBuiltInAdministratorPassword"]
        )

    def test_save_updates_both_files_and_preserves_share_password(self) -> None:
        result = save_image_config(
            {
                "localAdminName": "deployadmin",
                "localAdminPassword": "SuperSecret123",
                "enableBuiltInAdministrator": False,
                "enableSetupLocalAdmin": False,
                "enableGuiImageApplyProgress": False,
                "timeZone": "Russian Standard Time",
            },
            self.paths,
        )
        self.assertEqual(len(result["backups"]), 2)

        deploy_config = self.paths.winpe_config.read_text(encoding="utf-8-sig")
        # Targeted edit keeps the SMB password line SetupWeb owns.
        self.assertNotIn("$SharePassword = ", deploy_config)
        self.assertIn("$SetupLocalAdminName = 'deployadmin'", deploy_config)
        self.assertIn("$EnableBuiltInAdministrator = $false", deploy_config)
        self.assertIn("$EnableSetupLocalAdmin = $false", deploy_config)
        self.assertIn("$EnableGuiImageApplyProgress = $false", deploy_config)

        unattend = self.paths.unattend.read_text(encoding="utf-8-sig")
        self.assertIn("<Name>deployadmin</Name>", unattend)
        self.assertIn("<DisplayName>deployadmin</DisplayName>", unattend)
        self.assertIn("<Value>SuperSecret123</Value>", unattend)
        self.assertEqual(
            unattend.count("<TimeZone>Russian Standard Time</TimeZone>"), 2
        )

        config = result["config"]
        self.assertEqual(config["localAdminName"], "deployadmin")
        self.assertFalse(config["enableGuiImageApplyProgress"])
        self.assertTrue(config["hasLocalAdminPassword"])

    def test_legacy_disable_setting_is_read_and_migrated(self) -> None:
        legacy = self.paths.winpe_config.read_text(encoding="utf-8-sig").replace(
            "$EnableSetupLocalAdmin = $true",
            "$DisableSetupLocalAdmin = $true",
        )
        self.paths.winpe_config.write_text(legacy, encoding="utf-8")

        self.assertFalse(load_image_config(self.paths)["enableSetupLocalAdmin"])

        save_image_config(
            {
                "localAdminName": "localadmin",
                "timeZone": "UTC",
            },
            self.paths,
        )
        migrated = self.paths.winpe_config.read_text(encoding="utf-8-sig")
        self.assertIn("$EnableSetupLocalAdmin = $false", migrated)
        self.assertNotIn("$DisableSetupLocalAdmin", migrated)

    def test_blank_password_keeps_existing_value(self) -> None:
        save_image_config(
            {
                "localAdminName": "deployadmin",
                "localAdminPassword": "SuperSecret123",
                "timeZone": "UTC",
            },
            self.paths,
        )
        save_image_config(
            {
                "localAdminName": "deployadmin",
                "localAdminPassword": "",
                "timeZone": "UTC",
            },
            self.paths,
        )
        unattend = self.paths.unattend.read_text(encoding="utf-8-sig")
        self.assertIn("<Value>SuperSecret123</Value>", unattend)

    def test_special_characters_are_xml_escaped(self) -> None:
        save_image_config(
            {
                "localAdminName": "deployadmin",
                "localAdminPassword": "a&b<c>d\"e",
                "timeZone": "UTC",
            },
            self.paths,
        )
        unattend = self.paths.unattend.read_text(encoding="utf-8-sig")
        # & < > must be escaped; quotes are legal unescaped in element text.
        self.assertIn('<Value>a&amp;b&lt;c&gt;d"e</Value>', unattend)

    def test_invalid_values_are_rejected(self) -> None:
        cases = [
            {"localAdminName": "bad name", "timeZone": "UTC"},
            {"localAdminName": "ok", "timeZone": "Not/A/Zone"},
            {"localAdminName": "ok", "timeZone": "UTC", "localAdminPassword": "short"},
            {
                "localAdminName": "ok",
                "timeZone": "UTC",
                "localAdminPassword": "CHANGE_ME_placeholder",
            },
            {
                "localAdminName": "ok",
                "timeZone": "UTC",
                "builtInAdministratorPassword": "short",
            },
            {
                "localAdminName": "ok",
                "timeZone": "UTC",
                "builtInAdministratorPassword": "CHANGE_ME_placeholder",
            },
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ImageConfigError):
                    save_image_config(payload, self.paths)

    def test_files_created_from_examples_when_missing(self) -> None:
        self.paths.winpe_config.unlink()
        self.paths.unattend.unlink()
        result = save_image_config(
            {
                "localAdminName": "freshadmin",
                "localAdminPassword": "AnotherSecret1",
                "timeZone": "UTC",
            },
            self.paths,
        )
        # Nothing existed to back up.
        self.assertEqual(result["backups"], [])
        self.assertTrue(self.paths.winpe_config.is_file())
        self.assertTrue(self.paths.unattend.is_file())
        self.assertIn(
            "$SetupLocalAdminName = 'freshadmin'",
            self.paths.winpe_config.read_text(encoding="utf-8-sig"),
        )

    def test_legacy_unattend_is_moved_out_of_the_smb_share(self) -> None:
        legacy = (
            self.tmp
            / "Share"
            / "Unattend"
            / "unattend-win11-template.xml"
        )
        legacy.parent.mkdir(parents=True)
        self.paths.unattend.unlink()
        shutil.copy2(UNATTEND_EXAMPLE, legacy)

        load_image_config(self.paths)

        self.assertFalse(legacy.exists())
        self.assertTrue(self.paths.unattend.is_file())


if __name__ == "__main__":
    unittest.main()
