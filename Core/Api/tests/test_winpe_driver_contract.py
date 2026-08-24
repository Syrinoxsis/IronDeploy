import re
import unittest
from pathlib import Path

from pydantic import ValidationError

from app.deployments import DeploymentManifestRequest


IRONDEPLOY_ROOT = Path(__file__).resolve().parents[2]
ENGINE_PATH = IRONDEPLOY_ROOT / "WinPE" / "Runtime" / "IronDeploy.Engine.ps1"
GUI_PATH = IRONDEPLOY_ROOT / "WinPE" / "Runtime" / "IronDeploy.Gui.ps1"


class WinPEDriverContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = ENGINE_PATH.read_text(encoding="utf-8-sig")
        cls.gui = GUI_PATH.read_text(encoding="utf-8-sig")

    def test_gui_has_separate_third_step_and_single_choice_control(self) -> None:
        self.assertIn('x:Name="ProgramsPage"', self.gui)
        self.assertIn('x:Name="DriversPage"', self.gui)
        self.assertIn('x:Name="StepThreeIndicator"', self.gui)
        self.assertIn('x:Name="NoDriversCheck"', self.gui)
        self.assertIn('x:Name="DriverPackagesPanel"', self.gui)
        self.assertIn(
            "New-Object System.Windows.Controls.Expander",
            self.gui,
        )
        self.assertIn(
            "$driverCheck.Add_Checked($script:IronGuiDriverChecked)",
            self.gui,
        )
        self.assertIn('NoDriversOption = "Do not install drivers"', self.gui)
        self.assertNotIn('x:Name="DriverCombo"', self.gui)

    def test_selected_driver_is_sent_as_one_manifest_value(self) -> None:
        self.assertIn("driver_package = if (", self.engine)
        self.assertIn(
            '[string]$SelectedDriverPackage = ""',
            self.engine,
        )
        self.assertNotIn("[string[]]$SelectedDriverPackage", self.engine)
        self.assertIn(
            "-SelectedDriverPackage $SelectedDriverPackage",
            self.gui,
        )

    def test_manifest_accepts_none_or_one_vendor_model_path(self) -> None:
        without_driver = DeploymentManifestRequest(image_name="win11.wim")
        with_driver = DeploymentManifestRequest(
            image_name="win11.wim",
            driver_package="HP\\EliteBook 840 G10",
        )

        self.assertIsNone(without_driver.driver_package)
        self.assertEqual(
            with_driver.driver_package,
            "HP\\EliteBook 840 G10",
        )
        for invalid in (
            "HP",
            "HP/EliteBook",
            "HP\\Model\\Second",
            "..\\Model",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                DeploymentManifestRequest(
                    image_name="win11.wim",
                    driver_package=invalid,
                )

    def test_dism_uses_only_selected_package_and_no_selection_is_skipped(self) -> None:
        add_driver_commands = re.findall(
            r"(?im)^\s*dism(?:\.exe)?\s+.*?/Add-Driver.*$",
            self.engine,
        )
        self.assertEqual(len(add_driver_commands), 1)
        self.assertIn("/Driver:$DriverPackagePathToInject", add_driver_commands[0])
        self.assertNotIn("/Driver:$DriversPath", add_driver_commands[0])
        self.assertIn(
            'Write-IronLog "[SKIP] Driver installation was not selected"',
            self.engine,
        )
        self.assertIn('Skip-DeploymentStage "driver_injection"', self.engine)


if __name__ == "__main__":
    unittest.main()
