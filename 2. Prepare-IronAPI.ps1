#Requires -Version 5.1

<#
.SYNOPSIS
Prepares the IronAPI Python environment.

.DESCRIPTION
Creates Core\Api\.venv when needed and installs the API requirements.

This script does not configure or start IronAPI. Use
3. Start-IronDeploySetupWeb.ps1 for configuration after preparation.
#>

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ApiRoot = Join-Path $PSScriptRoot "Core\Api"
$ApiVenvRoot = Join-Path $ApiRoot ".venv"
$ApiPython = Join-Path $ApiVenvRoot "Scripts\python.exe"
$ApiRequirements = Join-Path $ApiRoot "requirements.txt"

if (-not (Test-Path -LiteralPath $ApiRequirements -PathType Leaf)) {
    throw "IronAPI requirements file is missing: $ApiRequirements"
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

$VenvExists = Test-Path -LiteralPath $ApiPython -PathType Leaf
if ($VenvExists) {
    $launcher = @($ApiPython)
}
else {
    $launcher = @(Get-PythonLauncher)
}

if ($launcher.Count -gt 1) {
    $pythonVersion = (& $launcher[0] $launcher[1] --version 2>&1 | Out-String).Trim()
}
else {
    $pythonVersion = (& $launcher[0] --version 2>&1 | Out-String).Trim()
}

if ($LASTEXITCODE -ne 0) {
    throw "Failed to query the selected Python; exit code $LASTEXITCODE."
}

$launcherDisplay = ($launcher | ForEach-Object {
    if ($_ -match "\s") {
        return '"{0}"' -f $_
    }

    return $_
}) -join " "
Write-Host "Selected Python: $launcherDisplay ($pythonVersion)" `
    -ForegroundColor Cyan

if (-not $VenvExists) {
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
Write-Host "IronAPI preparation completed." -ForegroundColor Green
Write-Host (
    "Next: run & '.\3. Start-IronDeploySetupWeb.ps1' to configure IronDeploy."
)
