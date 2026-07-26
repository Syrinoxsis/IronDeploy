#Requires -Version 5.1

[CmdletBinding()]
param(
    [int]$TokenTtlSeconds = 120,
    [int]$SessionTtlSeconds = 480,
    [switch]$SkipDependencyInstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$IronDeployRoot = Split-Path $PSScriptRoot -Parent
$Launcher = Join-Path $IronDeployRoot "SetupWeb\Start-IronDeploySetupWeb.ps1"

& $Launcher @PSBoundParameters
