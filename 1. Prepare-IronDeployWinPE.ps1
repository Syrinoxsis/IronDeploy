#Requires -Version 5.1

<#
.SYNOPSIS
Initializes the IronDeploy WinPE working tree.

.DESCRIPTION
Runs the existing IronDeploy WinPE initializer.

This script does not rebuild the WinPE WIM or ISO. Use
2. Prepare-IronAPI.ps1 to prepare the IronAPI Python environment afterward.
#>

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$IronDeployRoot = Join-Path $PSScriptRoot "Core"
$WinPEInitializer = Join-Path `
    $IronDeployRoot `
    "WinPE\Build\Initialize-IronDeployWinPE.ps1"

if (-not (Test-Path -LiteralPath $WinPEInitializer -PathType Leaf)) {
    throw "IronDeploy WinPE initializer is missing: $WinPEInitializer"
}

Write-Host "Preparing IronDeploy WinPE working tree." -ForegroundColor Cyan
& $WinPEInitializer
if ($LASTEXITCODE -ne 0) {
    throw "IronDeploy WinPE initialization failed with exit code $LASTEXITCODE."
}

Write-Host ""
Write-Host "IronDeploy WinPE preparation completed." -ForegroundColor Green
Write-Host (
    "Next: run & '.\2. Prepare-IronAPI.ps1' to prepare IronAPI."
)
