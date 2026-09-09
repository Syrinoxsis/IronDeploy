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
    "DriversPath"
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
$SevenZipPath = Join-Path $PSScriptRoot "Tools\7-Zip\7za.exe"

$script:DeploymentId = 0L
$script:CurrentDeploymentStage = $null
$script:DeploymentErrorReported = $false
$script:DeploymentCatalog = $null
$script:DeploymentAccessToken = ""
$script:IronApiRequestSamples = $null
$script:IronNetworkDiagnostics = $null
$script:IronSecretArtifacts = @()
$script:ImageApplyMode = "direct"
$script:DriverApplyMode = "direct"

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

function Format-IronNetworkLinkSpeed {
    param([object]$LinkSpeedBps)

    if ($null -eq $LinkSpeedBps -or [long]$LinkSpeedBps -le 0) {
        return "unavailable"
    }
    if ([long]$LinkSpeedBps -ge 1000000000) {
        return ("{0:N2} Gbps" -f ([long]$LinkSpeedBps / 1000000000.0))
    }
    return ("{0:N0} Mbps" -f ([long]$LinkSpeedBps / 1000000.0))
}

function Write-IronNetworkLinkSpeed {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RouteLabel,
        [AllowNull()]
        [object]$Adapter
    )

    if ($null -eq $Adapter) {
        Write-IronLog (
            "[WARN] $RouteLabel link speed is unavailable; " +
            "the route adapter was not detected"
        ) -Level warn
        return
    }

    $AdapterDetails = "{0} ({1})" -f $Adapter.Name, $Adapter.LocalIp
    $DisplaySpeed = Format-IronNetworkLinkSpeed $Adapter.LinkSpeedBps
    if (
        $null -eq $Adapter.LinkSpeedBps -or
        [long]$Adapter.LinkSpeedBps -le 0
    ) {
        Write-IronLog (
            "[WARN] $RouteLabel link speed is unavailable: $AdapterDetails"
        ) -Level warn
    } elseif ([long]$Adapter.LinkSpeedBps -lt 1000000000) {
        Write-IronLog (
            "[WARN] $RouteLabel LINK SPEED BELOW 1 GBPS: " +
            "$DisplaySpeed - $AdapterDetails"
        ) -Level warn
    } else {
        Write-IronLog (
            "[OK] $RouteLabel link speed: $DisplaySpeed - $AdapterDetails"
        ) -Level ok
    }
}

function Write-IronNetworkLinkSpeedSummary {
    $Context = $script:IronNetworkDiagnostics
    if ($null -eq $Context) {
        Write-IronLog (
            "[WARN] Network link speed is unavailable; diagnostics did not start"
        ) -Level warn
        return
    }

    if (
        $null -ne $Context.SmbAdapter -and
        $null -ne $Context.ApiAdapter -and
        $Context.SmbAdapter.AdapterId -eq $Context.ApiAdapter.AdapterId
    ) {
        Write-IronNetworkLinkSpeed `
            -RouteLabel "API/SMB" `
            -Adapter $Context.SmbAdapter
        return
    }

    Write-IronNetworkLinkSpeed -RouteLabel "IronAPI" -Adapter $Context.ApiAdapter
    Write-IronNetworkLinkSpeed -RouteLabel "SMB" -Adapter $Context.SmbAdapter
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
        StageReportsSent = @{}
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
            "image_download",
            "image_apply",
            "driver_download",
            "driver_injection",
            "postinstall_copy"
        ) -or
        ($Stage -eq "image_apply" -and $script:ImageApplyMode -eq "staged") -or
        ($Stage -eq "driver_injection" -and $script:DriverApplyMode -eq "staged")
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

function Invoke-IronNetworkReportWithRetry {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Uri,
        [Parameter(Mandatory = $true)]
        [string]$Body,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    $RetryDelaysSeconds = @(0, 5, 10)
    for ($Attempt = 1; $Attempt -le $RetryDelaysSeconds.Count; $Attempt++) {
        $DelaySeconds = $RetryDelaysSeconds[$Attempt - 1]
        if ($DelaySeconds -gt 0) {
            Write-IronLog (
                "[WARN] Retrying {0} in {1} seconds (attempt {2}/3)" -f
                $Description,
                $DelaySeconds,
                $Attempt
            ) -Level warn
            Start-Sleep -Seconds $DelaySeconds
        }

        try {
            Invoke-IronApiRestMethod `
                -Uri $Uri `
                -Method Put `
                -ContentType "application/json; charset=utf-8" `
                -Body $Body `
                -TimeoutSec 10 `
                -SkipNetworkTiming |
                Out-Null
            Write-IronLog (
                "[OK] Reported {0} (attempt {1}/3)" -f
                $Description,
                $Attempt
            ) -Level ok
            return $true
        } catch {
            Write-IronLog (
                "[WARN] Failed to report {0} (attempt {1}/3): {2}" -f
                $Description,
                $Attempt,
                $_.Exception.Message
            ) -Level warn
        }
    }

    return $false
}

function Send-IronNetworkStageDiagnostics {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet(
            "image_download", "image_apply", "driver_download",
            "driver_injection", "postinstall_copy"
        )]
        [string]$Stage
    )

    $Context = $script:IronNetworkDiagnostics
    if (
        $null -eq $Context -or
        $script:DeploymentId -le 0 -or
        $Context.StageReportsSent.ContainsKey($Stage) -or
        -not $Context.StageWindows.ContainsKey($Stage)
    ) {
        return
    }

    $Window = $Context.StageWindows[$Stage]
    if ($null -eq $Window.CompletedAtUtc) {
        return
    }

    try {
        $LinkSpeedBps = if ($null -ne $Context.SmbAdapter) {
            $Context.SmbAdapter.LinkSpeedBps
        } else {
            $null
        }
        $Aggregate = New-IronNetworkAggregate `
            -Samples @($Context.Samples.ToArray()) `
            -StartedAt $Window.StartedAtUtc `
            -CompletedAt $Window.CompletedAtUtc `
            -BytesBefore $Window.BytesBefore `
            -BytesAfter $Window.BytesAfter `
            -LinkSpeedBps $LinkSpeedBps
        $StageReport = [ordered]@{ stage = $Stage }
        foreach ($Name in $Aggregate.Keys) {
            $StageReport[$Name] = $Aggregate[$Name]
        }

        $Sent = Invoke-IronNetworkReportWithRetry `
            -Uri (
                "$($ApiBaseUrl.TrimEnd('/'))/api/deploy/" +
                "$script:DeploymentId/network-diagnostics/stages/$Stage"
            ) `
            -Body ($StageReport | ConvertTo-Json -Depth 5 -Compress) `
            -Description "network diagnostics for stage '$Stage'"
        if ($Sent) {
            $Context.StageReportsSent[$Stage] = $true
        } else {
            Add-IronNetworkDiagnosticError (
                "All attempts to report network diagnostics for stage " +
                "'$Stage' failed"
            )
        }
    } catch {
        Add-IronNetworkDiagnosticError (
            "Failed to prepare network diagnostics for stage '$Stage': " +
            $_.Exception.Message
        )
    }
}

function Send-IronNetworkAdapterSnapshot {
    $Context = $script:IronNetworkDiagnostics
    if ($null -eq $Context -or $script:DeploymentId -le 0) {
        return
    }

    try {
        $Payload = [ordered]@{
            smb_adapter = Get-IronAdapterReport -Adapter $Context.SmbAdapter
            api_adapter = Get-IronAdapterReport -Adapter $Context.ApiAdapter
            adapters_differ = [bool]$Context.AdaptersDiffer
        } | ConvertTo-Json -Depth 5 -Compress
        $Sent = Invoke-IronNetworkReportWithRetry `
            -Uri (
                "$($ApiBaseUrl.TrimEnd('/'))/api/deploy/" +
                "$script:DeploymentId/network-diagnostics/adapters"
            ) `
            -Body $Payload `
            -Description "early network adapter snapshot"
        if (-not $Sent) {
            Add-IronNetworkDiagnosticError (
                "All attempts to report the early network adapter snapshot failed"
            )
        }
    } catch {
        Add-IronNetworkDiagnosticError (
            "Failed to prepare the early network adapter snapshot: " +
            $_.Exception.Message
        )
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
                "image_download",
                "image_apply",
                "driver_download",
                "driver_injection",
                "postinstall_copy"
            )) {
                if (-not $Context.StageWindows.ContainsKey($StageName)) {
                    continue
                }
                if ($Context.StageReportsSent.ContainsKey($StageName)) {
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
            $Sent = Invoke-IronNetworkReportWithRetry `
                -Uri (
                    "$($ApiBaseUrl.TrimEnd('/'))/api/deploy/" +
                    "$script:DeploymentId/network-diagnostics"
                ) `
                -Body (
                    $Context.FinalReport |
                        ConvertTo-Json -Depth 8 -Compress
                ) `
                -Description (
                    "aggregate network diagnostics for deployment #{0}" -f
                    $script:DeploymentId
                )
            if ($Sent) {
                $Context.ReportSent = $true
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
    $MaximumAttempts = 5
    $RetryDelaySeconds = 5
    $LastError = $null
    for ($Attempt = 1; $Attempt -le $MaximumAttempts; $Attempt++) {
        try {
            $Response = Invoke-RestMethod `
                -Uri "$($ApiBaseUrl.TrimEnd('/'))/api/deploy/auth/policy" `
                -Method Get `
                -TimeoutSec 5 `
                -UseBasicParsing
        } catch {
            $LastError = $_.Exception.Message
            if ($Attempt -lt $MaximumAttempts) {
                Write-IronLog (
                    "[WARN] IronAPI is not ready after wpeinit " +
                    "(attempt $Attempt/$MaximumAttempts): $LastError; " +
                    "retrying in $RetryDelaySeconds seconds"
                ) -Level warn
                Start-Sleep -Seconds $RetryDelaySeconds
            }
            continue
        }

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

    throw (
        "IronAPI authorization policy is unavailable after " +
        "$MaximumAttempts attempts: $LastError"
    )
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
    if ($Stage -in @(
        "image_download", "image_apply", "driver_download", "driver_injection",
        "postinstall_copy"
    )) {
        Send-IronNetworkStageDiagnostics -Stage $Stage
    }
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

function Resolve-IronImageApplyMode {
    param([AllowNull()][object]$Value)

    $Mode = ([string]$Value).Trim().ToLowerInvariant()
    if ($Mode -in @("direct", "staged")) {
        return $Mode
    }

    $DisplayedValue = if ([string]::IsNullOrWhiteSpace([string]$Value)) {
        "<missing>"
    } else {
        [string]$Value
    }
    Write-IronLog (
        "[WARN] Unknown imageApplyMode '{0}'; falling back to direct" -f
        $DisplayedValue
    ) -Level warn
    return "direct"
}

function Resolve-IronDriverApplyMode {
    param([AllowNull()][object]$Value)

    $Mode = ([string]$Value).Trim().ToLowerInvariant()
    if ($Mode -in @("direct", "staged")) {
        return $Mode
    }

    $DisplayedValue = if ([string]::IsNullOrWhiteSpace([string]$Value)) {
        "<missing>"
    } else {
        [string]$Value
    }
    Write-IronLog (
        "[WARN] Unknown driverApplyMode '{0}'; falling back to direct" -f
        $DisplayedValue
    ) -Level warn
    return "direct"
}

function Test-IronRobocopyExitCode {
    param([Parameter(Mandatory = $true)][int]$ExitCode)

    return ($ExitCode -ge 0 -and $ExitCode -le 7)
}

function Remove-IronStagedImageArtifacts {
    param(
        [string]$StagingDirectory,
        [Parameter(Mandatory = $true)][string]$Reason
    )

    if ([string]::IsNullOrWhiteSpace($StagingDirectory)) {
        return
    }
    Write-IronLog (
        "[INFO] Staged WIM cleanup path: {0}; reason: {1}" -f
        $StagingDirectory,
        $Reason
    ) -Level info
    try {
        if (Test-Path -LiteralPath $StagingDirectory) {
            Remove-Item `
                -LiteralPath $StagingDirectory `
                -Recurse `
                -Force `
                -ErrorAction Stop
        }
    } catch {
        Write-IronLog (
            "[WARN] Failed to remove staged WIM path '{0}': {1}" -f
            $StagingDirectory,
            $_.Exception.Message
        ) -Level warn
    }
}

function Copy-IronImageToLocalStaging {
    param(
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][long]$ExpectedLength,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$ExpectedSha256,
        [Parameter(Mandatory = $true)][long]$DeploymentId
    )

    if ($ExpectedLength -le 0) {
        throw "The image manifest contains an invalid size"
    }
    if ($ExpectedSha256 -notmatch '^[0-9a-fA-F]{64}$') {
        throw "The image manifest contains an invalid SHA-256"
    }

    $LocalDrive = Get-PSDrive -Name $WindowsDrive.TrimEnd(":")
    if ($null -eq $LocalDrive.Free -or [long]$LocalDrive.Free -lt $ExpectedLength) {
        $FreeBytes = if ($null -eq $LocalDrive.Free) { 0L } else { [long]$LocalDrive.Free }
        throw (
            "Insufficient free space for staged WIM: need {0} bytes, have {1} bytes" -f
            $ExpectedLength,
            $FreeBytes
        )
    }

    $StagingDirectory = Join-Path `
        "$($WindowsDrive.TrimEnd('\'))\IronDeploy.Staging" `
        ([string]$DeploymentId)
    New-Item -ItemType Directory -Path $StagingDirectory -Force | Out-Null

    $SourceDirectory = Split-Path -Parent $SourcePath
    $SourceName = Split-Path -Leaf $SourcePath
    $RobocopyDirectory = Join-Path $StagingDirectory "robocopy"
    New-Item -ItemType Directory -Path $RobocopyDirectory -Force | Out-Null
    $RobocopyPath = Join-Path $RobocopyDirectory $SourceName
    $PartialPath = Join-Path $StagingDirectory "image.wim.partial"
    $FinalPath = Join-Path $StagingDirectory "image.wim"
    # Robocopy cannot rename a file in flight. Point its source-named target at
    # the .partial file with an NTFS hard link, so interrupted downloads never
    # expose the final .wim name.
    New-Item -ItemType File -Path $PartialPath -Force | Out-Null
    New-Item `
        -ItemType HardLink `
        -Path $RobocopyPath `
        -Target $PartialPath | Out-Null
    $Stopwatch = [Diagnostics.Stopwatch]::StartNew()
    try {
        Write-IronLog (
            "[STEP] Download image with robocopy /J: {0} -> {1}" -f
            $SourcePath,
            $PartialPath
        ) -Level step
        $PreviousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            & robocopy.exe `
                $SourceDirectory `
                $RobocopyDirectory `
                $SourceName `
                /J `
                /R:2 `
                /W:2 `
                /COPY:DAT `
                /DCOPY:T `
                /NP `
                /NFL `
                /NDL
            $RobocopyExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $PreviousErrorActionPreference
        }
        if (-not (Test-IronRobocopyExitCode -ExitCode $RobocopyExitCode)) {
            throw "robocopy failed with exit code $RobocopyExitCode"
        }
        if (-not (Test-Path -LiteralPath $RobocopyPath -PathType Leaf)) {
            throw "robocopy did not create the local image file"
        }

        Remove-Item -LiteralPath $RobocopyPath -Force
        Remove-Item -LiteralPath $RobocopyDirectory -Force
        $ActualLength = (Get-Item -LiteralPath $PartialPath).Length
        if ($ActualLength -ne $ExpectedLength) {
            throw (
                "Downloaded WIM size mismatch: expected {0}, received {1}" -f
                $ExpectedLength,
                $ActualLength
            )
        }
        $ActualSha256 = (
            Get-FileHash -LiteralPath $PartialPath -Algorithm SHA256
        ).Hash.ToLowerInvariant()
        if ($ActualSha256 -ne $ExpectedSha256.ToLowerInvariant()) {
            throw (
                "Downloaded WIM SHA-256 mismatch at {0}: expected {1}, actual {2}" -f
                $PartialPath,
                $ExpectedSha256.ToLowerInvariant(),
                $ActualSha256
            )
        }
        Move-Item -LiteralPath $PartialPath -Destination $FinalPath -Force
        $Stopwatch.Stop()
        $Seconds = [Math]::Max(0.001, $Stopwatch.Elapsed.TotalSeconds)
        $AverageMegabytesPerSecond = ($ActualLength / 1MB) / $Seconds
        Write-IronLog (
            "[OK] Image download completed: status=success; bytes={0}; " +
            "duration={1:N3}s; average={2:N3} MB/s; path={3}" -f
            $ActualLength,
            $Seconds,
            $AverageMegabytesPerSecond,
            $FinalPath
        ) -Level ok
        return [pscustomobject]@{
            Path = $FinalPath
            StagingDirectory = $StagingDirectory
            BytesTransferred = [long]$ActualLength
            DurationSeconds = [double]$Seconds
            AverageMegabytesPerSecond = [double]$AverageMegabytesPerSecond
            RobocopyExitCode = [int]$RobocopyExitCode
        }
    } catch {
        $Stopwatch.Stop()
        Write-IronLog (
            "[ERROR] Image download failed: status=failed; duration={0:N3}s; " +
            "partialPath={1}; finalPath={2}; reason={3}" -f
            $Stopwatch.Elapsed.TotalSeconds,
            $PartialPath,
            $FinalPath,
            $_.Exception.Message
        ) -Level error
        throw
    }
}

function Remove-IronStagedDriverArtifacts {
    param(
        [string]$StagingDirectory,
        [Parameter(Mandatory = $true)][string]$Reason
    )

    if ([string]::IsNullOrWhiteSpace($StagingDirectory)) {
        return
    }
    Write-IronLog (
        "[INFO] Staged driver cleanup path: {0}; reason: {1}" -f
        $StagingDirectory,
        $Reason
    ) -Level info
    try {
        if (Test-Path -LiteralPath $StagingDirectory) {
            Remove-Item `
                -LiteralPath $StagingDirectory `
                -Recurse `
                -Force `
                -ErrorAction Stop
        }
    } catch {
        Write-IronLog (
            "[WARN] Failed to remove staged driver path '{0}': {1}" -f
            $StagingDirectory,
            $_.Exception.Message
        ) -Level warn
    }
}

function Wait-IronDriverArchive {
    param(
        [Parameter(Mandatory = $true)][string]$StatusUrl,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [Parameter(Mandatory = $true)][long]$DeploymentId
    )

    $ExpectedStatusUrl = "/api/deploy/{0}/driver-archive" -f $DeploymentId
    if ($StatusUrl -ine $ExpectedStatusUrl -or $TimeoutSeconds -le 0) {
        throw "The driver archive manifest contains invalid metadata"
    }

    $Uri = "{0}/{1}" -f $ApiBaseUrl.TrimEnd('/'), $StatusUrl.TrimStart('/')
    $ExpectedRelativePath = (
        ".irondeploy-archives\{0}\drivers.tar" -f $DeploymentId
    )
    $Deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $LastRequestError = $null
    while ([DateTime]::UtcNow -lt $Deadline) {
        try {
            $Response = Invoke-IronApiRestMethod `
                -Uri $Uri `
                -Method Get `
                -TimeoutSec 15
            $LastRequestError = $null
            $Status = ([string]$Response.status).Trim().ToLowerInvariant()
            if ($Status -eq "ready") {
                if (
                    [string]$Response.archiveRelativePath -ine $ExpectedRelativePath -or
                    [long]$Response.archiveSize -le 0 -or
                    [long]$Response.sourceSize -lt 0 -or
                    [int]$Response.sourceFileCount -le 0 -or
                    [int]$Response.sourceInfCount -le 0
                ) {
                    throw "IronAPI returned invalid driver archive metadata"
                }
                return $Response
            }
            if ($Status -eq "failed") {
                $Reason = ([string]$Response.error).Trim()
                if ([string]::IsNullOrWhiteSpace($Reason)) {
                    $Reason = "the server did not provide an error message"
                }
                throw "Driver TAR preparation failed: $Reason"
            }
            if ($Status -ne "preparing") {
                throw "IronAPI returned unknown driver archive status '$Status'"
            }
        } catch {
            $LastRequestError = $_.Exception.Message
            if (
                $LastRequestError -like "Driver TAR preparation failed:*" -or
                $LastRequestError -like "IronAPI returned invalid driver archive metadata*" -or
                $LastRequestError -like "IronAPI returned unknown driver archive status*"
            ) {
                throw
            }
            Write-IronLog (
                "[WARN] Driver archive status request failed; retrying: {0}" -f
                $LastRequestError
            ) -Level warn
        }
        Start-Sleep -Seconds 2
    }
    $Suffix = if ([string]::IsNullOrWhiteSpace($LastRequestError)) {
        ""
    } else {
        "; last request error: $LastRequestError"
    }
    throw "Timed out waiting for the driver TAR after $TimeoutSeconds seconds$Suffix"
}

function Copy-IronDriverArchiveToLocalStaging {
    param(
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][long]$ExpectedArchiveLength,
        [Parameter(Mandatory = $true)][long]$ExpectedExtractedLength,
        [Parameter(Mandatory = $true)][long]$DeploymentId
    )

    if ($ExpectedArchiveLength -le 0 -or $ExpectedExtractedLength -lt 0) {
        throw "The driver archive contains invalid size metadata"
    }

    $LocalDrive = Get-PSDrive -Name $WindowsDrive.TrimEnd(":")
    $RequiredBytes = $ExpectedArchiveLength + $ExpectedExtractedLength
    if ($null -eq $LocalDrive.Free -or [long]$LocalDrive.Free -lt $RequiredBytes) {
        $FreeBytes = if ($null -eq $LocalDrive.Free) { 0L } else { [long]$LocalDrive.Free }
        throw (
            "Insufficient free space for staged drivers: need {0} bytes, have {1} bytes" -f
            $RequiredBytes,
            $FreeBytes
        )
    }

    $StagingDirectory = Join-Path `
        "$($WindowsDrive.TrimEnd('\'))\IronDeploy.Staging" `
        ([string]$DeploymentId)
    $TransferDirectory = Join-Path $StagingDirectory "archive.partial"
    $ArchivePath = Join-Path $StagingDirectory "drivers.tar"
    New-Item -ItemType Directory -Path $StagingDirectory -Force | Out-Null
    if (Test-Path -LiteralPath $TransferDirectory) {
        Remove-Item -LiteralPath $TransferDirectory -Recurse -Force
    }
    if (Test-Path -LiteralPath $ArchivePath) {
        Remove-Item -LiteralPath $ArchivePath -Force
    }
    New-Item -ItemType Directory -Path $TransferDirectory -Force | Out-Null

    $Stopwatch = [Diagnostics.Stopwatch]::StartNew()
    try {
        Write-IronLog (
            "[STEP] Download driver TAR with robocopy /J: {0} -> {1}" -f
            $SourcePath,
            $TransferDirectory
        ) -Level step
        $SourceDirectory = Split-Path -Parent $SourcePath
        $SourceName = Split-Path -Leaf $SourcePath
        $PreviousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            & robocopy.exe `
                $SourceDirectory `
                $TransferDirectory `
                $SourceName `
                /J `
                /R:2 `
                /W:2 `
                /COPY:DAT `
                /NP `
                /NFL `
                /NDL
            $RobocopyExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $PreviousErrorActionPreference
        }
        if (-not (Test-IronRobocopyExitCode -ExitCode $RobocopyExitCode)) {
            throw "robocopy failed with exit code $RobocopyExitCode"
        }
        $TransferredPath = Join-Path $TransferDirectory $SourceName
        if (-not (Test-Path -LiteralPath $TransferredPath -PathType Leaf)) {
            throw "robocopy did not create the local driver TAR"
        }
        $ActualLength = [long](Get-Item -LiteralPath $TransferredPath).Length
        if ($ActualLength -ne $ExpectedArchiveLength) {
            throw (
                "Downloaded driver TAR size mismatch: expected {0}, received {1}" -f
                $ExpectedArchiveLength,
                $ActualLength
            )
        }

        Move-Item -LiteralPath $TransferredPath -Destination $ArchivePath
        Remove-Item -LiteralPath $TransferDirectory -Recurse -Force
        $Stopwatch.Stop()
        $Seconds = [Math]::Max(0.001, $Stopwatch.Elapsed.TotalSeconds)
        $AverageMegabytesPerSecond = ($ActualLength / 1MB) / $Seconds
        Write-IronLog (
            "[OK] Driver TAR download completed: status=success; bytes={0}; " +
            "duration={1:N3}s; average={2:N3} MB/s; path={3}" -f
            $ActualLength,
            $Seconds,
            $AverageMegabytesPerSecond,
            $ArchivePath
        ) -Level ok
        return [pscustomobject]@{
            ArchivePath = $ArchivePath
            StagingDirectory = $StagingDirectory
            BytesTransferred = [long]$ActualLength
            DurationSeconds = [double]$Seconds
            AverageMegabytesPerSecond = [double]$AverageMegabytesPerSecond
            RobocopyExitCode = [int]$RobocopyExitCode
        }
    } catch {
        $Stopwatch.Stop()
        Write-IronLog (
            "[ERROR] Driver download failed: status=failed; duration={0:N3}s; " +
            "sourcePath={1}; localPath={2}; reason={3}" -f
            $Stopwatch.Elapsed.TotalSeconds,
            $SourcePath,
            $ArchivePath,
            $_.Exception.Message
        ) -Level error
        throw
    }
}

function Expand-IronDriverArchive {
    param(
        [Parameter(Mandatory = $true)][string]$ArchivePath,
        [Parameter(Mandatory = $true)][string]$StagingDirectory,
        [Parameter(Mandatory = $true)][long]$ExpectedLength,
        [Parameter(Mandatory = $true)][int]$ExpectedFileCount,
        [Parameter(Mandatory = $true)][int]$ExpectedInfCount
    )

    if (-not (Test-Path -LiteralPath $SevenZipPath -PathType Leaf)) {
        throw "The bundled x64 7za.exe is missing: $SevenZipPath"
    }
    $PartialPath = Join-Path $StagingDirectory "drivers.partial"
    $FinalPath = Join-Path $StagingDirectory "drivers"
    foreach ($Path in @($PartialPath, $FinalPath)) {
        if (Test-Path -LiteralPath $Path) {
            Remove-Item -LiteralPath $Path -Recurse -Force
        }
    }
    New-Item -ItemType Directory -Path $PartialPath -Force | Out-Null
    Write-IronLog "[STEP] Extract driver TAR with bundled x64 7-Zip" -Level step
    $PreviousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $SevenZipPath `
            x `
            -ttar `
            -y `
            -bso0 `
            -bsp0 `
            $ArchivePath `
            "-o$PartialPath" `
            2>&1 |
            ForEach-Object {
                Write-IronLog ("[7-Zip] {0}" -f ([string]$_)) -Level info
            }
        $SevenZipExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }
    if ($SevenZipExitCode -ne 0) {
        throw "7-Zip extraction failed with exit code $SevenZipExitCode"
    }

    $Files = @(Get-ChildItem -LiteralPath $PartialPath -File -Recurse)
    $ActualLength = [long]($Files | Measure-Object -Property Length -Sum).Sum
    $ActualFileCount = $Files.Count
    $ActualInfCount = @(
        $Files | Where-Object { $_.Extension -ieq ".inf" }
    ).Count
    if (
        $ActualLength -ne $ExpectedLength -or
        $ActualFileCount -ne $ExpectedFileCount -or
        $ActualInfCount -ne $ExpectedInfCount
    ) {
        throw (
            "Extracted driver package metadata mismatch: " +
            "expected bytes/files/INF {0}/{1}/{2}, received {3}/{4}/{5}" -f
            $ExpectedLength,
            $ExpectedFileCount,
            $ExpectedInfCount,
            $ActualLength,
            $ActualFileCount,
            $ActualInfCount
        )
    }
    Move-Item -LiteralPath $PartialPath -Destination $FinalPath
    Remove-Item -LiteralPath $ArchivePath -Force
    Write-IronLog (
        "[OK] Driver TAR extracted: bytes={0}; files={1}; INF={2}; path={3}" -f
        $ActualLength,
        $ActualFileCount,
        $ActualInfCount,
        $FinalPath
    ) -Level ok
    return $FinalPath
}

function Invoke-IronApplyWindowsImage {
    param(
        [Parameter(Mandatory = $true)][string]$ImagePath,
        [Parameter(Mandatory = $true)][int]$ImageIndex
    )

    $TrackImageApplyProgress = (
        [bool]$EnableGuiImageApplyProgress -and
        $null -ne $script:IronProgressCallback
    )
    if ($TrackImageApplyProgress) {
        $PreviousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            & dism.exe `
                /Apply-Image `
                /ImageFile:$ImagePath `
                /Index:$ImageIndex `
                /ApplyDir:C:\ `
                2>&1 |
                ForEach-Object {
                    Update-IronImageApplyProgress -OutputLine ([string]$_)
                }
            return $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $PreviousErrorActionPreference
        }
    }

    dism.exe /Apply-Image /ImageFile:$ImagePath /Index:$ImageIndex /ApplyDir:C:\
    return $LASTEXITCODE
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
    if (
        $NormalizedSerial.Length -gt 128 -or
        $NormalizedSerial -match "[\x00-\x1F\x7F]"
    ) {
        return $false
    }

    return $NormalizedSerial -notmatch (
        "^(To Be Filled By O\.?E\.?M\.?|Default string|" +
        "System Serial Number|Unknown|None|Not Applicable|" +
        "Not Specified|OEM|INVALID)$"
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

function Test-UsableHardwareIdentityValue($Value) {
    if ([string]::IsNullOrWhiteSpace([string]$Value)) {
        return $false
    }

    $NormalizedValue = ([string]$Value).Trim()
    return $NormalizedValue -notmatch (
        "^(To Be Filled By O\.E\.M\.|Default string|Unknown|None|" +
        "Not Applicable|Not Specified|OEM|INVALID)$"
    )
}

# Best-effort manufacturer and System SKU/Product Number. These values are
# deployment metadata only and never block deployment when WMI omits them.
function Get-SystemManufacturerAndSku {
    $Manufacturer = $null
    $SystemSku = $null

    try {
        $ComputerSystem = Get-CimInstance `
            -ClassName Win32_ComputerSystem `
            -ErrorAction Stop
        if (Test-UsableHardwareIdentityValue $ComputerSystem.Manufacturer) {
            $Manufacturer = ([string]$ComputerSystem.Manufacturer).Trim()
        }
        if (Test-UsableHardwareIdentityValue $ComputerSystem.SystemSKUNumber) {
            $SystemSku = ([string]$ComputerSystem.SystemSKUNumber).Trim()
        }
    } catch {
        Write-IronLog (
            "[WARN] Failed to read manufacturer/System SKU from " +
            "Win32_ComputerSystem: $($_.Exception.Message)"
        ) -Level warn
    }

    if (
        [string]::IsNullOrWhiteSpace([string]$Manufacturer) -or
        [string]::IsNullOrWhiteSpace([string]$SystemSku)
    ) {
        try {
            $Product = Get-CimInstance `
                -ClassName Win32_ComputerSystemProduct `
                -ErrorAction Stop
            if (
                [string]::IsNullOrWhiteSpace([string]$Manufacturer) -and
                (Test-UsableHardwareIdentityValue $Product.Vendor)
            ) {
                $Manufacturer = ([string]$Product.Vendor).Trim()
            }
            if (
                [string]::IsNullOrWhiteSpace([string]$SystemSku) -and
                (Test-UsableHardwareIdentityValue $Product.SKUNumber)
            ) {
                $SystemSku = ([string]$Product.SKUNumber).Trim()
            }
        } catch {
            Write-IronLog (
                "[WARN] Failed to read fallback manufacturer/System SKU from " +
                "Win32_ComputerSystemProduct: $($_.Exception.Message)"
            ) -Level warn
        }
    }

    if (
        -not [string]::IsNullOrWhiteSpace([string]$Manufacturer) -and
        $Manufacturer.Length -gt 128
    ) {
        $Manufacturer = $Manufacturer.Substring(0, 128).Trim()
    }
    if (
        -not [string]::IsNullOrWhiteSpace([string]$SystemSku) -and
        $SystemSku.Length -gt 128
    ) {
        $SystemSku = $SystemSku.Substring(0, 128).Trim()
    }

    return [pscustomobject]@{
        Manufacturer = $Manufacturer
        SystemSku = $SystemSku
    }
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
    $Issues = @()

    try {
        $Bios = Get-CimInstance `
            -ClassName Win32_BIOS `
            -ErrorAction Stop
        if (Test-UsableSerialNumber $Bios.SerialNumber) {
            return ([string]$Bios.SerialNumber).Trim()
        }
        $Issues += "Win32_BIOS did not return a usable serial number"
    } catch {
        $Issues += "Win32_BIOS: $($_.Exception.Message)"
    }

    try {
        $Product = Get-CimInstance `
            -ClassName Win32_ComputerSystemProduct `
            -ErrorAction Stop
        if (Test-UsableSerialNumber $Product.IdentifyingNumber) {
            return ([string]$Product.IdentifyingNumber).Trim()
        }
        $Issues += (
            "Win32_ComputerSystemProduct did not return a usable " +
            "identifying number"
        )
    } catch {
        $Issues += "Win32_ComputerSystemProduct: $($_.Exception.Message)"
    }

    Write-IronLog (
        "[WARN] System serial number is unavailable; continuing without it: " +
        ($Issues -join "; ")
    ) -Level warn
    return $null
}

# --- Input gathering (used by both front-ends before deployment) -------------

# Reads the hardware identity used to register the deployment. A usable network
# adapter remains required, while the serial number and descriptive hardware
# fields are best-effort and never block deployment.
function Get-IronDeployHardwareIdentity {
    $MacAddress = Get-PrimaryMacAddress
    $SerialNumber = Get-SystemSerialNumber
    $Model = Get-SystemModel
    $ProductIdentity = Get-SystemManufacturerAndSku

    return [pscustomobject]@{
        SerialNumber = $SerialNumber
        MacAddress = $MacAddress
        Model = $Model
        Manufacturer = $ProductIdentity.Manufacturer
        SystemSku = $ProductIdentity.SystemSku
    }
}

# Physical disks available for destructive deployment. Win32_DiskDrive is
# available in the same WinPE WMI/CIM environment already used for hardware
# identity and exposes the DiskPart disk number through Index.
function Get-IronDeployDiskList {
    try {
        $Disks = @(
            Get-CimInstance -ClassName Win32_DiskDrive -ErrorAction Stop |
                ForEach-Object {
                    $DiskNumber = 0
                    $DiskSize = 0L
                    if (
                        -not [int]::TryParse([string]$_.Index, [ref]$DiskNumber) -or
                        $DiskNumber -lt 0 -or
                        -not [long]::TryParse([string]$_.Size, [ref]$DiskSize) -or
                        $DiskSize -le 0
                    ) {
                        return
                    }
                    $DiskModel = (([string]$_.Model).Trim() -replace '\s+', ' ')
                    if ([string]::IsNullOrWhiteSpace($DiskModel)) {
                        $DiskModel = "Unknown disk"
                    }
                    if ($DiskModel.Length -gt 255) {
                        $DiskModel = $DiskModel.Substring(0, 255).Trim()
                    }
                    [pscustomobject]@{
                        Number = $DiskNumber
                        Model = $DiskModel
                        SizeBytes = $DiskSize
                    }
                } |
                Sort-Object Number
        )
    } catch {
        throw "Failed to enumerate physical disks: $($_.Exception.Message)"
    }
    if ($Disks.Count -eq 0) {
        throw "No usable physical disks were detected."
    }
    return $Disks
}

function Test-IronDeployTargetDisk {
    param(
        [Parameter(Mandatory = $true)][int]$Number,
        [Parameter(Mandatory = $true)][string]$Model,
        [Parameter(Mandatory = $true)][long]$SizeBytes
    )

    if ($Number -lt 0 -or $SizeBytes -le 0 -or [string]::IsNullOrWhiteSpace($Model)) {
        throw "The selected target disk is invalid."
    }
    $CurrentDisk = @(
        Get-IronDeployDiskList | Where-Object { $_.Number -eq $Number }
    ) | Select-Object -First 1
    if ($null -eq $CurrentDisk) {
        throw "Selected disk $Number is no longer available."
    }
    if (
        [long]$CurrentDisk.SizeBytes -ne $SizeBytes -or
        [string]$CurrentDisk.Model -ne $Model
    ) {
        throw (
            "Selected disk {0} changed after confirmation. Expected {1} " +
            "({2} bytes), found {3} ({4} bytes)."
        ) -f $Number, $Model, $SizeBytes, $CurrentDisk.Model, $CurrentDisk.SizeBytes
    }
    return $CurrentDisk
}

# C: or S: can already be a transient WinPE mount on another physical disk.
# Removing that mount point does not erase or repartition the other disk; it
# only makes the letters available for the selected offline Windows volume.
function Remove-IronDeployDriveLetterMountPoint {
    param(
        [Parameter(Mandatory = $true)]
        [ValidatePattern("^[A-Z]:$")]
        [string]$Drive
    )

    $MountPoint = "$Drive\"
    if (!(Test-Path -LiteralPath $MountPoint -ErrorAction SilentlyContinue)) {
        return
    }

    $PreviousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $MountVolOutput = @(
            & mountvol.exe $MountPoint /D 2>&1
        )
        $MountVolExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }

    if ($MountVolExitCode -ne 0) {
        $Details = (@($MountVolOutput | ForEach-Object { [string]$_ }) -join " ").Trim()
        $Message = (
            "Failed to release WinPE drive letter {0} before partitioning" -f
            $Drive
        )
        if ($Details) {
            $Message += ": $Details"
        } else {
            $Message += "."
        }
        throw $Message
    }
    Write-IronLog "[INFO] Released WinPE drive letter $Drive" -Level info
}

# Best-effort computer-name suggestion from IronAPI. Never throws: when IronAPI
# is unavailable it returns null suggestions and an empty known-name list, so a
# name can still be entered manually.
function Get-IronDeployNameSuggestion {
    param(
        [AllowNull()]
        [AllowEmptyString()]
        [string]$SerialNumber = "",

        [Parameter(Mandatory = $true)]
        [string]$MacAddress
    )

    $NameFormats = @()
    $KnownComputerNames = @()

    try {
        $SuggestionQueryParts = @()
        if (-not [string]::IsNullOrWhiteSpace([string]$SerialNumber)) {
            $SuggestionQueryParts += (
                "serial_number=" +
                [uri]::EscapeDataString(([string]$SerialNumber).Trim())
            )
        }
        $SuggestionQueryParts += (
            "mac_address=$([uri]::EscapeDataString($MacAddress))"
        )
        $SuggestionQuery = $SuggestionQueryParts -join "&"
        $NameResponse = Invoke-IronApiRestMethod `
            -Uri "$($ApiBaseUrl.TrimEnd('/'))/api/deploy/suggest-name?$SuggestionQuery" `
            -Method Get `
            -TimeoutSec 10 `
            -UseBasicParsing

        $NameFormats = @($NameResponse.formats) |
            Where-Object { $null -ne $_ }
        $KnownComputerNames = @($NameResponse.known_computer_names) |
            Where-Object { $null -ne $_ }
    } catch {
        Write-IronLog "[WARN] IronAPI unavailable: $($_.Exception.Message)" -Level warn
        $NameFormats = @()
        $KnownComputerNames = @()
    }

    return [pscustomobject]@{
        NameFormats = @($NameFormats)
        KnownComputerNames = @($KnownComputerNames)
    }
}

# Validates a computer name. Returns the normalised (lower-case) name or throws
# a plain error when it is not a valid Windows computer name. IronAPI owns and
# authoritatively validates the configured naming formats.
function Test-IronDeployComputerName {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$ComputerName
    )

    $Trimmed = ([string]$ComputerName).Trim()
    if (
        $Trimmed.Length -gt 15 -or
        $Trimmed -notmatch "^(?i:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)$"
    ) {
        throw "Invalid Windows computer name"
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

function Get-IronDeployPostPowerShellList {
    $Catalog = Get-IronDeployCatalog
    return @(
        $Catalog.postPowerShell | Where-Object { [bool]$_.available } | ForEach-Object {
            $Phase = if ([string]$_.runPhase -eq "before_software") {
                "before software"
            } else {
                "after software"
            }
            $Mode = if ([string]$_.selectionMode -eq "automatic") {
                "automatic"
            } else {
                "operator"
            }
            [pscustomobject]@{
                Name = [string]$_.name
                Automatic = ([string]$_.selectionMode -eq "automatic")
                Display = ("{0}   ({1}, {2}, timeout {3}s)" -f `
                    ([string]$_.name),
                    $Phase,
                    $Mode,
                    ([int]$_.timeoutSeconds))
            }
        }
    )
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

        [Parameter(Mandatory = $true)]
        [int]$SelectedDiskNumber,

        [Parameter(Mandatory = $true)]
        [string]$SelectedDiskModel,

        [Parameter(Mandatory = $true)]
        [long]$SelectedDiskSizeBytes,

        [string[]]$SelectedProgramNames = @(),

        [string[]]$SelectedPostPowerShellNames = @(),

        [string]$SelectedDriverPackage = ""
    )

    $script:DeploymentErrorReported = $false
    $script:IronNetworkDiagnostics = $null
    $script:ImageApplyMode = "direct"
    $script:DriverApplyMode = "direct"
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
    $SelectedPostPowerShellNames = @(
        $SelectedPostPowerShellNames | Where-Object {
            ![string]::IsNullOrWhiteSpace([string]$_)
        }
    )
    $SelectedDriverPackage = ([string]$SelectedDriverPackage).Trim()
    $SelectedDisk = Test-IronDeployTargetDisk `
        -Number $SelectedDiskNumber `
        -Model $SelectedDiskModel `
        -SizeBytes $SelectedDiskSizeBytes

    Set-IronProgress 2 "Reading hardware identity"
    Write-IronLog "[STEP] Read hardware identity" -Level step
    $Hardware = Get-IronDeployHardwareIdentity
    $SerialNumber = $Hardware.SerialNumber
    $MacAddress = $Hardware.MacAddress
    $SystemModel = $Hardware.Model
    $Manufacturer = $Hardware.Manufacturer
    $SystemSku = $Hardware.SystemSku
    if ([string]::IsNullOrWhiteSpace([string]$SerialNumber)) {
        Write-IronLog (
            "[WARN] System serial number is unavailable; deployment will use " +
            "the MAC address for hardware matching"
        ) -Level warn
    } else {
        Write-IronLog "[INFO] System serial number: $SerialNumber" -Level info
    }
    Write-IronLog "[INFO] Primary MAC address: $MacAddress" -Level info
    if ([string]::IsNullOrWhiteSpace([string]$SystemModel)) {
        Write-IronLog "[WARN] Hardware model is unknown" -Level warn
    } else {
        Write-IronLog "[INFO] Hardware model: $SystemModel" -Level info
    }
    if ([string]::IsNullOrWhiteSpace([string]$Manufacturer)) {
        Write-IronLog "[WARN] Hardware manufacturer is unknown" -Level warn
    } else {
        Write-IronLog "[INFO] Hardware manufacturer: $Manufacturer" -Level info
    }
    if ([string]::IsNullOrWhiteSpace([string]$SystemSku)) {
        Write-IronLog "[WARN] System SKU/Product Number is unknown" -Level warn
    } else {
        Write-IronLog "[INFO] System SKU/Product Number: $SystemSku" -Level info
    }

    Write-IronLog "[OK] Selected computer name: $ComputerName" -Level ok
    Write-IronLog (
        "[OK] Selected target disk: disk {0}, {1}, {2:N2} GiB" -f `
            $SelectedDisk.Number,
            $SelectedDisk.Model,
            ($SelectedDisk.SizeBytes / 1GB)
    ) -Level ok
    if ($UseDomainJoinValue) {
        Write-IronLog "[MODE] Domain join ENABLED" -Level ok
    } else {
        Write-IronLog "[MODE] Domain join DISABLED" -Level warn
    }

    Set-IronProgress 4 "Registering deployment"
    Write-IronLog "[STEP] Register deployment start" -Level step
    $BeginPayload = @{
        computer_name = $ComputerName
        serial_number = if (
            [string]::IsNullOrWhiteSpace([string]$SerialNumber)
        ) {
            $null
        } else {
            [string]$SerialNumber
        }
        mac_address = $MacAddress
        model = if ([string]::IsNullOrWhiteSpace([string]$SystemModel)) {
            $null
        } else {
            [string]$SystemModel
        }
        manufacturer = if (
            [string]::IsNullOrWhiteSpace([string]$Manufacturer)
        ) {
            $null
        } else {
            [string]$Manufacturer
        }
        system_sku = if ([string]::IsNullOrWhiteSpace([string]$SystemSku)) {
            $null
        } else {
            [string]$SystemSku
        }
        target_disk_number = [int]$SelectedDisk.Number
        target_disk_model = [string]$SelectedDisk.Model
        target_disk_size_bytes = [long]$SelectedDisk.SizeBytes
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
        Write-IronNetworkLinkSpeedSummary
        Send-IronNetworkAdapterSnapshot
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
        post_powershell_names = @($SelectedPostPowerShellNames)
        driver_package = if (
            [string]::IsNullOrWhiteSpace($SelectedDriverPackage)
        ) {
            $null
        } else {
            $SelectedDriverPackage
        }
    }
    if ($SelectedDriverPackage -in @("AUTO_LOCAL", "AUTO_LOCAL_WSUS")) {
        . (Join-Path $PSScriptRoot "IronDeploy.DriverInventory.ps1")
        $ManifestPayload.driver_package = $null
        $ManifestPayload.driver_mode = $SelectedDriverPackage
        $ManifestPayload.hardware_inventory = Get-IronDriverInventory
        Write-IronLog ("[INFO] AUTO inventory: {0} devices" -f $ManifestPayload.hardware_inventory.devices.Count)
    }
    $ManifestPayload = $ManifestPayload | ConvertTo-Json -Depth 12
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

    $script:ImageApplyMode = Resolve-IronImageApplyMode `
        -Value $DeploymentPlan.imageApplyMode
    Write-IronLog (
        "[MODE] Image apply strategy: {0}" -f $script:ImageApplyMode
    ) -Level info
    $script:DriverApplyMode = Resolve-IronDriverApplyMode `
        -Value $DeploymentPlan.driverApplyMode
    Write-IronLog (
        "[MODE] Driver apply strategy: {0}" -f $script:DriverApplyMode
    ) -Level info

    $SelectedImage = [pscustomobject]@{
        Name = [string]$DeploymentPlan.image.name
        FullName = "$($ImagesPath.TrimEnd('\'))\$($DeploymentPlan.image.name)"
        Length = [long]$DeploymentPlan.image.size
        Sha256 = [string]$DeploymentPlan.image.sha256
    }
    if (
        $script:ImageApplyMode -eq "staged" -and
        $SelectedImage.Sha256 -notmatch '^[0-9a-fA-F]{64}$'
    ) {
        Fail (
            "IronAPI manifest does not contain a valid SHA-256 for staged image '{0}'." -f
            $SelectedImage.Name
        )
    }
    $ImagePath = $SelectedImage.FullName
    $ImageIndexToApply = [int]$DeploymentPlan.image.defaultIndex
    $SelectedPrograms = @($DeploymentPlan.programs | ForEach-Object { $_ })
    $SelectedPostPowerShell = @(
        $DeploymentPlan.postPowerShell | ForEach-Object { $_ }
    )
    $DriverPackagePlan = $DeploymentPlan.driverPackage
    foreach ($driverWarning in @($DeploymentPlan.driverResolution.warnings)) {
        if ($driverWarning) { Write-IronLog ("[WARN] {0}" -f $driverWarning) -Level warn }
    }
    if ($null -ne $DeploymentPlan.driverResolution) {
        Write-IronLog ("[INFO] AUTO drivers: matched {0}/{1}; candidate packages: {2}" -f `
            $DeploymentPlan.driverResolution.matched_devices, `
            $DeploymentPlan.driverResolution.devices_detected, `
            @($DeploymentPlan.driverResolution.candidate_packages).Count)
    }
    $DriverArchivePlan = $DeploymentPlan.driverArchive
    $DriverArchiveStatusUrl = ""
    $DriverArchiveWaitTimeoutSeconds = 0
    $DriverPackagePath = $null
    $DriverPackageRelativePath = ""
    $DriverPackageSize = 0L
    $DriverPackageFileCount = 0
    $DriverPackageInfCount = 0
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
        $DriverPackageSize = [long]$DriverPackagePlan.size
        $DriverPackageFileCount = [int]$DriverPackagePlan.fileCount
        $DriverPackageInfCount = [int]$DriverPackagePlan.infCount
        if (
            $DriverPackageSize -lt 0 -or
            $DriverPackageInfCount -le 0 -or
            (
                $script:DriverApplyMode -eq "staged" -and
                $DriverPackageFileCount -le 0
            )
        ) {
            Fail "IronAPI returned invalid driver package metadata."
        }
        if ($script:DriverApplyMode -eq "staged") {
            $DriverArchiveStatusUrl = [string]$DriverArchivePlan.statusUrl
            $DriverArchiveWaitTimeoutSeconds = `
                [int]$DriverArchivePlan.waitTimeoutSeconds
            if (
                $null -eq $DriverArchivePlan -or
                [string]::IsNullOrWhiteSpace($DriverArchiveStatusUrl) -or
                $DriverArchiveWaitTimeoutSeconds -le 0
            ) {
                Fail "IronAPI did not provide a valid staged driver archive plan."
            }
        } else {
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
            $ActualDriverPackageSize = [long](
                $DriverFiles |
                    Measure-Object -Property Length -Sum
            ).Sum
            $ExpectedDriverFileCount = $DriverPackageFileCount
            if ($ExpectedDriverFileCount -le 0) {
                # Backward compatibility with manifests issued before fileCount.
                $ExpectedDriverFileCount = $DriverFiles.Count
            }
            if (
                $ActualDriverPackageSize -ne $DriverPackageSize -or
                $DriverFiles.Count -ne $ExpectedDriverFileCount -or
                $AvailableDrivers.Count -ne $DriverPackageInfCount
            ) {
                Fail (
                    "Selected driver package does not match the API manifest: " +
                    $DriverPackageRelativePath
                )
            }
            $DriverPackageFileCount = $ExpectedDriverFileCount
        }
        if ($DriverPackageInfCount -eq 0) {
            Fail (
                "Selected driver package contains no INF files: " +
                $DriverPackageRelativePath
            )
        }
    }

    if (!(Test-Path $DiskPartScript)) {
        Fail "DiskPart script not found: $DiskPartScript"
    }
    $DiskPartTemplate = Get-Content -LiteralPath $DiskPartScript -Raw
    if ($DiskPartTemplate -notmatch '\{\{TARGET_DISK_NUMBER\}\}') {
        Fail "DiskPart script does not contain the target disk placeholder."
    }

    Write-IronLog "[OK] Files found" -Level ok
    if ($null -ne $DriverPackagePlan) {
        Write-IronLog (
            "[OK] Selected driver package: {0} ({1} INF files)" -f `
                $DriverPackageRelativePath,
                $DriverPackageInfCount
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

    # Re-read the hardware immediately before the destructive command. If disk
    # numbering or identity changed since confirmation, stop without wiping.
    $SelectedDisk = Test-IronDeployTargetDisk `
        -Number $SelectedDiskNumber `
        -Model $SelectedDiskModel `
        -SizeBytes $SelectedDiskSizeBytes
    Remove-IronDeployDriveLetterMountPoint -Drive $WindowsDrive
    Remove-IronDeployDriveLetterMountPoint -Drive $EfiDrive
    $GeneratedDiskPartScript = "X:\IronDeploy\diskpart-target-$PID.txt"
    $DiskPartTemplate.Replace(
        "{{TARGET_DISK_NUMBER}}",
        ([string]$SelectedDisk.Number)
    ) | Out-File -LiteralPath $GeneratedDiskPartScript -Encoding ASCII -Force

    Set-IronProgress 22 "Wiping and partitioning disk $($SelectedDisk.Number)"
    Write-IronLog (
        "[STEP] DiskPart wipe and partition disk {0} ({1}, {2:N2} GiB)" -f `
            $SelectedDisk.Number,
            $SelectedDisk.Model,
            ($SelectedDisk.SizeBytes / 1GB)
    ) -Level step
    Start-DeploymentStage "disk_partitioning"
    try {
        $PreviousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            $DiskPartOutput = @(
                & diskpart.exe /s $GeneratedDiskPartScript 2>&1
            )
            $DiskPartExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $PreviousErrorActionPreference
        }
        foreach ($DiskPartLine in $DiskPartOutput) {
            if (-not [string]::IsNullOrWhiteSpace([string]$DiskPartLine)) {
                Write-IronLog "[DISKPART] $DiskPartLine" -Level info
            }
        }
        if ($DiskPartExitCode -ne 0) {
            $DiskPartDetails = @(
                $DiskPartOutput |
                    ForEach-Object { ([string]$_).Trim() } |
                    Where-Object { $_ } |
                    Select-Object -Last 8
            ) -join " | "
            $DiskPartError = "DiskPart failed with exit code $DiskPartExitCode"
            if ($DiskPartDetails) {
                $DiskPartError += ": $DiskPartDetails"
            }
            Fail $DiskPartError
        }
    } finally {
        Remove-Item `
            -LiteralPath $GeneratedDiskPartScript `
            -Force `
            -ErrorAction SilentlyContinue
    }
    Complete-DeploymentStage "disk_partitioning"

    $ImagePathToApply = $ImagePath
    $StagedImageDirectory = $null
    if ($script:ImageApplyMode -eq "staged") {
        Set-IronProgress 26 "Downloading Windows image"
        Start-DeploymentStage "image_download"
        try {
            $DownloadResult = Copy-IronImageToLocalStaging `
                -SourcePath $ImagePath `
                -ExpectedLength $SelectedImage.Length `
                -ExpectedSha256 $SelectedImage.Sha256 `
                -DeploymentId $script:DeploymentId
            $ImagePathToApply = $DownloadResult.Path
            $StagedImageDirectory = $DownloadResult.StagingDirectory
            Complete-DeploymentStage "image_download"
        } catch {
            $DownloadFailure = $_.Exception.Message
            if ([string]::IsNullOrWhiteSpace($StagedImageDirectory)) {
                $StagedImageDirectory = Join-Path `
                    "$($WindowsDrive.TrimEnd('\'))\IronDeploy.Staging" `
                    ([string]$script:DeploymentId)
            }
            Remove-IronStagedImageArtifacts `
                -StagingDirectory $StagedImageDirectory `
                -Reason $DownloadFailure
            Fail "Staged image download failed: $DownloadFailure"
        }
    }

    Set-IronProgress 30 "Applying Windows image"
    Write-IronLog (
        "[STEP] Apply Windows image from {0}" -f $ImagePathToApply
    ) -Level step
    Start-DeploymentStage "image_apply"
    try {
        $ImageApplyExitCode = Invoke-IronApplyWindowsImage `
            -ImagePath $ImagePathToApply `
            -ImageIndex $ImageIndexToApply
        if ($ImageApplyExitCode -ne 0) {
            throw "DISM Apply-Image failed"
        }
    } catch {
        $ImageApplyFailure = $_.Exception.Message
        if ($script:ImageApplyMode -eq "staged") {
            Remove-IronStagedImageArtifacts `
                -StagingDirectory $StagedImageDirectory `
                -Reason $ImageApplyFailure
        }
        Fail $ImageApplyFailure
    }
    Complete-DeploymentStage "image_apply"
    if ($script:ImageApplyMode -eq "staged") {
        Remove-IronStagedImageArtifacts `
            -StagingDirectory $StagedImageDirectory `
            -Reason "successful image apply"
    }

    if ($null -ne $DriverPackagePlan) {
        $DriverPackagePathToInject = $DriverPackagePath
        $StagedDriverDirectory = $null
        $StagedDriverArchivePath = $null
        if ($script:DriverApplyMode -eq "staged") {
            Set-IronProgress 60 "Downloading driver package"
            Start-DeploymentStage "driver_download"
            try {
                Write-IronLog "[STEP] Wait for server-side driver TAR" -Level step
                $DriverArchive = Wait-IronDriverArchive `
                    -StatusUrl $DriverArchiveStatusUrl `
                    -TimeoutSeconds $DriverArchiveWaitTimeoutSeconds `
                    -DeploymentId $script:DeploymentId
                $DriverPackageSize = [long]$DriverArchive.sourceSize
                $DriverPackageFileCount = [int]$DriverArchive.sourceFileCount
                $DriverPackageInfCount = [int]$DriverArchive.sourceInfCount
                $DriverArchiveSourcePath = (
                    "{0}\{1}" -f `
                        $DriversPath.TrimEnd("\"),
                        ([string]$DriverArchive.archiveRelativePath)
                )
                $DriverDownloadResult = Copy-IronDriverArchiveToLocalStaging `
                    -SourcePath $DriverArchiveSourcePath `
                    -ExpectedArchiveLength ([long]$DriverArchive.archiveSize) `
                    -ExpectedExtractedLength $DriverPackageSize `
                    -DeploymentId $script:DeploymentId
                $StagedDriverDirectory = $DriverDownloadResult.StagingDirectory
                $StagedDriverArchivePath = $DriverDownloadResult.ArchivePath
                Complete-DeploymentStage "driver_download"
            } catch {
                $DriverDownloadFailure = $_.Exception.Message
                if ([string]::IsNullOrWhiteSpace($StagedDriverDirectory)) {
                    $StagedDriverDirectory = Join-Path `
                        "$($WindowsDrive.TrimEnd('\'))\IronDeploy.Staging" `
                        ([string]$script:DeploymentId)
                }
                Remove-IronStagedDriverArtifacts `
                    -StagingDirectory $StagedDriverDirectory `
                    -Reason $DriverDownloadFailure
                Fail "Staged driver download failed: $DriverDownloadFailure"
            }
        }

        if ($script:DriverApplyMode -eq "staged") {
            Set-IronProgress 63 "Extracting driver package"
        } else {
            Set-IronProgress 66 "Injecting driver package"
        }
        Write-IronLog (
            "[STEP] Stage the selected driver package in offline Windows; " +
            "PnP selects compatible packages on first boot"
        ) -Level step
        Start-DeploymentStage "driver_injection"
        try {
            if ($script:DriverApplyMode -eq "staged") {
                $DriverPackagePathToInject = Expand-IronDriverArchive `
                    -ArchivePath $StagedDriverArchivePath `
                    -StagingDirectory $StagedDriverDirectory `
                    -ExpectedLength $DriverPackageSize `
                    -ExpectedFileCount $DriverPackageFileCount `
                    -ExpectedInfCount $DriverPackageInfCount
                Set-IronProgress 66 "Injecting driver package"
            }
            dism.exe /Image:C:\ /Add-Driver /Driver:$DriverPackagePathToInject /Recurse
            if ($LASTEXITCODE -notin @(0, 3010)) {
                throw "DISM Add-Driver failed with exit code $LASTEXITCODE"
            }
        } catch {
            $DriverInjectionFailure = $_.Exception.Message
            if ($script:DriverApplyMode -eq "staged") {
                Remove-IronStagedDriverArtifacts `
                    -StagingDirectory $StagedDriverDirectory `
                    -Reason $DriverInjectionFailure
            }
            Fail $DriverInjectionFailure
        }
        Complete-DeploymentStage "driver_injection"
        if ($script:DriverApplyMode -eq "staged") {
            Remove-IronStagedDriverArtifacts `
                -StagingDirectory $StagedDriverDirectory `
                -Reason "successful driver injection"
        }
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
        image_apply_mode = $script:ImageApplyMode
        driver_apply_mode = $script:DriverApplyMode
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

    $PostInstallBaseUrl = (
        "{0}/api/deploy/{1}/postinstall" -f `
            $ApiBaseUrl.TrimEnd("/"),
            $script:DeploymentId
    )
    $SetupCompleteTarget = Join-Path $SetupScriptsDir "SetupComplete.cmd"
    $PostInstallScriptTarget = Join-Path $SetupScriptsDir "postinstall.ps1"
    $SetupCompleteDownload = "$SetupCompleteTarget.download"
    $PostInstallScriptDownload = "$PostInstallScriptTarget.download"

    try {
        Invoke-IronApiWebRequest `
            -Uri "$PostInstallBaseUrl/setup-complete" `
            -Method Get `
            -OutFile $SetupCompleteDownload `
            -TimeoutSec 30 | Out-Null
        Invoke-IronApiWebRequest `
            -Uri "$PostInstallBaseUrl/script" `
            -Method Get `
            -OutFile $PostInstallScriptDownload `
            -TimeoutSec 30 | Out-Null

        foreach ($DownloadedFile in @(
            $SetupCompleteDownload,
            $PostInstallScriptDownload
        )) {
            if (
                !(Test-Path -LiteralPath $DownloadedFile -PathType Leaf) -or
                (Get-Item -LiteralPath $DownloadedFile).Length -le 0
            ) {
                throw "Downloaded post-install file is empty: $DownloadedFile"
            }
        }

        Move-Item `
            -LiteralPath $SetupCompleteDownload `
            -Destination $SetupCompleteTarget `
            -Force
        Move-Item `
            -LiteralPath $PostInstallScriptDownload `
            -Destination $PostInstallScriptTarget `
            -Force
    } catch {
        Remove-Item `
            -LiteralPath $SetupCompleteDownload `
            -Force `
            -ErrorAction SilentlyContinue
        Remove-Item `
            -LiteralPath $PostInstallScriptDownload `
            -Force `
            -ErrorAction SilentlyContinue
        Fail "Failed to download post-install scripts: $($_.Exception.Message)"
    }

    if (!(Test-Path -LiteralPath $SetupCompleteTarget -PathType Leaf)) {
        Fail "SetupComplete.cmd was not downloaded"
    }

    if (!(Test-Path -LiteralPath $PostInstallScriptTarget -PathType Leaf)) {
        Fail "postinstall.ps1 was not downloaded"
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

    $PostPowerShellTargetDir = "C:\IronDeploy\PostPowerShell"
    New-Item -ItemType Directory -Force $PostPowerShellTargetDir | Out-Null
    $PostPowerShellManifest = @()
    foreach ($SelectedScript in $SelectedPostPowerShell) {
        $ScriptName = [string]$SelectedScript.name
        $ScriptTarget = Join-Path $PostPowerShellTargetDir $ScriptName
        $ScriptDownload = "$ScriptTarget.download"
        $ScriptReady = $false
        $ScriptFailureStatus = ""
        $ScriptFailure = ""
        try {
            $ScriptUrl = "{0}{1}" -f `
                $ApiBaseUrl.TrimEnd("/"),
                ([string]$SelectedScript.downloadUrl)
            Invoke-IronApiWebRequest `
                -Uri $ScriptUrl `
                -Method Get `
                -OutFile $ScriptDownload `
                -TimeoutSec 120 | Out-Null
            if (!(Test-Path -LiteralPath $ScriptDownload -PathType Leaf)) {
                throw "Downloaded script file is missing"
            }
            $ActualSize = (Get-Item -LiteralPath $ScriptDownload).Length
            if ($ActualSize -ne [long]$SelectedScript.size) {
                throw (
                    "Downloaded size mismatch: expected {0}, got {1}" -f `
                        ([long]$SelectedScript.size),
                        $ActualSize
                )
            }
            $ActualHash = (
                Get-FileHash -LiteralPath $ScriptDownload -Algorithm SHA256
            ).Hash.ToLowerInvariant()
            $ExpectedHash = ([string]$SelectedScript.sha256).ToLowerInvariant()
            if ($ActualHash -ne $ExpectedHash) {
                $ScriptFailureStatus = "hash_mismatch"
                throw "SHA-256 mismatch. Script was not staged."
            }
            Move-Item -LiteralPath $ScriptDownload -Destination $ScriptTarget -Force
            $ScriptReady = $true
            Write-IronLog (
                "[OK] Post-PowerShell script staged and verified: {0}" -f `
                    $ScriptName
            ) -Level ok
        } catch {
            if ([string]::IsNullOrWhiteSpace($ScriptFailureStatus)) {
                $ScriptFailureStatus = "download_failed"
            }
            $ScriptFailure = $_.Exception.Message
            Remove-Item -LiteralPath $ScriptDownload, $ScriptTarget `
                -Force -ErrorAction SilentlyContinue
            Write-IronLog (
                "[WARN] Post-PowerShell script unavailable: {0}: {1}" -f `
                    $ScriptName,
                    $ScriptFailure
            ) -Level warn
        }
        $PostPowerShellManifest += @{
            position = [int]$SelectedScript.position
            name = $ScriptName
            path = $ScriptTarget
            ready = $ScriptReady
            preflightStatus = $ScriptFailureStatus
            preflightError = $ScriptFailure
            selectionMode = [string]$SelectedScript.selectionMode
            runPhase = [string]$SelectedScript.runPhase
            arguments = [string]$SelectedScript.arguments
            timeoutSeconds = [int]$SelectedScript.timeoutSeconds
            maxOutputBytes = [long]$SelectedScript.maxOutputBytes
            sha256 = ([string]$SelectedScript.sha256).ToLowerInvariant()
            reportUrl = [string]$SelectedScript.reportUrl
        }
    }
    $PostPowerShellManifestPath = Join-Path `
        $PostPowerShellTargetDir `
        "post-powershell.json"
    ConvertTo-Json -InputObject @($PostPowerShellManifest) -Depth 4 |
        Out-File $PostPowerShellManifestPath -Encoding UTF8 -Force

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
        ImageApplyMode = $script:ImageApplyMode
        DriverApplyMode = $script:DriverApplyMode
    }
}
