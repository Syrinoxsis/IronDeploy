Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)

function Write-JsonResult {
    param(
        [Parameter(Mandatory = $true)]
        [hashtable]$Result,
        [int]$ExitCode = 0
    )

    [Console]::Out.WriteLine(($Result | ConvertTo-Json -Compress -Depth 6))
    exit $ExitCode
}

function Stop-SmbOperation {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Code,
        [Parameter(Mandatory = $true)]
        [string]$Message,
        [hashtable]$Extra = @{}
    )

    $result = @{
        ok = $false
        code = $Code
        message = $Message
    }
    foreach ($name in $Extra.Keys) {
        $result[$name] = $Extra[$name]
    }
    Write-JsonResult -Result $result -ExitCode 1
}

function Get-LocalServerAliases {
    $aliases = @(
        "."
        "localhost"
        [string]$env:COMPUTERNAME
        [Net.Dns]::GetHostName()
    )

    try {
        $hostEntry = [Net.Dns]::GetHostEntry([Net.Dns]::GetHostName())
        $aliases += [string]$hostEntry.HostName
        $aliases += @($hostEntry.Aliases | ForEach-Object { [string]$_ })
    }
    catch {
        # Local DNS registration is optional; interface addresses are checked below.
    }

    try {
        foreach ($networkInterface in [Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
            foreach ($unicast in $networkInterface.GetIPProperties().UnicastAddresses) {
                $aliases += $unicast.Address.ToString()
            }
        }
    }
    catch {
        # Account resolution can still use the computer name and DNS aliases.
    }

    return @(
        $aliases |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
            Select-Object -Unique
    )
}

function Test-IsLocalServerAddress {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Address,
        [Parameter(Mandatory = $true)]
        [string[]]$LocalAliases
    )

    if ($LocalAliases -icontains $Address) {
        return $true
    }

    try {
        foreach ($resolvedAddress in [Net.Dns]::GetHostAddresses($Address)) {
            if ($LocalAliases -icontains $resolvedAddress.ToString()) {
                return $true
            }
        }
    }
    catch {
        return $false
    }
    return $false
}

function Resolve-SmbAclAccount {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Account,
        [Parameter(Mandatory = $true)]
        [string]$ServerAddress
    )

    try {
        $sid = [Security.Principal.NTAccount]::new($Account).Translate(
            [Security.Principal.SecurityIdentifier]
        )
        $canonical = $sid.Translate([Security.Principal.NTAccount]).Value
        return [pscustomobject]@{ Account = $canonical; Sid = $sid }
    }
    catch {
        # A local account written with this computer's FQDN or IP needs its
        # canonical COMPUTERNAME prefix before Windows can resolve its SID.
    }

    $separatorIndex = $Account.IndexOf("\")
    $authority = $Account.Substring(0, $separatorIndex)
    $username = $Account.Substring($separatorIndex + 1)
    $localAliases = @(Get-LocalServerAliases)
    $authorityIsLocal = Test-IsLocalServerAddress `
        -Address $authority `
        -LocalAliases $localAliases
    $connectionIsLocal = Test-IsLocalServerAddress `
        -Address $ServerAddress `
        -LocalAliases $localAliases
    $authorityUsesLocalComputerName = (
        $authority -ieq [string]$env:COMPUTERNAME -or
        (
            $authority.Contains(".") -and
            $authority.Split(".")[0] -ieq [string]$env:COMPUTERNAME
        )
    )
    $matchingLocalFqdn = (
        $authority -ieq $ServerAddress -and
        ($connectionIsLocal -or $authorityUsesLocalComputerName)
    )

    if (-not $authorityIsLocal -and -not $matchingLocalFqdn) {
        throw "Windows cannot resolve SMB account '$Account'. Use COMPUTERNAME\user for a local account or DOMAIN\user for a domain account."
    }

    $localAccount = "$env:COMPUTERNAME\$username"
    try {
        $sid = [Security.Principal.NTAccount]::new($localAccount).Translate(
            [Security.Principal.SecurityIdentifier]
        )
        $canonical = $sid.Translate([Security.Principal.NTAccount]).Value
        return [pscustomobject]@{ Account = $canonical; Sid = $sid }
    }
    catch {
        throw "Windows cannot resolve local SMB account '$localAccount' derived from '$Account'."
    }
}

try {
    $requestText = [Console]::In.ReadToEnd()
    if ([string]::IsNullOrWhiteSpace($requestText)) {
        Stop-SmbOperation -Code "invalid_request" -Message "SMB request is empty."
    }
    $request = $requestText | ConvertFrom-Json
    $serverAddress = ([string]$request.serverAddress).Trim()
    $shareName = [string]$request.shareName
    $account = [string]$request.account
    if (
        [string]::IsNullOrWhiteSpace($serverAddress) -or
        $serverAddress.Length -gt 253 -or
        $serverAddress -notmatch "^[A-Za-z0-9._-]+$"
    ) {
        Stop-SmbOperation -Code "invalid_server_address" -Message "SMB address must be a hostname, FQDN, or IPv4 address."
    }
    if ($shareName -notmatch "^[A-Za-z0-9._-]{1,80}$") {
        Stop-SmbOperation -Code "invalid_share_name" -Message "SMB share name is invalid."
    }
    if ($account -notmatch "^[^\\/@\r\n]+\\[^\\/@\r\n]+$") {
        Stop-SmbOperation -Code "invalid_account" -Message "Use SERVER\user or DOMAIN\user for the SMB account."
    }

    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Stop-SmbOperation -Code "not_elevated" -Message "SetupWeb must run as administrator to configure an SMB share."
    }

    try {
        $resolvedAccount = Resolve-SmbAclAccount `
            -Account $account `
            -ServerAddress $serverAddress
        $aclAccount = [string]$resolvedAccount.Account
    }
    catch {
        Stop-SmbOperation -Code "account_not_found" -Message $_.Exception.Message
    }

    $ironDeployRoot = Split-Path -Parent $PSScriptRoot
    $localPath = Join-Path $ironDeployRoot "Share"
    if (-not (Test-Path -LiteralPath $localPath -PathType Container)) {
        Stop-SmbOperation -Code "share_folder_missing" -Message "IronDeploy Share folder does not exist: $localPath"
    }
    $expectedPath = [IO.Path]::GetFullPath($localPath).TrimEnd("\")

    $shares = @(Get-SmbShare -ErrorAction Stop)
    $namedShare = @($shares | Where-Object { $_.Name -ieq $shareName }) | Select-Object -First 1
    if ($null -ne $namedShare) {
        $actualPath = [IO.Path]::GetFullPath([string]$namedShare.Path).TrimEnd("\")
        if ($actualPath -ine $expectedPath) {
            Stop-SmbOperation `
                -Code "name_conflict" `
                -Message "Share '$shareName' already points to '$actualPath'. Choose another name." `
                -Extra @{ existingPath = $actualPath }
        }
    }
    else {
        $samePathShares = @(
            $shares | Where-Object {
                $_.Path -and
                ([IO.Path]::GetFullPath([string]$_.Path).TrimEnd("\") -ieq $expectedPath)
            }
        )
        if ($samePathShares.Count -gt 0) {
            $existingNames = @($samePathShares | ForEach-Object { [string]$_.Name })
            Stop-SmbOperation `
                -Code "path_shared_as_other_name" `
                -Message "IronDeploy Share is already published as '$($existingNames -join ", ")'. Use the existing share name." `
                -Extra @{ existingNames = $existingNames }
        }
    }

    $status = "updated"
    if ($null -eq $namedShare) {
        New-SmbShare `
            -Name $shareName `
            -Path $expectedPath `
            -CachingMode Manual `
            -FolderEnumerationMode Unrestricted `
            -ReadAccess $aclAccount |
            Out-Null
        $status = "created"
    }
    else {
        Grant-SmbShareAccess `
            -Name $shareName `
            -AccountName $aclAccount `
            -AccessRight Read `
            -Force |
            Out-Null
    }

    $acl = Get-Acl -LiteralPath $expectedPath
    $rule = [Security.AccessControl.FileSystemAccessRule]::new(
        $aclAccount,
        [Security.AccessControl.FileSystemRights]::ReadAndExecute,
        (
            [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [Security.AccessControl.InheritanceFlags]::ObjectInherit
        ),
        [Security.AccessControl.PropagationFlags]::None,
        [Security.AccessControl.AccessControlType]::Allow
    )
    $acl.SetAccessRule($rule)
    Set-Acl -LiteralPath $expectedPath -AclObject $acl

    Write-JsonResult -Result @{
        ok = $true
        status = $status
        shareName = $shareName
        localPath = $expectedPath
        uncPath = "\\$serverAddress\$shareName"
        account = $account
        aclAccount = $aclAccount
    }
}
catch {
    Stop-SmbOperation -Code "operation_failed" -Message $_.Exception.Message
}
