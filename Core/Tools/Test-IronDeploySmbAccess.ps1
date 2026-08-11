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

function Get-SmbFailureCode {
    param([string]$Message)

    if ($Message -match "1219|multiple connections") {
        return "credential_conflict"
    }
    if ($Message -match "logon failure|user name or password is incorrect|unknown user name") {
        return "authentication_failed"
    }
    if ($Message -match "access is denied") {
        return "access_denied"
    }
    if ($Message -match "network name cannot be found|network path was not found") {
        return "share_not_found"
    }
    return "connection_failed"
}

$driveName = $null
$securePassword = $null
$credential = $null
try {
    $requestText = [Console]::In.ReadToEnd()
    if ([string]::IsNullOrWhiteSpace($requestText)) {
        throw "SMB request is empty."
    }
    $request = $requestText | ConvertFrom-Json
    $serverAddress = ([string]$request.serverAddress).Trim()
    $shareName = [string]$request.shareName
    $account = [string]$request.account
    $password = [string]$request.password
    if (
        [string]::IsNullOrWhiteSpace($serverAddress) -or
        $serverAddress.Length -gt 253 -or
        $serverAddress -notmatch "^[A-Za-z0-9._-]+$"
    ) {
        throw "SMB address must be a hostname, FQDN, or IPv4 address."
    }
    if ($serverAddress -match "^[0-9.]+$" -and $serverAddress.Contains(".")) {
        $parsedAddress = $null
        if (
            -not [Net.IPAddress]::TryParse($serverAddress, [ref]$parsedAddress) -or
            $parsedAddress.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork
        ) {
            throw "SMB IPv4 address is invalid."
        }
    }
    else {
        foreach ($label in $serverAddress.Split(".")) {
            if (
                -not $label -or
                $label.Length -gt 63 -or
                $label.StartsWith("-") -or
                $label.EndsWith("-")
            ) {
                throw "SMB hostname is invalid."
            }
        }
    }
    if ($shareName -notmatch "^[A-Za-z0-9._-]{1,80}$") {
        throw "SMB share name is invalid."
    }
    if ($account -notmatch "^[^\\/@\r\n]+\\[^\\/@\r\n]+$") {
        throw "Use SERVER\user or DOMAIN\user for the SMB account."
    }
    if ([string]::IsNullOrEmpty($password)) {
        throw "SMB password is required."
    }

    $uncPath = "\\$serverAddress\$shareName"
    $securePassword = ConvertTo-SecureString $password -AsPlainText -Force
    $password = $null
    $credential = [Management.Automation.PSCredential]::new($account, $securePassword)
    $driveName = "IDSMB" + ([Guid]::NewGuid().ToString("N").Substring(0, 8))

    New-PSDrive `
        -Name $driveName `
        -PSProvider FileSystem `
        -Root $uncPath `
        -Credential $credential `
        -Scope Script `
        -ErrorAction Stop |
        Out-Null

    $driveRoot = "$driveName`:\"
    [void](Get-Item -LiteralPath $driveRoot -ErrorAction Stop)
    [void](@(Get-ChildItem -LiteralPath $driveRoot -Force -ErrorAction Stop | Select-Object -First 1))

    $missingFolders = @()
    foreach ($folder in @("Images", "Drivers")) {
        if (-not (Test-Path -LiteralPath (Join-Path $driveRoot $folder) -PathType Container)) {
            $missingFolders += $folder
        }
    }

    Write-JsonResult -Result @{
        ok = $true
        status = "accessible"
        uncPath = $uncPath
        account = $account
        missingFolders = $missingFolders
    }
}
catch {
    $message = $_.Exception.Message
    Write-JsonResult -Result @{
        ok = $false
        code = (Get-SmbFailureCode -Message $message)
        message = $message
    } -ExitCode 1
}
finally {
    if ($driveName) {
        Remove-PSDrive -Name $driveName -Force -ErrorAction SilentlyContinue
    }
    $credential = $null
    $securePassword = $null
}
