import unittest
from pathlib import Path


IRONDEPLOY_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = IRONDEPLOY_ROOT / "WinPE" / "Runtime"


class WinPETargetDiskContractTests(unittest.TestCase):
    def test_diskpart_layout_uses_only_the_runtime_placeholder(self) -> None:
        template = (RUNTIME_ROOT / "diskpart-uefi.txt").read_text(
            encoding="utf-8-sig"
        )

        self.assertIn("select disk {{TARGET_DISK_NUMBER}}", template)
        self.assertNotIn("select disk 0", template.lower())

    def test_gui_requires_and_describes_an_explicit_disk_selection(self) -> None:
        gui = (RUNTIME_ROOT / "IronDeploy.Gui.ps1").read_text(
            encoding="utf-8-sig"
        )

        self.assertIn('x:Name="DiskCombo"', gui)
        self.assertIn("Get-IronDeployDiskList", gui)
        self.assertIn("$diskOk", gui)
        self.assertIn("SelectedDiskSizeBytes", gui)
        self.assertIn('"#{0} — {1} — {2:N2} GiB"', gui)

    def test_engine_reports_and_revalidates_the_selected_disk(self) -> None:
        engine = (RUNTIME_ROOT / "IronDeploy.Engine.ps1").read_text(
            encoding="utf-8-sig"
        )

        self.assertIn("function Get-IronDeployDiskList", engine)
        self.assertGreaterEqual(engine.count("Test-IronDeployTargetDisk"), 3)
        self.assertIn("target_disk_number", engine)
        self.assertIn("target_disk_model", engine)
        self.assertIn("target_disk_size_bytes", engine)
        self.assertIn("{{TARGET_DISK_NUMBER}}", engine)
        self.assertIn("Remove-IronDeployDriveLetterMountPoint", engine)
        self.assertIn("& mountvol.exe $MountPoint /D", engine)
        self.assertIn("[DISKPART]", engine)


if __name__ == "__main__":
    unittest.main()
