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

function Stop-OdjAclOperation {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Code,
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    Write-JsonResult -Result @{
        ok = $false
        code = $Code
        message = $Message
    } -ExitCode 1
}

function New-RestrictedOdjAcl {
    param(
        [Parameter(Mandatory = $true)]
        [bool]$IsDirectory,
        [Parameter(Mandatory = $true)]
        [Security.Principal.SecurityIdentifier]$ServiceSid
    )

    if ($IsDirectory) {
        $acl = [Security.AccessControl.DirectorySecurity]::new()
        $inheritance = (
            [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [Security.AccessControl.InheritanceFlags]::ObjectInherit
        )
    }
    else {
        $acl = [Security.AccessControl.FileSecurity]::new()
        $inheritance = [Security.AccessControl.InheritanceFlags]::None
    }

    $acl.SetAccessRuleProtection($true, $false)
    $propagation = [Security.AccessControl.PropagationFlags]::None
    $allow = [Security.AccessControl.AccessControlType]::Allow
    $entries = @(
        [pscustomobject]@{
            Sid = [Security.Principal.SecurityIdentifier]::new("S-1-5-18")
            Rights = [Security.AccessControl.FileSystemRights]::FullControl
        }
        [pscustomobject]@{
            Sid = [Security.Principal.SecurityIdentifier]::new("S-1-5-32-544")
            Rights = [Security.AccessControl.FileSystemRights]::FullControl
        }
        [pscustomobject]@{
            Sid = $ServiceSid
            Rights = [Security.AccessControl.FileSystemRights]::Modify
        }
    )

    $seenSids = @{}
    foreach ($entry in $entries) {
        $sid = [Security.Principal.SecurityIdentifier]$entry.Sid
        $sidValue = $sid.Value
        if ($seenSids.ContainsKey($sidValue)) {
            continue
        }
        $seenSids[$sidValue] = $true
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $sid,
            [Security.AccessControl.FileSystemRights]$entry.Rights,
            $inheritance,
            $propagation,
            $allow
        )
        [void]$acl.AddAccessRule($rule)
    }
    return $acl
}

try {
    $requestText = [Console]::In.ReadToEnd()
    if ([string]::IsNullOrWhiteSpace($requestText)) {
        Stop-OdjAclOperation -Code "invalid_request" -Message "ODJ ACL request is empty."
    }

    $request = $requestText | ConvertFrom-Json
    $account = ([string]$request.account).Trim()
    if (
        $account.Length -gt 256 -or
        $account -notmatch "^[^\\/@\r\n]+\\[^\\/@\r\n]+$"
    ) {
        Stop-OdjAclOperation `
            -Code "invalid_account" `
            -Message "Use DOMAIN\user or COMPUTER\user for the IronAPI process account."
    }

    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Stop-OdjAclOperation `
            -Code "not_elevated" `
            -Message "SetupWeb must run as administrator to secure the ODJ folder."
    }

    try {
        $serviceSid = [Security.Principal.NTAccount]::new($account).Translate(
            [Security.Principal.SecurityIdentifier]
        )
        $canonicalAccount = $serviceSid.Translate(
            [Security.Principal.NTAccount]
        ).Value
    }
    catch {
        Stop-OdjAclOperation `
            -Code "account_not_found" `
            -Message "Windows cannot resolve IronAPI process account '$account'."
    }

    $ironDeployRoot = Split-Path -Parent $PSScriptRoot
    $odjPath = Join-Path $ironDeployRoot "ODJ"
    $pendingPath = Join-Path $odjPath "pending"
    foreach ($path in @($odjPath, $pendingPath)) {
        if (-not (Test-Path -LiteralPath $path -PathType Container)) {
            [void](New-Item -ItemType Directory -Path $path -Force)
        }
    }

    $items = @(
        Get-ChildItem -LiteralPath $odjPath -Force -Recurse -ErrorAction Stop |
            Where-Object {
                -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint)
            }
    )
    foreach ($item in $items) {
        $itemAcl = New-RestrictedOdjAcl `
            -IsDirectory ([bool]$item.PSIsContainer) `
            -ServiceSid $serviceSid
        Set-Acl -LiteralPath $item.FullName -AclObject $itemAcl
    }

    $odjAcl = New-RestrictedOdjAcl -IsDirectory $true -ServiceSid $serviceSid
    Set-Acl -LiteralPath $odjPath -AclObject $odjAcl

    Write-JsonResult -Result @{
        ok = $true
        status = "secured"
        path = [IO.Path]::GetFullPath($odjPath)
        account = $account
        aclAccount = $canonicalAccount
        updatedItems = $items.Count + 1
    }
}
catch {
    Stop-OdjAclOperation -Code "operation_failed" -Message $_.Exception.Message
}
