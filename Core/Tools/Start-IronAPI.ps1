#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$BindHost,
    [ValidateRange(1, 65535)]
    [int]$Port,
    [switch]$AccessLog
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$IronDeployRoot = Split-Path $PSScriptRoot -Parent
$ApiRoot = Join-Path $IronDeployRoot "Api"
$EnvPath = Join-Path $ApiRoot ".env"
$PythonPath = Join-Path $ApiRoot ".venv\Scripts\python.exe"

function Get-DotEnvValue {
    param(
        [string]$Name,
        [string]$Default
    )

    if (Test-Path -LiteralPath $EnvPath -PathType Leaf) {
        foreach ($line in [IO.File]::ReadAllLines($EnvPath)) {
            if ($line -match "^\s*$([regex]::Escape($Name))\s*=(.*)$") {
                return $Matches[1].Trim().Trim("'").Trim('"')
            }
        }
    }
    return $Default
}

$accessMode = Get-DotEnvValue "IRONAPI_ACCESS_MODE" "http_direct"
if ($accessMode -notin @("http_direct", "https_proxy")) {
    throw "IRONAPI_ACCESS_MODE must be http_direct or https_proxy."
}

if ($accessMode -eq "https_proxy") {
    $BindHost = "127.0.0.1"
    $Port = 8000
}
elseif ([string]::IsNullOrWhiteSpace($BindHost)) {
    $BindHost = Get-DotEnvValue "IRONAPI_BIND_HOST" "0.0.0.0"
}
if ($accessMode -eq "http_direct" -and $Port -eq 0) {
    $Port = [int](Get-DotEnvValue "IRONAPI_PORT" "8000")
}
$cookieSecure = Get-DotEnvValue "IRONAPI_COOKIE_SECURE" "false"
if ($accessMode -eq "https_proxy" -and $cookieSecure -ne "true") {
    throw "https_proxy requires IRONAPI_COOKIE_SECURE=true. Save SetupWeb configuration."
}
if ($accessMode -eq "http_direct") {
    $parsedBindHost = $null
    if (
        ![Net.IPAddress]::TryParse($BindHost, [ref]$parsedBindHost) -or
        [Net.IPAddress]::IsLoopback($parsedBindHost)
    ) {
        throw "http_direct requires a non-loopback bind address."
    }
    if ($cookieSecure -ne "false") {
        throw "http_direct requires IRONAPI_COOKIE_SECURE=false."
    }
}
if (-not $AccessLog) {
    $AccessLog = (
        Get-DotEnvValue "IRONAPI_ACCESS_LOG" "false"
    ) -match "^(?i:true|1|yes|on)$"
}

if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "IronAPI virtual environment is missing: $PythonPath"
}
if (-not (Test-Path -LiteralPath $EnvPath -PathType Leaf)) {
    throw "IronAPI configuration is missing: $EnvPath"
}

@("Data", "ODJ\pending", "Logs") | ForEach-Object {
    New-Item `
        -ItemType Directory `
        -Path (Join-Path $IronDeployRoot $_) `
        -Force |
        Out-Null
}

$arguments = @(
    "-m",
    "uvicorn",
    "app.main:app",
    "--host",
    $BindHost,
    "--port",
    [string]$Port
)
if (-not $AccessLog) {
    $arguments += "--no-access-log"
}

Write-Host "Starting IronAPI" -ForegroundColor Cyan
Write-Host "Root:    $IronDeployRoot"
Write-Host "Address: http://${BindHost}:$Port"
Write-Host "Mode:    $accessMode"
Write-Host "Stop with Ctrl+C."

Push-Location $ApiRoot
try {
    & $PythonPath @arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
