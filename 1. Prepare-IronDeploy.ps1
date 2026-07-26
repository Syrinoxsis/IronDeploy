#Requires -Version 5.1

<#
.SYNOPSIS
Prepares WinPE and the IronAPI Python environment.

.DESCRIPTION
Runs the existing IronDeploy WinPE initializer, then creates
Core\Api\.venv when needed and installs the API requirements.

This script does not rebuild the WinPE WIM or ISO, configure IronAPI, or start
IronAPI. Use 2. Start-IronDeploySetupWeb.ps1 for configuration after
preparation.
#>

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$IronDeployRoot = Join-Path $PSScriptRoot "Core"
$WinPEInitializer = Join-Path `
    $IronDeployRoot `
    "WinPE\Build\Initialize-IronDeployWinPE.ps1"
$ApiRoot = Join-Path $IronDeployRoot "Api"
$ApiVenvRoot = Join-Path $ApiRoot ".venv"
$ApiPython = Join-Path $ApiVenvRoot "Scripts\python.exe"
$ApiRequirements = Join-Path $ApiRoot "requirements.txt"

foreach ($requiredFile in @($WinPEInitializer, $ApiRequirements)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Required IronDeploy file is missing: $requiredFile"
    }
}

function Get-PythonLauncher {
    $python = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -ne $python) {
        return @($python.Source, "-3")
    }

    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -ne $python) {
        return @($python.Source)
    }

    throw "Python 3 was not found in PATH."
}

Write-Host "Preparing IronDeploy WinPE working tree." -ForegroundColor Cyan
& $WinPEInitializer
if ($LASTEXITCODE -ne 0) {
    throw "IronDeploy WinPE initialization failed with exit code $LASTEXITCODE."
}

if (-not (Test-Path -LiteralPath $ApiPython -PathType Leaf)) {
    $launcher = @(Get-PythonLauncher)
    Write-Host "Creating IronAPI virtual environment: $ApiVenvRoot" `
        -ForegroundColor Cyan

    if ($launcher.Count -gt 1) {
        & $launcher[0] $launcher[1] -m venv $ApiVenvRoot
    }
    else {
        & $launcher[0] -m venv $ApiVenvRoot
    }

    if ($LASTEXITCODE -ne 0) {
        throw (
            "Failed to create the IronAPI virtual environment; exit code " +
            "$LASTEXITCODE."
        )
    }
}
else {
    Write-Host "Using existing IronAPI virtual environment: $ApiVenvRoot" `
        -ForegroundColor DarkGray
}

Write-Host "Installing IronAPI dependencies." -ForegroundColor Cyan
& $ApiPython `
    -m pip install `
    --disable-pip-version-check `
    -r $ApiRequirements
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install IronAPI dependencies; exit code $LASTEXITCODE."
}

Write-Host ""
Write-Host "IronDeploy preparation completed." -ForegroundColor Green
Write-Host (
    "Next: run & '.\2. Start-IronDeploySetupWeb.ps1' to configure IronDeploy."
)
