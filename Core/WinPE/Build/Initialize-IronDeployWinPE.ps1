#Requires -Version 5.1

<#
.SYNOPSIS
Creates or reinstalls the portable IronDeploy WinPE working tree.

.DESCRIPTION
Calls the installed Windows ADK environment and copype.cmd to create
.work\WinPE_amd64, then adds the required PowerShell, WMI, .NET, storage, and
DISM WinPE optional components to boot.wim.

If .work\WinPE_amd64 already exists, the script asks whether to reinstall it.
Reinstall removes only that exact working tree after verifying that DISM does
not report any mounted images.
#>

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$IronDeployRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$WinPERoot = Join-Path $IronDeployRoot ".work\WinPE_amd64"
$MountDir = Join-Path $WinPERoot "mount"
$BootWim = Join-Path $WinPERoot "media\sources\boot.wim"
$DandISetEnv = Join-Path `
    ${env:ProgramFiles(x86)} `
    "Windows Kits\10\Assessment and Deployment Kit\Deployment Tools\DandISetEnv.bat"
$Copype = Join-Path `
    ${env:ProgramFiles(x86)} `
    "Windows Kits\10\Assessment and Deployment Kit\Windows Preinstallation Environment\copype.cmd"
$OptionalComponentsRoot = Join-Path `
    ${env:ProgramFiles(x86)} `
    "Windows Kits\10\Assessment and Deployment Kit\Windows Preinstallation Environment\amd64\WinPE_OCs"
$Language = "en-us"

# Dependency order required by Microsoft:
# WMI -> NetFX -> Scripting -> PowerShell -> PowerShell-dependent components.
$Components = @(
    "WinPE-WMI",
    "WinPE-NetFX",
    "WinPE-Scripting",
    "WinPE-PowerShell",
    "WinPE-StorageWMI",
    "WinPE-DismCmdlets"
)

$CommitCompleted = $false

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

        [Parameter(Mandatory = $true)]
        [bool]$Default
    )

    $suffix = if ($Default) { "[Y/n]" } else { "[y/N]" }
    while ($true) {
        $answer = (Read-Host "$Prompt $suffix").Trim()
        if ([string]::IsNullOrEmpty($answer)) {
            return $Default
        }
        if ($answer -match "^(?i:y|yes)$") {
            return $true
        }
        if ($answer -match "^(?i:n|no)$") {
            return $false
        }
    }
}

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    Write-Host ""
    Write-Host "==> $Description" -ForegroundColor Cyan
    & $FilePath @Arguments
    $exitCode = $LASTEXITCODE

    if ($exitCode -ne 0) {
        throw "$Description failed with exit code $exitCode."
    }
}

function Get-NormalizedPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    return [IO.Path]::GetFullPath($Path).TrimEnd("\")
}

function Get-MountedWimInfo {
    Write-Host ""
    Write-Host "==> Checking DISM mount state" -ForegroundColor Cyan

    $output = @(
        & dism.exe /English /Get-MountedWimInfo 2>&1 |
            ForEach-Object { [string]$_ }
    )
    $exitCode = $LASTEXITCODE

    foreach ($line in $output) {
        Write-Host $line
    }

    if ($exitCode -ne 0) {
        throw "DISM mount-state check failed with exit code $exitCode."
    }

    $mounts = @()
    $current = $null

    foreach ($line in $output) {
        if ($line -match "^\s*Mount Dir\s*:\s*(.+?)\s*$") {
            if ($null -ne $current) {
                $mounts += [pscustomobject]$current
            }

            $current = @{
                MountDir = $Matches[1]
                ImageFile = $null
                ImageIndex = $null
                Status = $null
            }
            continue
        }

        if ($null -eq $current) {
            continue
        }

        if ($line -match "^\s*Image File\s*:\s*(.+?)\s*$") {
            $current.ImageFile = $Matches[1]
        }
        elseif ($line -match "^\s*Image Index\s*:\s*(\d+)\s*$") {
            $current.ImageIndex = [int]$Matches[1]
        }
        elseif ($line -match "^\s*Status\s*:\s*(.+?)\s*$") {
            $current.Status = $Matches[1]
        }
    }

    if ($null -ne $current) {
        $mounts += [pscustomobject]$current
    }

    return $mounts
}

function Assert-RequiredFiles {
    foreach ($requiredFile in @($DandISetEnv, $Copype)) {
        if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
            throw "Required Windows ADK file is missing: $requiredFile"
        }
    }

    if (
        -not (
            Test-Path `
                -LiteralPath $OptionalComponentsRoot `
                -PathType Container
        )
    ) {
        throw (
            "WinPE optional-components directory is missing: " +
            $OptionalComponentsRoot
        )
    }

    $requiredPackages = @()
    foreach ($component in $Components) {
        $requiredPackages += Join-Path `
            $OptionalComponentsRoot `
            "$component.cab"
        $requiredPackages += Join-Path `
            (Join-Path $OptionalComponentsRoot $Language) `
            "${component}_${Language}.cab"
    }

    foreach ($packagePath in $requiredPackages) {
        if (-not (Test-Path -LiteralPath $packagePath -PathType Leaf)) {
            throw "Required optional-component package is missing: $packagePath"
        }
    }
}

function Assert-SafeWinPERoot {
    $expectedPath = Get-NormalizedPath (
        Join-Path (Join-Path $IronDeployRoot ".work") "WinPE_amd64"
    )
    $actualPath = Get-NormalizedPath $WinPERoot

    if ($actualPath -ine $expectedPath) {
        throw "Refusing to remove unexpected WinPE path: $actualPath"
    }
    if ($actualPath -notmatch "\\\.work\\WinPE_amd64$") {
        throw "Refusing to remove path outside the WinPE working tree: $actualPath"
    }
}

function Remove-ExistingWinPEWorkTree {
    Assert-SafeWinPERoot

    $mounts = @(Get-MountedWimInfo)
    if ($mounts.Count -ne 0) {
        $paths = ($mounts | ForEach-Object { $_.MountDir }) -join ", "
        throw (
            "DISM reports mounted image(s). Unmount or recover them before " +
            "reinstalling .work: $paths"
        )
    }

    Write-Host "Removing existing WinPE working tree:" -ForegroundColor Yellow
    Write-Host "  $WinPERoot"
    Remove-Item -LiteralPath $WinPERoot -Recurse -Force
}

function Invoke-Copype {
    New-Item `
        -ItemType Directory `
        -Path (Split-Path $WinPERoot -Parent) `
        -Force |
        Out-Null

    Write-Host "Preparing WinPE working tree with copype" -ForegroundColor Cyan
    Write-Host "Destination: $WinPERoot"
    $commandLine = 'call "{0}" && call "{1}" amd64 "{2}"' -f `
        $DandISetEnv,
        $Copype,
        $WinPERoot
    & cmd.exe /d /s /c $commandLine
    if ($LASTEXITCODE -ne 0) {
        throw "copype failed with exit code $LASTEXITCODE."
    }
    if (-not (Test-Path -LiteralPath $BootWim -PathType Leaf)) {
        throw "copype reported success but boot.wim is missing: $BootWim"
    }

    Write-Host "WinPE working tree created successfully." -ForegroundColor Green
}

function Assert-Amd64Image {
    Write-Host ""
    Write-Host "==> Checking boot.wim architecture" -ForegroundColor Cyan

    $output = @(
        & dism.exe `
            /English `
            /Get-WimInfo `
            "/WimFile:$BootWim" `
            /Index:1 2>&1 |
            ForEach-Object { [string]$_ }
    )
    $exitCode = $LASTEXITCODE

    foreach ($line in $output) {
        Write-Host $line
    }

    if ($exitCode -ne 0) {
        throw "Unable to inspect boot.wim; DISM exit code: $exitCode."
    }
    $architectureLines = @(
        $output |
            Where-Object {
                $_ -match "^\s*Architecture\s*:\s*(x64|amd64)\s*$"
            }
    )
    if ($architectureLines.Count -eq 0) {
        throw "boot.wim index 1 is not an amd64 image."
    }
}

function Assert-ExpectedMount {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Mount
    )

    if ([string]::IsNullOrWhiteSpace([string]$Mount.ImageFile)) {
        throw "DISM did not report the image file for mount: $MountDir"
    }

    $mountedImage = Get-NormalizedPath ([string]$Mount.ImageFile)
    $expectedImage = Get-NormalizedPath $BootWim

    if ($mountedImage -ine $expectedImage -or $Mount.ImageIndex -ne 1) {
        throw (
            "The expected mount directory belongs to another image or " +
            "index. Image: $($Mount.ImageFile), index: $($Mount.ImageIndex)."
        )
    }
}

function Mount-BootWim {
    $expectedMountPath = Get-NormalizedPath $MountDir
    $mounts = @(Get-MountedWimInfo)
    $expectedMounts = @(
        $mounts | Where-Object {
            (Get-NormalizedPath $_.MountDir) -ieq $expectedMountPath
        }
    )
    $otherMounts = @(
        $mounts | Where-Object {
            (Get-NormalizedPath $_.MountDir) -ine $expectedMountPath
        }
    )

    if ($otherMounts.Count -gt 0) {
        $paths = ($otherMounts | ForEach-Object { $_.MountDir }) -join ", "
        throw (
            "Unrelated mounted image(s) found. Inspect them before " +
            "continuing: $paths"
        )
    }
    if ($expectedMounts.Count -gt 1) {
        throw (
            "DISM reported the expected mount directory more than once: " +
            $MountDir
        )
    }
    if ($expectedMounts.Count -eq 1) {
        Assert-ExpectedMount $expectedMounts[0]
        throw (
            "boot.wim is already mounted. Inspect the mount state before " +
            "running WinPE initialization again."
        )
    }

    if (-not (Test-Path -LiteralPath $MountDir -PathType Container)) {
        New-Item -ItemType Directory -Path $MountDir -Force | Out-Null
    }

    $mountDirectoryItems = @(
        Get-ChildItem -LiteralPath $MountDir -Force
    )
    if ($mountDirectoryItems.Count -ne 0) {
        throw (
            "Mount directory is not empty while DISM reports no mount: " +
            $MountDir
        )
    }

    Invoke-NativeCommand `
        -FilePath "dism.exe" `
        -Arguments @(
            "/English",
            "/Mount-Image",
            "/ImageFile:$BootWim",
            "/Index:1",
            "/MountDir:$MountDir"
        ) `
        -Description "Mounting boot.wim"
}

function Add-OptionalComponents {
    Write-Host ""
    Write-Host "==> Adding language-neutral components" -ForegroundColor Cyan

    foreach ($component in $Components) {
        $packagePath = Join-Path `
            $OptionalComponentsRoot `
            "$component.cab"

        Invoke-NativeCommand `
            -FilePath "dism.exe" `
            -Arguments @(
                "/English",
                "/Image:$MountDir",
                "/Add-Package",
                "/PackagePath:$packagePath"
            ) `
            -Description "Adding $component"
    }

    Write-Host ""
    Write-Host "==> Adding $Language component packs" -ForegroundColor Cyan

    foreach ($component in $Components) {
        $packagePath = Join-Path `
            (Join-Path $OptionalComponentsRoot $Language) `
            "${component}_${Language}.cab"

        Invoke-NativeCommand `
            -FilePath "dism.exe" `
            -Arguments @(
                "/English",
                "/Image:$MountDir",
                "/Add-Package",
                "/PackagePath:$packagePath"
            ) `
            -Description "Adding $component $Language pack"
    }
}

function Show-InstalledPackages {
    Invoke-NativeCommand `
        -FilePath "dism.exe" `
        -Arguments @(
            "/English",
            "/Image:$MountDir",
            "/Get-Packages",
            "/Format:Table"
        ) `
        -Description "Listing installed WinPE packages"
}

function Commit-AndUnmountBootWim {
    Invoke-NativeCommand `
        -FilePath "dism.exe" `
        -Arguments @(
            "/English",
            "/Unmount-Image",
            "/MountDir:$MountDir",
            "/Commit"
        ) `
        -Description "Committing and unmounting boot.wim"

    $script:CommitCompleted = $true

    $remainingMounts = @(Get-MountedWimInfo)
    if ($remainingMounts.Count -ne 0) {
        $paths = ($remainingMounts | ForEach-Object { $_.MountDir }) -join ", "
        throw "Mounted image(s) remain after commit: $paths"
    }
}

if (-not (Test-Administrator)) {
    Start-ElevatedCopy
}

try {
    Write-Host "Initialize IronDeploy WinPE working tree" -ForegroundColor Cyan
    Write-Host "Working tree: $WinPERoot"
    Write-Host "Language:     $Language"

    if (Test-Path -LiteralPath $WinPERoot -PathType Container) {
        Write-Host ""
        Write-Host "Existing WinPE working tree found:" -ForegroundColor Yellow
        Write-Host "  $WinPERoot"

        if (
            -not (
                Read-YesNo `
                    -Prompt "Reinstall existing WinPE working tree" `
                    -Default $false
            )
        ) {
            if (Test-Path -LiteralPath $BootWim -PathType Leaf) {
                Write-Host "Using the existing WinPE working tree." `
                    -ForegroundColor Green
                exit 0
            }

            throw (
                "The existing WinPE working tree is incomplete and boot.wim " +
                "is missing: $BootWim"
            )
        }

        Assert-RequiredFiles
        Remove-ExistingWinPEWorkTree
    }
    elseif (Test-Path -LiteralPath $WinPERoot) {
        throw "WinPE working path exists but is not a directory: $WinPERoot"
    }
    else {
        Assert-RequiredFiles
    }

    Invoke-Copype
    Assert-Amd64Image
    Mount-BootWim
    Add-OptionalComponents
    Show-InstalledPackages
    Commit-AndUnmountBootWim

    $image = Get-Item -LiteralPath $BootWim
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $BootWim).Hash

    Write-Host ""
    Write-Host "WinPE working tree initialized successfully." `
        -ForegroundColor Green
    Write-Host "Path:   $($image.FullName)"
    Write-Host "Size:   $($image.Length) bytes"
    Write-Host "SHA256: $hash"
    Write-Host ""
    Write-Host "The WIM/ISO artifacts were not rebuilt." `
        -ForegroundColor Yellow
}
catch {
    Write-Host ""
    Write-Host "WINPE INITIALIZATION FAILED: $($_.Exception.Message)" `
        -ForegroundColor Red

    if ($CommitCompleted) {
        Write-Host (
            "The commit command completed, but final verification failed."
        ) -ForegroundColor Yellow
    }
    else {
        Write-Host (
            "The script did not commit the image after the failure."
        ) -ForegroundColor Yellow
    }

    Write-Host "Inspect mount state before retrying:"
    Write-Host "  dism.exe /English /Get-MountedWimInfo"
    Write-Host ""
    Write-Host "Do not mount the image a second time."
    exit 1
}