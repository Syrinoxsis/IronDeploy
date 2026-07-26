#Requires -Version 5.1

[CmdletBinding()]
param(
    [switch]$InitialSetup,
    [switch]$Validate
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$IronDeployRoot = Split-Path $PSScriptRoot -Parent
$ApiRoot = Join-Path $IronDeployRoot "Api"
$ApiEnvPath = Join-Path $ApiRoot ".env"
$ApiEnvExamplePath = Join-Path $ApiRoot ".env.example"
$ApiSetupPath = Join-Path $ApiRoot "Setup-IronAPI.ps1"
$WinPEConfigPath = Join-Path `
    $IronDeployRoot `
    "WinPE\Runtime\deploy.config.ps1"
$WinPEConfigExamplePath = Join-Path `
    $IronDeployRoot `
    "WinPE\Runtime\deploy.config.example.ps1"
$UnattendPath = Join-Path `
    $IronDeployRoot `
    "ServerTemplates\Unattend\unattend-win11-template.xml"
$UnattendExamplePath = Join-Path `
    $IronDeployRoot `
    "ServerTemplates\Unattend\unattend-win11-template.example.xml"
$LegacyUnattendPath = Join-Path `
    $IronDeployRoot `
    "Share\Unattend\unattend-win11-template.xml"
$WinPEInitializePath = Join-Path `
    $IronDeployRoot `
    "WinPE\Build\Initialize-IronDeployWinPE.ps1"
$WinPEBuildPath = Join-Path `
    $IronDeployRoot `
    "Tools\Build-IronDeployWinPE.ps1"

function Write-Heading([string]$Text) {
    Write-Host ""
    Write-Host $Text -ForegroundColor Cyan
    Write-Host ("=" * $Text.Length) -ForegroundColor DarkCyan
}

function Backup-ConfigurationFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }
    $backupDirectory = Join-Path $IronDeployRoot "Logs\ConfigBackups"
    New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null
    $backupName = "{0}.{1}.bak" -f `
        (Split-Path $Path -Leaf),
        (Get-Date -Format "yyyyMMdd-HHmmss-fff")
    Copy-Item `
        -LiteralPath $Path `
        -Destination (Join-Path $backupDirectory $backupName)
}

function Read-ConfiguredValue {
    param(
        [string]$Label,
        [string]$Help,
        [string]$CurrentValue,
        [scriptblock]$Validator
    )

    while ($true) {
        Write-Host ""
        Write-Host $Help -ForegroundColor DarkGray
        $value = Read-Host "$Label [$CurrentValue]"
        if ([string]::IsNullOrWhiteSpace($value)) {
            $value = $CurrentValue
        }
        $errorMessage = & $Validator $value
        if ([string]::IsNullOrEmpty($errorMessage)) {
            return $value
        }
        Write-Host $errorMessage -ForegroundColor Yellow
    }
}

function Read-YesNo {
    param(
        [string]$Prompt,
        [bool]$Default
    )

    $suffix = if ($Default) { "[Y/n]" } else { "[y/N]" }
    while ($true) {
        $answer = (Read-Host "$Prompt $suffix").Trim()
        if ([string]::IsNullOrEmpty($answer)) {
            return $Default
        }
        if ($answer -match "^(?i:y|yes|д|да)$") {
            return $true
        }
        if ($answer -match "^(?i:n|no|н|нет)$") {
            return $false
        }
    }
}


function Get-TimeZoneChoices {
    return @(
        @{ Id = "Dateline Standard Time"; Offset = "UTC-12"; Label = "International Date Line West" },
        @{ Id = "UTC-11"; Offset = "UTC-11"; Label = "Coordinated Universal Time-11" },
        @{ Id = "Aleutian Standard Time"; Offset = "UTC-10"; Label = "Aleutian Islands" },
        @{ Id = "Hawaiian Standard Time"; Offset = "UTC-10"; Label = "Hawaii" },
        @{ Id = "Marquesas Standard Time"; Offset = "UTC-09:30"; Label = "Marquesas Islands" },
        @{ Id = "Alaskan Standard Time"; Offset = "UTC-09"; Label = "Alaska" },
        @{ Id = "UTC-09"; Offset = "UTC-09"; Label = "Coordinated Universal Time-09" },
        @{ Id = "Pacific Standard Time"; Offset = "UTC-08"; Label = "Pacific Time" },
        @{ Id = "UTC-08"; Offset = "UTC-08"; Label = "Coordinated Universal Time-08" },
        @{ Id = "Mountain Standard Time"; Offset = "UTC-07"; Label = "Mountain Time" },
        @{ Id = "US Mountain Standard Time"; Offset = "UTC-07"; Label = "Arizona" },
        @{ Id = "Central Standard Time"; Offset = "UTC-06"; Label = "Central Time" },
        @{ Id = "Canada Central Standard Time"; Offset = "UTC-06"; Label = "Saskatchewan" },
        @{ Id = "Eastern Standard Time"; Offset = "UTC-05"; Label = "Eastern Time" },
        @{ Id = "US Eastern Standard Time"; Offset = "UTC-05"; Label = "Indiana East" },
        @{ Id = "SA Pacific Standard Time"; Offset = "UTC-05"; Label = "Bogota, Lima, Quito" },
        @{ Id = "Atlantic Standard Time"; Offset = "UTC-04"; Label = "Atlantic Time" },
        @{ Id = "SA Western Standard Time"; Offset = "UTC-04"; Label = "Georgetown, La Paz, Manaus" },
        @{ Id = "Newfoundland Standard Time"; Offset = "UTC-03:30"; Label = "Newfoundland" },
        @{ Id = "E. South America Standard Time"; Offset = "UTC-03"; Label = "Brasilia" },
        @{ Id = "SA Eastern Standard Time"; Offset = "UTC-03"; Label = "Cayenne, Fortaleza" },
        @{ Id = "UTC-02"; Offset = "UTC-02"; Label = "Coordinated Universal Time-02" },
        @{ Id = "Azores Standard Time"; Offset = "UTC-01"; Label = "Azores" },
        @{ Id = "UTC"; Offset = "UTC+00"; Label = "Coordinated Universal Time" },
        @{ Id = "GMT Standard Time"; Offset = "UTC+00"; Label = "Dublin, Edinburgh, Lisbon, London" },
        @{ Id = "W. Europe Standard Time"; Offset = "UTC+01"; Label = "Amsterdam, Berlin, Rome" },
        @{ Id = "Central Europe Standard Time"; Offset = "UTC+01"; Label = "Budapest, Prague, Warsaw" },
        @{ Id = "Romance Standard Time"; Offset = "UTC+01"; Label = "Brussels, Copenhagen, Madrid, Paris" },
        @{ Id = "E. Europe Standard Time"; Offset = "UTC+02"; Label = "Chisinau" },
        @{ Id = "FLE Standard Time"; Offset = "UTC+02"; Label = "Helsinki, Kyiv, Riga, Sofia, Tallinn, Vilnius" },
        @{ Id = "GTB Standard Time"; Offset = "UTC+02"; Label = "Athens, Bucharest" },
        @{ Id = "South Africa Standard Time"; Offset = "UTC+02"; Label = "Harare, Pretoria" },
        @{ Id = "Turkey Standard Time"; Offset = "UTC+03"; Label = "Istanbul" },
        @{ Id = "Russian Standard Time"; Offset = "UTC+03"; Label = "Moscow, St. Petersburg" },
        @{ Id = "Arab Standard Time"; Offset = "UTC+03"; Label = "Kuwait, Riyadh" },
        @{ Id = "Iran Standard Time"; Offset = "UTC+03:30"; Label = "Tehran" },
        @{ Id = "Arabian Standard Time"; Offset = "UTC+04"; Label = "Abu Dhabi, Muscat" },
        @{ Id = "Astrakhan Standard Time"; Offset = "UTC+04"; Label = "Astrakhan, Ulyanovsk" },
        @{ Id = "Afghanistan Standard Time"; Offset = "UTC+04:30"; Label = "Kabul" },
        @{ Id = "West Asia Standard Time"; Offset = "UTC+05"; Label = "Ashgabat, Tashkent" },
        @{ Id = "Qyzylorda Standard Time"; Offset = "UTC+05"; Label = "Qyzylorda" },
        @{ Id = "Pakistan Standard Time"; Offset = "UTC+05"; Label = "Islamabad, Karachi" },
        @{ Id = "India Standard Time"; Offset = "UTC+05:30"; Label = "Chennai, Kolkata, Mumbai, New Delhi" },
        @{ Id = "Nepal Standard Time"; Offset = "UTC+05:45"; Label = "Kathmandu" },
        @{ Id = "Central Asia Standard Time"; Offset = "UTC+06"; Label = "Astana" },
        @{ Id = "Bangladesh Standard Time"; Offset = "UTC+06"; Label = "Dhaka" },
        @{ Id = "Myanmar Standard Time"; Offset = "UTC+06:30"; Label = "Yangon" },
        @{ Id = "SE Asia Standard Time"; Offset = "UTC+07"; Label = "Bangkok, Hanoi, Jakarta" },
        @{ Id = "North Asia Standard Time"; Offset = "UTC+07"; Label = "Krasnoyarsk" },
        @{ Id = "N. Central Asia Standard Time"; Offset = "UTC+07"; Label = "Novosibirsk" },
        @{ Id = "China Standard Time"; Offset = "UTC+08"; Label = "Beijing, Chongqing, Hong Kong" },
        @{ Id = "Singapore Standard Time"; Offset = "UTC+08"; Label = "Kuala Lumpur, Singapore" },
        @{ Id = "Taipei Standard Time"; Offset = "UTC+08"; Label = "Taipei" },
        @{ Id = "North Asia East Standard Time"; Offset = "UTC+08"; Label = "Irkutsk" },
        @{ Id = "Tokyo Standard Time"; Offset = "UTC+09"; Label = "Osaka, Sapporo, Tokyo" },
        @{ Id = "Korea Standard Time"; Offset = "UTC+09"; Label = "Seoul" },
        @{ Id = "AUS Central Standard Time"; Offset = "UTC+09:30"; Label = "Darwin" },
        @{ Id = "E. Australia Standard Time"; Offset = "UTC+10"; Label = "Brisbane" },
        @{ Id = "AUS Eastern Standard Time"; Offset = "UTC+10"; Label = "Canberra, Melbourne, Sydney" },
        @{ Id = "West Pacific Standard Time"; Offset = "UTC+10"; Label = "Guam, Port Moresby" },
        @{ Id = "Lord Howe Standard Time"; Offset = "UTC+10:30"; Label = "Lord Howe Island" },
        @{ Id = "Central Pacific Standard Time"; Offset = "UTC+11"; Label = "Solomon Islands, New Caledonia" },
        @{ Id = "New Zealand Standard Time"; Offset = "UTC+12"; Label = "Auckland, Wellington" },
        @{ Id = "UTC+12"; Offset = "UTC+12"; Label = "Coordinated Universal Time+12" },
        @{ Id = "Tonga Standard Time"; Offset = "UTC+13"; Label = "Nuku'alofa" },
        @{ Id = "Line Islands Standard Time"; Offset = "UTC+14"; Label = "Kiritimati Island" }
    )
}

function Read-TimeZoneChoice {
    param([string]$CurrentValue)

    $choices = @(Get-TimeZoneChoices)
    $defaultIndex = 0
    for ($index = 0; $index -lt $choices.Count; $index++) {
        if ($choices[$index].Id -eq $CurrentValue) {
            $defaultIndex = $index
            break
        }
    }

    Write-Host ""
    Write-Host "Select the Windows time zone for unattend." -ForegroundColor DarkGray
    for ($index = 0; $index -lt $choices.Count; $index++) {
        $choice = $choices[$index]
        Write-Host ("{0,2}. {1} - {2} ({3})" -f `
            ($index + 1),
            $choice.Offset,
            $choice.Id,
            $choice.Label)
    }

    while ($true) {
        $answer = (Read-Host ("Windows time zone [{0}]" -f ($defaultIndex + 1))).Trim()
        if ([string]::IsNullOrWhiteSpace($answer)) {
            return $choices[$defaultIndex].Id
        }
        $number = 0
        if ([int]::TryParse($answer, [ref]$number)) {
            if ($number -ge 1 -and $number -le $choices.Count) {
                return $choices[$number - 1].Id
            }
        }
        foreach ($choice in $choices) {
            if ($choice.Id -ieq $answer) {
                return $choice.Id
            }
        }
        Write-Host "Enter a list number or an exact Windows time zone ID." -ForegroundColor Yellow
    }
}

function Initialize-DirectoryLayout {
    @(
        "Data",
        "ODJ\pending",
        "Logs",
        ".work",
        "dist",
        "Share\Images",
        "Share\Drivers",
        "ServerTemplates\Unattend",
        "ServerTemplates\PostInstall"
    ) | ForEach-Object {
        New-Item `
            -ItemType Directory `
            -Path (Join-Path $IronDeployRoot $_) `
            -Force |
            Out-Null
    }

    if (
        -not (Test-Path -LiteralPath $UnattendPath -PathType Leaf) -and
        (Test-Path -LiteralPath $LegacyUnattendPath -PathType Leaf)
    ) {
        Move-Item `
            -LiteralPath $LegacyUnattendPath `
            -Destination $UnattendPath
        Write-Host (
            "Moved legacy unattend template to: $UnattendPath"
        ) -ForegroundColor Green
    }
    elseif (
        (Test-Path -LiteralPath $UnattendPath -PathType Leaf) -and
        (Test-Path -LiteralPath $LegacyUnattendPath -PathType Leaf)
    ) {
        throw (
            "Both legacy SMB-exposed and server-only unattend templates exist. " +
            "Remove the legacy Share\Unattend copy after verifying which " +
            "configuration is current."
        )
    }
}

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Configure-OdjAcl {
    Write-Heading "ODJ directory ACL"
    Write-Host (
        "ODJ blobs contain computer-account secrets. Only SYSTEM, local " +
        "administrators, and the IronAPI process identity should have access."
    ) -ForegroundColor DarkGray

    if (-not (Test-Administrator)) {
        Write-Host (
            "Run this master from an elevated PowerShell session to set ACLs."
        ) -ForegroundColor Yellow
        return
    }

    $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $serviceIdentity = Read-ConfiguredValue `
        "IronAPI process identity" `
        "Use the domain service account that starts Uvicorn and runs djoin.exe." `
        $currentIdentity `
        {
            param($value)
            try {
                $account = New-Object Security.Principal.NTAccount($value)
                [void]$account.Translate(
                    [Security.Principal.SecurityIdentifier]
                )
                return $null
            }
            catch {
                return "Windows cannot resolve this account."
            }
        }

    $inheritance = (
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [Security.AccessControl.InheritanceFlags]::ObjectInherit
    )
    $propagation = [Security.AccessControl.PropagationFlags]::None
    $allow = [Security.AccessControl.AccessControlType]::Allow
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($entry in @(
        @("NT AUTHORITY\SYSTEM", [Security.AccessControl.FileSystemRights]::FullControl),
        @("BUILTIN\Administrators", [Security.AccessControl.FileSystemRights]::FullControl),
        @($serviceIdentity, [Security.AccessControl.FileSystemRights]::Modify)
    )) {
        $rule = New-Object Security.AccessControl.FileSystemAccessRule(
            $entry[0],
            $entry[1],
            $inheritance,
            $propagation,
            $allow
        )
        $acl.AddAccessRule($rule)
    }

    foreach ($path in @(
        (Join-Path $IronDeployRoot "ODJ"),
        (Join-Path $IronDeployRoot "ODJ\pending")
    )) {
        Set-Acl -LiteralPath $path -AclObject $acl
    }
    Write-Host "Restricted ODJ ACL for $serviceIdentity." -ForegroundColor Green
}

function Prepare-WinPEWorkTree {
    Write-Heading "Prepare WinPE .work directory"
    $workingRoot = Join-Path $IronDeployRoot ".work\WinPE_amd64"
    $bootWim = Join-Path $workingRoot "media\sources\boot.wim"

    Write-Host (
        "This step runs Windows ADK copype.cmd, creates the mutable WinPE " +
        "working tree, and adds the required PowerShell, WMI, .NET, " +
        "StorageWMI, and DISM components."
    ) -ForegroundColor DarkGray
    Write-Host "Example result: $bootWim" -ForegroundColor DarkGray

    if (Test-Path -LiteralPath $workingRoot -PathType Container) {
        Write-Host "Existing WinPE .work tree detected." `
            -ForegroundColor Yellow
        Write-Host (
            "The initializer will ask before reinstalling it."
        ) -ForegroundColor DarkGray
        if (-not (Read-YesNo "Run WinPE initializer now" $false)) {
            Write-Host "WinPE preparation skipped."
            return
        }
    }
    elseif (-not (Read-YesNo "Run copype and prepare .work now" $true)) {
        Write-Host "WinPE preparation skipped."
        return
    }

    & $WinPEInitializePath
    if ($LASTEXITCODE -ne 0) {
        throw "WinPE working-tree initialization failed."
    }
}

function Read-PowerShellConfig {
    $values = @{}
    if (-not (Test-Path -LiteralPath $WinPEConfigPath -PathType Leaf)) {
        return $values
    }

    foreach ($line in [IO.File]::ReadAllLines($WinPEConfigPath)) {
        if ($line -match "^\s*\`$([A-Za-z][A-Za-z0-9_]*)\s*=\s*(['`"])(.*?)\2\s*$") {
            $values[$Matches[1]] = $Matches[3]
        }
        elseif ($line -match "^\s*\`$ImageIndex\s*=\s*(\d+)\s*$") {
            $values["ImageIndex"] = $Matches[1]
        }
        elseif ($line -match "^\s*\`$([A-Za-z][A-Za-z0-9_]*)\s*=\s*\`$(true|false)\s*$") {
            $values[$Matches[1]] = $Matches[2].ToLowerInvariant()
        }
    }
    return $values
}

function Get-ConfigValue {
    param(
        [hashtable]$Values,
        [string]$Name,
        [string]$Default
    )

    if ($Values.ContainsKey($Name)) {
        return [string]$Values[$Name]
    }
    return $Default
}

function Get-ConfigBoolean {
    param(
        [hashtable]$Values,
        [string]$Name,
        [bool]$Default
    )

    if (-not $Values.ContainsKey($Name)) {
        return $Default
    }
    return ([string]$Values[$Name]) -ieq "true"
}

function ConvertTo-PowerShellLiteral([string]$Value) {
    return "'" + $Value.Replace("'", "''") + "'"
}

function ConvertTo-PowerShellBooleanLiteral([bool]$Value) {
    if ($Value) {
        return "`$true"
    }
    return "`$false"
}

function Read-PlainTextSecret([string]$Prompt) {
    $secureValue = Read-Host $Prompt -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
        $secureValue
    )
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        $secureValue.Dispose()
    }
}

function Configure-WinPE {
    Write-Heading "WinPE and deployment share"
    Write-Host (
        "This writes a credential-free WinPE configuration. " +
        "SMB credentials are stored server-side in Api\.env."
    ) -ForegroundColor DarkGray

    $current = Read-PowerShellConfig
    $serverConfig = Read-DotEnvValues
    $sharePath = Read-ConfiguredValue `
        "SMB share UNC path" `
        "Example: \\DEPLOY-SERVER\IronDeploy. This share must expose the Share folder." `
        (Get-ConfigValue $serverConfig "IRONAPI_SMB_SHARE_PATH" (Get-ConfigValue $current "SharePath" "\\$env:COMPUTERNAME\IronDeploy")) `
        {
            param($value)
            if ($value -notmatch "^\\\\[^\\]+\\[^\\]+") {
                return "Enter a UNC path such as \\SERVER\IronDeploy."
            }
            return $null
        }
    $shareDrive = Read-ConfiguredValue `
        "WinPE drive letter" `
        "Temporary drive used after connecting the SMB share." `
        (Get-ConfigValue $current "ShareDrive" "Z:") `
        {
            param($value)
            if ($value -notmatch "^[A-Za-z]:$") {
                return "Enter one drive letter followed by a colon."
            }
            return $null
        }
    $shareUser = Read-ConfiguredValue `
        "Read-only SMB account" `
        "Example: DOMAIN\irondeploy_ro or SERVER\irondeploy_ro." `
        (Get-ConfigValue $serverConfig "IRONAPI_SMB_USER" (Get-ConfigValue $current "ShareUser" "$env:COMPUTERNAME\iron_ro")) `
        {
            param($value)
            if ([string]::IsNullOrWhiteSpace($value)) {
                return "The SMB account cannot be empty."
            }
            return $null
        }

    $sharePassword = Get-ConfigValue $serverConfig "IRONAPI_SMB_PASSWORD" (Get-ConfigValue $current "SharePassword" "")
    if (
        [string]::IsNullOrEmpty($sharePassword) -or
        -not (Read-YesNo "Keep the existing SMB password" $true)
    ) {
        $sharePassword = Read-PlainTextSecret (
            "SMB password (input is hidden)"
        )
        if ([string]::IsNullOrEmpty($sharePassword)) {
            throw "The SMB password cannot be empty."
        }
    }

    $apiBaseUrl = Read-ConfiguredValue `
        "IronAPI URL" `
        "Address reachable from WinPE, including scheme and port." `
        (Get-ConfigValue $current "ApiBaseUrl" "http://127.0.0.1:8000") `
        {
            param($value)
            if ($value -notmatch "^https?://[^/]+(?::\d+)?$") {
                return "Enter a URL such as http://198.51.100.10:8000."
            }
            return $null
        }
    $imageIndex = Read-ConfiguredValue `
        "Default Windows image index" `
        "DISM edition index inside the selected WIM." `
        (Get-ConfigValue $current "ImageIndex" "4") `
        {
            param($value)
            $number = 0
            if (![int]::TryParse($value, [ref]$number) -or $number -lt 1) {
                return "Enter a positive integer."
            }
            return $null
        }
    $setupLocalAdminName = Read-ConfiguredValue `
        "Setup local admin name" `
        "Local recovery account created by unattend; must match the unattend template." `
        (Get-ConfigValue $current "SetupLocalAdminName" "localadmin") `
        {
            param($value)
            if ($value -notmatch "^[A-Za-z0-9._-]{1,20}$") {
                return "Use 1-20 letters, digits, dot, underscore, or hyphen."
            }
            return $null
        }
    $enableBuiltInAdministrator = Read-YesNo `
        "Enable built-in Windows Administrator account after setup" `
        (Get-ConfigBoolean $current "EnableBuiltInAdministrator" $true)
    $enableSetupLocalAdminDefault = Get-ConfigBoolean `
        $current `
        "EnableSetupLocalAdmin" `
        (-not (Get-ConfigBoolean $current "DisableSetupLocalAdmin" $false))
    $enableSetupLocalAdmin = Read-YesNo `
        "Keep setup local admin account enabled after setup" `
        $enableSetupLocalAdminDefault
    $enableGuiImageApplyProgress = Read-YesNo `
        "Show live DISM Apply-Image progress in the WinPE GUI" `
        (Get-ConfigBoolean $current "EnableGuiImageApplyProgress" $true)

    $lines = @(
        "# Generated by Tools\ConfigureMaster.ps1.",
        "# Contains no deployment credentials; secrets stay server-side in Api\.env.",
        "",
        "`$ShareDrive = $(ConvertTo-PowerShellLiteral $shareDrive)",
        "`$ApiBaseUrl = $(ConvertTo-PowerShellLiteral $apiBaseUrl)",
        "`$ImagesPath = $(ConvertTo-PowerShellLiteral ($shareDrive + '\Images'))",
        "`$DriversPath = $(ConvertTo-PowerShellLiteral ($shareDrive + '\Drivers'))",
        "`$ImageIndex = $imageIndex",
        "`$SetupLocalAdminName = $(ConvertTo-PowerShellLiteral $setupLocalAdminName)",
        "`$EnableBuiltInAdministrator = $(ConvertTo-PowerShellBooleanLiteral $enableBuiltInAdministrator)",
        "`$EnableSetupLocalAdmin = $(ConvertTo-PowerShellBooleanLiteral $enableSetupLocalAdmin)",
        "`$EnableGuiImageApplyProgress = $(ConvertTo-PowerShellBooleanLiteral $enableGuiImageApplyProgress)"
    )
    Backup-ConfigurationFile $WinPEConfigPath
    [IO.File]::WriteAllLines(
        $WinPEConfigPath,
        $lines,
        [Text.UTF8Encoding]::new($false)
    )
    Set-DotEnvValues @{
        IRONAPI_SMB_SHARE_PATH = $sharePath
        IRONAPI_SMB_USER = $shareUser
        IRONAPI_SMB_PASSWORD = $sharePassword
    }
    $sharePassword = $null
    Write-Host "Saved: $WinPEConfigPath" -ForegroundColor Green
}

function Configure-LocalSmbShare {
    Write-Heading "Local SMB share"
    Write-Host (
        "This publishes the local Share directory. Skip this step when SMB " +
        "is hosted by another server."
    ) -ForegroundColor DarkGray

    if (-not (Test-Administrator)) {
        Write-Host (
            "Run this master from an elevated PowerShell session to configure SMB."
        ) -ForegroundColor Yellow
        return
    }

    $current = Read-PowerShellConfig
    if ($current.Count -eq 0) {
        throw "Configure WinPE and SMB settings first."
    }

    $sharePath = Get-ConfigValue `
        $current `
        "SharePath" `
        "\\$env:COMPUTERNAME\IronDeploy"
    $shareName = Read-ConfiguredValue `
        "Local SMB share name" `
        "This is the final component of the UNC path, normally IronDeploy." `
        (($sharePath.TrimEnd("\") -split "\\")[-1]) `
        {
            param($value)
            if ($value -notmatch "^[A-Za-z0-9._-]{1,80}$") {
                return "Use 1-80 letters, digits, dot, underscore, or hyphen."
            }
            return $null
        }
    $readAccount = Read-ConfiguredValue `
        "Windows account granted SMB read access" `
        "Use the same account as ShareUser, but specify a resolvable SERVER\user or DOMAIN\user name." `
        (Get-ConfigValue `
            $current `
            "ShareUser" `
            "$env:COMPUTERNAME\iron_ro") `
        {
            param($value)
            try {
                $account = New-Object Security.Principal.NTAccount($value)
                [void]$account.Translate(
                    [Security.Principal.SecurityIdentifier]
                )
                return $null
            }
            catch {
                return "Windows cannot resolve this account."
            }
        }
    $localPath = Join-Path $IronDeployRoot "Share"

    Write-Host "Share name : $shareName"
    Write-Host "Local path : $localPath"
    Write-Host "Read account: $readAccount"
    if (-not (Read-YesNo "Apply this local SMB configuration" $true)) {
        Write-Host "SMB configuration skipped."
        return
    }

    $existingShare = Get-SmbShare `
        -Name $shareName `
        -ErrorAction SilentlyContinue
    if ($null -eq $existingShare) {
        New-SmbShare `
            -Name $shareName `
            -Path $localPath `
            -CachingMode Manual `
            -FolderEnumerationMode Unrestricted `
            -ReadAccess $readAccount |
            Out-Null
    }
    else {
        $actualPath = [IO.Path]::GetFullPath($existingShare.Path)
        $expectedPath = [IO.Path]::GetFullPath($localPath)
        if ($actualPath -ine $expectedPath) {
            throw (
                "Share '$shareName' already points to '$actualPath'. " +
                "Move it deliberately before running this step again."
            )
        }
        Grant-SmbShareAccess `
            -Name $shareName `
            -AccountName $readAccount `
            -AccessRight Read `
            -Force |
            Out-Null
    }

    $acl = Get-Acl -LiteralPath $localPath
    $rule = New-Object Security.AccessControl.FileSystemAccessRule(
        $readAccount,
        [Security.AccessControl.FileSystemRights]::ReadAndExecute,
        (
            [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [Security.AccessControl.InheritanceFlags]::ObjectInherit
        ),
        [Security.AccessControl.PropagationFlags]::None,
        [Security.AccessControl.AccessControlType]::Allow
    )
    $acl.SetAccessRule($rule)
    Set-Acl -LiteralPath $localPath -AclObject $acl
    Write-Host "SMB share and read ACL configured." -ForegroundColor Green
}

function Set-DotEnvValues {
    param([hashtable]$Updates)

    if (-not (Test-Path -LiteralPath $ApiEnvPath -PathType Leaf)) {
        Copy-Item -LiteralPath $ApiEnvExamplePath -Destination $ApiEnvPath
    }
    else {
        Backup-ConfigurationFile $ApiEnvPath
    }

    $lines = [Collections.Generic.List[string]]::new()
    foreach ($line in [IO.File]::ReadAllLines($ApiEnvPath)) {
        $lines.Add($line)
    }

    foreach ($name in $Updates.Keys) {
        $updated = $false
        for ($index = 0; $index -lt $lines.Count; $index++) {
            if ($lines[$index] -match "^\s*$([regex]::Escape($name))\s*=") {
                $lines[$index] = "$name=$($Updates[$name])"
                $updated = $true
                break
            }
        }
        if (-not $updated) {
            $lines.Add("$name=$($Updates[$name])")
        }
    }

    [IO.File]::WriteAllLines(
        $ApiEnvPath,
        $lines,
        [Text.UTF8Encoding]::new($false)
    )
}

function Read-DotEnvValues {
    $values = @{}
    if (-not (Test-Path -LiteralPath $ApiEnvPath -PathType Leaf)) {
        return $values
    }
    foreach ($line in [IO.File]::ReadAllLines($ApiEnvPath)) {
        if ($line -match "^\s*(IRONAPI_[A-Z0-9_]+)\s*=(.*)$") {
            $values[$Matches[1]] = $Matches[2].Trim().Trim("'").Trim('"')
        }
    }
    return $values
}

function Configure-ApiLaunch {
    Write-Heading "IronAPI listener"
    $current = Read-DotEnvValues
    $bindHost = Read-ConfiguredValue `
        "Bind address" `
        "Use a specific server IP, or 0.0.0.0 to listen on every interface." `
        (Get-ConfigValue $current "IRONAPI_BIND_HOST" "127.0.0.1") `
        {
            param($value)
            $parsed = $null
            if (
                $value -ne "0.0.0.0" -and
                ![Net.IPAddress]::TryParse($value, [ref]$parsed)
            ) {
                return "Enter an IP address such as 198.51.100.10."
            }
            return $null
        }
    $port = Read-ConfiguredValue `
        "TCP port" `
        "WinPE must be able to reach this port on the API server." `
        (Get-ConfigValue $current "IRONAPI_PORT" "8000") `
        {
            param($value)
            $number = 0
            if (
                ![int]::TryParse($value, [ref]$number) -or
                $number -lt 1 -or
                $number -gt 65535
            ) {
                return "Enter a port from 1 to 65535."
            }
            return $null
        }
    $currentAccessLog = (
        Get-ConfigValue $current "IRONAPI_ACCESS_LOG" "false"
    ) -match "^(?i:true|1|yes|on)$"
    $accessLog = Read-YesNo "Enable Uvicorn access log" $currentAccessLog
    $allowedNetworks = Read-ConfiguredValue `
        "Allowed client networks" `
        "Comma-separated CIDRs allowed to call IronAPI. Loopback is always allowed." `
        (Get-ConfigValue `
            $current `
            "IRONAPI_ALLOWED_CLIENT_NETWORKS" `
            "192.0.2.0/24") `
        {
            param($value)
            foreach ($item in $value.Split(",")) {
                if ($item.Trim() -notmatch "^([^/]+)/(\d+)$") {
                    return "Use CIDR values such as 192.0.2.0/24."
                }
                $address = $null
                if (![Net.IPAddress]::TryParse($Matches[1], [ref]$address)) {
                    return "Invalid network address: $($Matches[1])."
                }
                $prefix = [int]$Matches[2]
                $maximum = if (
                    $address.AddressFamily -eq
                    [Net.Sockets.AddressFamily]::InterNetwork
                ) { 32 } else { 128 }
                if ($prefix -lt 0 -or $prefix -gt $maximum) {
                    return "Invalid prefix length for $address."
                }
            }
            return $null
        }
    Set-DotEnvValues @{
        IRONAPI_BIND_HOST = $bindHost
        IRONAPI_PORT = $port
        IRONAPI_ACCESS_LOG = $accessLog.ToString().ToLowerInvariant()
        IRONAPI_ALLOWED_CLIENT_NETWORKS = $allowedNetworks.Replace(" ", "")
    }
    Write-Host "Saved API listener settings." -ForegroundColor Green
}

function Configure-Unattend {
    Write-Heading "Windows unattend template"
    if (
        (Test-Path -LiteralPath $UnattendPath -PathType Leaf) -and
        (Read-YesNo "Keep the existing working unattend template" $true)
    ) {
        Write-Host "Existing unattend template kept."
        return
    }

    $adminName = Read-ConfiguredValue `
        "Local administrator name" `
        "Local recovery account created during Windows setup." `
        "localadmin" `
        {
            param($value)
            if ($value -notmatch "^[A-Za-z0-9._-]{1,20}$") {
                return "Use 1-20 letters, digits, dot, underscore, or hyphen."
            }
            return $null
        }
    $timeZone = Read-TimeZoneChoice "Central Asia Standard Time"
    $locale = Read-ConfiguredValue `
        "Windows locale" `
        "Applied to input, system, UI, and user locale." `
        "ru-RU" `
        {
            param($value)
            if ($value -notmatch "^[a-z]{2}-[A-Z]{2}$") {
                return "Enter a locale such as ru-RU or en-US."
            }
            return $null
        }
    $password = Read-PlainTextSecret (
        "Local administrator password (input is hidden)"
    )
    if ([string]::IsNullOrEmpty($password)) {
        throw "The local administrator password cannot be empty."
    }

    $content = [IO.File]::ReadAllText($UnattendExamplePath)
    $content = $content.Replace(
        "CHANGE_ME_USE_A_UNIQUE_PASSWORD",
        [Security.SecurityElement]::Escape($password)
    )
    $content = $content.Replace("<Name>localadmin</Name>", "<Name>$adminName</Name>")
    $content = $content.Replace(
        "<DisplayName>localadmin</DisplayName>",
        "<DisplayName>$adminName</DisplayName>"
    )
    $content = $content.Replace(
        "<TimeZone>Central Asia Standard Time</TimeZone>",
        "<TimeZone>$timeZone</TimeZone>"
    )
    foreach ($element in @(
        "InputLocale",
        "SystemLocale",
        "UILanguage",
        "UserLocale"
    )) {
        $content = $content.Replace(
            "<$element>ru-RU</$element>",
            "<$element>$locale</$element>"
        )
    }
    Backup-ConfigurationFile $UnattendPath
    [IO.File]::WriteAllText(
        $UnattendPath,
        $content,
        [Text.UTF8Encoding]::new($false)
    )

    if (Test-Path -LiteralPath $WinPEConfigPath -PathType Leaf) {
        Backup-ConfigurationFile $WinPEConfigPath
        $configLines = [Collections.Generic.List[string]]::new()
        $configLines.AddRange([IO.File]::ReadAllLines($WinPEConfigPath))
        $adminLine = (
            "`$SetupLocalAdminName = {0}" -f `
                (ConvertTo-PowerShellLiteral $adminName)
        )
        $updated = $false
        for ($i = 0; $i -lt $configLines.Count; $i++) {
            if ($configLines[$i] -match "^\s*\`$SetupLocalAdminName\s*=") {
                $configLines[$i] = $adminLine
                $updated = $true
            }
        }
        if (-not $updated) {
            $configLines.Add($adminLine)
        }
        [IO.File]::WriteAllLines(
            $WinPEConfigPath,
            $configLines,
            [Text.UTF8Encoding]::new($false)
        )
    }

    $password = $null
    Write-Host "Saved: $UnattendPath" -ForegroundColor Green
}

function Invoke-Validation {
    & (Join-Path $PSScriptRoot "Test-IronDeploy.ps1")
    return $LASTEXITCODE
}

function Invoke-WinPERebuild {
    Write-Heading "Rebuild WinPE WIM/ISO"
    Write-Host (
        "This starts the WinPE rebuild wrapper. It will ask whether to " +
        "rebuild ISO only, WIM only, or both."
    ) -ForegroundColor DarkGray
    & powershell.exe `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File $WinPEBuildPath
    if ($LASTEXITCODE -ne 0) {
        throw "WinPE rebuild wrapper failed with exit code $LASTEXITCODE."
    }
}

function Invoke-InitialSetup {
    Initialize-DirectoryLayout
    if (
        -not (Test-Path `
            -LiteralPath (
                Join-Path `
                    $IronDeployRoot `
                    ".work\WinPE_amd64\media\sources\boot.wim"
            ) `
            -PathType Leaf)
    ) {
        Prepare-WinPEWorkTree
    }
    Configure-WinPE
    if (Read-YesNo "Is the SMB share hosted on this server" $true) {
        Configure-LocalSmbShare
    }
    Configure-ApiLaunch
    Configure-Unattend
    Configure-OdjAcl
    if (Read-YesNo "Open the detailed IronAPI setup now" $true) {
        & $ApiSetupPath -InitialSetup
    }
    Invoke-Validation | Out-Null
}

function Show-Menu {
    Write-Heading "IronDeploy setup"
    Write-Host "1. Prepare WinPE .work (copype + optional components)"
    Write-Host "2. Configure IronAPI"
    Write-Host "3. Configure WinPE deploy.config"
    Write-Host "4. Configure local SMB share and read ACL"
    Write-Host "5. Configure Windows unattend template"
    Write-Host "6. Configure ODJ directory ACL"
    Write-Host "7. Validate the complete installation"
    Write-Host "8. Run the complete initial setup"
    Write-Host "9. Rebuild WinPE WIM/ISO"
    Write-Host "0. Exit"
}

if ($Validate -and $InitialSetup) {
    throw "Use either -Validate or -InitialSetup, not both."
}
if ($Validate) {
    exit (Invoke-Validation)
}
if ($InitialSetup) {
    Invoke-InitialSetup
}

while ($true) {
    Show-Menu
    switch ((Read-Host "Select an option").Trim()) {
        "1" { Prepare-WinPEWorkTree }
        "2" {
            Configure-ApiLaunch
            & $ApiSetupPath
        }
        "3" { Configure-WinPE }
        "4" { Configure-LocalSmbShare }
        "5" { Configure-Unattend }
        "6" { Configure-OdjAcl }
        "7" { Invoke-Validation | Out-Null }
        "8" { Invoke-InitialSetup }
        "9" { Invoke-WinPERebuild }
        "0" { return }
        default { Write-Host "Unknown option." -ForegroundColor Yellow }
    }
}
