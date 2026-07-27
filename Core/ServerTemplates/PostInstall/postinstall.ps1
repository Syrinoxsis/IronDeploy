$LogDir = "C:\IronDeploy"
$LogFile = "$LogDir\postinstall.log"
$MarkerFile = "$LogDir\postinstall-ok.txt"
$DeploymentStateFile = "$LogDir\deployment.json"
$NotificationErrorFile = "$LogDir\postinstall-api-failed.txt"
$PostInstallConfigPath = Join-Path $PSScriptRoot "IronDeployPostInstall.config.ps1"
$ProgramsDir = "$LogDir\Programs"
$ProgramsManifestFile = "$ProgramsDir\programs.json"
$ProgramInstallTimeoutSeconds = 6 * 60
$CompleteMaxAttempts = 12
$CompleteRetryDelaySeconds = 10

# SetupComplete redirects this script's console output into a log file. Use
# UTF-8 explicitly so localized Windows and native-command messages remain
# readable instead of being decoded through the legacy OEM code page.
$Utf8OutputEncoding = New-Object System.Text.UTF8Encoding($false)
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

New-Item -ItemType Directory -Force $LogDir | Out-Null

Start-Transcript -Path $LogFile -Append

Write-Host "IronDeploy postinstall started"
Write-Host "Date: $(Get-Date)"

$cs = Get-CimInstance Win32_ComputerSystem

Write-Host "ComputerName: $env:COMPUTERNAME"
Write-Host "Domain: $($cs.Domain)"
Write-Host "PartOfDomain: $($cs.PartOfDomain)"

ipconfig /all | Out-File "$LogDir\network.txt" -Encoding UTF8

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

try {
    Invoke-LocalAdminPolicy
} catch {
    Write-Host "Local administrator policy failed: $($_.Exception.Message)" `
        -ForegroundColor Red
}

try {
    $ProgramResults = @(Install-IronDeployPrograms)
} catch {
    $ProgramResults = @()
    Write-Host "Post-install software installation failed: $($_.Exception.Message)" `
        -ForegroundColor Red
}

"OK: postinstall completed at $(Get-Date)" | Out-File $MarkerFile -Encoding UTF8

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
