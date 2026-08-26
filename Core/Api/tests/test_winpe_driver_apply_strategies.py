import subprocess
import tempfile
import unittest
from pathlib import Path


IRONDEPLOY_ROOT = Path(__file__).resolve().parents[2]
ENGINE_PATH = IRONDEPLOY_ROOT / "WinPE" / "Runtime" / "IronDeploy.Engine.ps1"


class WinPEDriverApplyStrategyTests(unittest.TestCase):
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
            ["Resolve-IronDriverApplyMode"],
            """
$script:Warnings = @()
function Write-IronLog {
    param($Message, $Level)
    if ($Level -eq 'warn') { $script:Warnings += $Message }
}
if ((Resolve-IronDriverApplyMode -Value $null) -ne 'direct') {
    throw 'Missing mode did not fall back to direct'
}
if ((Resolve-IronDriverApplyMode -Value 'future') -ne 'direct') {
    throw 'Unknown mode did not fall back to direct'
}
if ((Resolve-IronDriverApplyMode -Value 'STAGED') -ne 'staged') {
    throw 'Known staged mode was not normalized'
}
if ($script:Warnings.Count -ne 2) { throw 'Expected two fallback warnings' }
""",
        )

    def test_staged_copy_validates_tar_and_cleanup_removes_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "share" / ".irondeploy-archives" / "42"
            source.mkdir(parents=True)
            archive = source / "drivers.tar"
            archive.write_bytes(b"tar-payload")
            local_drive = root / "local"
            local_drive.mkdir()
            escaped_source = str(archive).replace("'", "''")
            escaped_local = str(local_drive).replace("'", "''")

            self.assert_powershell(
                [
                    "Test-IronRobocopyExitCode",
                    "Copy-IronDriverArchiveToLocalStaging",
                    "Remove-IronStagedDriverArtifacts",
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
    New-Item -ItemType Directory -Path $DestinationDirectory -Force | Out-Null
    Copy-Item `
        -LiteralPath (Join-Path $SourceDirectory $SourceName) `
        -Destination (Join-Path $DestinationDirectory $SourceName) `
        -Force
    $global:LASTEXITCODE = 3
}}
$result = Copy-IronDriverArchiveToLocalStaging `
    -SourcePath '{escaped_source}' `
    -ExpectedArchiveLength {archive.stat().st_size} `
    -ExpectedExtractedLength 25 `
    -DeploymentId 42
if ($result.RobocopyExitCode -ne 3) {{ throw 'Robocopy code was not retained' }}
if (-not (Test-Path -LiteralPath $result.ArchivePath -PathType Leaf)) {{
    throw 'Final staged driver TAR is missing'
}}
if (Test-Path -LiteralPath (Join-Path $result.StagingDirectory 'archive.partial')) {{
    throw 'Partial transfer directory should be removed after verification'
}}
Remove-IronStagedDriverArtifacts `
    -StagingDirectory $result.StagingDirectory `
    -Reason 'test cleanup'
if (Test-Path -LiteralPath $result.StagingDirectory) {{
    throw 'Staged driver directory was not cleaned'
}}
""",
            )

    def test_staged_mode_measures_download_but_not_local_injection(self) -> None:
        self.assert_powershell(
            ["Start-IronNetworkStageMeasurement"],
            """
$script:ImageApplyMode = 'direct'
$script:DriverApplyMode = 'staged'
$script:IronNetworkDiagnostics = [pscustomobject]@{
    SmbAdapter = $null
    StageWindows = @{}
}
Start-IronNetworkStageMeasurement -Stage 'driver_injection'
if ($script:IronNetworkDiagnostics.StageWindows.ContainsKey('driver_injection')) {
    throw 'Local driver injection must not have a network window'
}
Start-IronNetworkStageMeasurement -Stage 'driver_download'
if (-not $script:IronNetworkDiagnostics.StageWindows.ContainsKey('driver_download')) {
    throw 'Staged driver download must have a network window'
}
$script:DriverApplyMode = 'direct'
Start-IronNetworkStageMeasurement -Stage 'driver_injection'
if (-not $script:IronNetworkDiagnostics.StageWindows.ContainsKey('driver_injection')) {
    throw 'Direct injection must retain its SMB network window'
}
""",
        )

    def test_pipeline_splits_staged_download_and_injection_progress(self) -> None:
        engine = ENGINE_PATH.read_text(encoding="utf-8-sig")
        pipeline = engine[engine.index("function Invoke-IronDeployment") :]

        self.assertIn('$script:DriverApplyMode -eq "staged"', pipeline)
        self.assertIn('Start-DeploymentStage "driver_download"', pipeline)
        self.assertIn('Start-DeploymentStage "driver_injection"', pipeline)
        self.assertIn("Wait-IronDriverArchive", pipeline)
        self.assertIn("Copy-IronDriverArchiveToLocalStaging", pipeline)
        self.assertIn("Expand-IronDriverArchive", pipeline)
        self.assertIn('Set-IronProgress 60 "Downloading driver package"', pipeline)
        self.assertIn('Set-IronProgress 66 "Injecting driver package"', pipeline)
        self.assertIn("/Driver:$DriverPackagePathToInject", pipeline)


if __name__ == "__main__":
    unittest.main()
