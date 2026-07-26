# IronDeploy deployment engine (UI-agnostic).
#
# All destructive deployment logic lives here. IronDeploy.Gui.ps1 collects
# operator input, then drives this engine through Invoke-IronDeployment and
# receives progress through callbacks.
#
# This file never prompts and never reboots. It is dot-sourced by deploy.ps1
# and, separately, inside each background runspace the GUI creates.

$ErrorActionPreference = "Stop"

# --- Progress / log plumbing -------------------------------------------------
# Front-ends register callbacks with Set-IronDeployCallbacks. When no callback
# is registered the engine falls back to coloured Write-Host so it stays usable
# from a bare console.

$script:IronLogCallback = $null       # param([string]$Level, [string]$Message)
$script:IronProgressCallback = $null  # param([int]$Percent, [string]$Activity)
$script:IronStageCallback = $null     # param([string]$Stage, [string]$Event)

function Set-IronDeployCallbacks {
    param(
        [scriptblock]$OnLog,
        [scriptblock]$OnProgress,
        [scriptblock]$OnStage
    )

    $script:IronLogCallback = $OnLog
    $script:IronProgressCallback = $OnProgress
    $script:IronStageCallback = $OnStage
}

function Write-IronLog {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Message,

        [ValidateSet("info", "ok", "warn", "error", "step")]
        [string]$Level = "info"
    )

    if ($null -ne $script:IronLogCallback) {
        & $script:IronLogCallback $Level $Message
        return
    }

    $color = switch ($Level) {
        "ok" { "Green" }
        "warn" { "Yellow" }
        "error" { "Red" }
        "step" { "Cyan" }
        default { "Gray" }
    }
    Write-Host $Message -ForegroundColor $color
}

function Set-IronProgress {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Percent,

        [Parameter(Mandatory = $true)]
        [string]$Activity
    )

    if ($null -ne $script:IronProgressCallback) {
        & $script:IronProgressCallback $Percent $Activity
    }
}

# Converts the percentage printed by DISM into the image-apply portion of the
# overall deployment progress bar. DISM may use either a dot or a comma as the
# decimal separator, depending on the WinPE locale.
function Update-IronImageApplyProgress {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$OutputLine
    )

    if ($OutputLine -notmatch "(?<Percent>\d{1,3}(?:[.,]\d+)?)\s*%") {
        return
    }

    $ImagePercent = 0.0
    $NormalizedPercent = $Matches.Percent.Replace(",", ".")
    $Parsed = [double]::TryParse(
        $NormalizedPercent,
        [Globalization.NumberStyles]::Float,
        [Globalization.CultureInfo]::InvariantCulture,
        [ref]$ImagePercent
    )
    if (-not $Parsed) {
        return
    }

    $ImagePercent = [Math]::Max(0.0, [Math]::Min(100.0, $ImagePercent))
    $OverallPercent = 30 + [int][Math]::Round($ImagePercent * 0.30)
    Set-IronProgress `
        -Percent $OverallPercent `
        -Activity ("Applying Windows image ({0:0.0}%)" -f $ImagePercent)
}

# --- Configuration -----------------------------------------------------------

$ConfigPath = Join-Path $PSScriptRoot "deploy.config.ps1"
if (!(Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "Deployment configuration is missing: $ConfigPath"
}

. $ConfigPath

$RequiredConfigValues = @(
    "ShareDrive",
    "ApiBaseUrl",
    "ImagesPath",
    "DriversPath",
    "PostInstallPath",
    "ImageIndex"
)

foreach ($ConfigName in $RequiredConfigValues) {
    $ConfigVariable = Get-Variable `
        -Name $ConfigName `
        -ErrorAction SilentlyContinue

    if (
        $null -eq $ConfigVariable -or
        $null -eq $ConfigVariable.Value -or
        (
            $ConfigVariable.Value -is [string] -and
            [string]::IsNullOrWhiteSpace($ConfigVariable.Value)
        )
    ) {
        throw "Deployment configuration value is missing: $ConfigName"
    }
}

if (-not (Get-Variable -Name SetupLocalAdminName -ErrorAction SilentlyContinue)) {
    $SetupLocalAdminName = "localadmin"
}
if (-not (Get-Variable -Name EnableBuiltInAdministrator -ErrorAction SilentlyContinue)) {
    $EnableBuiltInAdministrator = $true
}
if (-not (Get-Variable -Name EnableSetupLocalAdmin -ErrorAction SilentlyContinue)) {
    $LegacyDisableSetupLocalAdmin = Get-Variable `
        -Name DisableSetupLocalAdmin `
        -ErrorAction SilentlyContinue
    if ($null -ne $LegacyDisableSetupLocalAdmin) {
        $EnableSetupLocalAdmin = -not [bool]$LegacyDisableSetupLocalAdmin.Value
    } else {
        $EnableSetupLocalAdmin = $true
    }
}
if (-not (Get-Variable -Name EnableGuiImageApplyProgress -ErrorAction SilentlyContinue)) {
    $EnableGuiImageApplyProgress = $true
}
if (-not (Get-Variable -Name ValidateApiServerCertificate -ErrorAction SilentlyContinue)) {
    # Backward compatibility for existing deploy.config.ps1 files.
    $ValidateApiServerCertificate = $false
}
if (-not (Get-Variable -Name ApiServerCertificateType -ErrorAction SilentlyContinue)) {
    $ApiServerCertificateType = "self_signed"
}
if (-not (Get-Variable -Name ApiServerCertificateBase64 -ErrorAction SilentlyContinue)) {
    $ApiServerCertificateBase64 = ""
}
if ($ApiServerCertificateType -notin @("self_signed", "ca")) {
    throw "ApiServerCertificateType must be self_signed or ca"
}
if ($ValidateApiServerCertificate -and $ApiBaseUrl -notmatch "^https://") {
    throw "API certificate validation requires an https:// ApiBaseUrl"
}
if (
    -not (Get-Variable -Name ProgramsPath -ErrorAction SilentlyContinue) -or
    [string]::IsNullOrWhiteSpace([string]$ProgramsPath)
) {
    # Optional: existing deploy.config.ps1 files may not define it yet.
    # String concatenation, not Join-Path: this runs when the engine is
    # dot-sourced, before the share drive is mapped, and Join-Path refuses
    # drive-qualified paths on drives that do not exist yet.
    $ProgramsPath = "$($ShareDrive.TrimEnd('\'))\Programs"
}

$WindowsDrive = "C:"
$EfiDrive = "S:"
$DiskPartScript = "X:\IronDeploy\diskpart-uefi.txt"
$UnattendTarget = "C:\Windows\Panther\Unattend.xml"

$script:DeploymentId = 0L
$script:CurrentDeploymentStage = $null
$script:DeploymentErrorReported = $false
$script:DeploymentCatalog = $null
$script:DeploymentAccessToken = ""
$script:IronApiRequestSamples = $null
$script:IronNetworkDiagnostics = $null
$script:IronSecretArtifacts = @()

# --- API reporting -----------------------------------------------------------

# IronAPI is always a directly reachable deployment-network service. Avoid
# inherited WinHTTP/IE proxy or WPAD settings inside WinPE.
[System.Net.WebRequest]::DefaultWebProxy = $null

function Install-IronApiTrustedCertificate {
    param(
        [Parameter(Mandatory = $true)]
        [string]$CertificateBase64,

        [Parameter(Mandatory = $true)]
        [ValidateSet("self_signed", "ca")]
        [string]$CertificateType
    )

    if ([string]::IsNullOrWhiteSpace($CertificateBase64)) {
        throw "The trusted IronAPI certificate is missing"
    }

    try {
        $CertificateBytes = [Convert]::FromBase64String($CertificateBase64)
        $Certificate = New-Object `
            System.Security.Cryptography.X509Certificates.X509Certificate2
        $Certificate.Import($CertificateBytes)
    } catch {
        throw "The trusted IronAPI certificate is invalid: $($_.Exception.Message)"
    }

    $Now = Get-Date
    if ($Certificate.NotBefore -gt $Now) {
        throw "The trusted IronAPI certificate is not valid yet"
    }
    if ($Certificate.NotAfter -le $Now) {
        throw "The trusted IronAPI certificate has expired"
    }
    if (
        $CertificateType -eq "self_signed" -and
        $Certificate.Subject -ne $Certificate.Issuer
    ) {
        throw "Self-signed mode requires a self-issued server certificate"
    }

    $Store = New-Object `
        System.Security.Cryptography.X509Certificates.X509Store `
        "Root", `
        ([System.Security.Cryptography.X509Certificates.StoreLocation]::LocalMachine)
    try {
        $Store.Open(
            [System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite
        )
        $Store.Add($Certificate)
    } finally {
        $Store.Close()
    }

    $Thumbprint = $Certificate.Thumbprint
    $Certificate.Dispose()
    return $Thumbprint
}

if ($ApiBaseUrl -match "^https://") {
    # WinPE PowerShell 5.1 may otherwise offer only legacy TLS versions, which
    # modern reverse proxies reject before any HTTP request is sent.
    [System.Net.ServicePointManager]::SecurityProtocol = `
        [System.Net.SecurityProtocolType]::Tls12
    [System.Net.ServicePointManager]::CheckCertificateRevocationList = $false

    if ($ValidateApiServerCertificate) {
        $TrustedCertificateThumbprint = Install-IronApiTrustedCertificate `
            -CertificateBase64 $ApiServerCertificateBase64 `
            -CertificateType $ApiServerCertificateType
        [System.Net.ServicePointManager]::ServerCertificateValidationCallback = $null
        Write-IronLog (
            "[INFO] Trusted IronAPI {0} certificate installed: {1}" -f `
                $ApiServerCertificateType,
                $TrustedCertificateThumbprint
        )
    } else {
        # Do not use a PowerShell scriptblock as the TLS callback.
        # HttpWebRequest may invoke it without a PowerShell runspace.
        if (-not ("IronDeploy.InsecureCertificateValidator" -as [type])) {
        Add-Type -TypeDefinition @'
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;

namespace IronDeploy {
    public static class InsecureCertificateValidator {
        public static readonly RemoteCertificateValidationCallback Callback =
            new RemoteCertificateValidationCallback(Validate);

        private static bool Validate(
            object sender,
            X509Certificate certificate,
            X509Chain chain,
            SslPolicyErrors errors
        ) {
            return true;
        }
    }
}
'@
        }
        [System.Net.ServicePointManager]::ServerCertificateValidationCallback = `
            [IronDeploy.InsecureCertificateValidator]::Callback
    }
}

function Start-IronApiTimingCollection {
    $script:IronApiRequestSamples = New-Object `
        "System.Collections.Concurrent.ConcurrentQueue[object]"
}

function Add-IronApiRequestMeasurement {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Uri,
        [Parameter(Mandatory = $true)]
        [string]$Method,
        [Parameter(Mandatory = $true)]
        [double]$DurationMs,
        [Parameter(Mandatory = $true)]
        [bool]$Success,
        [string]$ErrorMessage
    )

    if ($null -eq $script:IronApiRequestSamples) {
        return
    }
    $script:IronApiRequestSamples.Enqueue([pscustomobject]@{
        TimestampUtc = [DateTime]::UtcNow
        Uri = $Uri
        Method = $Method
        DurationMs = [Math]::Max(0.0, $DurationMs)
        Success = $Success
        ErrorMessage = $ErrorMessage
    })
}

function Invoke-IronApiRestMethod {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Uri,
        [string]$Method = "Get",
        [string]$ContentType,
        [object]$Body,
        [int]$TimeoutSec = 30,
        [switch]$UseBasicParsing,
        [switch]$SkipNetworkTiming
    )

    if ([string]::IsNullOrWhiteSpace($script:DeploymentAccessToken)) {
        throw "WinPE deployment authorization is required"
    }

    $Parameters = @{
        Uri = $Uri
        Method = $Method
        Headers = @{ Authorization = "Bearer $script:DeploymentAccessToken" }
        TimeoutSec = $TimeoutSec
        UseBasicParsing = $true
    }
    if (![string]::IsNullOrWhiteSpace($ContentType)) {
        $Parameters.ContentType = $ContentType
    }
    if ($PSBoundParameters.ContainsKey("Body")) {
        $Parameters.Body = $Body
    }
    $Timer = [Diagnostics.Stopwatch]::StartNew()
    $Success = $false
    $RequestError = $null
    try {
        $Result = Invoke-RestMethod @Parameters
        $Success = $true
        return $Result
    } catch {
        $RequestError = $_.Exception.Message
        throw
    } finally {
        $Timer.Stop()
        if (-not $SkipNetworkTiming) {
            Add-IronApiRequestMeasurement `
                -Uri $Uri `
                -Method $Method `
                -DurationMs $Timer.Elapsed.TotalMilliseconds `
                -Success $Success `
                -ErrorMessage $RequestError
        }
    }
}

function Invoke-IronApiWebRequest {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Uri,
        [string]$Method = "Get",
        [Parameter(Mandatory = $true)]
        [string]$OutFile,
        [int]$TimeoutSec = 30,
        [switch]$UseBasicParsing,
        [switch]$SkipNetworkTiming
    )

    if ([string]::IsNullOrWhiteSpace($script:DeploymentAccessToken)) {
        throw "WinPE deployment authorization is required"
    }

    $Timer = [Diagnostics.Stopwatch]::StartNew()
    $Success = $false
    $RequestError = $null
    try {
        $Result = Invoke-WebRequest `
            -Uri $Uri `
            -Method $Method `
            -Headers @{ Authorization = "Bearer $script:DeploymentAccessToken" } `
            -OutFile $OutFile `
            -TimeoutSec $TimeoutSec `
            -UseBasicParsing
        $Success = $true
        return $Result
    } catch {
        $RequestError = $_.Exception.Message
        throw
    } finally {
        $Timer.Stop()
        if (-not $SkipNetworkTiming) {
            Add-IronApiRequestMeasurement `
                -Uri $Uri `
                -Method $Method `
                -DurationMs $Timer.Elapsed.TotalMilliseconds `
                -Success $Success `
                -ErrorMessage $RequestError
        }
    }
}

function Get-IronSmbServerName {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if ($Path -notmatch '^\\\\(?<Server>\[[^\]]+\]|[^\\]+)\\') {
        throw "SMB share path has no server name: $Path"
    }
    return $Matches.Server.Trim("[", "]")
}

function Get-IronNetworkRouteAdapter {
    param(
        [Parameter(Mandatory = $true)]
        [string]$TargetHost
    )

    $Addresses = @(
        [Net.Dns]::GetHostAddresses($TargetHost) |
            Where-Object {
                $_.AddressFamily -in @(
                    [Net.Sockets.AddressFamily]::InterNetwork,
                    [Net.Sockets.AddressFamily]::InterNetworkV6
                )
            } |
            Sort-Object {
                if (
                    $_.AddressFamily -eq
                    [Net.Sockets.AddressFamily]::InterNetwork
                ) {
                    0
                } else {
                    1
                }
            }
    )
    if ($Addresses.Count -eq 0) {
        throw "DNS returned no usable address for $TargetHost"
    }

    $RouteErrors = @()
    foreach ($Address in $Addresses) {
        $Socket = New-Object Net.Sockets.Socket(
            $Address.AddressFamily,
            [Net.Sockets.SocketType]::Dgram,
            [Net.Sockets.ProtocolType]::Udp
        )
        try {
            # UDP Connect selects a route and local address without sending a
            # packet. Port 9 is not probed and no SMB connectivity check occurs.
            $Socket.Connect(
                (New-Object Net.IPEndPoint($Address, 9))
            )
            $LocalAddress = $Socket.LocalEndPoint.Address
        } catch {
            $RouteErrors += $_.Exception.Message
            continue
        } finally {
            $Socket.Dispose()
        }

        foreach (
            $Adapter in [Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()
        ) {
            $MatchesLocalAddress = @(
                $Adapter.GetIPProperties().UnicastAddresses |
                    Where-Object { $_.Address.Equals($LocalAddress) }
            ).Count -gt 0
            if (-not $MatchesLocalAddress) {
                continue
            }

            $LinkSpeed = $null
            try {
                if ([long]$Adapter.Speed -gt 0) {
                    $LinkSpeed = [long]$Adapter.Speed
                }
            } catch {
            }
            return [pscustomobject]@{
                Name = [string]$Adapter.Name
                Description = [string]$Adapter.Description
                AdapterId = [string]$Adapter.Id
                LocalIp = [string]$LocalAddress
                LinkSpeedBps = $LinkSpeed
                TargetAddress = [string]$Address
            }
        }
        $RouteErrors += "No network adapter owns local address $LocalAddress"
    }

    throw (
        "Failed to identify the route adapter for ${TargetHost}: " +
        ($RouteErrors -join "; ")
    )
}

function Get-IronAdapterBytesReceived {
    param(
        [Parameter(Mandatory = $true)]
        [string]$AdapterId
    )

    $Adapter = @(
        [Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces() |
            Where-Object { $_.Id -eq $AdapterId }
    ) | Select-Object -First 1
    if ($null -eq $Adapter) {
        throw "Network adapter is no longer available: $AdapterId"
    }
    return [long]$Adapter.GetIPStatistics().BytesReceived
}

function Add-IronNetworkDiagnosticError {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    if (
        $null -ne $script:IronNetworkDiagnostics -and
        $null -ne $script:IronNetworkDiagnostics.Errors
    ) {
        $script:IronNetworkDiagnostics.Errors.Enqueue($Message)
    }
    Write-IronLog "[WARN] Network diagnostics: $Message" -Level warn
}

function Start-IronPingMonitor {
    param(
        [Parameter(Mandatory = $true)]
        [string]$TargetHost,
        [Parameter(Mandatory = $true)]
        [object]$SampleQueue,
        [Parameter(Mandatory = $true)]
        [object]$ErrorQueue,
        [int]$TimeoutMs = 800,
        [int]$IntervalMs = 1000
    )

    $StopSignal = New-Object Threading.ManualResetEventSlim($false)
    $Runspace = [runspacefactory]::CreateRunspace()
    $Runspace.ApartmentState = "MTA"
    $Runspace.ThreadOptions = "ReuseThread"
    $Runspace.Open()

    $PowerShell = [powershell]::Create()
    $PowerShell.Runspace = $Runspace
    [void]$PowerShell.AddScript({
        param(
            $Target,
            $Samples,
            $Errors,
            $Stop,
            $PingTimeout,
            $PingInterval
        )

        $Ping = New-Object Net.NetworkInformation.Ping
        $LastErrorMessage = $null
        try {
            while (-not $Stop.IsSet) {
                $IterationTimer = [Diagnostics.Stopwatch]::StartNew()
                $StartedAt = [DateTime]::UtcNow
                $Success = $false
                $LatencyMs = $null
                $PingError = $null
                try {
                    $Reply = $Ping.Send(
                        [string]$Target,
                        [int]$PingTimeout
                    )
                    if (
                        $Reply.Status -eq
                        [Net.NetworkInformation.IPStatus]::Success
                    ) {
                        $Success = $true
                        $LatencyMs = [double]$Reply.RoundtripTime
                    } else {
                        $PingError = [string]$Reply.Status
                    }
                } catch {
                    $PingError = $_.Exception.Message
                    if ($PingError -ne $LastErrorMessage) {
                        $Errors.Enqueue("Ping failed: $PingError")
                        $LastErrorMessage = $PingError
                    }
                }

                $Samples.Enqueue([pscustomobject]@{
                    TimestampUtc = $StartedAt
                    Success = $Success
                    LatencyMs = $LatencyMs
                    ErrorMessage = $PingError
                })
                $IterationTimer.Stop()
                $RemainingMs = [Math]::Max(
                    0,
                    [int]$PingInterval -
                        [int][Math]::Ceiling(
                            $IterationTimer.Elapsed.TotalMilliseconds
                        )
                )
                if ($RemainingMs -gt 0) {
                    [void]$Stop.Wait($RemainingMs)
                }
            }
        } catch {
            $Errors.Enqueue(
                "Ping monitor stopped unexpectedly: $($_.Exception.Message)"
            )
        } finally {
            $Ping.Dispose()
        }
    })
    [void]$PowerShell.AddArgument($TargetHost)
    [void]$PowerShell.AddArgument($SampleQueue)
    [void]$PowerShell.AddArgument($ErrorQueue)
    [void]$PowerShell.AddArgument($StopSignal)
    [void]$PowerShell.AddArgument($TimeoutMs)
    [void]$PowerShell.AddArgument($IntervalMs)

    try {
        $Handle = $PowerShell.BeginInvoke()
    } catch {
        $PowerShell.Dispose()
        $Runspace.Dispose()
        $StopSignal.Dispose()
        throw
    }
    return [pscustomobject]@{
        PowerShell = $PowerShell
        Handle = $Handle
        Runspace = $Runspace
        StopSignal = $StopSignal
        Stopped = $false
    }
}

function Stop-IronPingMonitor {
    param(
        [object]$Monitor,
        [Parameter(Mandatory = $true)]
        [object]$ErrorQueue
    )

    if ($null -eq $Monitor -or [bool]$Monitor.Stopped) {
        return
    }
    try {
        $Monitor.StopSignal.Set()
        if (-not $Monitor.Handle.AsyncWaitHandle.WaitOne(3000)) {
            $ErrorQueue.Enqueue("Ping monitor did not stop within 3 seconds")
            $Monitor.PowerShell.Stop()
        }
        try {
            [void]$Monitor.PowerShell.EndInvoke($Monitor.Handle)
        } catch {
            $ErrorQueue.Enqueue(
                "Failed to finish ping monitor: $($_.Exception.Message)"
            )
        }
    } finally {
        try { $Monitor.PowerShell.Dispose() } catch {}
        try { $Monitor.Runspace.Dispose() } catch {}
        try { $Monitor.StopSignal.Dispose() } catch {}
        $Monitor.PowerShell = $null
        $Monitor.Handle = $null
        $Monitor.Runspace = $null
        $Monitor.StopSignal = $null
        $Monitor.Stopped = $true
    }
}

function Get-IronNetworkPingStatistics {
    param(
        [object[]]$Samples,
        [Parameter(Mandatory = $true)]
        [DateTime]$StartedAt,
        [Parameter(Mandatory = $true)]
        [DateTime]$CompletedAt
    )

    $Selected = @(
        $Samples | Where-Object {
            $_.TimestampUtc -ge $StartedAt -and
            $_.TimestampUtc -le $CompletedAt
        }
    )
    $Replies = @($Selected | Where-Object { [bool]$_.Success })
    $Sent = $Selected.Count
    $Received = $Replies.Count
    $Lost = $Sent - $Received
    $Status = if ($Sent -eq 0) {
        "not_measured"
    } elseif ($Received -eq 0) {
        "unavailable"
    } else {
        "available"
    }
    $LossPercentage = if ($Status -eq "available") {
        [Math]::Round(($Lost * 100.0) / $Sent, 3)
    } else {
        $null
    }
    $Latencies = @($Replies | ForEach-Object { [double]$_.LatencyMs })
    $LatencyMeasurement = if ($Latencies.Count -gt 0) {
        $Latencies | Measure-Object -Minimum -Average -Maximum
    } else {
        $null
    }

    return [pscustomobject]@{
        icmp_status = $Status
        ping_sent = $Sent
        ping_received = $Received
        ping_lost = $Lost
        loss_percentage = $LossPercentage
        rtt_min_ms = if ($null -ne $LatencyMeasurement) {
            [Math]::Round([double]$LatencyMeasurement.Minimum, 3)
        } else { $null }
        rtt_avg_ms = if ($null -ne $LatencyMeasurement) {
            [Math]::Round([double]$LatencyMeasurement.Average, 3)
        } else { $null }
        rtt_max_ms = if ($null -ne $LatencyMeasurement) {
            [Math]::Round([double]$LatencyMeasurement.Maximum, 3)
        } else { $null }
        latency_spikes = @(
            $Latencies | Where-Object { $_ -gt 50.0 }
        ).Count
    }
}

function New-IronNetworkAggregate {
    param(
        [object[]]$Samples,
        [Parameter(Mandatory = $true)]
        [DateTime]$StartedAt,
        [Parameter(Mandatory = $true)]
        [DateTime]$CompletedAt,
        [object]$BytesBefore,
        [object]$BytesAfter,
        [object]$LinkSpeedBps
    )

    $DurationSeconds = [Math]::Max(
        0.0,
        ($CompletedAt - $StartedAt).TotalSeconds
    )
    $Ping = Get-IronNetworkPingStatistics `
        -Samples $Samples `
        -StartedAt $StartedAt `
        -CompletedAt $CompletedAt
    $BytesReceived = $null
    $AverageInboundMbps = $null
    $LinkUtilization = $null
    if (
        $null -ne $BytesBefore -and
        $null -ne $BytesAfter -and
        [long]$BytesAfter -ge [long]$BytesBefore
    ) {
        $BytesReceived = [long](
            [long]$BytesAfter - [long]$BytesBefore
        )
        if ($DurationSeconds -gt 0) {
            $AverageInboundMbps = [Math]::Round(
                ($BytesReceived / $DurationSeconds) / 1MB,
                3
            )
            if ($null -ne $LinkSpeedBps -and [long]$LinkSpeedBps -gt 0) {
                $LinkUtilization = [Math]::Round(
                    (
                        ($BytesReceived * 8.0) /
                        $DurationSeconds /
                        [long]$LinkSpeedBps
                    ) * 100.0,
                    3
                )
            }
        }
    }

    return [ordered]@{
        started_at = $StartedAt.ToString("o")
        completed_at = $CompletedAt.ToString("o")
        duration_seconds = [Math]::Round($DurationSeconds, 3)
        icmp_status = $Ping.icmp_status
        ping_sent = $Ping.ping_sent
        ping_received = $Ping.ping_received
        ping_lost = $Ping.ping_lost
        loss_percentage = $Ping.loss_percentage
        rtt_min_ms = $Ping.rtt_min_ms
        rtt_avg_ms = $Ping.rtt_avg_ms
        rtt_max_ms = $Ping.rtt_max_ms
        latency_spikes = $Ping.latency_spikes
        bytes_received = $BytesReceived
        average_inbound_mbps = $AverageInboundMbps
        link_utilization_percent = $LinkUtilization
    }
}

function Get-IronAdapterReport {
    param([object]$Adapter)

    if ($null -eq $Adapter) {
        return $null
    }
    return [ordered]@{
        name = $Adapter.Name
        description = $Adapter.Description
        adapter_id = $Adapter.AdapterId
        local_ip = $Adapter.LocalIp
        link_speed_bps = $Adapter.LinkSpeedBps
    }
}

function Start-IronNetworkDiagnostics {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SharePath
    )

    if ($null -ne $script:IronNetworkDiagnostics) {
        return
    }
    $Samples = New-Object `
        "System.Collections.Concurrent.ConcurrentQueue[object]"
    $Errors = New-Object `
        "System.Collections.Concurrent.ConcurrentQueue[string]"
    $StartedAt = [DateTime]::UtcNow
    $SmbHost = Get-IronSmbServerName -Path $SharePath
    $ApiHost = ([Uri]$ApiBaseUrl).DnsSafeHost
    $SmbAdapter = $null
    $ApiAdapter = $null

    try {
        $SmbAdapter = Get-IronNetworkRouteAdapter -TargetHost $SmbHost
    } catch {
        $Errors.Enqueue(
            "Failed to identify SMB route adapter: $($_.Exception.Message)"
        )
    }
    try {
        $ApiAdapter = Get-IronNetworkRouteAdapter -TargetHost $ApiHost
    } catch {
        $Errors.Enqueue(
            "Failed to identify IronAPI route adapter: $($_.Exception.Message)"
        )
    }
    if (
        $null -ne $SmbAdapter -and
        $null -eq $SmbAdapter.LinkSpeedBps
    ) {
        $Errors.Enqueue("SMB adapter link speed is unavailable")
    }
    if (
        $null -ne $ApiAdapter -and
        $null -eq $ApiAdapter.LinkSpeedBps
    ) {
        $Errors.Enqueue("IronAPI adapter link speed is unavailable")
    }

    $BytesBefore = $null
    if ($null -ne $SmbAdapter) {
        try {
            $BytesBefore = Get-IronAdapterBytesReceived `
                -AdapterId $SmbAdapter.AdapterId
        } catch {
            $Errors.Enqueue(
                "Failed to read initial receive counter: $($_.Exception.Message)"
            )
        }
    }
    $Context = [pscustomobject]@{
        StartedAtUtc = $StartedAt
        CompletedAtUtc = $null
        PingTarget = $SmbHost
        Samples = $Samples
        Errors = $Errors
        PingMonitor = $null
        SmbAdapter = $SmbAdapter
        ApiAdapter = $ApiAdapter
        AdaptersDiffer = (
            $null -ne $SmbAdapter -and
            $null -ne $ApiAdapter -and
            $SmbAdapter.AdapterId -ne $ApiAdapter.AdapterId
        )
        BytesBefore = $BytesBefore
        BytesAfter = $null
        StageWindows = @{}
        SmbSuccess = $null
        SmbAttempts = 0
        SmbDurationMs = $null
        SmbErrorMessage = $null
        FinalReport = $null
        ReportSent = $false
        Stopped = $false
    }
    $script:IronNetworkDiagnostics = $Context

    try {
        $Context.PingMonitor = Start-IronPingMonitor `
            -TargetHost $SmbHost `
            -SampleQueue $Samples `
            -ErrorQueue $Errors `
            -TimeoutMs 800 `
            -IntervalMs 1000
        Write-IronLog (
            "[INFO] Network diagnostics started for SMB server {0}" -f $SmbHost
        ) -Level info
    } catch {
        Add-IronNetworkDiagnosticError (
            "Failed to start ping monitor: $($_.Exception.Message)"
        )
    }
}

function Start-IronNetworkStageMeasurement {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Stage
    )

    if (
        $null -eq $script:IronNetworkDiagnostics -or
        $Stage -notin @(
            "image_apply",
            "driver_injection",
            "postinstall_copy"
        )
    ) {
        return
    }
    $BytesBefore = $null
    if ($null -ne $script:IronNetworkDiagnostics.SmbAdapter) {
        try {
            $BytesBefore = Get-IronAdapterBytesReceived `
                -AdapterId $script:IronNetworkDiagnostics.SmbAdapter.AdapterId
        } catch {
            Add-IronNetworkDiagnosticError (
                "Failed to read receive counter at start of ${Stage}: " +
                $_.Exception.Message
            )
        }
    }
    $script:IronNetworkDiagnostics.StageWindows[$Stage] = [pscustomobject]@{
        Stage = $Stage
        StartedAtUtc = [DateTime]::UtcNow
        CompletedAtUtc = $null
        BytesBefore = $BytesBefore
        BytesAfter = $null
    }
}

function Complete-IronNetworkStageMeasurement {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Stage
    )

    if (
        $null -eq $script:IronNetworkDiagnostics -or
        -not $script:IronNetworkDiagnostics.StageWindows.ContainsKey($Stage)
    ) {
        return
    }
    $Window = $script:IronNetworkDiagnostics.StageWindows[$Stage]
    if ($null -ne $Window.CompletedAtUtc) {
        return
    }
    $Window.CompletedAtUtc = [DateTime]::UtcNow
    if ($null -ne $script:IronNetworkDiagnostics.SmbAdapter) {
        try {
            $Window.BytesAfter = Get-IronAdapterBytesReceived `
                -AdapterId $script:IronNetworkDiagnostics.SmbAdapter.AdapterId
        } catch {
            Add-IronNetworkDiagnosticError (
                "Failed to read receive counter at end of ${Stage}: " +
                $_.Exception.Message
            )
        }
    }
}

function Set-IronNetworkSmbResult {
    param(
        [Parameter(Mandatory = $true)]
        [bool]$Success,
        [Parameter(Mandatory = $true)]
        [int]$Attempts,
        [Parameter(Mandatory = $true)]
        [double]$DurationMs,
        [string]$ErrorMessage
    )

    if ($null -eq $script:IronNetworkDiagnostics) {
        return
    }
    $script:IronNetworkDiagnostics.SmbSuccess = $Success
    $script:IronNetworkDiagnostics.SmbAttempts = $Attempts
    $script:IronNetworkDiagnostics.SmbDurationMs = [Math]::Max(0, $DurationMs)
    $script:IronNetworkDiagnostics.SmbErrorMessage = $ErrorMessage
}

function Get-IronApiAggregate {
    $Samples = if ($null -ne $script:IronApiRequestSamples) {
        @($script:IronApiRequestSamples.ToArray())
    } else {
        @()
    }
    $Successful = @($Samples | Where-Object { [bool]$_.Success })
    $Measurement = if ($Successful.Count -gt 0) {
        $Successful |
            Measure-Object -Property DurationMs -Minimum -Average -Maximum
    } else {
        $null
    }
    return [ordered]@{
        request_count = $Samples.Count
        error_count = @(
            $Samples | Where-Object { -not [bool]$_.Success }
        ).Count
        min_ms = if ($null -ne $Measurement) {
            [Math]::Round([double]$Measurement.Minimum, 3)
        } else { $null }
        avg_ms = if ($null -ne $Measurement) {
            [Math]::Round([double]$Measurement.Average, 3)
        } else { $null }
        max_ms = if ($null -ne $Measurement) {
            [Math]::Round([double]$Measurement.Maximum, 3)
        } else { $null }
    }
}

function Complete-IronNetworkDiagnostics {
    param([switch]$Send)

    if ($null -eq $script:IronNetworkDiagnostics) {
        return
    }
    $Context = $script:IronNetworkDiagnostics
    try {
        if (-not $Context.Stopped) {
            $CompletedAt = [DateTime]::UtcNow
            foreach ($Window in $Context.StageWindows.Values) {
                if ($null -eq $Window.CompletedAtUtc) {
                    $Window.CompletedAtUtc = $CompletedAt
                    if ($null -ne $Context.SmbAdapter) {
                        try {
                            $Window.BytesAfter = Get-IronAdapterBytesReceived `
                                -AdapterId $Context.SmbAdapter.AdapterId
                        } catch {
                            $Context.Errors.Enqueue(
                                "Failed to read final receive counter for " +
                                "$($Window.Stage): $($_.Exception.Message)"
                            )
                        }
                    }
                }
            }
            Stop-IronPingMonitor `
                -Monitor $Context.PingMonitor `
                -ErrorQueue $Context.Errors
            $Context.CompletedAtUtc = $CompletedAt
            if ($null -ne $Context.SmbAdapter) {
                try {
                    $Context.BytesAfter = Get-IronAdapterBytesReceived `
                        -AdapterId $Context.SmbAdapter.AdapterId
                } catch {
                    $Context.Errors.Enqueue(
                        "Failed to read final receive counter: " +
                        $_.Exception.Message
                    )
                }
            }

            $PingSamples = @($Context.Samples.ToArray())
            $LinkSpeedBps = if ($null -ne $Context.SmbAdapter) {
                $Context.SmbAdapter.LinkSpeedBps
            } else {
                $null
            }
            $Overall = New-IronNetworkAggregate `
                -Samples $PingSamples `
                -StartedAt $Context.StartedAtUtc `
                -CompletedAt $Context.CompletedAtUtc `
                -BytesBefore $Context.BytesBefore `
                -BytesAfter $Context.BytesAfter `
                -LinkSpeedBps $LinkSpeedBps
            $StageReports = @()
            foreach ($StageName in @(
                "image_apply",
                "driver_injection",
                "postinstall_copy"
            )) {
                if (-not $Context.StageWindows.ContainsKey($StageName)) {
                    continue
                }
                $Window = $Context.StageWindows[$StageName]
                $Aggregate = New-IronNetworkAggregate `
                    -Samples $PingSamples `
                    -StartedAt $Window.StartedAtUtc `
                    -CompletedAt $Window.CompletedAtUtc `
                    -BytesBefore $Window.BytesBefore `
                    -BytesAfter $Window.BytesAfter `
                    -LinkSpeedBps $LinkSpeedBps
                $Aggregate["stage"] = $StageName
                $StageReports += $Aggregate
            }
            $DiagnosticErrors = @(
                $Context.Errors.ToArray() |
                    ForEach-Object { [string]$_ } |
                    Select-Object -Unique -First 100
            )
            $Context.FinalReport = [ordered]@{
                ping_target = $Context.PingTarget
                smb_adapter = (
                    Get-IronAdapterReport -Adapter $Context.SmbAdapter
                )
                api_adapter = (
                    Get-IronAdapterReport -Adapter $Context.ApiAdapter
                )
                adapters_differ = [bool]$Context.AdaptersDiffer
                overall = $Overall
                stages = @($StageReports)
                api = (Get-IronApiAggregate)
                smb = [ordered]@{
                    success = $Context.SmbSuccess
                    attempts = [int]$Context.SmbAttempts
                    duration_ms = $Context.SmbDurationMs
                    error_message = $Context.SmbErrorMessage
                }
                diagnostic_errors = @($DiagnosticErrors)
            }
            $Context.Stopped = $true
            Write-IronLog "[INFO] Network diagnostics stopped" -Level info
        }

        if (
            $Send -and
            -not $Context.ReportSent -and
            $script:DeploymentId -gt 0 -and
            $null -ne $Context.FinalReport
        ) {
            try {
                Invoke-IronApiRestMethod `
                    -Uri (
                        "$($ApiBaseUrl.TrimEnd('/'))/api/deploy/" +
                        "$script:DeploymentId/network-diagnostics"
                    ) `
                    -Method Put `
                    -ContentType "application/json; charset=utf-8" `
                    -Body (
                        $Context.FinalReport |
                            ConvertTo-Json -Depth 8 -Compress
                    ) `
                    -TimeoutSec 10 `
                    -SkipNetworkTiming |
                    Out-Null
                $Context.ReportSent = $true
                Write-IronLog (
                    "[OK] Network diagnostics reported for deployment #{0}" -f
                    $script:DeploymentId
                ) -Level ok
            } catch {
                Write-IronLog (
                    "[WARN] Failed to report network diagnostics: {0}" -f
                    $_.Exception.Message
                ) -Level warn
            }
        }
    } catch {
        Write-IronLog (
            "[WARN] Network diagnostics cleanup failed: {0}" -f
            $_.Exception.Message
        ) -Level warn
    }
}

function Set-IronDeploymentAuthorization {
    param(
        [Parameter(Mandatory = $true)]
        [string]$AccessToken
    )

    if ([string]::IsNullOrWhiteSpace($AccessToken)) {
        throw "Deployment access token is empty"
    }
    $script:DeploymentAccessToken = $AccessToken
}

function Get-IronDeploymentAuthorizationPolicy {
    $Response = Invoke-RestMethod `
        -Uri "$($ApiBaseUrl.TrimEnd('/'))/api/deploy/auth/policy" `
        -Method Get `
        -TimeoutSec 30 `
        -UseBasicParsing
    $Mode = [string]$Response.mode
    if ($Mode -notin @("account", "pin", "none")) {
        throw "IronAPI returned an unsupported WinPE authorization mode"
    }
    return [pscustomobject]@{
        Mode = $Mode
        PinConfigured = [bool]$Response.pin_configured
        PinMinLength = [int]$Response.pin_min_length
        PinMaxLength = [int]$Response.pin_max_length
    }
}

function New-IronDeploymentAuthorization {
    param(
        [string]$Username,
        [string]$Password,
        [string]$Pin,
        [ValidateSet("account", "pin", "none")]
        [string]$Mode = "account"
    )

    if ($Mode -eq "account") {
        $Uri = "$($ApiBaseUrl.TrimEnd('/'))/api/deploy/auth/login"
        $Payload = @{
            username = $Username
            password = $Password
        } | ConvertTo-Json
    } else {
        $Uri = "$($ApiBaseUrl.TrimEnd('/'))/api/deploy/auth/authorize"
        $PayloadData = @{ mode = $Mode }
        if ($Mode -eq "pin") {
            $PayloadData.pin = $Pin
        }
        $Payload = $PayloadData | ConvertTo-Json
    }
    try {
        $Response = Invoke-RestMethod `
            -Uri $Uri `
            -Method Post `
            -ContentType "application/json; charset=utf-8" `
            -Body $Payload `
            -TimeoutSec 30 `
            -UseBasicParsing
    } finally {
        $Payload = $null
        $Password = $null
        $Pin = $null
    }

    $AccessToken = [string]$Response.access_token
    if ([string]::IsNullOrWhiteSpace($AccessToken)) {
        throw "IronAPI returned no deployment access token"
    }
    Set-IronDeploymentAuthorization -AccessToken $AccessToken
    return [pscustomobject]@{
        AccessToken = $AccessToken
        AuthorizationId = [long]$Response.authorization_id
        ExpiresAt = [string]$Response.expires_at
    }
}

function Get-IronApiErrorDetails {
    param([Parameter(Mandatory = $true)][System.Exception]$Exception)

    $Messages = New-Object System.Collections.Generic.List[string]
    $Current = $Exception
    while ($null -ne $Current) {
        if (![string]::IsNullOrWhiteSpace($Current.Message)) {
            $Messages.Add($Current.Message)
        }
        $Current = $Current.InnerException
    }
    return ($Messages | Select-Object -Unique) -join " -> "
}

function Send-DeploymentStageEvent {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Stage,

        [Parameter(Mandatory = $true)]
        [ValidateSet("start", "complete", "fail", "skip")]
        [string]$Event
    )

    if ($null -ne $script:IronStageCallback) {
        try {
            & $script:IronStageCallback $Stage $Event
        } catch {
        }
    }

    if ($script:DeploymentId -le 0) {
        return
    }

    $StageUrl = (
        "{0}/api/deploy/{1}/stages/{2}/{3}" -f `
            $ApiBaseUrl.TrimEnd("/"),
            $script:DeploymentId,
            $Stage,
            $Event
    )

    try {
        Invoke-IronApiRestMethod `
            -Uri $StageUrl `
            -Method Post `
            -TimeoutSec 3 `
            -UseBasicParsing |
            Out-Null
    } catch {
        Write-IronLog (
            "[WARN] Failed to report stage '{0}' event '{1}': {2}" -f `
                $Stage,
                $Event,
                $_.Exception.Message
        ) -Level warn
    }
}

function Start-DeploymentStage {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Stage
    )

    $script:CurrentDeploymentStage = $Stage
    Send-DeploymentStageEvent -Stage $Stage -Event "start"
    Start-IronNetworkStageMeasurement -Stage $Stage
}

function Complete-DeploymentStage {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Stage
    )

    Complete-IronNetworkStageMeasurement -Stage $Stage
    Send-DeploymentStageEvent -Stage $Stage -Event "complete"
    if ($script:CurrentDeploymentStage -eq $Stage) {
        $script:CurrentDeploymentStage = $null
    }
}

function Skip-DeploymentStage {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Stage
    )

    Send-DeploymentStageEvent -Stage $Stage -Event "skip"
}

function Send-DeploymentError {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    Complete-IronNetworkDiagnostics -Send

    if (
        $script:DeploymentId -le 0 -or
        $script:DeploymentErrorReported
    ) {
        return
    }

    $ErrorPayload = @{
        message = $Message
    }
    if (![string]::IsNullOrWhiteSpace($script:CurrentDeploymentStage)) {
        $ErrorPayload.stage = $script:CurrentDeploymentStage
    }

    try {
        Invoke-IronApiRestMethod `
            -Uri "$($ApiBaseUrl.TrimEnd('/'))/api/deploy/$script:DeploymentId/error" `
            -Method Post `
            -ContentType "application/json; charset=utf-8" `
            -Body ($ErrorPayload | ConvertTo-Json) `
            -TimeoutSec 5 `
            -UseBasicParsing |
            Out-Null
        $script:DeploymentErrorReported = $true
    } catch {
        Write-IronLog (
            "[WARN] Failed to report deployment error: {0}" -f `
                $_.Exception.Message
        ) -Level warn
    }
}

# --- Secret artifacts --------------------------------------------------------

# Files holding domain secrets (ODJ blobs and the unattend that embeds one) live
# on the WinPE ramdisk. Registering them here guarantees they are shredded on
# every exit path, not only on the ones that happen to remember.
function Register-IronSecretArtifact {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return
    }
    if ($script:IronSecretArtifacts -notcontains $Path) {
        $script:IronSecretArtifacts += $Path
    }
}

function Clear-IronSecretArtifacts {
    foreach ($ArtifactPath in @($script:IronSecretArtifacts)) {
        try {
            Remove-Item -LiteralPath $ArtifactPath -Force -ErrorAction SilentlyContinue
        } catch {
            # Cleanup is best effort: never mask the failure that triggered it.
        }
    }
    $script:IronSecretArtifacts = @()
}

# Terminal failure. Reports the error to IronAPI (best effort) and throws so
# the active front-end can present a failure screen. Unlike the old console
# flow, the engine never prompts or exits the process here.
function Fail {
    param(
        [Parameter(Mandatory = $true)]
        [string]$msg
    )

    Clear-IronSecretArtifacts
    Send-DeploymentError -Message $msg
    $script:CurrentDeploymentStage = $null
    Write-IronLog "ERROR: $msg" -Level error
    throw [System.Exception]::new($msg)
}

# --- Helpers -----------------------------------------------------------------

function ConvertTo-PowerShellLiteral {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    return "'" + $Value.Replace("'", "''") + "'"
}

function ConvertTo-PowerShellBooleanLiteral {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Value
    )

    if ($Value -is [bool]) {
        if ($Value) {
            return '$true'
        }
        return '$false'
    }

    if ([string]$Value -match '^(?i:true|1|yes|y|on)$') {
        return '$true'
    }
    return '$false'
}

function Read-OfflineDomainJoinBlob {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BlobPath
    )

    $BlobBytes = [IO.File]::ReadAllBytes($BlobPath)
    if ($BlobBytes.Length -eq 0) {
        Fail "Offline Domain Join blob is empty"
    }

    if (
        $BlobBytes.Length -ge 2 -and
        $BlobBytes[0] -eq 0xFF -and
        $BlobBytes[1] -eq 0xFE
    ) {
        $AccountData = [Text.Encoding]::Unicode.GetString(
            $BlobBytes,
            2,
            $BlobBytes.Length - 2
        )
    }
    elseif (
        $BlobBytes.Length -ge 2 -and
        $BlobBytes[0] -eq 0xFE -and
        $BlobBytes[1] -eq 0xFF
    ) {
        $AccountData = [Text.Encoding]::BigEndianUnicode.GetString(
            $BlobBytes,
            2,
            $BlobBytes.Length - 2
        )
    }
    elseif (
        $BlobBytes.Length -ge 3 -and
        $BlobBytes[0] -eq 0xEF -and
        $BlobBytes[1] -eq 0xBB -and
        $BlobBytes[2] -eq 0xBF
    ) {
        $AccountData = [Text.Encoding]::UTF8.GetString(
            $BlobBytes,
            3,
            $BlobBytes.Length - 3
        )
    }
    else {
        $OddNullBytes = 0
        $EvenNullBytes = 0
        for ($Index = 0; $Index -lt $BlobBytes.Length; $Index++) {
            if ($BlobBytes[$Index] -eq 0) {
                if (($Index % 2) -eq 0) {
                    $EvenNullBytes++
                }
                else {
                    $OddNullBytes++
                }
            }
        }

        $HalfLength = [Math]::Floor($BlobBytes.Length / 2)
        $Utf16Threshold = [Math]::Max(1, [int]($HalfLength * 0.25))
        if ($HalfLength -gt 0 -and $OddNullBytes -ge $Utf16Threshold) {
            $AccountData = [Text.Encoding]::Unicode.GetString($BlobBytes)
        }
        elseif ($HalfLength -gt 0 -and $EvenNullBytes -ge $Utf16Threshold) {
            $AccountData = [Text.Encoding]::BigEndianUnicode.GetString($BlobBytes)
        }
        else {
            $AccountData = [Text.Encoding]::UTF8.GetString($BlobBytes)
        }
    }

    $AccountData = $AccountData.Replace([string][char]0, "").Trim()
    if ([string]::IsNullOrWhiteSpace($AccountData)) {
        Fail "Offline Domain Join blob is empty"
    }

    return $AccountData
}

function New-OfflineDomainJoinUnattend {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BlobPath,

        [Parameter(Mandatory = $true)]
        [string]$OutputPath
    )

    $AccountData = Read-OfflineDomainJoinBlob -BlobPath $BlobPath

    $UnattendNamespace = "urn:schemas-microsoft-com:unattend"
    $XmlDocument = New-Object System.Xml.XmlDocument
    $XmlDocument.PreserveWhitespace = $false

    $Declaration = $XmlDocument.CreateXmlDeclaration("1.0", "utf-8", $null)
    [void]$XmlDocument.AppendChild($Declaration)

    $UnattendNode = $XmlDocument.CreateElement("unattend", $UnattendNamespace)
    [void]$XmlDocument.AppendChild($UnattendNode)

    $SettingsNode = $XmlDocument.CreateElement("settings", $UnattendNamespace)
    $SettingsNode.SetAttribute("pass", "offlineServicing")
    [void]$UnattendNode.AppendChild($SettingsNode)

    $ComponentNode = $XmlDocument.CreateElement("component", $UnattendNamespace)
    $ComponentNode.SetAttribute("name", "Microsoft-Windows-UnattendedJoin")
    $ComponentNode.SetAttribute("processorArchitecture", "amd64")
    $ComponentNode.SetAttribute("publicKeyToken", "31bf3856ad364e35")
    $ComponentNode.SetAttribute("language", "neutral")
    $ComponentNode.SetAttribute("versionScope", "nonSxS")
    [void]$SettingsNode.AppendChild($ComponentNode)

    $OfflineIdentificationNode = $XmlDocument.CreateElement(
        "OfflineIdentification",
        $UnattendNamespace
    )
    [void]$ComponentNode.AppendChild($OfflineIdentificationNode)

    $ProvisioningNode = $XmlDocument.CreateElement(
        "Provisioning",
        $UnattendNamespace
    )
    [void]$OfflineIdentificationNode.AppendChild($ProvisioningNode)

    $AccountDataNode = $XmlDocument.CreateElement(
        "AccountData",
        $UnattendNamespace
    )
    $AccountDataNode.InnerText = $AccountData
    [void]$ProvisioningNode.AppendChild($AccountDataNode)

    $XmlSettings = New-Object System.Xml.XmlWriterSettings
    $XmlSettings.Encoding = New-Object System.Text.UTF8Encoding($false)
    $XmlSettings.Indent = $true

    $XmlWriter = [System.Xml.XmlWriter]::Create($OutputPath, $XmlSettings)
    try {
        $XmlDocument.Save($XmlWriter)
    } finally {
        $XmlWriter.Close()
    }
}

function Get-PrimaryMacAddress {
    try {
        $NetworkAdapters = @(
            Get-CimInstance `
                -ClassName Win32_NetworkAdapterConfiguration `
                -ErrorAction Stop |
            Where-Object {
                $_.IPEnabled -and ![string]::IsNullOrWhiteSpace($_.MACAddress)
            }
        )
    } catch {
        Fail "Failed to inspect network adapters: $($_.Exception.Message)"
    }

    if ($NetworkAdapters.Count -eq 0) {
        Fail "No active network adapter with a MAC address was found"
    }

    foreach ($Adapter in $NetworkAdapters) {
        foreach ($Address in @($Adapter.IPAddress)) {
            if ($Address -match "^172\.16\.9\.") {
                return [string]$Adapter.MACAddress
            }
        }
    }

    return [string]$NetworkAdapters[0].MACAddress
}

function Test-UsableSerialNumber($SerialNumber) {
    if ([string]::IsNullOrWhiteSpace([string]$SerialNumber)) {
        return $false
    }

    $NormalizedSerial = ([string]$SerialNumber).Trim()
    return $NormalizedSerial -notmatch (
        "^(To Be Filled By O\.E\.M\.|Default string|" +
        "System Serial Number|Unknown|None)$"
    )
}

function Test-UsableSystemModel($Model) {
    if ([string]::IsNullOrWhiteSpace([string]$Model)) {
        return $false
    }

    $NormalizedModel = ([string]$Model).Trim()
    return $NormalizedModel -notmatch (
        "^(To Be Filled By O\.E\.M\.|Default string|System Product Name|" +
        "System Version|System Name|Unknown|None|Not Applicable|" +
        "Not Specified|OEM|INVALID)$"
    )
}

# Lenovo reports the sales article ("20XW00A6US") in Win32_ComputerSystem.Model
# and the readable name ("ThinkPad T14 Gen 2") in Win32_ComputerSystemProduct.
# Version, so an article-shaped value is treated as a weaker candidate.
function Test-ArticleShapedModel($Model) {
    $NormalizedModel = ([string]$Model).Trim()
    return $NormalizedModel -match "^[0-9]{2}[0-9A-Z]{4,10}$"
}

# Best-effort hardware model. Unlike the serial number this never fails the
# deployment: IronAPI accepts a missing model and the dashboard shows a dash.
function Get-SystemModel {
    $Candidates = @()

    try {
        $ComputerSystem = Get-CimInstance `
            -ClassName Win32_ComputerSystem `
            -ErrorAction Stop
        $Candidates += [string]$ComputerSystem.Model
    } catch {
        Write-IronLog (
            "[WARN] Failed to read Win32_ComputerSystem: $($_.Exception.Message)"
        ) -Level warn
    }

    try {
        $Product = Get-CimInstance `
            -ClassName Win32_ComputerSystemProduct `
            -ErrorAction Stop
        $Candidates += [string]$Product.Version
        $Candidates += [string]$Product.Name
    } catch {
        Write-IronLog (
            "[WARN] Failed to read Win32_ComputerSystemProduct: " +
            "$($_.Exception.Message)"
        ) -Level warn
    }

    try {
        $BaseBoard = Get-CimInstance `
            -ClassName Win32_BaseBoard `
            -ErrorAction Stop
        $Candidates += [string]$BaseBoard.Product
    } catch {
        Write-IronLog (
            "[WARN] Failed to read Win32_BaseBoard: $($_.Exception.Message)"
        ) -Level warn
    }

    $Usable = @(
        $Candidates | Where-Object { Test-UsableSystemModel $_ } | ForEach-Object {
            ([string]$_).Trim()
        }
    )
    if ($Usable.Count -eq 0) {
        Write-IronLog "[WARN] No usable hardware model reported by WMI" -Level warn
        return $null
    }

    $Readable = @($Usable | Where-Object { -not (Test-ArticleShapedModel $_) })
    $Model = if ($Readable.Count -gt 0) { $Readable[0] } else { $Usable[0] }

    if ($Model.Length -gt 128) {
        $Model = $Model.Substring(0, 128).Trim()
    }
    return $Model
}

function Get-SystemSerialNumber {
    $BiosError = $null
    $ProductError = $null

    try {
        $Bios = Get-CimInstance `
            -ClassName Win32_BIOS `
            -ErrorAction Stop
        if (Test-UsableSerialNumber $Bios.SerialNumber) {
            return ([string]$Bios.SerialNumber).Trim()
        }
    } catch {
        $BiosError = $_.Exception.Message
    }

    try {
        $Product = Get-CimInstance `
            -ClassName Win32_ComputerSystemProduct `
            -ErrorAction Stop
        if (Test-UsableSerialNumber $Product.IdentifyingNumber) {
            return ([string]$Product.IdentifyingNumber).Trim()
        }
    } catch {
        $ProductError = $_.Exception.Message
    }

    $Details = @($BiosError, $ProductError) |
        Where-Object { ![string]::IsNullOrWhiteSpace($_) }
    if ($Details.Count -gt 0) {
        Fail "Failed to read system serial number: $($Details -join '; ')"
    }

    Fail "BIOS did not provide a usable system serial number"
}

# --- Input gathering (used by both front-ends before deployment) -------------

# Reads the hardware identity used to register the deployment. Throws (via
# Fail) when no usable adapter or serial number is present.
function Get-IronDeployHardwareIdentity {
    $MacAddress = Get-PrimaryMacAddress
    $SerialNumber = Get-SystemSerialNumber
    $Model = Get-SystemModel

    return [pscustomobject]@{
        SerialNumber = $SerialNumber
        MacAddress = $MacAddress
        Model = $Model
    }
}

# Best-effort computer-name suggestion from IronAPI. Never throws: when IronAPI
# is unavailable it returns null suggestions and an empty known-name list, so a
# name can still be entered manually.
function Get-IronDeployNameSuggestion {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SerialNumber,

        [Parameter(Mandatory = $true)]
        [string]$MacAddress
    )

    $LastDomainName = $null
    $SuggestedName = $null
    $KnownComputerNames = @()

    try {
        $SuggestionQuery = @(
            "serial_number=$([uri]::EscapeDataString($SerialNumber))",
            "mac_address=$([uri]::EscapeDataString($MacAddress))"
        ) -join "&"
        $NameResponse = Invoke-IronApiRestMethod `
            -Uri "$($ApiBaseUrl.TrimEnd('/'))/api/deploy/suggest-name?$SuggestionQuery" `
            -Method Get `
            -TimeoutSec 10 `
            -UseBasicParsing

        $LastDomainName = [string]$NameResponse.last_domain_name
        $SuggestedName = [string]$NameResponse.suggested_name
        $KnownComputerNames = @($NameResponse.known_computer_names) |
            Where-Object { $null -ne $_ }
    } catch {
        Write-IronLog "[WARN] IronAPI unavailable: $($_.Exception.Message)" -Level warn
        $KnownComputerNames = @()
    }

    return [pscustomobject]@{
        LastDomainName = $LastDomainName
        SuggestedName = $SuggestedName
        KnownComputerNames = @($KnownComputerNames)
    }
}

# Validates a computer name. Returns the normalised (lower-case) name or throws
# a plain error when the format is wrong. Format: pc + 5 digits.
function Test-IronDeployComputerName {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$ComputerName
    )

    $Trimmed = ([string]$ComputerName).Trim()
    if ($Trimmed -notmatch "^(?i:pc)\d{5}$") {
        throw "Invalid name. Expected format: pc00001"
    }
    return $Trimmed.ToLowerInvariant()
}

# (Re)connects the SMB deployment share. Deletes any stale mapping first, then
# retries the connection up to three times. Throws (via Fail) on failure.
function Connect-IronDeployShare {
    Write-IronLog "[STEP] Delete old SMB mapping" -Level step
    # A missing mapping is the normal first-boot state. In a background
    # runspace, net.exe stderr becomes terminating when ErrorActionPreference
    # is Stop, so suppress only this expected cleanup failure.
    $PreviousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & net.exe use $ShareDrive /delete /y 2>$null | Out-Null
    } finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }

    $SmbConnected = $false
    $SmbError = $null
    $SmbAttempts = 0
    $SmbTimer = [Diagnostics.Stopwatch]::StartNew()
    for ($SmbAttempt = 1; $SmbAttempt -le 3; $SmbAttempt++) {
        $SmbAttempts = $SmbAttempt
        Write-IronLog (
            "[STEP] Connect SMB share, attempt {0}/3" -f $SmbAttempt
        ) -Level step
        $PreviousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            $SmbOutput = @(
                & net.exe use `
                    $ShareDrive `
                    $SharePath `
                    "/user:$ShareUser" `
                    $SharePassword `
                    2>&1
            )
            $SmbExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $PreviousErrorActionPreference
        }
        if ($SmbExitCode -eq 0) {
            $SmbConnected = $true
            break
        }
        $SmbError = (
            @($SmbOutput | ForEach-Object { [string]$_ }) -join
            [Environment]::NewLine
        ).Trim()
        if ([string]::IsNullOrWhiteSpace($SmbError)) {
            $SmbError = "net use exited with code $SmbExitCode"
        }

        if ($SmbAttempt -lt 3) {
            Write-IronLog (
                "[WARN] SMB connection failed. Retrying in 1 second..."
            ) -Level warn
            Start-Sleep -Seconds 1
        }
    }
    $SmbTimer.Stop()
    Set-IronNetworkSmbResult `
        -Success $SmbConnected `
        -Attempts $SmbAttempts `
        -DurationMs $SmbTimer.Elapsed.TotalMilliseconds `
        -ErrorMessage $SmbError
    if (!$SmbConnected) {
        Fail "Failed to connect SMB share: $SharePath ($SmbError)"
    }
}

# Retrieves all deployment control data through the authenticated API. SMB is
# used only later for the heavy files named by this catalog.
function Get-IronDeployCatalog {
    param([switch]$Refresh)

    if ($null -ne $script:DeploymentCatalog -and -not $Refresh) {
        return $script:DeploymentCatalog
    }
    if ($ApiBaseUrl -match "^https://") {
        if ($ValidateApiServerCertificate) {
            Write-IronLog (
                "[INFO] IronAPI transport: TLS 1.2; certificate validation enabled ({0})" -f `
                    $ApiServerCertificateType
            ) -Level info
        } else {
            Write-IronLog (
                "[WARN] IronAPI transport: TLS 1.2; certificate validation bypass enabled"
            ) -Level warn
        }
    }
    try {
        $script:DeploymentCatalog = Invoke-IronApiRestMethod `
            -Uri "$($ApiBaseUrl.TrimEnd('/'))/api/deploy/catalog" `
            -Method Get `
            -TimeoutSec 90
    } catch {
        $ApiError = Get-IronApiErrorDetails -Exception $_.Exception
        Fail "Failed to retrieve deployment catalog: $ApiError"
    }
    return $script:DeploymentCatalog
}

function Get-IronDeployShareCredentials {
    if ($script:DeploymentId -le 0) {
        throw "Deployment must be registered before requesting SMB credentials"
    }
    $Response = Invoke-IronApiRestMethod `
        -Uri "$($ApiBaseUrl.TrimEnd('/'))/api/deploy/$script:DeploymentId/smb-credentials" `
        -Method Get `
        -TimeoutSec 15
    $script:SharePath = [string]$Response.share_path
    $script:ShareUser = [string]$Response.username
    $script:SharePassword = [string]$Response.password
    if (
        [string]::IsNullOrWhiteSpace($script:SharePath) -or
        [string]::IsNullOrWhiteSpace($script:ShareUser) -or
        [string]::IsNullOrWhiteSpace($script:SharePassword)
    ) {
        throw "IronAPI returned incomplete SMB credentials"
    }
}

function Get-IronDeployImageList {
    $Catalog = Get-IronDeployCatalog
    $Images = @(
        $Catalog.images |
            Where-Object { [bool]$_.ready } |
            ForEach-Object {
                [pscustomobject]@{
                    Name = [string]$_.name
                    FullName = "$($ImagesPath.TrimEnd('\'))\$($_.name)"
                    Length = [long]$_.size
                    Format = [string]$_.format
                    Indexes = @($_.indexes)
                    DefaultIndex = [int]$_.defaultIndex
                }
            }
    )
    if ($Images.Count -eq 0) {
        Fail "IronAPI returned no ready WIM/ESD images."
    }
    return $Images
}

function Get-IronDeployProgramList {
    $Catalog = Get-IronDeployCatalog
    $Programs = @(
        $Catalog.programs | ForEach-Object {
            $Arguments = [string]$_.arguments
            $Display = "{0}   ({1:N1} MB)" -f `
                ([string]$_.name),
                ([long]$_.size / 1MB)
            if (![string]::IsNullOrWhiteSpace($Arguments)) {
                $Display = "{0}   [{1}]" -f $Display, $Arguments
            }
            [pscustomobject]@{
                Name = [string]$_.name
                FullName = "$($ProgramsPath.TrimEnd('\'))\$($_.name)"
                Length = [long]$_.size
                Type = [string]$_.type
                Arguments = $Arguments
                Sha256 = [string]$_.sha256
                Display = $Display
            }
        }
    )
    return $Programs
}

function Get-IronDeployDriverPackageList {
    $Catalog = Get-IronDeployCatalog
    $DriverPackages = @(
        $Catalog.drivers | ForEach-Object {
            $Display = "{0} / {1}   ({2:N1} MB, {3} INF)" -f `
                ([string]$_.vendor),
                ([string]$_.model),
                ([long]$_.size / 1MB),
                ([int]$_.infCount)
            [pscustomobject]@{
                Vendor = [string]$_.vendor
                Model = [string]$_.model
                RelativePath = [string]$_.relativePath
                Length = [long]$_.size
                InfCount = [int]$_.infCount
                Display = $Display
            }
        }
    )
    return $DriverPackages
}

# --- Main deployment pipeline ------------------------------------------------

# Runs the full destructive deployment. Assumes the operator has already
# confirmed the disk wipe. Reports progress through the registered callbacks,
# reports state to IronAPI, and returns a result object on success. It does NOT
# reboot; the caller decides when to reboot into Windows.
function Invoke-IronDeployment {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ComputerName,

        [switch]$UseDomainJoin,

        [Parameter(Mandatory = $true)]
        [string]$SelectedImageName,

        [string[]]$SelectedProgramNames = @(),

        [string]$SelectedDriverPackage = ""
    )

    $script:DeploymentErrorReported = $false
    $script:IronNetworkDiagnostics = $null
    # Shred anything a previous attempt left behind on the ramdisk.
    Clear-IronSecretArtifacts
    try {
        Start-IronApiTimingCollection
    } catch {
        $script:IronApiRequestSamples = $null
        Write-IronLog (
            "[WARN] Network diagnostics: failed to start API timing: {0}" -f
            $_.Exception.Message
        ) -Level warn
    }
    $ComputerName = (Test-IronDeployComputerName -ComputerName $ComputerName)
    $UseDomainJoinValue = [bool]$UseDomainJoin
    $SelectedProgramNames = @(
        $SelectedProgramNames | Where-Object {
            ![string]::IsNullOrWhiteSpace([string]$_)
        }
    )
    $SelectedDriverPackage = ([string]$SelectedDriverPackage).Trim()

    Set-IronProgress 2 "Reading hardware identity"
    Write-IronLog "[STEP] Read hardware identity" -Level step
    $Hardware = Get-IronDeployHardwareIdentity
    $SerialNumber = $Hardware.SerialNumber
    $MacAddress = $Hardware.MacAddress
    $SystemModel = $Hardware.Model
    Write-IronLog "[INFO] System serial number: $SerialNumber" -Level info
    Write-IronLog "[INFO] Primary MAC address: $MacAddress" -Level info
    if ([string]::IsNullOrWhiteSpace([string]$SystemModel)) {
        Write-IronLog "[WARN] Hardware model is unknown" -Level warn
    } else {
        Write-IronLog "[INFO] Hardware model: $SystemModel" -Level info
    }

    Write-IronLog "[OK] Selected computer name: $ComputerName" -Level ok
    if ($UseDomainJoinValue) {
        Write-IronLog "[MODE] Domain join ENABLED" -Level ok
    } else {
        Write-IronLog "[MODE] Domain join DISABLED" -Level warn
    }

    Set-IronProgress 4 "Registering deployment"
    Write-IronLog "[STEP] Register deployment start" -Level step
    $BeginPayload = @{
        computer_name = $ComputerName
        serial_number = $SerialNumber
        mac_address = $MacAddress
        model = if ([string]::IsNullOrWhiteSpace([string]$SystemModel)) {
            $null
        } else {
            [string]$SystemModel
        }
        domain_join = [bool]$UseDomainJoinValue
    } | ConvertTo-Json

    try {
        $BeginResponse = Invoke-IronApiRestMethod `
            -Uri "$ApiBaseUrl/api/deploy/begin" `
            -Method Post `
            -ContentType "application/json; charset=utf-8" `
            -Body $BeginPayload `
            -TimeoutSec 15 `
            -UseBasicParsing
    } catch {
        Fail "Failed to register deployment start: $($_.Exception.Message)"
    }

    $DeploymentIdIsValid = [long]::TryParse(
        [string]$BeginResponse.deployment_id,
        [ref]$script:DeploymentId
    )
    if (!$DeploymentIdIsValid -or $script:DeploymentId -le 0) {
        Fail "IronAPI returned an invalid deployment_id"
    }

    if ([string]$BeginResponse.status -ne "begin") {
        Fail "IronAPI returned an unexpected deployment status"
    }

    Write-IronLog (
        "[OK] Deployment #{0} registered: status begin" -f $script:DeploymentId
    ) -Level ok

    Set-IronProgress 6 "Connecting deployment share"
    Get-IronDeployShareCredentials
    try {
        Start-IronNetworkDiagnostics -SharePath $script:SharePath
    } catch {
        Write-IronLog (
            "[WARN] Network diagnostics could not start: {0}" -f
            $_.Exception.Message
        ) -Level warn
    }
    Connect-IronDeployShare

    Set-IronProgress 10 "Selecting Windows image"
    Write-IronLog "[STEP] Request final deployment manifest" -Level step
    $ManifestPayload = @{
        image_name = $SelectedImageName
        program_names = @($SelectedProgramNames)
        driver_package = if (
            [string]::IsNullOrWhiteSpace($SelectedDriverPackage)
        ) {
            $null
        } else {
            $SelectedDriverPackage
        }
    } | ConvertTo-Json
    try {
        $DeploymentPlan = Invoke-IronApiRestMethod `
            -Uri "$($ApiBaseUrl.TrimEnd('/'))/api/deploy/$script:DeploymentId/manifest" `
            -Method Post `
            -ContentType "application/json; charset=utf-8" `
            -Body $ManifestPayload `
            -TimeoutSec 90
    } catch {
        Fail "Failed to retrieve final deployment manifest: $($_.Exception.Message)"
    }
    if ([long]$DeploymentPlan.deploymentId -ne $script:DeploymentId) {
        Fail "IronAPI returned a manifest for another deployment."
    }

    $SelectedImage = [pscustomobject]@{
        Name = [string]$DeploymentPlan.image.name
        FullName = "$($ImagesPath.TrimEnd('\'))\$($DeploymentPlan.image.name)"
        Length = [long]$DeploymentPlan.image.size
    }
    $ImagePath = $SelectedImage.FullName
    $ImageIndexToApply = [int]$DeploymentPlan.image.defaultIndex
    $SelectedPrograms = @($DeploymentPlan.programs | ForEach-Object { $_ })
    $DriverPackagePlan = $DeploymentPlan.driverPackage
    $DriverPackagePath = $null
    $DriverPackageRelativePath = ""
    $AvailableDrivers = @()
    $SetupLocalAdminName = [string]$DeploymentPlan.postinstall.localAdminName
    $EnableBuiltInAdministrator = [bool]$DeploymentPlan.postinstall.enableBuiltInAdministrator
    $EnableSetupLocalAdmin = [bool]$DeploymentPlan.postinstall.enableSetupLocalAdmin

    if ($ImageIndexToApply -le 0) {
        Fail "IronAPI returned an invalid default image index."
    }

    Write-IronLog (
        "[OK] Selected Windows image: {0} (index {1})" -f `
            $SelectedImage.Name,
            $ImageIndexToApply
    ) -Level ok

    Set-IronProgress 14 "Checking deployment files"
    Write-IronLog "[STEP] Check files" -Level step
    if (!(Test-Path $ImagePath)) {
        Fail "Windows image not found: $ImagePath"
    }
    if ((Get-Item -LiteralPath $ImagePath).Length -ne $SelectedImage.Length) {
        Fail "Windows image size does not match the API manifest: $ImagePath"
    }

    if ($null -ne $DriverPackagePlan) {
        $DriverPackageRelativePath = [string]$DriverPackagePlan.relativePath
        if (
            $DriverPackageRelativePath -notmatch `
                '^[^\\/:*?"<>|]+\\[^\\/:*?"<>|]+$'
        ) {
            Fail "IronAPI returned an invalid driver package path."
        }
        $DriverPackagePath = (
            "{0}\{1}" -f `
                $DriversPath.TrimEnd("\"),
                $DriverPackageRelativePath
        )
        if (!(Test-Path -LiteralPath $DriverPackagePath -PathType Container)) {
            Fail "Selected driver package not found: $DriverPackagePath"
        }
        try {
            $DriverFiles = @(
                Get-ChildItem `
                    -LiteralPath $DriverPackagePath `
                    -File `
                    -Recurse
            )
            $AvailableDrivers = @(
                $DriverFiles | Where-Object {
                    $_.Extension -ieq ".inf"
                }
            )
        } catch {
            Fail (
                "Failed to list drivers in ${DriverPackagePath}: " +
                "$($_.Exception.Message)"
            )
        }
        $DriverPackageSize = [long](
            $DriverFiles |
                Measure-Object -Property Length -Sum
        ).Sum
        if (
            $DriverPackageSize -ne [long]$DriverPackagePlan.size -or
            $AvailableDrivers.Count -ne [int]$DriverPackagePlan.infCount
        ) {
            Fail (
                "Selected driver package does not match the API manifest: " +
                $DriverPackageRelativePath
            )
        }
        if ($AvailableDrivers.Count -eq 0) {
            Fail (
                "Selected driver package contains no INF files: " +
                $DriverPackageRelativePath
            )
        }
    }

    if (!(Test-Path $DiskPartScript)) {
        Fail "DiskPart script not found: $DiskPartScript"
    }

    Write-IronLog "[OK] Files found" -Level ok
    if ($null -ne $DriverPackagePlan) {
        Write-IronLog (
            "[OK] Selected driver package: {0} ({1} INF files)" -f `
                $DriverPackageRelativePath,
                $AvailableDrivers.Count
        ) -Level ok
    } else {
        Write-IronLog "[INFO] No driver package selected" -Level info
    }

    if ($UseDomainJoinValue) {
        Set-IronProgress 18 "Provisioning Offline Domain Join"
        Write-IronLog "[STEP] Request Offline Domain Join provisioning" -Level step
        $DomainJoinBaseUrl = (
            "{0}/api/deploy/{1}/domain-join" -f `
                $ApiBaseUrl.TrimEnd("/"),
                $script:DeploymentId
        )

        try {
            $ProvisionResponse = Invoke-IronApiRestMethod `
                -Uri "$DomainJoinBaseUrl/provision" `
                -Method Post `
                -TimeoutSec 90 `
                -UseBasicParsing
        } catch {
            Fail "Failed to provision Offline Domain Join: $($_.Exception.Message)"
        }

        if (
            [string]$ProvisionResponse.status -ne "ready" -or
            [string]$ProvisionResponse.computer_name -ne $ComputerName
        ) {
            Fail "IronAPI returned an invalid Offline Domain Join response"
        }

        $ODJDirectory = "X:\IronDeploy\ODJ"
        $ODJBlob = Join-Path $ODJDirectory "$ComputerName.txt"
        New-Item -ItemType Directory -Force $ODJDirectory | Out-Null

        # Registered before the download so a partial transfer is shredded too.
        # From here until the blob is applied, every Fail cleans it up.
        Register-IronSecretArtifact -Path $ODJBlob

        try {
            Invoke-IronApiWebRequest `
                -Uri "$DomainJoinBaseUrl/blob" `
                -Method Get `
                -OutFile $ODJBlob `
                -TimeoutSec 30 `
                -UseBasicParsing
        } catch {
            Fail "Failed to download Offline Domain Join blob: $($_.Exception.Message)"
        }

        if (
            !(Test-Path -LiteralPath $ODJBlob -PathType Leaf) -or
            (Get-Item -LiteralPath $ODJBlob).Length -le 0
        ) {
            Fail "Downloaded Offline Domain Join blob is empty"
        }

        Write-IronLog "[OK] Offline Domain Join blob downloaded" -Level ok
    }

    Set-IronProgress 22 "Wiping and partitioning disk 0"
    Write-IronLog "[STEP] DiskPart wipe and partition" -Level step
    Start-DeploymentStage "disk_partitioning"
    diskpart /s $DiskPartScript
    if ($LASTEXITCODE -ne 0) {
        Fail "DiskPart failed"
    }
    Complete-DeploymentStage "disk_partitioning"

    Set-IronProgress 30 "Applying Windows image"
    Write-IronLog "[STEP] Apply Windows image" -Level step
    Start-DeploymentStage "image_apply"
    $TrackImageApplyProgress = (
        [bool]$EnableGuiImageApplyProgress -and
        $null -ne $script:IronProgressCallback
    )
    if ($TrackImageApplyProgress) {
        # Native stderr becomes terminating in a background runspace when the
        # global preference is Stop. Let DISM finish, then handle its exit code
        # consistently while parsing its normal percentage output.
        $PreviousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            & dism.exe `
                /Apply-Image `
                /ImageFile:$ImagePath `
                /Index:$ImageIndexToApply `
                /ApplyDir:C:\ `
                2>&1 |
                ForEach-Object {
                    Update-IronImageApplyProgress -OutputLine ([string]$_)
                }
            $ImageApplyExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $PreviousErrorActionPreference
        }
    } else {
        dism.exe /Apply-Image /ImageFile:$ImagePath /Index:$ImageIndexToApply /ApplyDir:C:\
        $ImageApplyExitCode = $LASTEXITCODE
    }
    if ($ImageApplyExitCode -ne 0) {
        Fail "DISM Apply-Image failed"
    }
    Complete-DeploymentStage "image_apply"

    if ($null -ne $DriverPackagePlan) {
        Set-IronProgress 62 "Staging driver packages"
        Write-IronLog (
            "[STEP] Stage the selected driver package in offline Windows; " +
            "PnP selects compatible packages on first boot"
        ) -Level step
        Start-DeploymentStage "driver_injection"
        dism.exe /Image:C:\ /Add-Driver /Driver:$DriverPackagePath /Recurse
        if ($LASTEXITCODE -notin @(0, 3010)) {
            Fail "DISM Add-Driver failed"
        }
        Complete-DeploymentStage "driver_injection"
    } else {
        Write-IronLog "[SKIP] Driver installation was not selected" -Level warn
        Skip-DeploymentStage "driver_injection"
    }

    Set-IronProgress 74 "Saving deployment state"
    Write-IronLog "[STEP] Save deployment state" -Level step
    Start-DeploymentStage "deployment_state"
    $DeploymentStateDir = "C:\IronDeploy"
    $DeploymentStatePath = "$DeploymentStateDir\deployment.json"
    New-Item -ItemType Directory -Force $DeploymentStateDir | Out-Null

    @{
        deployment_id = $script:DeploymentId
        api_base_url = $ApiBaseUrl
        api_deployment_token = $script:DeploymentAccessToken
        driver_package = $DriverPackageRelativePath
        api_validate_server_certificate = [bool]$ValidateApiServerCertificate
        api_server_certificate_type = $ApiServerCertificateType
        api_server_certificate_base64 = $ApiServerCertificateBase64
        computer_name = $ComputerName
    } |
        ConvertTo-Json |
        Out-File $DeploymentStatePath -Encoding UTF8 -Force

    if (!(Test-Path $DeploymentStatePath)) {
        Fail "Failed to save deployment state: $DeploymentStatePath"
    }
    Complete-DeploymentStage "deployment_state"

    Set-IronProgress 76 "Generating unattend.xml"
    Write-IronLog "[STEP] Download deployment unattend.xml" -Level step
    Start-DeploymentStage "unattend_generation"
    New-Item -ItemType Directory -Force "C:\Windows\Panther" | Out-Null
    try {
        Invoke-IronApiWebRequest `
            -Uri "$($ApiBaseUrl.TrimEnd('/'))/api/deploy/$script:DeploymentId/unattend" `
            -Method Get `
            -OutFile $UnattendTarget `
            -TimeoutSec 30 | Out-Null
    } catch {
        Fail "Failed to download unattend.xml: $($_.Exception.Message)"
    }

    if (!(Test-Path $UnattendTarget)) {
        Fail "Failed to create unattend.xml: $UnattendTarget"
    }
    Complete-DeploymentStage "unattend_generation"

    Set-IronProgress 80 "Applying unattend.xml"
    Write-IronLog "[STEP] Apply unattend.xml" -Level step
    Start-DeploymentStage "unattend_apply"
    dism /Image:C:\ /Apply-Unattend:$UnattendTarget
    if ($LASTEXITCODE -ne 0) {
        Fail "DISM Apply-Unattend failed"
    }
    Complete-DeploymentStage "unattend_apply"

    if ($UseDomainJoinValue) {
        Set-IronProgress 84 "Applying Offline Domain Join"
        Write-IronLog "[STEP] Apply Offline Domain Join blob" -Level step
        Start-DeploymentStage "domain_join"

        $ODJUnattend = Join-Path $ODJDirectory "odj-unattend.xml"
        Register-IronSecretArtifact -Path $ODJUnattend

        try {
            New-OfflineDomainJoinUnattend `
                -BlobPath $ODJBlob `
                -OutputPath $ODJUnattend
        } catch {
            Fail "Failed to create Offline Domain Join unattend: $($_.Exception.Message)"
        }

        dism /Image:C:\ /Apply-Unattend:$ODJUnattend
        $ODJApplyExitCode = $LASTEXITCODE

        Clear-IronSecretArtifacts

        if ($ODJApplyExitCode -ne 0) {
            Fail "Offline Domain Join failed: DISM Apply-Unattend exited with code $ODJApplyExitCode"
        }

        try {
            $AcknowledgeResponse = Invoke-IronApiRestMethod `
                -Uri "$DomainJoinBaseUrl/acknowledge" `
                -Method Post `
                -TimeoutSec 15 `
                -UseBasicParsing
        } catch {
            Fail (
                "Offline Domain Join was applied, but IronAPI could not delete " +
                "the server blob: $($_.Exception.Message)"
            )
        }

        if (
            [string]$AcknowledgeResponse.status -notmatch `
                "^(deleted|already_deleted)$"
        ) {
            Fail "IronAPI returned an invalid domain join acknowledgement"
        }

        Complete-DeploymentStage "domain_join"
    } else {
        Write-IronLog "[SKIP] Offline Domain Join disabled" -Level warn
        Skip-DeploymentStage "domain_join"
    }

    Set-IronProgress 90 "Copying post-install scripts"
    Write-IronLog "[STEP] Copy post-install scripts" -Level step
    Start-DeploymentStage "postinstall_copy"
    $SetupScriptsDir = "C:\Windows\Setup\Scripts"
    New-Item -ItemType Directory -Force $SetupScriptsDir | Out-Null

    Copy-Item `
        (Join-Path $PostInstallPath "SetupComplete.cmd") `
        "$SetupScriptsDir\SetupComplete.cmd" `
        -Force
    Copy-Item `
        (Join-Path $PostInstallPath "postinstall.ps1") `
        "$SetupScriptsDir\postinstall.ps1" `
        -Force

    if (!(Test-Path "$SetupScriptsDir\SetupComplete.cmd")) {
        Fail "SetupComplete.cmd was not copied"
    }

    if (!(Test-Path "$SetupScriptsDir\postinstall.ps1")) {
        Fail "postinstall.ps1 was not copied"
    }

    $PostInstallConfigPath = Join-Path `
        $SetupScriptsDir `
        "IronDeployPostInstall.config.ps1"
    @(
        "# Generated by IronDeploy WinPE deploy engine.",
        "# Consumed by postinstall.ps1 during Windows SetupComplete.",
        "",
        "`$SetupLocalAdminName = $(ConvertTo-PowerShellLiteral $SetupLocalAdminName)",
        "`$EnableBuiltInAdministrator = $(ConvertTo-PowerShellBooleanLiteral $EnableBuiltInAdministrator)",
        "`$EnableSetupLocalAdmin = $(ConvertTo-PowerShellBooleanLiteral $EnableSetupLocalAdmin)"
    ) | Out-File $PostInstallConfigPath -Encoding UTF8 -Force

    if (!(Test-Path $PostInstallConfigPath)) {
        Fail "Post-install config was not written: $PostInstallConfigPath"
    }

    if ($SelectedPrograms.Count -gt 0) {
        Write-IronLog (
            "[STEP] Copy post-install programs ({0} selected)" -f `
                $SelectedPrograms.Count
        ) -Level step
        $ProgramsTargetDir = "C:\IronDeploy\Programs"
        New-Item -ItemType Directory -Force $ProgramsTargetDir | Out-Null

        $ProgramManifest = @()
        foreach ($SelectedProgram in $SelectedPrograms) {
            $ProgramName = [string]$SelectedProgram.name
            $ProgramSourcePath = "$($ProgramsPath.TrimEnd('\'))\$ProgramName"
            $ProgramTargetPath = Join-Path $ProgramsTargetDir $ProgramName
            Copy-Item $ProgramSourcePath $ProgramTargetPath -Force
            if (!(Test-Path $ProgramTargetPath)) {
                Fail "Program was not copied: $ProgramName"
            }

            $ActualProgramHash = (
                Get-FileHash -LiteralPath $ProgramTargetPath -Algorithm SHA256
            ).Hash.ToLowerInvariant()
            $ExpectedProgramHash = ([string]$SelectedProgram.sha256).ToLowerInvariant()
            if ($ActualProgramHash -ne $ExpectedProgramHash) {
                Write-IronLog (
                    "[ERROR] SHA-256 mismatch after copy: {0}" -f $ProgramName
                ) -Level error
            } else {
                Write-IronLog (
                    "[OK] Program staged and verified: {0} {1}" -f `
                        $ProgramName,
                        ([string]$SelectedProgram.arguments)
                ) -Level ok
            }
            $ProgramManifest += @{
                name = $ProgramName
                size = [long]$SelectedProgram.size
                type = [string]$SelectedProgram.type
                arguments = [string]$SelectedProgram.arguments
                sha256 = $ExpectedProgramHash
            }
        }

        $ProgramManifestPath = Join-Path $ProgramsTargetDir "programs.json"
        ConvertTo-Json -InputObject @($ProgramManifest) |
            Out-File $ProgramManifestPath -Encoding UTF8 -Force
        if (!(Test-Path $ProgramManifestPath)) {
            Fail "Program manifest was not written: $ProgramManifestPath"
        }
    } else {
        Write-IronLog "[SKIP] No post-install programs selected" -Level warn
    }
    Complete-DeploymentStage "postinstall_copy"

    Set-IronProgress 95 "Creating UEFI boot files"
    Write-IronLog "[STEP] Create UEFI boot files" -Level step
    Start-DeploymentStage "boot_files"
    bcdboot C:\Windows /s $EfiDrive /f UEFI
    if ($LASTEXITCODE -ne 0) {
        Fail "bcdboot failed"
    }
    Complete-DeploymentStage "boot_files"

    Write-IronLog "IronDeploy finished successfully." -Level ok
    Set-IronProgress 100 "Ready to boot into Windows"
    Start-DeploymentStage "windows_setup"

    $PostInstallPhaseError = $null
    $PostInstallPhaseEntered = $false
    for ($Attempt = 1; $Attempt -le 3; $Attempt++) {
        try {
            Write-IronLog "Entering post-install phase (attempt $Attempt of 3)."
            $PhaseResponse = Invoke-IronApiRestMethod `
                -Uri "$($ApiBaseUrl.TrimEnd('/'))/api/deploy/$script:DeploymentId/postinstall" `
                -Method Post `
                -TimeoutSec 15
            if ([string]$PhaseResponse.status -ne "postinstall") {
                throw "IronAPI returned an unexpected deployment phase"
            }
            $PostInstallPhaseEntered = $true
            break
        } catch {
            $PostInstallPhaseError = $_.Exception.Message
            if ($Attempt -lt 3) {
                Start-Sleep -Seconds 2
            }
        }
    }
    if (!$PostInstallPhaseEntered) {
        Fail "Failed to enter post-install phase after 3 attempts: $PostInstallPhaseError"
    }

    Complete-IronNetworkDiagnostics -Send

    return [pscustomobject]@{
        ComputerName = $ComputerName
        DeploymentId = $script:DeploymentId
        UseDomainJoin = $UseDomainJoinValue
        ImageName = $SelectedImage.Name
    }
}
