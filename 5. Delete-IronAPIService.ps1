#Requires -Version 5.1

<#
.SYNOPSIS
Removes the IronAPI Windows Service registration.

.DESCRIPTION
Stops the IronAPI service when it is running and unregisters it from the
Windows Service Control Manager. Only the service registration is removed.
The repository, configuration, database, Windows images, drivers, programs,
Offline Domain Join blobs, and logs are left untouched, so step 4 can register
the service again at any time.

This script is not part of installation. Run it only when the service must be
removed, for example before moving IronDeploy to another host or when changing
the service identity from scratch.
#>

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ServiceName = "IronAPI"
$ScExe = Join-Path $env:SystemRoot "System32\sc.exe"
$StopTimeout = [TimeSpan]::FromSeconds(30)

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Start-ElevatedCopy {
    $hostPath = (Get-Process -Id $PID).Path
    $arguments = @(
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        "`"$PSCommandPath`""
    )

    Write-Host (
        "Administrator rights are required. Requesting elevation..."
    ) -ForegroundColor Yellow
    $process = Start-Process `
        -FilePath $hostPath `
        -ArgumentList $arguments `
        -Verb RunAs `
        -Wait `
        -PassThru
    exit $process.ExitCode
}

if (-not (Test-Administrator)) {
    Start-ElevatedCopy
}

# sc.exe is resolved from %SystemRoot% rather than the PATH so that the
# elevated session cannot be redirected to a different executable.
if (-not (Test-Path -LiteralPath $ScExe -PathType Leaf)) {
    throw "Windows Service Control utility is missing: $ScExe"
}

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($null -eq $service) {
    Write-Host "The IronAPI service is not installed. Nothing was changed."
    exit 0
}

Write-Host ""
Write-Host "This stops and unregisters the IronAPI Windows service." `
    -ForegroundColor Yellow
Write-Host (
    "Any deployment that is still running will lose the API and fail."
) -ForegroundColor Yellow
Write-Host (
    "Configuration, database, images, drivers, programs, ODJ blobs, logs, " +
    "and repository files are preserved."
) -ForegroundColor Green
Write-Host ""

$confirmation = (Read-Host "Type REMOVE to continue").Trim()
if ($confirmation -ne "REMOVE") {
    Write-Host "Service removal was cancelled. Nothing was changed."
    exit 0
}

try {
    if ($service.Status -ne [ServiceProcess.ServiceControllerStatus]::Stopped) {
        Write-Host "Stopping the IronAPI service..." -ForegroundColor Cyan
        Stop-Service -Name $ServiceName -Force
        $service.WaitForStatus(
            [ServiceProcess.ServiceControllerStatus]::Stopped,
            $StopTimeout
        )
    }
}
catch [ServiceProcess.TimeoutException] {
    throw (
        "The IronAPI service did not stop within $($StopTimeout.TotalSeconds) " +
        "seconds. It was not removed; stop it manually and run this script again."
    )
}
finally {
    # Release the handle before deleting so the Service Control Manager can
    # complete the removal instead of only marking the service for deletion.
    $service.Dispose()
}

& $ScExe delete $ServiceName
if ($LASTEXITCODE -ne 0) {
    throw "sc.exe failed to delete '$ServiceName' (exit code $LASTEXITCODE)."
}

$removed = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    if ($null -eq (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
        $removed = $true
        break
    }
    Start-Sleep -Milliseconds 200
}

Write-Host ""
if ($removed) {
    Write-Host "The IronAPI service was removed." -ForegroundColor Green
}
else {
    Write-Host (
        "The IronAPI service is marked for deletion. Windows completes the " +
        "removal once the last open service handle closes, or after a restart."
    ) -ForegroundColor Yellow
}
Write-Host (
    "Configuration, database, images, drivers, programs, ODJ blobs, logs, " +
    "and repository files were not deleted."
) -ForegroundColor Green
Write-Host (
    "Run step 4 to register the service again."
) -ForegroundColor DarkGray
