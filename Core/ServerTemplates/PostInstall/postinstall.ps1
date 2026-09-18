$LogDir = "C:\IronDeploy"
$LogFile = "$LogDir\postinstall.log"
$MarkerFile = "$LogDir\postinstall-ok.txt"
$DeploymentStateFile = "$LogDir\deployment.json"
$NotificationErrorFile = "$LogDir\postinstall-api-failed.txt"
$PostInstallConfigPath = Join-Path $PSScriptRoot "IronDeployPostInstall.config.ps1"
$ProgramsDir = "$LogDir\Programs"
$ProgramsManifestFile = "$ProgramsDir\programs.json"
$ProgramInstallTimeoutSeconds = 6 * 60
$PostPowerShellDir = "$LogDir\PostPowerShell"
$PostPowerShellManifestFile = "$PostPowerShellDir\post-powershell.json"
$PostPowerShellResultsDir = "$PostPowerShellDir\Results"
$PostPowerShellDefaultMaxOutputBytes = 20MB
$CompleteMaxAttempts = 12
$CompleteRetryDelaySeconds = 10

# SetupComplete redirects this script's console output into a log file. Use
# UTF-8 explicitly so localized Windows and native-command messages remain
# readable instead of being decoded through the legacy OEM code page.
$Utf8OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$StrictUtf8OutputEncoding = New-Object System.Text.UTF8Encoding($false, $true)
[Console]::OutputEncoding = $Utf8OutputEncoding
$OutputEncoding = $Utf8OutputEncoding

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

function Initialize-IronApiTransport {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ApiBaseUrl,

        [bool]$ValidateApiServerCertificate = $false,

        [ValidateSet("self_signed", "ca")]
        [string]$ApiServerCertificateType = "self_signed",

        [string]$ApiServerCertificateBase64 = ""
    )
    if ($ValidateApiServerCertificate -and $ApiBaseUrl -notmatch "^https://") {
        throw "API certificate validation requires an https:// ApiBaseUrl"
    }
    [System.Net.WebRequest]::DefaultWebProxy = $null
    if ($ApiBaseUrl -notmatch "^https://") {
        return
    }
    [System.Net.ServicePointManager]::SecurityProtocol = `
        [System.Net.SecurityProtocolType]::Tls12
    [System.Net.ServicePointManager]::CheckCertificateRevocationList = $false
    if ($ValidateApiServerCertificate) {
        $Thumbprint = Install-IronApiTrustedCertificate `
            -CertificateBase64 $ApiServerCertificateBase64 `
            -CertificateType $ApiServerCertificateType
        [System.Net.ServicePointManager]::ServerCertificateValidationCallback = $null
        Write-Host "IronAPI certificate validation enabled: $Thumbprint" `
            -ForegroundColor Green
        return
    }
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
        ) { return true; }
    }
}
'@
    }
    [System.Net.ServicePointManager]::ServerCertificateValidationCallback = `
        [IronDeploy.InsecureCertificateValidator]::Callback
    Write-Host "IronAPI certificate validation bypass enabled" `
        -ForegroundColor Yellow
}

function Complete-Deployment {
    param(
        [Parameter(Mandatory = $true)]
        [long]$DeploymentId,

        [Parameter(Mandatory = $true)]
        [string]$ApiBaseUrl,

        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [object[]]$ProgramResults,

        [Parameter(Mandatory = $true)]
        [string]$ApiDeploymentToken,

        [bool]$ValidateApiServerCertificate = $false,

        [ValidateSet("self_signed", "ca")]
        [string]$ApiServerCertificateType = "self_signed",

        [string]$ApiServerCertificateBase64 = ""
    )
    $CompleteBody = @{
        programs = @($ProgramResults)
    } | ConvertTo-Json -Depth 4

    $CompleteUrl = (
        "{0}/api/deploy/{1}/complete" -f `
            $ApiBaseUrl.TrimEnd("/"),
            $DeploymentId
    )
    if ($ValidateApiServerCertificate -and $ApiBaseUrl -notmatch "^https://") {
        throw "API certificate validation requires an https:// ApiBaseUrl"
    }
    [System.Net.WebRequest]::DefaultWebProxy = $null
    if ($ApiBaseUrl -match "^https://") {
        [System.Net.ServicePointManager]::SecurityProtocol = `
            [System.Net.SecurityProtocolType]::Tls12
        [System.Net.ServicePointManager]::CheckCertificateRevocationList = $false

        if ($ValidateApiServerCertificate) {
            $Thumbprint = Install-IronApiTrustedCertificate `
                -CertificateBase64 $ApiServerCertificateBase64 `
                -CertificateType $ApiServerCertificateType
            [System.Net.ServicePointManager]::ServerCertificateValidationCallback = $null
            Write-Host (
                "IronAPI certificate validation enabled ({0}): {1}" -f `
                    $ApiServerCertificateType,
                    $Thumbprint
            ) -ForegroundColor Green
        } else {
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
            Write-Host "IronAPI certificate validation bypass enabled" `
                -ForegroundColor Yellow
        }
    }

    for ($Attempt = 1; $Attempt -le $CompleteMaxAttempts; $Attempt++) {
        try {
            Write-Host (
                "Notify IronAPI: deployment #{0}, attempt {1}/{2}" -f `
                    $DeploymentId,
                    $Attempt,
                    $CompleteMaxAttempts
            )

            $Response = Invoke-RestMethod `
                -Uri $CompleteUrl `
                -Method Post `
                -Headers @{ Authorization = "Bearer $ApiDeploymentToken" } `
                -Body $CompleteBody `
                -ContentType "application/json; charset=utf-8" `
                -TimeoutSec 10 `
                -UseBasicParsing

            if ([string]$Response.status -ne "completed") {
                throw "IronAPI returned status '$($Response.status)'"
            }

            Write-Host (
                "Deployment #{0} marked completed" -f $DeploymentId
            ) -ForegroundColor Green
            return $true
        } catch {
            Write-Host (
                "IronAPI notification failed: {0}" -f $_.Exception.Message
            ) -ForegroundColor Yellow

            if ($Attempt -lt $CompleteMaxAttempts) {
                Start-Sleep -Seconds $CompleteRetryDelaySeconds
            }
        }
    }

    return $false
}

function Get-LocalUserBySidSuffix {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SidSuffix
    )

    return Get-CimInstance Win32_UserAccount -Filter "LocalAccount=True" |
        Where-Object { $_.SID -like "*$SidSuffix" } |
        Select-Object -First 1
}

function Get-LocalUserByName {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    return Get-CimInstance Win32_UserAccount -Filter "LocalAccount=True" |
        Where-Object { $_.Name -ieq $Name } |
        Select-Object -First 1
}

function Set-LocalUserActive {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,

        [Parameter(Mandatory = $true)]
        [bool]$Enabled
    )

    $State = if ($Enabled) { "yes" } else { "no" }
    & net.exe user $Name "/active:$State"
    if ($LASTEXITCODE -ne 0) {
        throw "net user '$Name' /active:$State failed with exit code $LASTEXITCODE"
    }
}

function Invoke-LocalAdminPolicy {
    Write-Host "Local administrator policy"

    if (Test-Path -LiteralPath $PostInstallConfigPath -PathType Leaf) {
        Write-Host "Loading post-install config: $PostInstallConfigPath"
        . $PostInstallConfigPath
    }
    else {
        Write-Host "Post-install config not found; local admin policy skipped." `
            -ForegroundColor Yellow
        return
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

    if ($EnableBuiltInAdministrator) {
        $BuiltInAdministrator = Get-LocalUserBySidSuffix -SidSuffix "-500"
        if ($null -eq $BuiltInAdministrator) {
            Write-Host "Built-in Administrator SID *-500 was not found." `
                -ForegroundColor Yellow
        }
        else {
            Write-Host (
                "Enabling built-in Administrator account: {0} ({1})" -f `
                    $BuiltInAdministrator.Name,
                    $BuiltInAdministrator.SID
            )
            Set-LocalUserActive `
                -Name ([string]$BuiltInAdministrator.Name) `
                -Enabled $true
        }
    }
    else {
        Write-Host "Built-in Administrator enable step disabled."
    }

    if (-not $EnableSetupLocalAdmin) {
        if ([string]::IsNullOrWhiteSpace([string]$SetupLocalAdminName)) {
            Write-Host "Setup local admin name is empty; disable step skipped." `
                -ForegroundColor Yellow
            return
        }

        $SetupLocalAdmin = Get-LocalUserByName `
            -Name ([string]$SetupLocalAdminName)
        if ($null -eq $SetupLocalAdmin) {
            Write-Host (
                "Setup local admin account not found: {0}" -f `
                    $SetupLocalAdminName
            ) -ForegroundColor Yellow
            return
        }
        if ([string]$SetupLocalAdmin.SID -like "*-500") {
            Write-Host (
                "Refusing to disable setup admin '{0}' because it is SID *-500." -f `
                    $SetupLocalAdmin.Name
            ) -ForegroundColor Yellow
            return
        }

        Write-Host (
            "Disabling setup local admin account: {0} ({1})" -f `
                $SetupLocalAdmin.Name,
                $SetupLocalAdmin.SID
        )
        Set-LocalUserActive `
            -Name ([string]$SetupLocalAdmin.Name) `
            -Enabled $false
    }
    else {
        Write-Host "Setup local admin account remains enabled."
    }
}

function Install-IronDeployPrograms {
    # Use a native PowerShell array. Windows PowerShell 5.1 can throw
    # "Argument types do not match" when @() enumerates List[object], which
    # would discard an otherwise successful installation report.
    $Results = @()

    if (!(Test-Path $ProgramsManifestFile -PathType Leaf)) {
        Write-Host "No post-install programs manifest; software install skipped."
        return @($Results)
    }

    try {
        $ParsedPrograms = Get-Content $ProgramsManifestFile -Raw | ConvertFrom-Json
        # Windows PowerShell 5.1 emits a JSON array as one System.Object[]
        # pipeline item. Enumerate it explicitly; otherwise .name returns all
        # names and casting that array to [string] joins them with spaces.
        $Programs = @($ParsedPrograms | ForEach-Object { $_ })
    } catch {
        Write-Host "Failed to read program manifest: $($_.Exception.Message)" `
            -ForegroundColor Red
        return @($Results)
    }

    Write-Host ("Installing {0} post-install program(s)" -f $Programs.Count)

    foreach ($Program in $Programs) {
        $ProgramName = [string]$Program.name
        $ProgramArguments = [string]$Program.arguments
        $ProgramPath = Join-Path $ProgramsDir $ProgramName
        $Stopwatch = [System.Diagnostics.Stopwatch]::StartNew()

        if ([string]::IsNullOrWhiteSpace($ProgramName)) {
            Write-Host "Skipping manifest entry without a program name." `
                -ForegroundColor Yellow
            continue
        }
        if (!(Test-Path -LiteralPath $ProgramPath -PathType Leaf)) {
            Write-Host "Program file not found: $ProgramPath" `
                -ForegroundColor Yellow
            $Stopwatch.Stop()
            $Results += [pscustomobject]@{
                name = $ProgramName
                status = "failed"
                exit_code = $null
                duration_seconds = [int][Math]::Round($Stopwatch.Elapsed.TotalSeconds)
                error_message = "Program file not found."
            }
            continue
        }

        $ExpectedHash = ([string]$Program.sha256).ToLowerInvariant()
        $ActualHash = ""
        try {
            $ActualHash = (
                Get-FileHash -LiteralPath $ProgramPath -Algorithm SHA256
            ).Hash.ToLowerInvariant()
        } catch {
            Write-Host (
                "{0} SHA-256 calculation failed: {1}" -f `
                    $ProgramName,
                    $_.Exception.Message
            ) -ForegroundColor Red
        }
        if (
            $ExpectedHash -notmatch "^[0-9a-f]{64}$" -or
            $ActualHash -ne $ExpectedHash
        ) {
            $Stopwatch.Stop()
            Write-Host ("{0} SHA-256 mismatch; installation blocked." -f $ProgramName) `
                -ForegroundColor Red
            $Results += [pscustomobject]@{
                name = $ProgramName
                status = "failed"
                reason = "hash_mismatch"
                exit_code = $null
                duration_seconds = [int][Math]::Round($Stopwatch.Elapsed.TotalSeconds)
                error_message = "SHA-256 mismatch. Program was not started."
            }
            continue
        }

        Write-Host ("Installing {0} {1}" -f $ProgramName, $ProgramArguments)
        try {
            if ($ProgramName -match "\.msi$") {
                $MsiArguments = "/i `"$ProgramPath`""
                if (![string]::IsNullOrWhiteSpace($ProgramArguments)) {
                    $MsiArguments += " $ProgramArguments"
                }
                $Process = Start-Process `
                    -FilePath msiexec.exe `
                    -ArgumentList $MsiArguments `
                    -PassThru
            } elseif (![string]::IsNullOrWhiteSpace($ProgramArguments)) {
                $Process = Start-Process `
                    -FilePath $ProgramPath `
                    -ArgumentList $ProgramArguments `
                    -PassThru
            } else {
                $Process = Start-Process -FilePath $ProgramPath -PassThru
            }

            $Exited = $Process.WaitForExit($ProgramInstallTimeoutSeconds * 1000)
            if (-not $Exited) {
                $Stopwatch.Stop()
                Write-Host (
                    "{0} timed out after {1} minutes; terminating it and continuing." -f `
                        $ProgramName,
                        ($ProgramInstallTimeoutSeconds / 60)
                ) -ForegroundColor Yellow
                & taskkill.exe /PID $Process.Id /T /F 2>&1 | Out-Null
                $Results += [pscustomobject]@{
                    name = $ProgramName
                    status = "timed_out"
                    exit_code = $null
                    duration_seconds = [int][Math]::Round($Stopwatch.Elapsed.TotalSeconds)
                    error_message = "Installation timed out after 6 minutes."
                }
                continue
            }

            # Ensure redirected/native process state is fully populated before
            # reading ExitCode after the timed wait.
            $Process.WaitForExit()
            $Stopwatch.Stop()
            $ExitCode = $Process.ExitCode
            if ($ExitCode -in @(0, 3010)) {
                Write-Host ("{0} installed (exit code {1})" -f $ProgramName, $ExitCode) `
                    -ForegroundColor Green
                $ProgramStatus = "installed"
                $ErrorMessage = $null
            } else {
                Write-Host ("{0} exited with code {1}" -f $ProgramName, $ExitCode) `
                    -ForegroundColor Yellow
                $ProgramStatus = "failed"
                $ErrorMessage = "Installer exited with code $ExitCode."
            }
            $Results += [pscustomobject]@{
                name = $ProgramName
                status = $ProgramStatus
                exit_code = $ExitCode
                duration_seconds = [int][Math]::Round($Stopwatch.Elapsed.TotalSeconds)
                error_message = $ErrorMessage
            }
        } catch {
            $Stopwatch.Stop()
            Write-Host ("{0} failed to start: {1}" -f $ProgramName, $_.Exception.Message) `
                -ForegroundColor Red
            $Results += [pscustomobject]@{
                name = $ProgramName
                status = "failed"
                exit_code = $null
                duration_seconds = [int][Math]::Round($Stopwatch.Elapsed.TotalSeconds)
                error_message = [string]$_.Exception.Message
            }
        }
    }

    return @($Results)
}

function Send-IronPostPowerShellReport {
    param(
        [Parameter(Mandatory = $true)]
        [object]$DeploymentState,

        [Parameter(Mandatory = $true)]
        [object]$Script,

        [Parameter(Mandatory = $true)]
        [string]$Status,

        [AllowNull()]
        [Nullable[int]]$ExitCode,

        [int]$DurationSeconds = 0,

        [Parameter(Mandatory = $true)]
        [string]$OutputPath,

        [long]$OutputTotalBytes = 0,

        [bool]$OutputTruncated = $false,

        [string]$ErrorMessage = ""
    )
    $ReportUrl = "{0}{1}" -f `
        ([string]$DeploymentState.api_base_url).TrimEnd("/"),
        ([string]$Script.reportUrl)
    $Headers = @{
        Authorization = "Bearer $([string]$DeploymentState.api_deployment_token)"
        "x-irondeploy-status" = $Status
        "x-irondeploy-duration-seconds" = [string]$DurationSeconds
        "x-irondeploy-output-total-bytes" = [string]$OutputTotalBytes
        "x-irondeploy-output-truncated" = if ($OutputTruncated) { "true" } else { "false" }
        "x-irondeploy-error" = [Uri]::EscapeDataString($ErrorMessage)
    }
    if ($null -ne $ExitCode) {
        $Headers["x-irondeploy-exit-code"] = [string]$ExitCode
    }
    for ($Attempt = 1; $Attempt -le 3; $Attempt++) {
        try {
            Invoke-WebRequest `
                -Uri $ReportUrl `
                -Method Post `
                -Headers $Headers `
                -InFile $OutputPath `
                -ContentType "text/plain; charset=utf-8" `
                -TimeoutSec 60 `
                -UseBasicParsing | Out-Null
            return $true
        } catch {
            Write-Host (
                "Failed to report {0} (attempt {1}/3): {2}" -f `
                    ([string]$Script.name),
                    $Attempt,
                    $_.Exception.Message
            ) -ForegroundColor Yellow
            if ($Attempt -lt 3) { Start-Sleep -Seconds 2 }
        }
    }
    return $false
}

function Invoke-IronPostPowerShellPhase {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("before_software", "after_software")]
        [string]$RunPhase,

        [AllowNull()]
        [object]$DeploymentState
    )
    if (!(Test-Path $PostPowerShellManifestFile -PathType Leaf)) {
        Write-Host "No post-PowerShell manifest; phase $RunPhase skipped."
        return
    }
    try {
        $Scripts = @(
            Get-Content $PostPowerShellManifestFile -Raw | ConvertFrom-Json |
                ForEach-Object { $_ }
        )
    } catch {
        Write-Host "Failed to read post-PowerShell manifest: $($_.Exception.Message)" `
            -ForegroundColor Red
        return
    }
    New-Item -ItemType Directory -Force $PostPowerShellResultsDir | Out-Null
    foreach ($Script in @($Scripts | Where-Object { $_.runPhase -eq $RunPhase })) {
        $OutputPath = Join-Path `
            $PostPowerShellResultsDir `
            ("{0:D4}-{1}.log" -f ([int]$Script.position), ([string]$Script.name))
        New-Item -ItemType File -Path $OutputPath -Force | Out-Null
        $Status = "failed"
        $ExitCode = $null
        $DurationSeconds = 0
        $OutputTotalBytes = 0L
        $OutputTruncated = $false
        $ErrorMessage = ""

        if (-not [bool]$Script.ready) {
            $Status = [string]$Script.preflightStatus
            if ($Status -notin @("hash_mismatch", "download_failed")) {
                $Status = "download_failed"
            }
            $ErrorMessage = [string]$Script.preflightError
        } else {
            $ScriptPath = [string]$Script.path
            try {
                $ActualHash = (
                    Get-FileHash -LiteralPath $ScriptPath -Algorithm SHA256
                ).Hash.ToLowerInvariant()
                if ($ActualHash -ne ([string]$Script.sha256).ToLowerInvariant()) {
                    $Status = "hash_mismatch"
                    $ErrorMessage = "SHA-256 mismatch. Script was not started."
                    throw $ErrorMessage
                }

                $MaxOutputBytes = [long]$Script.maxOutputBytes
                if ($MaxOutputBytes -le 0) {
                    $MaxOutputBytes = $PostPowerShellDefaultMaxOutputBytes
                }
                $TimeoutSeconds = [int]$Script.timeoutSeconds
                $StartInfo = New-Object System.Diagnostics.ProcessStartInfo
                $StartInfo.FileName = "powershell.exe"
                $StartInfo.Arguments = (
                    "-NoProfile -NonInteractive -ExecutionPolicy Bypass " +
                    "-File `"$ScriptPath`""
                )
                if (![string]::IsNullOrWhiteSpace([string]$Script.arguments)) {
                    $StartInfo.Arguments += " $([string]$Script.arguments)"
                }
                $StartInfo.UseShellExecute = $false
                # Inherit this process's UTF-8 console code page so Windows
                # PowerShell 5.1 emits localized stdout/stderr as UTF-8. A
                # hidden window style prevents a console flash if postinstall
                # is started manually without an existing console.
                $StartInfo.CreateNoWindow = $false
                $StartInfo.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
                $StartInfo.RedirectStandardOutput = $true
                $StartInfo.RedirectStandardError = $true
                $StartInfo.StandardOutputEncoding = $Utf8OutputEncoding
                $StartInfo.StandardErrorEncoding = $Utf8OutputEncoding

                $Process = New-Object System.Diagnostics.Process
                $Process.StartInfo = $StartInfo
                $Stopwatch = [Diagnostics.Stopwatch]::StartNew()
                if (!$Process.Start()) { throw "PowerShell process did not start." }
                $StdOutTask = $Process.StandardOutput.ReadLineAsync()
                $StdErrTask = $Process.StandardError.ReadLineAsync()
                $StdOutDone = $false
                $StdErrDone = $false
                $OutputStream = [IO.File]::Open(
                    $OutputPath,
                    [IO.FileMode]::Create,
                    [IO.FileAccess]::Write,
                    [IO.FileShare]::Read
                )
                try {
                    while (-not ($StdOutDone -and $StdErrDone -and $Process.HasExited)) {
                        $Tasks = @()
                        if (!$StdOutDone) { $Tasks += $StdOutTask }
                        if (!$StdErrDone) { $Tasks += $StdErrTask }
                        if ($Tasks.Count -gt 0) {
                            $AnyTask = [Threading.Tasks.Task]::WhenAny(
                                [Threading.Tasks.Task[]]$Tasks
                            )
                            if ($AnyTask.Wait(100)) {
                                $CompletedTask = $AnyTask.Result
                                $Line = [string]$CompletedTask.Result
                                if ($null -eq $CompletedTask.Result) {
                                    if ($CompletedTask -eq $StdOutTask) { $StdOutDone = $true }
                                    if ($CompletedTask -eq $StdErrTask) { $StdErrDone = $true }
                                } else {
                                    $Bytes = $Utf8OutputEncoding.GetBytes(
                                        $Line + [Environment]::NewLine
                                    )
                                    $OutputTotalBytes += $Bytes.Length
                                    $Remaining = $MaxOutputBytes - $OutputStream.Length
                                    if ($Remaining -gt 0) {
                                        $WriteCount = [int][Math]::Min($Remaining, $Bytes.Length)
                                        while ($WriteCount -gt 0) {
                                            try {
                                                [void]$StrictUtf8OutputEncoding.GetString(
                                                    $Bytes,
                                                    0,
                                                    $WriteCount
                                                )
                                                break
                                            } catch {
                                                # At most three bytes are removed when the
                                                # limit cuts through one UTF-8 code point.
                                                $WriteCount--
                                            }
                                        }
                                        $OutputStream.Write($Bytes, 0, $WriteCount)
                                    }
                                    if ($OutputTotalBytes -gt $MaxOutputBytes) {
                                        $OutputTruncated = $true
                                    }
                                }
                                if ($CompletedTask -eq $StdOutTask -and !$StdOutDone) {
                                    $StdOutTask = $Process.StandardOutput.ReadLineAsync()
                                }
                                if ($CompletedTask -eq $StdErrTask -and !$StdErrDone) {
                                    $StdErrTask = $Process.StandardError.ReadLineAsync()
                                }
                            }
                        } else {
                            Start-Sleep -Milliseconds 100
                        }
                        if (!$Process.HasExited -and $Stopwatch.Elapsed.TotalSeconds -ge $TimeoutSeconds) {
                            & taskkill.exe /PID $Process.Id /T /F 2>&1 | Out-Null
                            $Status = "timed_out"
                            $ErrorMessage = "Script exceeded timeout of $TimeoutSeconds seconds."
                        }
                    }
                    $Process.WaitForExit()
                    if ($Status -ne "timed_out") {
                        $ExitCode = [int]$Process.ExitCode
                        if ($ExitCode -eq 0) {
                            $Status = "succeeded"
                        } else {
                            $Status = "failed"
                            $ErrorMessage = "Script exited with code $ExitCode."
                        }
                    }
                } finally {
                    $OutputStream.Dispose()
                    $Stopwatch.Stop()
                    $DurationSeconds = [int][Math]::Min(
                        86400,
                        [Math]::Ceiling($Stopwatch.Elapsed.TotalSeconds)
                    )
                    $Process.Dispose()
                }
            } catch {
                if ($Status -notin @("hash_mismatch", "timed_out")) {
                    $Status = "failed"
                    $ErrorMessage = $_.Exception.Message
                }
            }
        }

        Write-Host (
            "Post-PowerShell {0}: status={1}; exit={2}; duration={3}s; truncated={4}" -f `
                ([string]$Script.name),
                $Status,
                $(if ($null -eq $ExitCode) { "-" } else { $ExitCode }),
                $DurationSeconds,
                $OutputTruncated
        )
        if ($null -ne $DeploymentState) {
            [void](Send-IronPostPowerShellReport `
                -DeploymentState $DeploymentState `
                -Script $Script `
                -Status $Status `
                -ExitCode $ExitCode `
                -DurationSeconds $DurationSeconds `
                -OutputPath $OutputPath `
                -OutputTotalBytes $OutputTotalBytes `
                -OutputTruncated $OutputTruncated `
                -ErrorMessage $ErrorMessage)
        } else {
            Write-Host "PowerShell result not reported: deployment state unavailable." `
                -ForegroundColor Yellow
        }
    }
}

function Invoke-IronDeploymentApi {
    param(
        [Parameter(Mandatory = $true)][object]$DeploymentState,
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [ValidateSet("Get", "Post")][string]$Method = "Get",
        [AllowNull()][object]$Body = $null
    )
    $Parameters = @{
        Uri = "{0}{1}" -f `
            ([string]$DeploymentState.api_base_url).TrimEnd("/"), $RelativePath
        Method = $Method
        Headers = @{
            Authorization = "Bearer $([string]$DeploymentState.api_deployment_token)"
        }
        TimeoutSec = 60
        UseBasicParsing = $true
    }
    if ($null -ne $Body) {
        $Parameters.Body = $Body | ConvertTo-Json -Depth 10
        $Parameters.ContentType = "application/json; charset=utf-8"
    }
    return Invoke-RestMethod @Parameters
}

function Get-IronInstalledDriverInventory {
    $ComputerSystem = Get-CimInstance Win32_ComputerSystem
    $OperatingSystem = Get-CimInstance Win32_OperatingSystem
    $ComputerProduct = Get-CimInstance Win32_ComputerSystemProduct
    $SignedByDevice = @{}
    foreach ($Driver in @(Get-CimInstance Win32_PnPSignedDriver)) {
        $DeviceId = [string]$Driver.DeviceID
        if (![string]::IsNullOrWhiteSpace($DeviceId) -and !$SignedByDevice.ContainsKey($DeviceId)) {
            $SignedByDevice[$DeviceId] = $Driver
        }
    }
    $Devices = @()
    foreach ($Device in @(Get-CimInstance Win32_PnPEntity)) {
        $InstanceId = [string]$Device.PNPDeviceID
        if ([string]::IsNullOrWhiteSpace($InstanceId)) { continue }
        $Signed = $SignedByDevice[$InstanceId]
        $Devices += [pscustomobject]@{
            instance_id = $InstanceId
            hardware_ids = @($Device.HardwareID | Where-Object { $_ })
            compatible_ids = @($Device.CompatibleID | Where-Object { $_ })
            device_class = [string]$Device.PNPClass
            status = [string]$Device.Status
            problem_code = [int]$Device.ConfigManagerErrorCode
            device_name = [string]$Device.Name
            driver_inf_name = if ($null -ne $Signed) { [string]$Signed.InfName } else { $null }
            driver_provider = if ($null -ne $Signed) { [string]$Signed.DriverProviderName } else { $null }
            driver_version = if ($null -ne $Signed) { [string]$Signed.DriverVersion } else { $null }
        }
    }
    $Architecture = if ([Environment]::Is64BitOperatingSystem) { "amd64" } else { "x86" }
    return [pscustomobject]@{
        inventory = [pscustomobject]@{
            manufacturer = [string]$ComputerSystem.Manufacturer
            model = [string]$ComputerSystem.Model
            family = [string]$ComputerSystem.SystemFamily
            sku = [string]$ComputerProduct.Name
            architecture = $Architecture
            devices = @($Devices)
            warnings = @()
        }
        os_version = [string]$OperatingSystem.Version
        product_type = [int]$OperatingSystem.ProductType
    }
}

function Suspend-IronWindowsUpdateDrivers {
    $Path = "HKLM:\Software\Policies\Microsoft\Windows\WindowsUpdate"
    $Name = "ExcludeWUDriversInQualityUpdate"
    $Existing = Get-ItemProperty -LiteralPath $Path -Name $Name -ErrorAction SilentlyContinue
    $State = [pscustomobject]@{
        Path = $Path
        Name = $Name
        Existed = $null -ne $Existing
        Value = if ($null -ne $Existing) { $Existing.$Name } else { $null }
    }
    New-Item -Path $Path -Force | Out-Null
    New-ItemProperty -LiteralPath $Path -Name $Name -PropertyType DWord -Value 1 -Force |
        Out-Null
    Write-Host "Windows Update driver offers held during local reconciliation."
    return $State
}

function Resume-IronWindowsUpdateDrivers {
    param([Parameter(Mandatory = $true)][object]$State)
    if ([bool]$State.Existed) {
        New-ItemProperty -LiteralPath ([string]$State.Path) `
            -Name ([string]$State.Name) -PropertyType DWord `
            -Value ([int]$State.Value) -Force | Out-Null
    } else {
        Remove-ItemProperty -LiteralPath ([string]$State.Path) `
            -Name ([string]$State.Name) -ErrorAction SilentlyContinue
    }
    Write-Host "Previous Windows Update driver policy restored."
}

function Receive-IronDriverArchive {
    param(
        [Parameter(Mandatory = $true)][object]$DeploymentState,
        [Parameter(Mandatory = $true)][int]$PassNumber
    )
    $DeploymentId = [long]$DeploymentState.deployment_id
    $Status = $null
    for ($Attempt = 1; $Attempt -le 120; $Attempt++) {
        $Status = Invoke-IronDeploymentApi -DeploymentState $DeploymentState `
            -RelativePath "/api/deploy/$DeploymentId/driver-archive"
        if ([string]$Status.status -eq "ready") { break }
        if ([string]$Status.status -eq "failed") {
            throw "Driver archive failed: $([string]$Status.error)"
        }
        Start-Sleep -Seconds 2
    }
    if ([string]$Status.status -ne "ready") {
        throw "Driver archive preparation timed out"
    }

    $Credentials = Invoke-IronDeploymentApi -DeploymentState $DeploymentState `
        -RelativePath "/api/deploy/$DeploymentId/smb-credentials"
    $SecurePassword = ConvertTo-SecureString ([string]$Credentials.password) `
        -AsPlainText -Force
    $Credential = New-Object System.Management.Automation.PSCredential(
        [string]$Credentials.username, $SecurePassword
    )
    $DriveName = "IRD$PID"
    $LocalTar = Join-Path $LogDir "driver-pass-$PassNumber.tar"
    Remove-Item -LiteralPath $LocalTar -Force -ErrorAction SilentlyContinue
    try {
        New-PSDrive -Name $DriveName -PSProvider FileSystem `
            -Root ([string]$Credentials.share_path) -Credential $Credential `
            -Scope Script -ErrorAction Stop | Out-Null
        $DriversRoot = Join-Path "$($DriveName):\" "Drivers"
        $RemoteTar = Join-Path $DriversRoot ([string]$Status.archiveRelativePath)
        Copy-Item -LiteralPath $RemoteTar -Destination $LocalTar `
            -Force -ErrorAction Stop
    } finally {
        Remove-PSDrive -Name $DriveName -Scope Script -Force -ErrorAction SilentlyContinue
    }
    $ActualSize = (Get-Item -LiteralPath $LocalTar -ErrorAction Stop).Length
    $ExpectedSize = [long]$Status.archiveSize
    if ($ActualSize -ne $ExpectedSize) {
        throw "Downloaded driver archive size mismatch (expected $ExpectedSize, got $ActualSize)"
    }
    $ActualHash = (Get-FileHash -LiteralPath $LocalTar -Algorithm SHA256).Hash
    if ($ActualHash -ine [string]$Status.archiveSha256) {
        throw "Downloaded driver archive SHA-256 mismatch"
    }
    return $LocalTar
}

function Install-IronDriverArchive {
    param(
        [Parameter(Mandatory = $true)][string]$ArchivePath,
        [Parameter(Mandatory = $true)][int]$PassNumber
    )
    $Root = Join-Path $LogDir "DriverReconciliation"
    $Partial = Join-Path $Root "pass-$PassNumber.partial"
    $Destination = Join-Path $Root "pass-$PassNumber"
    New-Item -ItemType Directory -Path $Root -Force | Out-Null
    Remove-Item -LiteralPath $Partial -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $Destination -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $Partial -Force | Out-Null
    & "$env:SystemRoot\System32\tar.exe" -xf $ArchivePath -C $Partial
    if ($LASTEXITCODE -ne 0) {
        throw "tar.exe failed with exit code $LASTEXITCODE"
    }
    if (@(Get-ChildItem -LiteralPath $Partial -Recurse -Filter *.inf -File).Count -eq 0) {
        throw "Driver archive contains no INF files"
    }
    Move-Item -LiteralPath $Partial -Destination $Destination
    $InfPattern = Join-Path $Destination "*.inf"
    & pnputil.exe /add-driver $InfPattern /subdirs /install 2>&1 | Write-Host
    $ExitCode = $LASTEXITCODE
    if ($ExitCode -notin @(0, 3010)) {
        throw "pnputil /add-driver failed with exit code $ExitCode"
    }
    & pnputil.exe /scan-devices 2>&1 | Write-Host
    if ($LASTEXITCODE -ne 0) {
        throw "pnputil /scan-devices failed with exit code $LASTEXITCODE"
    }
    Start-Sleep -Seconds 2
    $RebootRequired = $ExitCode -eq 3010
    Remove-Item -LiteralPath $Destination -Recurse -Force -ErrorAction SilentlyContinue
    return $RebootRequired
}

function Invoke-IronDriverReconciliation {
    param([AllowNull()][object]$DeploymentState)
    $Result = [pscustomobject]@{ RebootRequired = $false; Warnings = @() }
    if ($null -eq $DeploymentState) {
        Write-Host "Installed-Windows driver reconciliation skipped."
        return $Result
    }
    $DeploymentId = [long]$DeploymentState.deployment_id
    $DriverMode = [string]$DeploymentState.driver_mode
    if ([string]::IsNullOrWhiteSpace($DriverMode)) {
        try {
            $Configuration = Invoke-IronDeploymentApi `
                -DeploymentState $DeploymentState `
                -RelativePath "/api/deploy/$DeploymentId/drivers/reconcile"
            $DriverMode = [string]$Configuration.driverMode
        } catch {
            $Result.Warnings += "Driver reconciliation config failed: $($_.Exception.Message)"
            Write-Host $Result.Warnings[-1] -ForegroundColor Yellow
            return $Result
        }
    }
    if ($DriverMode -notin @("AUTO_LOCAL", "AUTO_LOCAL_WSUS")) {
        Write-Host "Installed-Windows driver reconciliation skipped for mode $DriverMode."
        return $Result
    }
    $PolicyState = $null
    try {
        $PolicyState = Suspend-IronWindowsUpdateDrivers
        & pnputil.exe /scan-devices 2>&1 | Write-Host
        if ($LASTEXITCODE -ne 0) {
            $Result.Warnings += "Initial device scan failed with exit code $LASTEXITCODE"
        }
        Start-Sleep -Seconds 2
        for ($Pass = 1; $Pass -le 3; $Pass++) {
            Write-Host "Local driver reconciliation pass $Pass/3"
            $Snapshot = Get-IronInstalledDriverInventory
            $Body = @{
                pass_number = $Pass
                inventory = $Snapshot.inventory
                os_version = $Snapshot.os_version
                product_type = $Snapshot.product_type
            }
            $Response = Invoke-IronDeploymentApi -DeploymentState $DeploymentState `
                -RelativePath "/api/deploy/$DeploymentId/drivers/reconcile" `
                -Method Post -Body $Body
            if ([string]$Response.status -eq "no_new_drivers") {
                Write-Host "No new local driver packages found; reconciliation complete."
                break
            }
            $Archive = $null
            try {
                $Archive = Receive-IronDriverArchive `
                    -DeploymentState $DeploymentState -PassNumber $Pass
                if (Install-IronDriverArchive -ArchivePath $Archive -PassNumber $Pass) {
                    $Result.RebootRequired = $true
                }
            } finally {
                if ($null -ne $Archive) {
                    Remove-Item -LiteralPath $Archive -Force -ErrorAction SilentlyContinue
                }
                try {
                    Invoke-IronDeploymentApi -DeploymentState $DeploymentState `
                        -RelativePath "/api/deploy/$DeploymentId/drivers/reconcile/archive-complete" `
                        -Method Post | Out-Null
                } catch {
                    $Result.Warnings += "Server archive cleanup failed: $($_.Exception.Message)"
                }
            }
        }
    } catch {
        $Message = "Driver reconciliation failed: $($_.Exception.Message)"
        $Result.Warnings += $Message
        Write-Host $Message -ForegroundColor Red
    } finally {
        if ($null -ne $PolicyState) {
            try { Resume-IronWindowsUpdateDrivers -State $PolicyState }
            catch {
                $Result.Warnings += "Windows Update policy restore failed: $($_.Exception.Message)"
                Write-Host $Result.Warnings[-1] -ForegroundColor Red
            }
        }
    }
    try {
        $Final = Get-IronInstalledDriverInventory
        Invoke-IronDeploymentApi -DeploymentState $DeploymentState `
            -RelativePath "/api/deploy/$DeploymentId/drivers/reconcile/final" `
            -Method Post -Body @{
                inventory = $Final.inventory
                os_version = $Final.os_version
                product_type = $Final.product_type
                reboot_required = [bool]$Result.RebootRequired
                warnings = @($Result.Warnings)
            } | Out-Null
    } catch {
        Write-Host "Final driver report failed: $($_.Exception.Message)" `
            -ForegroundColor Yellow
    }
    return $Result
}

New-Item -ItemType Directory -Force $LogDir | Out-Null
Remove-Item -LiteralPath $MarkerFile -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath "$LogDir\postinstall-reboot-required.txt" `
    -Force -ErrorAction SilentlyContinue

Start-Transcript -Path $LogFile -Append

Write-Host "IronDeploy postinstall started"
Write-Host "Date: $(Get-Date)"

$cs = Get-CimInstance Win32_ComputerSystem

Write-Host "ComputerName: $env:COMPUTERNAME"
Write-Host "Domain: $($cs.Domain)"
Write-Host "PartOfDomain: $($cs.PartOfDomain)"

ipconfig /all | Out-File "$LogDir\network.txt" -Encoding UTF8

$DeploymentState = $null
if (Test-Path $DeploymentStateFile -PathType Leaf) {
    try {
        $DeploymentState = Get-Content $DeploymentStateFile -Raw | ConvertFrom-Json
    } catch {
        Write-Host "Failed to read deployment state: $($_.Exception.Message)" `
            -ForegroundColor Red
        $DeploymentState = $null
    }
    try {
        if ($null -eq $DeploymentState) {
            throw "Deployment state is unavailable."
        }
        $CertificateType = [string]$DeploymentState.api_server_certificate_type
        if ([string]::IsNullOrWhiteSpace($CertificateType)) {
            $CertificateType = "self_signed"
        }
        Initialize-IronApiTransport `
            -ApiBaseUrl ([string]$DeploymentState.api_base_url) `
            -ValidateApiServerCertificate ([bool]$DeploymentState.api_validate_server_certificate) `
            -ApiServerCertificateType $CertificateType `
            -ApiServerCertificateBase64 ([string]$DeploymentState.api_server_certificate_base64)
    } catch {
        Write-Host "Failed to initialize IronAPI transport: $($_.Exception.Message)" `
            -ForegroundColor Red
    }
}

if ($cs.PartOfDomain) {
    # SetupComplete runs before Winlogon starts foreground computer policy.
    # Calling gpupdate here creates a circular wait: gpupdate waits for boot
    # policy processing while Windows waits for SetupComplete to exit. Normal
    # synchronous Group Policy processing starts automatically immediately
    # after this script and SetupComplete finish.
    Write-Host (
        "Domain detected; Group Policy will run automatically after " +
        "SetupComplete."
    )
} else {
    Write-Host "No domain detected; Group Policy processing is not expected."
}

$DriverReconciliation = Invoke-IronDriverReconciliation `
    -DeploymentState $DeploymentState

try {
    Invoke-LocalAdminPolicy
} catch {
    Write-Host "Local administrator policy failed: $($_.Exception.Message)" `
        -ForegroundColor Red
}

Invoke-IronPostPowerShellPhase `
    -RunPhase "before_software" `
    -DeploymentState $DeploymentState

try {
    $ProgramResults = @(Install-IronDeployPrograms)
} catch {
    $ProgramResults = @()
    Write-Host "Post-install software installation failed: $($_.Exception.Message)" `
        -ForegroundColor Red
}

Invoke-IronPostPowerShellPhase `
    -RunPhase "after_software" `
    -DeploymentState $DeploymentState

"OK: postinstall completed at $(Get-Date)" | Out-File $MarkerFile -Encoding UTF8
if (
    [bool]$DriverReconciliation.RebootRequired -or
    @($ProgramResults | Where-Object { $_.exit_code -eq 3010 }).Count -gt 0
) {
    "Post-install processing requested a reboot." |
        Out-File "$LogDir\postinstall-reboot-required.txt" -Encoding UTF8 -Force
}

if (!(Test-Path $DeploymentStateFile -PathType Leaf)) {
    "Deployment state file not found: $DeploymentStateFile" |
        Out-File $NotificationErrorFile -Encoding UTF8 -Force
    Write-Host "Deployment state file not found; status remains begin" `
        -ForegroundColor Red
} else {
    try {
        $DeploymentState = Get-Content $DeploymentStateFile -Raw |
            ConvertFrom-Json
        $DeploymentId = [long]$DeploymentState.deployment_id
        $ApiBaseUrl = [string]$DeploymentState.api_base_url
        $ApiDeploymentToken = [string]$DeploymentState.api_deployment_token
        $ValidateApiServerCertificate = `
            [bool]$DeploymentState.api_validate_server_certificate
        $ApiServerCertificateType = `
            [string]$DeploymentState.api_server_certificate_type
        $ApiServerCertificateBase64 = `
            [string]$DeploymentState.api_server_certificate_base64
        if ([string]::IsNullOrWhiteSpace($ApiServerCertificateType)) {
            $ApiServerCertificateType = "self_signed"
        }

        if ($DeploymentId -le 0) {
            throw "deployment_id is invalid"
        }
        if ([string]::IsNullOrWhiteSpace($ApiBaseUrl)) {
            throw "api_base_url is missing"
        }
        if ([string]::IsNullOrWhiteSpace($ApiDeploymentToken)) {
            throw "api_deployment_token is missing"
        }

        $Completed = Complete-Deployment `
            -DeploymentId $DeploymentId `
            -ApiBaseUrl $ApiBaseUrl `
            -ProgramResults $ProgramResults `
            -ApiDeploymentToken $ApiDeploymentToken `
            -ValidateApiServerCertificate $ValidateApiServerCertificate `
            -ApiServerCertificateType $ApiServerCertificateType `
            -ApiServerCertificateBase64 $ApiServerCertificateBase64

        if ($Completed) {
            Remove-Item $NotificationErrorFile -Force -ErrorAction SilentlyContinue
        } else {
            $NotificationError = (
                "Failed to notify IronAPI after {0} attempts at {1}. " +
                "Deployment #{2} remains begin."
            ) -f `
                $CompleteMaxAttempts,
                (Get-Date),
                $DeploymentId
            $NotificationError |
                Out-File $NotificationErrorFile -Encoding UTF8 -Force
        }
    } catch {
        "Failed to read deployment state or notify IronAPI: $($_.Exception.Message)" |
            Out-File $NotificationErrorFile -Encoding UTF8 -Force
        Write-Host "Deployment completion notification failed; status remains begin" `
            -ForegroundColor Red
    }
}

# The per-deployment token is needed only for the completion notification. Do
# not leave an extra plaintext copy behind in the installed operating system.
Remove-Item $DeploymentStateFile -Force -ErrorAction SilentlyContinue

Write-Host "IronDeploy postinstall finished"

Stop-Transcript
