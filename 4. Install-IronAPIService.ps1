#Requires -Version 5.1

<#
.SYNOPSIS
Installs IronAPI as an automatically started Windows Service.

.DESCRIPTION
Prompts for a DOMAIN\user service identity and securely reads its password.
LocalSystem is available only as an explicit, warned alternative. The script
grants SeServiceLogonRight to a domain identity, registers the pywin32 service,
configures restart-on-failure actions, and asks whether to start it now.

The password is sent to the Python registration helper over redirected stdin.
It is never included in process arguments or written to disk.
#>

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ServiceName = "IronAPI"
$IronDeployRoot = Join-Path $PSScriptRoot "Core"
$ApiRoot = Join-Path $IronDeployRoot "Api"
$PythonPath = Join-Path $ApiRoot ".venv\Scripts\python.exe"
$EnvPath = Join-Path $ApiRoot ".env"
$ServiceHostPath = Join-Path $ApiRoot "irondeploy_service.py"
$ScExe = Join-Path $env:SystemRoot "System32\sc.exe"

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

function Read-YesNo {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Prompt,

        [bool]$Default = $false
    )

    $suffix = if ($Default) { "[Y/n]" } else { "[y/N]" }
    while ($true) {
        $answer = (Read-Host "$Prompt $suffix").Trim().ToLowerInvariant()
        if ([string]::IsNullOrWhiteSpace($answer)) {
            return $Default
        }
        if ($answer -in @("y", "yes")) {
            return $true
        }
        if ($answer -in @("n", "no")) {
            return $false
        }
        Write-Host "Enter Y or N." -ForegroundColor Yellow
    }
}

function Test-SecureStringEqual {
    param(
        [Parameter(Mandatory = $true)]
        [Security.SecureString]$First,

        [Parameter(Mandatory = $true)]
        [Security.SecureString]$Second
    )

    if ($First.Length -ne $Second.Length) {
        return $false
    }

    $firstPointer = [IntPtr]::Zero
    $secondPointer = [IntPtr]::Zero
    try {
        $firstPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
            $First
        )
        $secondPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
            $Second
        )
        for ($index = 0; $index -lt $First.Length; $index++) {
            $offset = $index * 2
            if (
                [Runtime.InteropServices.Marshal]::ReadInt16(
                    $firstPointer,
                    $offset
                ) -ne
                [Runtime.InteropServices.Marshal]::ReadInt16(
                    $secondPointer,
                    $offset
                )
            ) {
                return $false
            }
        }
        return $true
    }
    finally {
        if ($firstPointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($firstPointer)
        }
        if ($secondPointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secondPointer)
        }
    }
}

function Read-DomainServiceIdentity {
    while ($true) {
        $account = (Read-Host (
            "Service account in DOMAIN\user format " +
            "(example: DOMAIN\svc_irondeploy)"
        )).Trim()

        if (
            $account -notmatch "^[^\\/:*?`"<>|]+\\[^\\/:*?`"<>|]+$" -or
            $account.StartsWith(".\") -or
            $account.StartsWith(
                "$env:COMPUTERNAME\",
                [StringComparison]::OrdinalIgnoreCase
            ) -or
            $account.StartsWith(
                "BUILTIN\",
                [StringComparison]::OrdinalIgnoreCase
            ) -or
            $account.StartsWith(
                "NT AUTHORITY\",
                [StringComparison]::OrdinalIgnoreCase
            )
        ) {
            Write-Host (
                "Enter an existing domain account as DOMAIN\user."
            ) -ForegroundColor Yellow
            continue
        }

        try {
            $null = (
                New-Object Security.Principal.NTAccount($account)
            ).Translate([Security.Principal.SecurityIdentifier])
        }
        catch {
            Write-Host (
                "Windows could not resolve '$account'. Check the domain " +
                "connection and account name."
            ) -ForegroundColor Yellow
            continue
        }

        $password = Read-Host `
            -Prompt "Password for $account" `
            -AsSecureString
        if ($password.Length -eq 0) {
            $password.Dispose()
            Write-Host "The password cannot be empty." -ForegroundColor Yellow
            continue
        }
        $confirmation = Read-Host `
            -Prompt "Confirm password for $account" `
            -AsSecureString
        $passwordsMatch = Test-SecureStringEqual $password $confirmation
        $confirmation.Dispose()
        if (-not $passwordsMatch) {
            $password.Dispose()
            Write-Host "The passwords do not match." -ForegroundColor Yellow
            continue
        }

        return [pscustomobject]@{
            Mode = "domain"
            Account = $account
            Password = $password
        }
    }
}

function Read-ServiceIdentity {
    Write-Host ""
    Write-Host "Choose the IronAPI service identity:" -ForegroundColor Cyan
    Write-Host "  1. Domain service account (recommended)"
    Write-Host "  2. LocalSystem (high-privilege fallback)"

    while ($true) {
        $choice = (Read-Host "Selection [1]").Trim()
        if ([string]::IsNullOrWhiteSpace($choice)) {
            $choice = "1"
        }

        if ($choice -eq "1") {
            return Read-DomainServiceIdentity
        }
        if ($choice -eq "2") {
            Write-Host ""
            Write-Host "WARNING: LocalSystem has full local-machine control." `
                -ForegroundColor Red
            Write-Host (
                "It also cannot use credentials saved in another user's " +
                "Windows Credential Manager profile."
            ) -ForegroundColor Yellow
            Write-Host (
                "Prefer a dedicated DOMAIN\svc_irondeploy-style account " +
                "with only the required AD, file, and DISM permissions."
            ) -ForegroundColor Yellow
            $confirmation = Read-Host (
                "Type LocalSystem to confirm this high-privilege choice"
            )
            if ($confirmation -cne "LocalSystem") {
                Write-Host "LocalSystem was not confirmed." `
                    -ForegroundColor Yellow
                continue
            }
            return [pscustomobject]@{
                Mode = "local_system"
                Account = "LocalSystem"
                Password = $null
            }
        }

        Write-Host "Enter 1 or 2." -ForegroundColor Yellow
    }
}

function Add-ServiceLogonRight {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Account
    )

    if (-not ("IronDeploy.ServiceLogonRight" -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Principal;

namespace IronDeploy
{
    public static class ServiceLogonRight
    {
        private const UInt32 PolicyCreateAccount = 0x00000010;
        private const UInt32 PolicyLookupNames = 0x00000800;

        [StructLayout(LayoutKind.Sequential)]
        private struct LsaObjectAttributes
        {
            public UInt32 Length;
            public IntPtr RootDirectory;
            public IntPtr ObjectName;
            public UInt32 Attributes;
            public IntPtr SecurityDescriptor;
            public IntPtr SecurityQualityOfService;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct LsaUnicodeString
        {
            public UInt16 Length;
            public UInt16 MaximumLength;
            public IntPtr Buffer;
        }

        [DllImport("advapi32.dll")]
        private static extern UInt32 LsaOpenPolicy(
            IntPtr systemName,
            ref LsaObjectAttributes objectAttributes,
            UInt32 desiredAccess,
            out IntPtr policyHandle
        );

        [DllImport("advapi32.dll")]
        private static extern UInt32 LsaAddAccountRights(
            IntPtr policyHandle,
            IntPtr accountSid,
            LsaUnicodeString[] userRights,
            UInt32 countOfRights
        );

        [DllImport("advapi32.dll")]
        private static extern UInt32 LsaNtStatusToWinError(UInt32 status);

        [DllImport("advapi32.dll")]
        private static extern UInt32 LsaClose(IntPtr policyHandle);

        public static void Grant(string accountName)
        {
            var account = new NTAccount(accountName);
            var sid = (SecurityIdentifier)account.Translate(
                typeof(SecurityIdentifier)
            );
            var sidBytes = new byte[sid.BinaryLength];
            sid.GetBinaryForm(sidBytes, 0);

            IntPtr sidPointer = IntPtr.Zero;
            IntPtr rightPointer = IntPtr.Zero;
            IntPtr policyHandle = IntPtr.Zero;
            try
            {
                sidPointer = Marshal.AllocHGlobal(sidBytes.Length);
                Marshal.Copy(sidBytes, 0, sidPointer, sidBytes.Length);

                const string rightName = "SeServiceLogonRight";
                rightPointer = Marshal.StringToHGlobalUni(rightName);
                var rights = new[]
                {
                    new LsaUnicodeString
                    {
                        Length = (UInt16)(rightName.Length * 2),
                        MaximumLength = (UInt16)((rightName.Length + 1) * 2),
                        Buffer = rightPointer
                    }
                };

                var attributes = new LsaObjectAttributes();
                var status = LsaOpenPolicy(
                    IntPtr.Zero,
                    ref attributes,
                    PolicyCreateAccount | PolicyLookupNames,
                    out policyHandle
                );
                ThrowOnError(status, "LsaOpenPolicy");

                status = LsaAddAccountRights(
                    policyHandle,
                    sidPointer,
                    rights,
                    1
                );
                ThrowOnError(status, "LsaAddAccountRights");
            }
            finally
            {
                if (policyHandle != IntPtr.Zero)
                {
                    LsaClose(policyHandle);
                }
                if (rightPointer != IntPtr.Zero)
                {
                    Marshal.FreeHGlobal(rightPointer);
                }
                if (sidPointer != IntPtr.Zero)
                {
                    Marshal.FreeHGlobal(sidPointer);
                }
            }
        }

        private static void ThrowOnError(UInt32 status, string operation)
        {
            if (status == 0)
            {
                return;
            }
            var error = LsaNtStatusToWinError(status);
            throw new Win32Exception((int)error, operation + " failed");
        }
    }
}
'@
    }

    [IronDeploy.ServiceLogonRight]::Grant($Account)
}

function Invoke-ServiceRegistration {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("install-from-stdin", "update-from-stdin")]
        [string]$Action,

        [Parameter(Mandatory = $true)]
        [pscustomobject]$Identity
    )

    if ($ServiceHostPath.Contains('"')) {
        throw "The service host path cannot contain a double quote."
    }

    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $PythonPath
    $startInfo.Arguments = (
        "`"$ServiceHostPath`" $Action"
    )
    $startInfo.WorkingDirectory = $ApiRoot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true

    $process = New-Object Diagnostics.Process
    $process.StartInfo = $startInfo
    $plainTextPassword = $null
    $passwordPointer = [IntPtr]::Zero
    try {
        if (-not $process.Start()) {
            throw "Failed to start the pywin32 registration helper."
        }

        $process.StandardInput.WriteLine($Identity.Mode)
        $process.StandardInput.WriteLine($Identity.Account)
        if ($null -ne $Identity.Password) {
            $passwordPointer = (
                [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
                    $Identity.Password
                )
            )
            $plainTextPassword = (
                [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
                    $passwordPointer
                )
            )
            $process.StandardInput.WriteLine($plainTextPassword)
        }
        else {
            $process.StandardInput.WriteLine("")
        }
        $process.StandardInput.Close()

        $standardOutput = $process.StandardOutput.ReadToEnd()
        $standardError = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) {
            throw (
                "pywin32 service registration failed with exit code " +
                "$($process.ExitCode).`n$standardError"
            )
        }
        if (-not [string]::IsNullOrWhiteSpace($standardOutput)) {
            Write-Host $standardOutput.Trim()
        }
    }
    finally {
        $plainTextPassword = $null
        if ($passwordPointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
        }
        $process.Dispose()
    }
}

function Invoke-ServiceControl {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    & $ScExe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw (
            "sc.exe failed with exit code ${LASTEXITCODE}: " +
            ($Arguments -join " ")
        )
    }
}

if (-not (Test-Administrator)) {
    Start-ElevatedCopy
}

foreach ($requiredFile in @(
    $PythonPath,
    $EnvPath,
    $ServiceHostPath,
    $ScExe
)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Required IronAPI service file is missing: $requiredFile"
    }
}

& $PythonPath -c (
    "import servicemanager, uvicorn, win32service, win32serviceutil"
)
if ($LASTEXITCODE -ne 0) {
    throw (
        "IronAPI pywin32 dependencies are unavailable. Run " +
        "& '.\1. Prepare-IronDeploy.ps1' first."
    )
}

$existingService = Get-Service `
    -Name $ServiceName `
    -ErrorAction SilentlyContinue
$action = "install-from-stdin"
if ($null -ne $existingService) {
    Write-Host (
        "Windows service '$ServiceName' is already installed."
    ) -ForegroundColor Yellow
    if (-not (Read-YesNo "Update its identity and configuration")) {
        Write-Host "No service changes were made."
        exit 0
    }

    if ($existingService.Status -ne "Stopped") {
        if (-not (Read-YesNo "Stop '$ServiceName' before updating it")) {
            Write-Host "The running service was not changed."
            exit 0
        }
        Stop-Service -Name $ServiceName
        $existingService.WaitForStatus(
            [ServiceProcess.ServiceControllerStatus]::Stopped,
            [TimeSpan]::FromSeconds(30)
        )
    }
    $action = "update-from-stdin"
}

$identity = Read-ServiceIdentity
try {
    if ($identity.Mode -eq "domain") {
        Add-ServiceLogonRight -Account $identity.Account
        Write-Host (
            "Granted 'Log on as a service' to $($identity.Account)."
        ) -ForegroundColor Green
    }
    else {
        Write-Host (
            "LocalSystem already has the built-in right to run services."
        ) -ForegroundColor DarkGray
    }

    Invoke-ServiceRegistration -Action $action -Identity $identity
}
finally {
    if ($null -ne $identity.Password) {
        $identity.Password.Dispose()
        $identity.Password = $null
    }
}

Invoke-ServiceControl -Arguments @(
    "failure",
    $ServiceName,
    "reset=",
    "86400",
    "actions=",
    "restart/5000/restart/15000/restart/60000"
)
Invoke-ServiceControl -Arguments @(
    "failureflag",
    $ServiceName,
    "1"
)

$configuredService = Get-CimInstance `
    -ClassName Win32_Service `
    -Filter "Name='$ServiceName'"
if ($null -eq $configuredService) {
    throw "Windows service '$ServiceName' was not found after registration."
}
if ($configuredService.StartMode -ne "Auto") {
    throw (
        "Windows service '$ServiceName' was registered, but its start mode " +
        "is '$($configuredService.StartMode)' instead of 'Auto'."
    )
}

Write-Host ""
Write-Host "IronAPI service configuration completed." -ForegroundColor Green
Write-Host "Service name : $ServiceName"
Write-Host "Account      : $($configuredService.StartName)"
Write-Host "Start mode   : $($configuredService.StartMode)"
Write-Host "Recovery     : restart after 5s, 15s, then 60s"

if (Read-YesNo "Start the IronAPI service now") {
    Start-Service -Name $ServiceName
    $service = Get-Service -Name $ServiceName
    $service.WaitForStatus(
        [ServiceProcess.ServiceControllerStatus]::Running,
        [TimeSpan]::FromSeconds(30)
    )
    Write-Host "IronAPI service is running." -ForegroundColor Green
}
else {
    Write-Host "IronAPI service was installed but not started."
}
