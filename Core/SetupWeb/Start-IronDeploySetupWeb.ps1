#Requires -Version 5.1

[CmdletBinding()]
param(
    [int]$TokenTtlSeconds = 120,
    [int]$SessionTtlSeconds = 1500,
    [switch]$SkipDependencyInstall,
    [switch]$NoBrowser
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Start-ElevatedSetupWeb {
    $commandParts = @(
        "& '$($PSCommandPath.Replace("'", "''"))'"
        "-TokenTtlSeconds $TokenTtlSeconds"
        "-SessionTtlSeconds $SessionTtlSeconds"
    )
    if ($SkipDependencyInstall) {
        $commandParts += "-SkipDependencyInstall"
    }
    if ($NoBrowser) {
        $commandParts += "-NoBrowser"
    }

    $command = $commandParts -join " "
    $encodedCommand = [Convert]::ToBase64String(
        [Text.Encoding]::Unicode.GetBytes($command)
    )
    $powershell = (Get-Command powershell.exe -ErrorAction Stop).Source

    Write-Host "SetupWeb requires administrator rights; requesting UAC approval." `
        -ForegroundColor Yellow
    try {
        $process = Start-Process `
            -FilePath $powershell `
            -ArgumentList @(
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                $encodedCommand
            ) `
            -WorkingDirectory $PSScriptRoot `
            -Verb RunAs `
            -WindowStyle Normal `
            -Wait `
            -PassThru
    }
    catch {
        throw "Administrator approval is required to start SetupWeb. $($_.Exception.Message)"
    }

    exit $process.ExitCode
}

if (-not (Test-IsAdministrator)) {
    Start-ElevatedSetupWeb
}

$SetupWebRoot = $PSScriptRoot
$VenvRoot = Join-Path $SetupWebRoot ".venv"
$PythonPath = Join-Path $VenvRoot "Scripts\python.exe"
$RequirementsPath = Join-Path $SetupWebRoot "requirements.txt"

function New-UrlSafeToken([int]$ByteCount) {
    $bytes = New-Object byte[] $ByteCount
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
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

function New-RandomLocalPort {
    for ($attempt = 0; $attempt -lt 100; $attempt++) {
        $port = Get-Random -Minimum 49152 -Maximum 65535
        $listener = $null
        try {
            $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Parse("127.0.0.1"), $port)
            $listener.Start()
            return $port
        }
        catch {
            continue
        }
        finally {
            if ($null -ne $listener) {
                $listener.Stop()
            }
        }
    }
    throw "Unable to find a free localhost TCP port."
}

$VenvExists = Test-Path -LiteralPath $PythonPath -PathType Leaf
if ($VenvExists) {
    $launcher = @($PythonPath)
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
    Write-Host "Creating SetupWeb virtual environment: $VenvRoot" -ForegroundColor Cyan
    if ($launcher.Count -gt 1) {
        & $launcher[0] $launcher[1] -m venv $VenvRoot
    }
    else {
        & $launcher[0] -m venv $VenvRoot
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create SetupWeb virtual environment."
    }
}

if (-not $SkipDependencyInstall) {
    Write-Host "Installing SetupWeb dependencies." -ForegroundColor Cyan
    & $PythonPath -m pip install --disable-pip-version-check -r $RequirementsPath
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install SetupWeb dependencies."
    }
}

$port = New-RandomLocalPort
$token = New-UrlSafeToken 32
$env:SETUPWEB_BOOTSTRAP_TOKEN = $token
$env:SETUPWEB_PORT = [string]$port
$env:SETUPWEB_TOKEN_TTL_SECONDS = [string]$TokenTtlSeconds
$env:SETUPWEB_SESSION_TTL_SECONDS = [string]$SessionTtlSeconds

$url = "http://127.0.0.1:$port/?token=$token"
Write-Host ""
Write-Host "IronDeploy SetupWeb is local-only." -ForegroundColor Green
Write-Host "Bootstrap token TTL : $TokenTtlSeconds seconds"
Write-Host "Session cookie TTL  : $SessionTtlSeconds seconds"
Write-Host "Setup URL           : $url" -ForegroundColor Cyan
if (-not $NoBrowser) {
    Write-Host "The default browser will open automatically."
}
Write-Host ""

$browserJob = $null
if (-not $NoBrowser) {
    $browserJob = Start-Job -ArgumentList $url, $port -ScriptBlock {
        param([string]$SetupUrl, [int]$SetupPort)

        for ($attempt = 0; $attempt -lt 100; $attempt++) {
            $client = [Net.Sockets.TcpClient]::new()
            try {
                $connection = $client.BeginConnect(
                    "127.0.0.1",
                    $SetupPort,
                    $null,
                    $null
                )
                if ($connection.AsyncWaitHandle.WaitOne(250)) {
                    $client.EndConnect($connection)
                    Start-Process -FilePath $SetupUrl
                    return
                }
            }
            catch {
                # SetupWeb may still be starting; retry below.
            }
            finally {
                $client.Dispose()
            }
            Start-Sleep -Milliseconds 100
        }
    }
}

Push-Location $SetupWebRoot
try {
    & $PythonPath -m uvicorn app.main:app --host 127.0.0.1 --port $port --no-access-log
}
finally {
    Pop-Location
    if ($null -ne $browserJob) {
        Stop-Job -Job $browserJob -ErrorAction SilentlyContinue
        Remove-Job -Job $browserJob -Force -ErrorAction SilentlyContinue
    }
    Remove-Item Env:\SETUPWEB_BOOTSTRAP_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:\SETUPWEB_PORT -ErrorAction SilentlyContinue
    Remove-Item Env:\SETUPWEB_TOKEN_TTL_SECONDS -ErrorAction SilentlyContinue
    Remove-Item Env:\SETUPWEB_SESSION_TTL_SECONDS -ErrorAction SilentlyContinue
}
