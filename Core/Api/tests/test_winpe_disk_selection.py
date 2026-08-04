import subprocess
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

    def test_gui_scales_the_complete_design_surface_without_scrollbars(self) -> None:
        gui = (RUNTIME_ROOT / "IronDeploy.Gui.ps1").read_text(
            encoding="utf-8-sig"
        )

        self.assertIn('x:Name="RootViewbox"', gui)
        self.assertIn('Stretch="Uniform"', gui)
        self.assertIn('StretchDirection="DownOnly"', gui)
        self.assertIn('x:Name="DesignSurface"', gui)
        self.assertIn('Width="820" Height="640"', gui)
        self.assertIn("function Set-IronGuiWindowBounds", gui)
        self.assertIn("[System.Windows.SystemParameters]::WorkArea", gui)
        self.assertIn("[object]$WorkArea = $null", gui)
        self.assertIn("Set-IronGuiWindowBounds -Window $script:IronGuiWindow", gui)

        identity_page = gui[
            gui.index('<Grid x:Name="IdentityPage"') :
            gui.index('<!-- Step 2: optional post-install software -->')
        ]
        popup_start = identity_page.index('<Popup x:Name="KnownDeploymentsPopup"')
        popup_end = identity_page.index("</Popup>", popup_start) + len("</Popup>")
        identity_page_without_popup = (
            identity_page[:popup_start] + identity_page[popup_end:]
        )
        self.assertNotIn("<ScrollViewer", identity_page_without_popup)

    def test_gui_downscales_for_a_constrained_logical_work_area(self) -> None:
        gui_path = str(RUNTIME_ROOT / "IronDeploy.Gui.ps1").replace("'", "''")
        script = f"""
$ErrorActionPreference = 'Stop'
. '{gui_path}'
Add-Type -AssemblyName PresentationFramework,PresentationCore,WindowsBase,System.Xaml
$reader = New-Object System.Xml.XmlNodeReader ([xml]$IronDeployGuiXaml)
$window = [System.Windows.Markup.XamlReader]::Load($reader)
$workArea = New-Object System.Windows.Rect(0, 0, 640, 480)
Set-IronGuiWindowBounds -Window $window -WorkArea $workArea
if ($window.Width -ne 624 -or $window.Height -ne 464) {{
    throw 'Window was not constrained to the logical work area'
}}
$viewbox = $window.FindName('RootViewbox')
$viewbox.Measure((New-Object System.Windows.Size(600, 420)))
$viewbox.Arrange((New-Object System.Windows.Rect(0, 0, 600, 420)))
$viewbox.UpdateLayout()
$visual = [System.Windows.Media.VisualTreeHelper]::GetChild($viewbox, 0)
$scale = $visual.Transform.Value.M11
if ($scale -le 0 -or $scale -ge 1) {{
    throw "Expected a downscale transform, received $scale"
}}
"""
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-STA",
                "-Command",
                script,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

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
