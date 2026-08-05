import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path


IRONDEPLOY_ROOT = Path(__file__).resolve().parents[2]
ENGINE_PATH = IRONDEPLOY_ROOT / "WinPE" / "Runtime" / "IronDeploy.Engine.ps1"


class WinPEImageApplyStrategyTests(unittest.TestCase):
    @staticmethod
    def run_powershell(
        function_names: list[str], assertions: str
    ) -> subprocess.CompletedProcess[str]:
        escaped_engine = str(ENGINE_PATH).replace("'", "''")
        names = ",".join(f"'{name}'" for name in function_names)
        script = f"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    '{escaped_engine}',
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
foreach ($name in @({names})) {{
    $functionAst = $ast.FindAll(
        {{
            param($node)
            $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
        }},
        $true
    ) | Select-Object -First 1
    if ($null -eq $functionAst) {{ throw "Missing function $name" }}
    Invoke-Expression $functionAst.Extent.Text
}}
{assertions}
"""
        return subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def assert_powershell(self, function_names: list[str], assertions: str) -> None:
        result = self.run_powershell(function_names, assertions)
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def test_missing_and_unknown_modes_fall_back_to_direct_with_warning(self) -> None:
        self.assert_powershell(
            ["Resolve-IronImageApplyMode"],
            """
$script:Warnings = @()
function Write-IronLog {
    param($Message, $Level)
    if ($Level -eq 'warn') { $script:Warnings += $Message }
}
if ((Resolve-IronImageApplyMode -Value $null) -ne 'direct') {
    throw 'Missing mode did not fall back to direct'
}
if ((Resolve-IronImageApplyMode -Value 'future') -ne 'direct') {
    throw 'Unknown mode did not fall back to direct'
}
if ((Resolve-IronImageApplyMode -Value 'STAGED') -ne 'staged') {
    throw 'Known staged mode was not normalized'
}
if ($script:Warnings.Count -ne 2) { throw 'Expected two fallback warnings' }
""",
        )

    def test_robocopy_exit_codes_zero_through_seven_are_success(self) -> None:
        self.assert_powershell(
            ["Test-IronRobocopyExitCode"],
            """
foreach ($code in 0..7) {
    if (-not (Test-IronRobocopyExitCode -ExitCode $code)) {
        throw "Expected robocopy code $code to succeed"
    }
}
foreach ($code in @(-1, 8, 16)) {
    if (Test-IronRobocopyExitCode -ExitCode $code) {
        throw "Expected robocopy code $code to fail"
    }
}
""",
        )

    def test_empty_staged_hash_is_rejected_with_a_clear_error(self) -> None:
        self.assert_powershell(
            ["Test-IronRobocopyExitCode", "Copy-IronImageToLocalStaging"],
            """
$WindowsDrive = 'C:'
try {
    Copy-IronImageToLocalStaging `
        -SourcePath 'Z:\\Images\\install.wim' `
        -ExpectedLength 123 `
        -ExpectedSha256 '' `
        -DeploymentId 42
    throw 'Expected invalid SHA-256 to fail'
} catch {
    Write-Output ("CAUGHT: " + $_.Exception.Message)
    if ($_.Exception.Message -notmatch 'invalid SHA-256') {
        throw "Unexpected error: $($_.Exception.Message)"
    }
}
""",
        )

    def test_staged_copy_verifies_hash_and_cleanup_removes_local_wim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_dir = root / "share"
            source_dir.mkdir()
            source = source_dir / "install.wim"
            payload = b"staged-wim-payload"
            source.write_bytes(payload)
            local_drive = root / "local"
            local_drive.mkdir()
            escaped_source = str(source).replace("'", "''")
            escaped_local = str(local_drive).replace("'", "''")
            expected_hash = hashlib.sha256(payload).hexdigest()

            self.assert_powershell(
                [
                    "Test-IronRobocopyExitCode",
                    "Copy-IronImageToLocalStaging",
                    "Remove-IronStagedImageArtifacts",
                ],
                f"""
$WindowsDrive = '{escaped_local}'
function Write-IronLog {{ param($Message, $Level) }}
function Get-PSDrive {{
    param($Name)
    return [pscustomobject]@{{ Free = 1GB }}
}}
function robocopy.exe {{
    param($SourceDirectory, $DestinationDirectory, $SourceName)
    [IO.File]::WriteAllBytes(
        (Join-Path $DestinationDirectory $SourceName),
        [IO.File]::ReadAllBytes((Join-Path $SourceDirectory $SourceName))
    )
    $global:LASTEXITCODE = 7
}}
$result = Copy-IronImageToLocalStaging `
    -SourcePath '{escaped_source}' `
    -ExpectedLength {len(payload)} `
    -ExpectedSha256 '{expected_hash}' `
    -DeploymentId 42
if ($result.RobocopyExitCode -ne 7) {{ throw 'Robocopy code was not retained' }}
if (-not (Test-Path -LiteralPath $result.Path -PathType Leaf)) {{
    throw 'Final staged WIM is missing'
}}
if (Test-Path -LiteralPath (Join-Path $result.StagingDirectory 'image.wim.partial')) {{
    throw 'Partial WIM should be renamed after verification'
}}
if ((Get-FileHash -LiteralPath $result.Path -Algorithm SHA256).Hash.ToLowerInvariant() -ne '{expected_hash}') {{
    throw 'Final staged WIM hash is wrong'
}}
Remove-IronStagedImageArtifacts `
    -StagingDirectory $result.StagingDirectory `
    -Reason 'test cleanup'
if (Test-Path -LiteralPath $result.StagingDirectory) {{
    throw 'Staged WIM directory was not cleaned'
}}
""",
            )

    def test_dism_receives_the_selected_smb_or_local_path(self) -> None:
        self.assert_powershell(
            ["Invoke-IronApplyWindowsImage"],
            """
$EnableGuiImageApplyProgress = $false
$script:IronProgressCallback = $null
function dism.exe {
    $script:DismArguments = @($args)
    $global:LASTEXITCODE = 0
}
foreach ($path in @('Z:\\Images\\install.wim', 'C:\\stage\\image.wim')) {
    $code = Invoke-IronApplyWindowsImage -ImagePath $path -ImageIndex 6
    if ($code -ne 0) { throw 'DISM wrapper returned an error' }
    if ($script:DismArguments -notcontains "/ImageFile:$path") {
        throw "DISM did not receive image path $path"
    }
}
""",
        )

    def test_staged_apply_has_download_network_window_only(self) -> None:
        self.assert_powershell(
            ["Start-IronNetworkStageMeasurement"],
            """
$script:ImageApplyMode = 'staged'
$script:IronNetworkDiagnostics = [pscustomobject]@{
    SmbAdapter = $null
    StageWindows = @{}
}
Start-IronNetworkStageMeasurement -Stage 'image_apply'
if ($script:IronNetworkDiagnostics.StageWindows.ContainsKey('image_apply')) {
    throw 'Local DISM must not have a network measurement window'
}
Start-IronNetworkStageMeasurement -Stage 'image_download'
if (-not $script:IronNetworkDiagnostics.StageWindows.ContainsKey('image_download')) {
    throw 'Staged download must have a network measurement window'
}
""",
        )

    def test_pipeline_keeps_direct_and_staged_paths_behind_one_dism_wrapper(self) -> None:
        engine = ENGINE_PATH.read_text(encoding="utf-8-sig")
        pipeline = engine[engine.index("function Invoke-IronDeployment") :]

        self.assertIn('$script:ImageApplyMode -eq "staged"', pipeline)
        self.assertIn('Start-DeploymentStage "image_download"', pipeline)
        self.assertIn('Start-DeploymentStage "image_apply"', pipeline)
        self.assertEqual(pipeline.count("Invoke-IronApplyWindowsImage"), 1)
        self.assertIn("$ImagePathToApply = $ImagePath", pipeline)
        self.assertIn("$ImagePathToApply = $DownloadResult.Path", pipeline)
        self.assertLess(
            pipeline.index("IronAPI manifest does not contain a valid SHA-256"),
            pipeline.index('Start-DeploymentStage "disk_partitioning"'),
        )


if __name__ == "__main__":
    unittest.main()
