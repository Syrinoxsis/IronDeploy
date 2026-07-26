[CmdletBinding()]
param(
    [switch]$Validate,
    [switch]$InitialSetup,
    [string]$ServiceName = "IronAPI"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$IronDeployRoot = Split-Path $PSScriptRoot -Parent
$DefaultDatabaseUrl = "sqlite:///{IRONDEPLOY_ROOT}/Data/irondeploy.db"
$DefaultOdjBlobDirectory = "{IRONDEPLOY_ROOT}\ODJ\pending"
$DefaultDjoinPath = Join-Path $env:SystemRoot "System32\djoin.exe"
$EnvPath = Join-Path $PSScriptRoot ".env"
$EnvExamplePath = Join-Path $PSScriptRoot ".env.example"
$BackupDirectory = Join-Path $IronDeployRoot "Logs\ConfigBackups"
$script:ValidationErrors = 0
$script:ValidationWarnings = 0

function Write-Heading {
    param([string]$Text)

    Write-Host ""
    Write-Host $Text -ForegroundColor Cyan
    Write-Host ("=" * $Text.Length) -ForegroundColor DarkCyan
}

function ConvertFrom-DotEnvValue {
    param([string]$Value)

    $result = $Value.Trim()
    if ($result.Length -ge 2) {
        if ($result.StartsWith("'") -and $result.EndsWith("'")) {
            return $result.Substring(1, $result.Length - 2).Replace("\'", "'")
        }
        if ($result.StartsWith('"') -and $result.EndsWith('"')) {
            $result = $result.Substring(1, $result.Length - 2)
            $result = $result.Replace('\"', '"')
            return $result.Replace("\\", "\")
        }
    }
    return $result
}

function ConvertTo-DotEnvValue {
    param([AllowEmptyString()][string]$Value)

    if ([string]::IsNullOrEmpty($Value)) {
        return ""
    }

    if ($Value -notmatch "[\s#'`"]") {
        return $Value
    }

    return "'" + $Value.Replace("'", "\'") + "'"
}

function Read-EnvSettings {
    $settings = [ordered]@{}
    if (!(Test-Path -LiteralPath $EnvPath -PathType Leaf)) {
        return $settings
    }

    foreach ($line in [System.IO.File]::ReadAllLines($EnvPath)) {
        if ($line -match "^\s*(IRONAPI_[A-Z0-9_]+)\s*=(.*)$") {
            $settings[$Matches[1]] = ConvertFrom-DotEnvValue $Matches[2]
        }
    }
    return $settings
}

function Get-Setting {
    param(
        [System.Collections.IDictionary]$Settings,
        [string]$Name,
        [string]$Default = ""
    )

    if ($Settings.Contains($Name)) {
        return [string]$Settings[$Name]
    }
    return $Default
}

function Resolve-IronDeployPathToken {
    param([string]$Value)

    return $Value.Replace("{IRONDEPLOY_ROOT}", $IronDeployRoot)
}

function Initialize-EnvFile {
    if (Test-Path -LiteralPath $EnvPath -PathType Leaf) {
        return $false
    }
    if (!(Test-Path -LiteralPath $EnvExamplePath -PathType Leaf)) {
        throw "Configuration template not found: $EnvExamplePath"
    }

    Copy-Item -LiteralPath $EnvExamplePath -Destination $EnvPath
    Write-Host "Created $EnvPath from .env.example" -ForegroundColor Green
    return $true
}

function Save-EnvUpdates {
    param([System.Collections.IDictionary]$Updates)

    if ($Updates.Count -eq 0) {
        return
    }

    Initialize-EnvFile | Out-Null
    New-Item -ItemType Directory -Force $BackupDirectory | Out-Null

    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
    $backupPath = Join-Path $BackupDirectory ".env.$timestamp.bak"

    $lines = [System.Collections.Generic.List[string]]::new()
    foreach ($line in [System.IO.File]::ReadAllLines($EnvPath)) {
        $lines.Add($line)
    }

    $updatedNames = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )

    for ($index = 0; $index -lt $lines.Count; $index++) {
        if ($lines[$index] -notmatch "^\s*(IRONAPI_[A-Z0-9_]+)\s*=") {
            continue
        }

        $name = $Matches[1]
        if (!$Updates.Contains($name)) {
            continue
        }

        $value = ConvertTo-DotEnvValue ([string]$Updates[$name])
        $lines[$index] = "$name=$value"
        $updatedNames.Add($name) | Out-Null
    }

    foreach ($entry in $Updates.GetEnumerator()) {
        if ($updatedNames.Contains([string]$entry.Key)) {
            continue
        }
        if ($lines.Count -gt 0 -and $lines[$lines.Count - 1] -ne "") {
            $lines.Add("")
        }
        $value = ConvertTo-DotEnvValue ([string]$entry.Value)
        $lines.Add("$($entry.Key)=$value")
    }

    $temporaryPath = "$EnvPath.tmp.$([guid]::NewGuid().ToString('N'))"
    $utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)
    try {
        [System.IO.File]::WriteAllLines(
            $temporaryPath,
            $lines,
            $utf8WithoutBom
        )
        [System.IO.File]::Replace(
            $temporaryPath,
            $EnvPath,
            $backupPath,
            $true
        )
    } finally {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
    }

    Write-Host "Configuration saved. Backup: $backupPath" -ForegroundColor Green
}

function Read-ConfiguredValue {
    param(
        [string]$Label,
        [string]$HelpText,
        [AllowEmptyString()][string]$CurrentValue,
        [scriptblock]$Validator,
        [switch]$AllowEmpty
    )

    if (![string]::IsNullOrWhiteSpace($HelpText)) {
        Write-Host ""
        Write-Host $HelpText -ForegroundColor DarkGray
    }

    while ($true) {
        $displayValue = $CurrentValue
        if ([string]::IsNullOrEmpty($displayValue)) {
            $displayValue = "<empty>"
        }

        $suffix = "Enter keeps current"
        if ($AllowEmpty) {
            $suffix += ", '-' clears"
        }
        $answer = Read-Host "$Label [$displayValue] ($suffix)"

        if ([string]::IsNullOrWhiteSpace($answer)) {
            $candidate = $CurrentValue
        } elseif ($AllowEmpty -and $answer.Trim() -eq "-") {
            $candidate = ""
        } else {
            $candidate = $answer.Trim()
        }

        if (!$AllowEmpty -and [string]::IsNullOrWhiteSpace($candidate)) {
            Write-Host "$Label cannot be empty." -ForegroundColor Red
            continue
        }

        $validationMessage = & $Validator $candidate
        if ([string]::IsNullOrEmpty([string]$validationMessage)) {
            return $candidate
        }
        Write-Host $validationMessage -ForegroundColor Red
    }
}

function Read-BooleanValue {
    param(
        [string]$Label,
        [string]$HelpText,
        [bool]$CurrentValue
    )

    if (![string]::IsNullOrWhiteSpace($HelpText)) {
        Write-Host ""
        Write-Host $HelpText -ForegroundColor DarkGray
    }

    $currentText = if ($CurrentValue) { "Y" } else { "N" }
    while ($true) {
        $answer = (Read-Host "$Label [Y/N, current: $currentText]").Trim()
        if ([string]::IsNullOrEmpty($answer)) {
            return $CurrentValue
        }
        if ($answer -match "^(?i:y|yes)$") {
            return $true
        }
        if ($answer -match "^(?i:n|no)$") {
            return $false
        }
        Write-Host "Enter Y or N." -ForegroundColor Red
    }
}

function Get-MaskedDatabaseUrl {
    param([string]$DatabaseUrl)

    if (
        $DatabaseUrl -match (
            "^(?<scheme>[^:]+://)(?<user>[^:/@]+):" +
            "(?<password>[^@]+)@(?<rest>.+)$"
        )
    ) {
        return (
            $Matches["scheme"] +
            $Matches["user"] +
            ":***@" +
            $Matches["rest"]
        )
    }
    return $DatabaseUrl
}

function Initialize-CredentialManagerType {
    if ("IronDeploy.CredentialManager" -as [type]) {
        return
    }

    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

namespace IronDeploy
{
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    internal struct NativeCredential
    {
        public UInt32 Flags;
        public UInt32 Type;
        public string TargetName;
        public string Comment;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public UInt32 CredentialBlobSize;
        public IntPtr CredentialBlob;
        public UInt32 Persist;
        public UInt32 AttributeCount;
        public IntPtr Attributes;
        public string TargetAlias;
        public string UserName;
    }

    public static class CredentialManager
    {
        private const UInt32 GenericCredential = 1;
        private const UInt32 LocalMachinePersistence = 2;
        private const int ErrorNotFound = 1168;

        [DllImport("advapi32.dll", EntryPoint = "CredWriteW",
            CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool CredWrite(
            ref NativeCredential credential,
            UInt32 flags);

        [DllImport("advapi32.dll", EntryPoint = "CredReadW",
            CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool CredRead(
            string target,
            UInt32 type,
            UInt32 flags,
            out IntPtr credential);

        [DllImport("advapi32.dll", SetLastError = false)]
        private static extern void CredFree(IntPtr buffer);

        public static string ReadUserName(string target)
        {
            IntPtr pointer;
            if (!CredRead(target, GenericCredential, 0, out pointer))
            {
                int error = Marshal.GetLastWin32Error();
                if (error == ErrorNotFound)
                    return null;
                throw new Win32Exception(error);
            }

            try
            {
                NativeCredential credential =
                    (NativeCredential)Marshal.PtrToStructure(
                        pointer,
                        typeof(NativeCredential));
                return credential.UserName;
            }
            finally
            {
                CredFree(pointer);
            }
        }

        public static void WriteGeneric(
            string target,
            string userName,
            string password)
        {
            byte[] secret = Encoding.Unicode.GetBytes(password);
            if (secret.Length > 2560)
                throw new ArgumentException("Credential secret is too long.");

            IntPtr secretPointer = IntPtr.Zero;
            try
            {
                secretPointer = Marshal.AllocCoTaskMem(secret.Length);
                Marshal.Copy(secret, 0, secretPointer, secret.Length);

                NativeCredential credential = new NativeCredential();
                credential.Type = GenericCredential;
                credential.TargetName = target;
                credential.UserName = userName;
                credential.CredentialBlob = secretPointer;
                credential.CredentialBlobSize = (UInt32)secret.Length;
                credential.Persist = LocalMachinePersistence;

                if (!CredWrite(ref credential, 0))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            finally
            {
                Array.Clear(secret, 0, secret.Length);
                if (secretPointer != IntPtr.Zero)
                {
                    for (int index = 0; index < secret.Length; index++)
                        Marshal.WriteByte(secretPointer, index, 0);
                    Marshal.FreeCoTaskMem(secretPointer);
                }
            }
        }
    }
}
'@
}

function Get-CredentialUserName {
    param([string]$Target)

    try {
        Initialize-CredentialManagerType
        return [IronDeploy.CredentialManager]::ReadUserName($Target)
    } catch {
        Write-Host (
            "Unable to read Windows Credential Manager: " +
            $_.Exception.Message
        ) -ForegroundColor Yellow
        return $null
    }
}

function Set-LdapCredential {
    $settings = Read-EnvSettings
    $target = Get-Setting $settings "IRONAPI_LDAP_CREDENTIAL_TARGET" "IronDeploy-LDAP"

    Write-Heading "LDAP credential"
    Write-Host "Credential target: $target"
    $existingUserName = Get-CredentialUserName $target
    if (![string]::IsNullOrEmpty($existingUserName)) {
        Write-Host "Existing username: $existingUserName"
    }

    $credential = Get-Credential -Message (
        "Enter the LDAP domain credential for target '$target'"
    )
    if ($null -eq $credential) {
        Write-Host "Credential update cancelled." -ForegroundColor Yellow
        return
    }

    $networkCredential = $credential.GetNetworkCredential()
    if ([string]::IsNullOrEmpty($networkCredential.Password)) {
        Write-Host "Credential password cannot be empty." -ForegroundColor Red
        return
    }
    try {
        Initialize-CredentialManagerType
        [IronDeploy.CredentialManager]::WriteGeneric(
            $target,
            $credential.UserName,
            $networkCredential.Password
        )
        Write-Host "Credential '$target' saved." -ForegroundColor Green
    } finally {
        $networkCredential.Password = ""
        $networkCredential = $null
        $credential = $null
    }
}

function Show-CurrentConfiguration {
    $settings = Read-EnvSettings
    $credentialTarget = Get-Setting `
        $settings `
        "IRONAPI_LDAP_CREDENTIAL_TARGET" `
        "IronDeploy-LDAP"
    $credentialUser = Get-CredentialUserName $credentialTarget
    if ([string]::IsNullOrEmpty($credentialUser)) {
        $credentialUser = "<not found>"
    }

    Write-Heading "Current IronAPI configuration"
    Write-Host (
        "Database URL : " +
        (Get-MaskedDatabaseUrl (
            Get-Setting $settings "IRONAPI_DATABASE_URL" $DefaultDatabaseUrl
        ))
    )
    Write-Host (
        "Name scheme  : {0} + {1} digits, starts at {2}" -f `
            (Get-Setting $settings "IRONAPI_NAME_PREFIX" "<missing>"),
            (Get-Setting $settings "IRONAPI_NAME_WIDTH" "<missing>"),
            (Get-Setting $settings "IRONAPI_NAME_START" "<missing>")
    )
    Write-Host (
        "LDAP server  : " +
        (Get-Setting $settings "IRONAPI_LDAP_SERVER" "<disabled>")
    )
    Write-Host (
        "LDAP base DN : " +
        (Get-Setting $settings "IRONAPI_LDAP_BASE_DN" "<disabled>")
    )
    Write-Host "Credential   : $credentialTarget ($credentialUser)"
    Write-Host (
        "ODJ domain   : " +
        (Get-Setting $settings "IRONAPI_ODJ_DOMAIN" "<missing>")
    )
    Write-Host (
        "ODJ OU       : " +
        (Get-Setting $settings "IRONAPI_ODJ_MACHINE_OU" "<missing>")
    )
    Write-Host (
        "ODJ blobs    : " +
        (Get-Setting `
            $settings `
            "IRONAPI_ODJ_BLOB_DIR" `
            $DefaultOdjBlobDirectory)
    )
    Write-Host (
        "djoin.exe    : " +
        (Get-Setting $settings "IRONAPI_ODJ_DJOIN_PATH" "<missing>")
    )
}

function Configure-Database {
    $settings = Read-EnvSettings
    Write-Heading "Database"

    $databaseUrl = Read-ConfiguredValue `
        -Label "SQLAlchemy database URL" `
        -HelpText (
            "Where IronAPI stores deployment state. Example: " +
            "sqlite:///{IRONDEPLOY_ROOT}/Data/irondeploy.db"
        ) `
        -CurrentValue (
            Get-Setting `
                $settings `
                "IRONAPI_DATABASE_URL" `
                $DefaultDatabaseUrl
        ) `
        -Validator {
            param($value)
            if ($value -notmatch "^[a-zA-Z0-9+]+://") {
                return "Enter a valid SQLAlchemy URL."
            }
            return $null
        }

    Save-EnvUpdates ([ordered]@{
        IRONAPI_DATABASE_URL = $databaseUrl
    })
}

function Configure-Naming {
    $settings = Read-EnvSettings
    Write-Heading "Computer naming"

    $prefix = Read-ConfiguredValue `
        -Label "Computer name prefix" `
        -HelpText "Text before the number. Example: prefix 'pc' produces pc00001." `
        -CurrentValue (Get-Setting $settings "IRONAPI_NAME_PREFIX" "pc") `
        -Validator {
            param($value)
            if ($value -notmatch "^[a-zA-Z][a-zA-Z0-9-]{0,14}$") {
                return "Use 1-15 letters, digits, or hyphens; start with a letter."
            }
            return $null
        }

    $width = Read-ConfiguredValue `
        -Label "Number width" `
        -HelpText "Digits after the prefix. Example: 5 produces pc00001." `
        -CurrentValue (Get-Setting $settings "IRONAPI_NAME_WIDTH" "5") `
        -Validator {
            param($value)
            $number = 0
            if (![int]::TryParse($value, [ref]$number) -or $number -lt 1 -or $number -gt 20) {
                return "Enter an integer from 1 to 20."
            }
            return $null
        }

    $start = Read-ConfiguredValue `
        -Label "Starting number" `
        -HelpText "First number used when LDAP contains no matching computers. Example: 1." `
        -CurrentValue (Get-Setting $settings "IRONAPI_NAME_START" "1") `
        -Validator {
            param($value)
            $number = 0
            if (![int]::TryParse($value, [ref]$number) -or $number -lt 0) {
                return "Enter a non-negative integer."
            }
            return $null
        }

    Save-EnvUpdates ([ordered]@{
        IRONAPI_NAME_PREFIX = $prefix.ToLowerInvariant()
        IRONAPI_NAME_WIDTH = $width
        IRONAPI_NAME_START = $start
    })
}

function Configure-Ldap {
    $settings = Read-EnvSettings
    Write-Heading "LDAP"
    Write-Host "Use '-' to disable LDAP by clearing both server and base DN."

    $server = Read-ConfiguredValue `
        -Label "LDAP server (host name or IP, no ldap:// prefix)" `
        -HelpText "Domain/DC address used for searches. Example: dc01.example.test." `
        -CurrentValue (Get-Setting $settings "IRONAPI_LDAP_SERVER" "") `
        -AllowEmpty `
        -Validator {
            param($value)
            if (
                ![string]::IsNullOrEmpty($value) -and
                $value -match "://"
            ) {
                return "Enter a host name or IP without a URL scheme."
            }
            return $null
        }

    $baseDn = Read-ConfiguredValue `
        -Label "LDAP base DN" `
        -HelpText (
            "Search root that must include every managed computer OU. " +
            "Example: DC=example,DC=local."
        ) `
        -CurrentValue (Get-Setting $settings "IRONAPI_LDAP_BASE_DN" "") `
        -AllowEmpty `
        -Validator {
            param($value)
            if (
                ![string]::IsNullOrEmpty($value) -and
                $value -notmatch "^(?i:(OU|DC|CN)=)"
            ) {
                return "Base DN must start with OU=, DC=, or CN=."
            }
            return $null
        }

    if (
        [string]::IsNullOrEmpty($server) -xor
        [string]::IsNullOrEmpty($baseDn)
    ) {
        Write-Host (
            "LDAP server and base DN must both be set or both be empty."
        ) -ForegroundColor Red
        return
    }

    $target = Read-ConfiguredValue `
        -Label "Windows Credential Manager target" `
        -HelpText (
            "Name of the Generic Credential entry, not the username. " +
            "Example: IronDeploy-LDAP."
        ) `
        -CurrentValue (
            Get-Setting `
                $settings `
                "IRONAPI_LDAP_CREDENTIAL_TARGET" `
                "IronDeploy-LDAP"
        ) `
        -Validator {
            param($value)
            if ($value.Length -gt 256) {
                return "Credential target is too long."
            }
            return $null
        }

    $useSslCurrent = (
        Get-Setting $settings "IRONAPI_LDAP_USE_SSL" "false"
    ) -match "^(?i:true|1|yes|y|on)$"
    $useSsl = Read-BooleanValue `
        -Label "Use LDAPS" `
        -HelpText (
            "Enable only when the domain controller has a trusted LDAP TLS " +
            "certificate. Example: Y uses TCP 636; N uses TCP 389."
        ) `
        -CurrentValue $useSslCurrent

    $timeout = Read-ConfiguredValue `
        -Label "LDAP timeout in seconds" `
        -HelpText "Connection timeout for LDAP operations. Example: 5." `
        -CurrentValue (
            Get-Setting $settings "IRONAPI_LDAP_CONNECT_TIMEOUT" "5"
        ) `
        -Validator {
            param($value)
            $number = 0
            if (![int]::TryParse($value, [ref]$number) -or $number -lt 1 -or $number -gt 60) {
                return "Enter an integer from 1 to 60."
            }
            return $null
        }

    Save-EnvUpdates ([ordered]@{
        IRONAPI_LDAP_SERVER = $server
        IRONAPI_LDAP_BASE_DN = $baseDn
        IRONAPI_LDAP_CREDENTIAL_TARGET = $target
        IRONAPI_LDAP_USE_SSL = $useSsl.ToString().ToLowerInvariant()
        IRONAPI_LDAP_CONNECT_TIMEOUT = $timeout
    })

    if (![string]::IsNullOrEmpty($server)) {
        $replaceCredential = Read-BooleanValue `
            -Label "Set or replace the LDAP credential now" `
            -HelpText (
                "Stores the LDAP username and password in Windows Credential " +
                "Manager under the target configured above."
            ) `
            -CurrentValue (
                [string]::IsNullOrEmpty((Get-CredentialUserName $target))
            )
        if ($replaceCredential) {
            Set-LdapCredential
        }
    }
}

function Configure-Odj {
    $settings = Read-EnvSettings
    Write-Heading "Offline Domain Join"

    $domain = Read-ConfiguredValue `
        -Label "AD DNS domain" `
        -HelpText "DNS name passed to djoin.exe. Example: example.test." `
        -CurrentValue (
            Get-Setting $settings "IRONAPI_ODJ_DOMAIN" "example.test"
        ) `
        -Validator {
            param($value)
            if ($value -notmatch "^(?i:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+)$") {
                return "Enter a DNS domain such as example.test."
            }
            return $null
        }

    $machineOu = Read-ConfiguredValue `
        -Label "Computer OU distinguished name" `
        -HelpText (
            "OU where new computer accounts are created. Example: " +
            "OU=Workstations,OU=Clients,DC=example,DC=test."
        ) `
        -CurrentValue (
            Get-Setting `
                $settings `
                "IRONAPI_ODJ_MACHINE_OU" `
                "OU=Workstations,OU=Clients,DC=example,DC=test"
        ) `
        -Validator {
            param($value)
            if ($value -notmatch "^(?i:OU=).+,(?:DC=.+,?)+$") {
                return "Enter an OU distinguished name ending in DC= components."
            }
            return $null
        }

    $blobDirectory = Read-ConfiguredValue `
        -Label "ODJ blob directory" `
        -HelpText (
            "Local protected temporary storage, never the SMB share. Example: " +
            "{IRONDEPLOY_ROOT}\ODJ\pending."
        ) `
        -CurrentValue (
            Get-Setting `
                $settings `
                "IRONAPI_ODJ_BLOB_DIR" `
                $DefaultOdjBlobDirectory
        ) `
        -Validator {
            param($value)
            if (
                !$value.StartsWith("{IRONDEPLOY_ROOT}\") -and
                ![System.IO.Path]::IsPathRooted($value)
            ) {
                return "Use an absolute local path."
            }
            if ($value.StartsWith("\\")) {
                return "Use a local directory, not a UNC share."
            }
            return $null
        }

    $djoinPath = Read-ConfiguredValue `
        -Label "djoin.exe path" `
        -HelpText (
            "Windows Offline Domain Join executable. Example: " +
            "C:\Windows\System32\djoin.exe."
        ) `
        -CurrentValue (
            Get-Setting `
                $settings `
                "IRONAPI_ODJ_DJOIN_PATH" `
                $DefaultDjoinPath
        ) `
        -Validator {
            param($value)
            if (![System.IO.Path]::IsPathRooted($value)) {
                return "Use an absolute executable path."
            }
            if (![string]::Equals(
                [System.IO.Path]::GetExtension($value),
                ".exe",
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
                return "The djoin path must point to an .exe file."
            }
            return $null
        }

    $timeout = Read-ConfiguredValue `
        -Label "Provisioning timeout in seconds" `
        -HelpText "Maximum time allowed for djoin /provision. Example: 60." `
        -CurrentValue (
            Get-Setting $settings "IRONAPI_ODJ_PROVISION_TIMEOUT" "60"
        ) `
        -Validator {
            param($value)
            $number = 0
            if (![int]::TryParse($value, [ref]$number) -or $number -lt 1 -or $number -gt 300) {
                return "Enter an integer from 1 to 300."
            }
            return $null
        }

    Save-EnvUpdates ([ordered]@{
        IRONAPI_ODJ_DOMAIN = $domain.ToLowerInvariant()
        IRONAPI_ODJ_MACHINE_OU = $machineOu
        IRONAPI_ODJ_BLOB_DIR = $blobDirectory
        IRONAPI_ODJ_DJOIN_PATH = $djoinPath
        IRONAPI_ODJ_PROVISION_TIMEOUT = $timeout
    })

    try {
        New-Item -ItemType Directory -Force $blobDirectory | Out-Null
        Write-Host "ODJ directory is ready: $blobDirectory" -ForegroundColor Green
    } catch {
        Write-Host (
            "Unable to create ODJ directory: " + $_.Exception.Message
        ) -ForegroundColor Red
    }
}

function Write-ValidationPass {
    param([string]$Message)
    Write-Host "[OK]   $Message" -ForegroundColor Green
}

function Write-ValidationWarning {
    param([string]$Message)
    $script:ValidationWarnings++
    Write-Host "[WARN] $Message" -ForegroundColor Yellow
}

function Write-ValidationFailure {
    param([string]$Message)
    $script:ValidationErrors++
    Write-Host "[FAIL] $Message" -ForegroundColor Red
}

function Test-TcpConnection {
    param(
        [string]$HostName,
        [int]$Port,
        [int]$TimeoutSeconds
    )

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $asyncResult = $client.BeginConnect($HostName, $Port, $null, $null)
        if (!$asyncResult.AsyncWaitHandle.WaitOne($TimeoutSeconds * 1000)) {
            return $false
        }
        $client.EndConnect($asyncResult)
        return $true
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Test-IronApiConfiguration {
    $script:ValidationErrors = 0
    $script:ValidationWarnings = 0
    Write-Heading "Configuration validation"

    if (!(Test-Path -LiteralPath $EnvPath -PathType Leaf)) {
        Write-ValidationFailure "Configuration file is missing: $EnvPath"
        return $false
    }
    Write-ValidationPass "Configuration file exists"

    $settings = Read-EnvSettings
    $requiredNames = @(
        "IRONAPI_NAME_PREFIX",
        "IRONAPI_NAME_WIDTH",
        "IRONAPI_NAME_START"
    )
    foreach ($name in $requiredNames) {
        if ([string]::IsNullOrWhiteSpace((Get-Setting $settings $name ""))) {
            Write-ValidationFailure "$name is missing or empty"
        }
    }

    $accessMode = Get-Setting $settings "IRONAPI_ACCESS_MODE" ""
    $bindHost = Get-Setting $settings "IRONAPI_BIND_HOST" ""
    $portValue = Get-Setting $settings "IRONAPI_PORT" ""
    $cookieSecure = Get-Setting $settings "IRONAPI_COOKIE_SECURE" ""
    $port = 0
    if ($accessMode -notin @("http_direct", "https_proxy")) {
        Write-ValidationFailure (
            "IRONAPI_ACCESS_MODE must be http_direct or https_proxy"
        )
    }
    elseif (
        $accessMode -eq "https_proxy" -and
        (
            $bindHost -ne "127.0.0.1" -or
            $portValue -ne "8000" -or
            $cookieSecure -ne "true"
        )
    ) {
        Write-ValidationFailure (
            "https_proxy requires 127.0.0.1:8000 and a Secure cookie"
        )
    }
    elseif ($accessMode -eq "http_direct") {
        $parsedBindHost = $null
        if (
            ![Net.IPAddress]::TryParse($bindHost, [ref]$parsedBindHost) -or
            [Net.IPAddress]::IsLoopback($parsedBindHost)
        ) {
            Write-ValidationFailure (
                "http_direct requires a non-loopback IRONAPI_BIND_HOST"
            )
        }
        elseif ($cookieSecure -ne "false") {
            Write-ValidationFailure (
                "http_direct requires IRONAPI_COOKIE_SECURE=false"
            )
        }
        elseif (
            ![int]::TryParse($portValue, [ref]$port) -or
            $port -lt 1 -or
            $port -gt 65535
        ) {
            Write-ValidationFailure "IRONAPI_PORT must be from 1 to 65535"
        }
        else {
            Write-ValidationPass "IronAPI access mode is consistent"
        }
    }
    elseif (
        ![int]::TryParse($portValue, [ref]$port) -or
        $port -lt 1 -or
        $port -gt 65535
    ) {
        Write-ValidationFailure "IRONAPI_PORT must be from 1 to 65535"
    }
    else {
        Write-ValidationPass "IronAPI access mode is consistent"
    }

    $smbSharePath = Get-Setting $settings "IRONAPI_SMB_SHARE_PATH" ""
    $smbUser = Get-Setting $settings "IRONAPI_SMB_USER" ""
    $smbPassword = Get-Setting $settings "IRONAPI_SMB_PASSWORD" ""
    if (
        $smbSharePath -match "^\\\\[^\\]+\\[^\\]+" -and
        ![string]::IsNullOrWhiteSpace($smbUser) -and
        ![string]::IsNullOrWhiteSpace($smbPassword) -and
        $smbPassword -notmatch "CHANGE_ME"
    ) {
        Write-ValidationPass "Server-side SMB deployment credentials are configured"
    }
    else {
        Write-ValidationFailure "Configure IronAPI SMB credentials through SetupWeb"
    }

    $width = 0
    $widthValue = Get-Setting $settings "IRONAPI_NAME_WIDTH" ""
    if (
        [int]::TryParse($widthValue, [ref]$width) -and
        $width -ge 1 -and
        $width -le 20
    ) {
        Write-ValidationPass "Computer name width is valid"
    } else {
        Write-ValidationFailure "IRONAPI_NAME_WIDTH must be from 1 to 20"
    }

    $start = 0
    $startValue = Get-Setting $settings "IRONAPI_NAME_START" ""
    if ([int]::TryParse($startValue, [ref]$start) -and $start -ge 0) {
        Write-ValidationPass "Computer name start is valid"
    } else {
        Write-ValidationFailure "IRONAPI_NAME_START must be non-negative"
    }

    $blobDirectorySetting = Get-Setting `
        $settings `
        "IRONAPI_ODJ_BLOB_DIR" `
        $DefaultOdjBlobDirectory
    $blobDirectory = Resolve-IronDeployPathToken $blobDirectorySetting
    if (
        ![string]::IsNullOrWhiteSpace($blobDirectory) -and
        (Test-Path -LiteralPath $blobDirectory -PathType Container)
    ) {
        try {
            $testFile = Join-Path (
                $blobDirectory
            ) ".setup-write-test-$([guid]::NewGuid().ToString('N')).tmp"
            [System.IO.File]::WriteAllText($testFile, "test")
            Remove-Item -LiteralPath $testFile -Force
            Write-ValidationPass "ODJ directory is writable"
        } catch {
            Write-ValidationFailure (
                "ODJ directory is not writable: " + $_.Exception.Message
            )
        }
    } else {
        Write-ValidationFailure "ODJ directory does not exist: $blobDirectory"
    }

    $djoinPath = Get-Setting `
        $settings `
        "IRONAPI_ODJ_DJOIN_PATH" `
        $DefaultDjoinPath
    if (
        ![string]::IsNullOrWhiteSpace($djoinPath) -and
        (Test-Path -LiteralPath $djoinPath -PathType Leaf)
    ) {
        Write-ValidationPass "djoin.exe exists"
    } else {
        Write-ValidationFailure "djoin.exe not found: $djoinPath"
    }

    $ldapServer = Get-Setting $settings "IRONAPI_LDAP_SERVER" ""
    $ldapBaseDn = Get-Setting $settings "IRONAPI_LDAP_BASE_DN" ""
    if (
        [string]::IsNullOrWhiteSpace($ldapServer) -and
        [string]::IsNullOrWhiteSpace($ldapBaseDn)
    ) {
        Write-ValidationWarning (
            "LDAP is disabled; name suggestion and ODJ provisioning will return 503"
        )
    } elseif (
        [string]::IsNullOrWhiteSpace($ldapServer) -or
        [string]::IsNullOrWhiteSpace($ldapBaseDn)
    ) {
        Write-ValidationFailure "LDAP server and base DN must both be configured"
    } else {
        $useSsl = (
            Get-Setting $settings "IRONAPI_LDAP_USE_SSL" "false"
        ) -match "^(?i:true|1|yes|y|on)$"
        $ldapPort = if ($useSsl) { 636 } else { 389 }
        $timeout = 5
        $configuredTimeout = 0
        if ([int]::TryParse(
            (Get-Setting $settings "IRONAPI_LDAP_CONNECT_TIMEOUT" "5"),
            [ref]$configuredTimeout
        )) {
            $timeout = [Math]::Min([Math]::Max($configuredTimeout, 1), 60)
        }

        if (Test-TcpConnection $ldapServer $ldapPort $timeout) {
            Write-ValidationPass "LDAP TCP connection to ${ldapServer}:$ldapPort"
        } else {
            Write-ValidationFailure (
                "Cannot connect to LDAP at ${ldapServer}:$ldapPort"
            )
        }

        $target = Get-Setting `
            $settings `
            "IRONAPI_LDAP_CREDENTIAL_TARGET" `
            "IronDeploy-LDAP"
        $credentialUser = Get-CredentialUserName $target
        if ([string]::IsNullOrEmpty($credentialUser)) {
            Write-ValidationFailure (
                "Windows credential '$target' was not found for the current user"
            )
        } else {
            Write-ValidationPass "LDAP credential exists for $credentialUser"
        }
    }

    $identityName = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    if (
        ![string]::IsNullOrEmpty($env:COMPUTERNAME) -and
        $identityName.StartsWith(
            "$($env:COMPUTERNAME)\",
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        Write-ValidationWarning (
            "Current identity '$identityName' is local. Run the IronAPI service " +
            "as a domain account with create-computer rights in the configured OU."
        )
    } else {
        Write-ValidationPass "Current identity appears to be a domain identity"
        Write-ValidationWarning (
            "The script cannot safely test OU create rights without creating " +
            "an AD computer object."
        )
    }

    Write-Host ""
    Write-Host (
        "Validation result: {0} error(s), {1} warning(s)" -f `
            $script:ValidationErrors,
            $script:ValidationWarnings
    )
    return $script:ValidationErrors -eq 0
}

function Restart-IronApiService {
    Write-Heading "Restart service"
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($null -eq $service) {
        Write-Host (
            "Windows service '$ServiceName' is not installed."
        ) -ForegroundColor Yellow
        return
    }

    $confirmed = Read-BooleanValue `
        -Label "Restart Windows service '$ServiceName' now" `
        -HelpText "Use Y only after saving and validating the new configuration." `
        -CurrentValue $false
    if (!$confirmed) {
        Write-Host "Service restart cancelled."
        return
    }

    Restart-Service -Name $ServiceName -ErrorAction Stop
    $service = Get-Service -Name $ServiceName
    Write-Host (
        "Service '$ServiceName' status: $($service.Status)"
    ) -ForegroundColor Green
}

function Invoke-FullSetup {
    Write-Heading "IronAPI initial setup"
    Configure-Database
    Configure-Naming
    Configure-Ldap
    Configure-Odj
    Test-IronApiConfiguration | Out-Null
}

function Show-Menu {
    Write-Heading "IronAPI setup menu"
    Write-Host "1. Show current configuration"
    Write-Host "2. Database"
    Write-Host "3. Computer naming"
    Write-Host "4. LDAP"
    Write-Host "5. Offline Domain Join"
    Write-Host "6. LDAP credential"
    Write-Host "7. Validate configuration"
    Write-Host "8. Restart IronAPI service"
    Write-Host "9. Run full setup"
    Write-Host "0. Exit"
}

if ($Validate -and $InitialSetup) {
    throw "Use either -Validate or -InitialSetup, not both."
}

if ($Validate) {
    $isValid = Test-IronApiConfiguration
    if (!$isValid) {
        exit 1
    }
    exit 0
}

$created = Initialize-EnvFile
if ($created -or $InitialSetup) {
    Invoke-FullSetup
} else {
    Write-Host "Existing .env detected; no values were changed." -ForegroundColor Green
    Show-CurrentConfiguration
}

while ($true) {
    Show-Menu
    $choice = (Read-Host "Select an option").Trim()
    switch ($choice) {
        "1" { Show-CurrentConfiguration }
        "2" { Configure-Database }
        "3" { Configure-Naming }
        "4" { Configure-Ldap }
        "5" { Configure-Odj }
        "6" { Set-LdapCredential }
        "7" { Test-IronApiConfiguration | Out-Null }
        "8" { Restart-IronApiService }
        "9" { Invoke-FullSetup }
        "0" { return }
        default {
            Write-Host "Unknown option." -ForegroundColor Yellow
        }
    }
}
