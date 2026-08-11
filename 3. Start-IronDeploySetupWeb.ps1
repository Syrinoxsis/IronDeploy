#Requires -Version 5.1

<#
.SYNOPSIS
Starts step 3 of the IronDeploy setup flow from the repository root.

.DESCRIPTION
Forwards all supplied parameters to the canonical SetupWeb launcher under the
Core directory. SetupWeb creates and maintains its own virtual
environment.
#>

[CmdletBinding()]
param(
    [int]$TokenTtlSeconds = 120,
    [int]$SessionTtlSeconds = 1500,
    [switch]$SkipDependencyInstall,
    [switch]$NoBrowser
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Launcher = Join-Path `
    $PSScriptRoot `
    "Core\SetupWeb\Start-IronDeploySetupWeb.ps1"
if (-not (Test-Path -LiteralPath $Launcher -PathType Leaf)) {
    throw "IronDeploy SetupWeb launcher is missing: $Launcher"
}

& $Launcher @PSBoundParameters
exit $LASTEXITCODE
