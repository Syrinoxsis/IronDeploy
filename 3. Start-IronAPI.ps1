#Requires -Version 5.1

<#
.SYNOPSIS
Starts step 3, IronAPI, from the repository root.

.DESCRIPTION
Convenience launcher that forwards to
Core\Tools\Start-IronAPI.ps1, which holds the actual startup logic: it
reads Api\.env, resolves the bind address, access mode, and port, creates the
Data, ODJ\pending, and Logs folders, and runs uvicorn from the Api virtual
environment. LDAP searches and djoin.exe run as the Windows account that
starts this script; use a domain account with the required AD rights.

.PARAMETER BindHost
Overrides IRONAPI_BIND_HOST. Ignored when IRONAPI_ACCESS_MODE is https_proxy,
which always binds 127.0.0.1.

.PARAMETER Port
Overrides IRONAPI_PORT. Ignored when IRONAPI_ACCESS_MODE is https_proxy, which
always uses port 8000.

.PARAMETER AccessLog
Enables the uvicorn access log regardless of IRONAPI_ACCESS_LOG.

.EXAMPLE
& ".\3. Start-IronAPI.ps1"

.EXAMPLE
& ".\3. Start-IronAPI.ps1" -Port 8080 -AccessLog
#>

[CmdletBinding()]
param(
    [string]$BindHost,
    [ValidateRange(1, 65535)]
    [int]$Port,
    [switch]$AccessLog
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$StartScript = Join-Path `
    $PSScriptRoot `
    "Core\Tools\Start-IronAPI.ps1"
if (-not (Test-Path -LiteralPath $StartScript -PathType Leaf)) {
    throw "IronAPI start script is missing: $StartScript"
}

# Forward only the parameters that were actually supplied, so unspecified ones
# keep falling back to Api\.env instead of being passed as empty defaults.
& $StartScript @PSBoundParameters
exit $LASTEXITCODE
