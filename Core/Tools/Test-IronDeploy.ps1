#Requires -Version 5.1

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$IronDeployRoot = Split-Path $PSScriptRoot -Parent
$script:Failures = 0
$script:Warnings = 0

function Write-Pass([string]$Message) {
    Write-Host "[PASS] $Message" -ForegroundColor Green
}

function Write-WarningResult([string]$Message) {
    $script:Warnings++
    Write-Host "[WARN] $Message" -ForegroundColor Yellow
}

function Write-Failure([string]$Message) {
    $script:Failures++
    Write-Host "[FAIL] $Message" -ForegroundColor Red
}

function Test-RequiredPath {
    param(
        [string]$RelativePath,
        [ValidateSet("Leaf", "Container")]
        [string]$PathType
    )

    $path = Join-Path $IronDeployRoot $RelativePath
    if (Test-Path -LiteralPath $path -PathType $PathType) {
        Write-Pass $RelativePath
    }
    else {
        Write-Failure "Missing $PathType path: $path"
    }
}

function Test-PowerShellSyntax {
    $files = @(
        Get-ChildItem `
            -LiteralPath $IronDeployRoot `
            -Filter "*.ps1" `
            -File `
            -Recurse |
            Where-Object {
                $_.FullName -notlike "*\.venv\*" -and
                $_.FullName -notlike "*\.work\*"
            }
    )

    foreach ($file in $files) {
        $tokens = $null
        $errors = $null
        [void][Management.Automation.Language.Parser]::ParseFile(
            $file.FullName,
            [ref]$tokens,
            [ref]$errors
        )
        if ($errors.Count -eq 0) {
            Write-Pass "PowerShell syntax: $($file.FullName)"
        }
        else {
            foreach ($parseError in $errors) {
                Write-Failure (
                    "PowerShell syntax: {0}:{1}: {2}" -f `
                        $file.FullName,
                        $parseError.Extent.StartLineNumber,
                        $parseError.Message
                )
            }
        }
    }
}

Write-Host "IronDeploy configuration check" -ForegroundColor Cyan
Write-Host "Root: $IronDeployRoot"

@(
    @("Api\app\main.py", "Leaf"),
    @("Api\.env", "Leaf"),
    @("Api\.venv\Scripts\python.exe", "Leaf"),
    @("WinPE\Runtime\deploy.ps1", "Leaf"),
    @("WinPE\Runtime\IronDeploy.Engine.ps1", "Leaf"),
    @("WinPE\Runtime\IronDeploy.Gui.ps1", "Leaf"),
    @("WinPE\Runtime\deploy.config.ps1", "Leaf"),
    @("WinPE\Runtime\diskpart-uefi.txt", "Leaf"),
    @("WinPE\Runtime\startnet.cmd", "Leaf"),
    @("Share\Images", "Container"),
    @("Share\Drivers", "Container"),
    @("Library\PostPowerShell", "Container"),
    @("ServerTemplates\PostInstall\SetupComplete.cmd", "Leaf"),
    @("ServerTemplates\PostInstall\postinstall.ps1", "Leaf"),
    @("Data", "Container"),
    @("ODJ\pending", "Container"),
    @("Logs", "Container")
) | ForEach-Object {
    Test-RequiredPath -RelativePath $_[0] -PathType $_[1]
}

$legacyUnattend = Join-Path `
    $IronDeployRoot `
    "Share\Unattend\unattend-win11-template.xml"
if (Test-Path -LiteralPath $legacyUnattend -PathType Leaf) {
    Write-Failure (
        "Sensitive unattend template is still exposed through the SMB share: " +
        $legacyUnattend
    )
}
else {
    Write-Pass "No sensitive unattend template is exposed through SMB"
}

$images = @(
    Get-ChildItem `
        -LiteralPath (Join-Path $IronDeployRoot "Share\Images") `
        -Filter "*.wim" `
        -File `
        -ErrorAction SilentlyContinue
)
if ($images.Count -gt 0) {
    Write-Pass "Windows images found: $($images.Count)"
}
else {
    Write-WarningResult "No Windows WIM images found in Share\Images."
}

$sharePath = $null
try {
    $share = Get-SmbShare -Name "IronDeploy" -ErrorAction Stop
    $sharePath = [string]$share.Path
}
catch {
    $shareLine = @(
        & net.exe share 2>$null |
            Where-Object { $_ -match "^\s*IronDeploy\s+(.+?)\s*$" }
    ) | Select-Object -First 1
    if ($shareLine -match "^\s*IronDeploy\s+(.+?)\s*$") {
        $sharePath = $Matches[1].Trim()
    }
}

if ([string]::IsNullOrWhiteSpace($sharePath)) {
    Write-WarningResult "Local SMB share 'IronDeploy' is not configured."
}
else {
    $expectedSharePath = [IO.Path]::GetFullPath(
        (Join-Path $IronDeployRoot "Share")
    )
    $actualSharePath = [IO.Path]::GetFullPath($sharePath)
    if ($actualSharePath -ieq $expectedSharePath) {
        Write-Pass "SMB share points to $expectedSharePath"
    }
    else {
        Write-Failure (
            "SMB share points to '$actualSharePath', expected " +
            "'$expectedSharePath'."
        )
    }
}

$workingWim = Join-Path `
    $IronDeployRoot `
    ".work\WinPE_amd64\media\sources\boot.wim"
if (Test-Path -LiteralPath $workingWim -PathType Leaf) {
    Write-Pass "WinPE working boot.wim exists"
}
else {
    Write-WarningResult (
        "WinPE working boot.wim is not initialized. Use setup menu option 1."
    )
}

$odjAcl = Get-Acl -LiteralPath (Join-Path $IronDeployRoot "ODJ")
$broadOdjRules = @(
    $odjAcl.Access | Where-Object {
        $_.AccessControlType -eq "Allow" -and
        (
            $_.IdentityReference -match "Everyone$" -or
            $_.IdentityReference -match "\\iron_ro$" -or
            $_.IdentityReference -match "BUILTIN\\Users$"
        )
    }
)
if ($broadOdjRules.Count -eq 0) {
    Write-Pass "ODJ ACL has no broad read access"
}
else {
    Write-Failure "ODJ ACL exposes provisioning blobs to a broad/read-only account."
}

# IronAPI purges these on its own; anything left is a sign the API has not run
# since the deployment that abandoned it.
$odjPendingPath = Join-Path $IronDeployRoot "ODJ\pending"
$staleBlobBoundary = (Get-Date).AddHours(-24)
$staleBlobs = @(
    Get-ChildItem -LiteralPath $odjPendingPath -Filter "*.txt" -File `
        -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $staleBlobBoundary }
)
if ($staleBlobs.Count -eq 0) {
    Write-Pass "No abandoned ODJ blobs older than 24 hours"
}
else {
    Write-WarningResult (
        "{0} ODJ blob(s) older than 24 hours are still in {1}." -f `
            $staleBlobs.Count,
            $odjPendingPath
    )
}

Test-PowerShellSyntax

Write-Host ""
Write-Host (
    "Result: {0} failure(s), {1} warning(s)" -f `
        $script:Failures,
        $script:Warnings
)
if ($script:Failures -gt 0) {
    exit 1
}
exit 0
