#Requires -Version 5.1

<#
.SYNOPSIS
Updates the IronDeploy WinPE boot.wim without rebuilding the ISO.

.DESCRIPTION
Mounts boot.wim index 1, copies the current WinPE runtime files,
verifies each copy with SHA-256, and commits/unmounts the image.

The deployment script is copied as data and is never executed on the host.

.EXAMPLE
.\Rebuild-IronDeployWim.ps1

.EXAMPLE
.\Rebuild-IronDeployWim.ps1 -UseExistingMount

.EXAMPLE
.\Rebuild-IronDeployWim.ps1 -RecoverInvalidMount
#>

[CmdletBinding()]
param(
    [switch]$RecoverInvalidMount,
    [switch]$UseExistingMount
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$IronDeployRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$RuntimeRoot = Join-Path $IronDeployRoot "WinPE\Runtime"
$WinPERoot = Join-Path $IronDeployRoot ".work\WinPE_amd64"
$DistRoot = Join-Path $IronDeployRoot "dist"
$PublishedWim = Join-Path $DistRoot "IronDeploy_PE.wim"
$MountDir = Join-Path $WinPERoot "mount"
$BootWim = Join-Path $WinPERoot "media\sources\boot.wim"
# TEMPORARY WINPE DRIVER UPLOAD: optional directory copied by the block below.
$WinPEDriversSource = Join-Path $RuntimeRoot "WinPEDrivers"
$WinPEDriversDestination = Join-Path $MountDir "IronDeploy\WinPEDrivers"

$FilesToCopy = @(
    @{
        Source = Join-Path $RuntimeRoot "deploy.ps1"
        Destination = Join-Path $MountDir "IronDeploy\deploy.ps1"
    },
    @{
        Source = Join-Path $RuntimeRoot "IronDeploy.DriverInventory.ps1"
        Destination = Join-Path $MountDir "IronDeploy\IronDeploy.DriverInventory.ps1"
    },
    @{
        Source = Join-Path $RuntimeRoot "IronDeploy.Engine.ps1"
        Destination = Join-Path $MountDir "IronDeploy\IronDeploy.Engine.ps1"
    },
    @{
        Source = Join-Path $RuntimeRoot "IronDeploy.Gui.ps1"
        Destination = Join-Path $MountDir "IronDeploy\IronDeploy.Gui.ps1"
    },
    @{
        Source = Join-Path $RuntimeRoot "deploy.config.ps1"
        Destination = Join-Path $MountDir "IronDeploy\deploy.config.ps1"
    },
    @{
        Source = Join-Path $RuntimeRoot "diskpart-uefi.txt"
        Destination = Join-Path $MountDir "IronDeploy\diskpart-uefi.txt"
    },
    @{
        Source = Join-Path $RuntimeRoot "Tools\7-Zip\7za.exe"
        Destination = Join-Path $MountDir "IronDeploy\Tools\7-Zip\7za.exe"
    },
    @{
        Source = Join-Path $RuntimeRoot "Tools\7-Zip\7-Zip-LICENSE.txt"
        Destination = Join-Path $MountDir "IronDeploy\Tools\7-Zip\7-Zip-LICENSE.txt"
    },
    @{
        Source = Join-Path $RuntimeRoot "startnet.cmd"
        Destination = Join-Path $MountDir "Windows\System32\startnet.cmd"
    },
    @{
        Source = Join-Path $RuntimeRoot "Load-WinPEDrivers.ps1"
        Destination = Join-Path $MountDir "IronDeploy\Load-WinPEDrivers.ps1"
    }
)

$ObsoleteFilesToRemove = @(
    (Join-Path $MountDir "IronDeploy\IronDeploy.Console.ps1")
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

    if ($RecoverInvalidMount) {
        $arguments += "-RecoverInvalidMount"
    }
    if ($UseExistingMount) {
        $arguments += "-UseExistingMount"
    }

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
    if (-not (Test-Path -LiteralPath $WinPERoot -PathType Container)) {
        throw "WinPE working tree is missing: $WinPERoot"
    }

    $requiredFiles = @($BootWim)
    $requiredFiles += $FilesToCopy | ForEach-Object { $_.Source }

    foreach ($path in $requiredFiles) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Required file is missing: $path"
        }
    }

    $configPath = Join-Path $RuntimeRoot "deploy.config.ps1"
    $legacySecretNames = @(
        'SharePath',
        'ShareUser',
        'SharePassword',
        'ApiDeploymentSecret',
        'UnattendTemplate'
    )
    $configText = Get-Content -LiteralPath $configPath -Raw
    foreach ($secretName in $legacySecretNames) {
        if ($configText -match "(?m)^\s*\`$$secretName\s*=") {
            throw (
                "WinPE config still contains legacy deployment secret " +
                "'$secretName'. Save the configuration through SetupWeb " +
                "before rebuilding the WIM."
            )
        }
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
        $mount = $expectedMounts[0]
        Assert-ExpectedMount $mount

        if ($mount.Status -ieq "Ok") {
            if (-not $UseExistingMount) {
                throw (
                    "boot.wim is already mounted. Inspect the mount, then " +
                    "rerun with -UseExistingMount to copy files and commit it."
                )
            }

            Write-Host (
                "Using the existing healthy boot.wim mount."
            ) -ForegroundColor Green
            return
        }

        if ($mount.Status -ieq "Invalid" -and $RecoverInvalidMount) {
            Invoke-NativeCommand `
                -FilePath "dism.exe" `
                -Arguments @(
                    "/English",
                    "/Unmount-Image",
                    "/MountDir:$MountDir",
                    "/Discard"
                ) `
                -Description "Discarding invalid expected mount"

            Invoke-NativeCommand `
                -FilePath "dism.exe" `
                -Arguments @("/English", "/Cleanup-Wim") `
                -Description "Cleaning invalid WIM mount records"

            $remainingMounts = @(Get-MountedWimInfo)
            if ($remainingMounts.Count -ne 0) {
                throw "A mounted image remains after invalid-mount recovery."
            }
        }
        elseif ($mount.Status -ieq "Invalid") {
            throw (
                "The expected mount is Invalid. Inspect it, or rerun with " +
                "-RecoverInvalidMount to discard only this invalid mount."
            )
        }
        else {
            throw (
                "The expected mount has unsupported status " +
                "'$($mount.Status)'."
            )
        }
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

function Copy-AndVerifyWinPEFiles {
    Write-Host ""
    Write-Host "==> Copying current WinPE source files" -ForegroundColor Cyan

    foreach ($file in $FilesToCopy) {
        $destinationDirectory = Split-Path -Parent $file.Destination
        New-Item `
            -ItemType Directory `
            -Path $destinationDirectory `
            -Force |
            Out-Null

        Copy-Item `
            -LiteralPath $file.Source `
            -Destination $file.Destination `
            -Force

        $sourceHash = (
            Get-FileHash `
                -Algorithm SHA256 `
                -LiteralPath $file.Source
        ).Hash
        $destinationHash = (
            Get-FileHash `
                -Algorithm SHA256 `
                -LiteralPath $file.Destination
        ).Hash

        if ($sourceHash -ne $destinationHash) {
            throw (
                "SHA-256 mismatch after copying '$($file.Source)' to " +
                "'$($file.Destination)'."
            )
        }

        Write-Host "Verified: $($file.Source)" -ForegroundColor Green
    }

    # TEMPORARY WINPE DRIVER UPLOAD: replace this isolated embedded directory.
    if (Test-Path -LiteralPath $WinPEDriversDestination -PathType Container) {
        Remove-Item -LiteralPath $WinPEDriversDestination -Recurse -Force
    }
    if (Test-Path -LiteralPath $WinPEDriversSource -PathType Container) {
        foreach ($sourceFile in Get-ChildItem -LiteralPath $WinPEDriversSource -File -Recurse) {
            $relativePath = $sourceFile.FullName.Substring(
                $WinPEDriversSource.Length
            ).TrimStart([IO.Path]::DirectorySeparatorChar)
            $destination = Join-Path $WinPEDriversDestination $relativePath
            New-Item -ItemType Directory -Path (Split-Path $destination -Parent) -Force |
                Out-Null
            Copy-Item -LiteralPath $sourceFile.FullName -Destination $destination -Force
            if (
                (Get-FileHash -Algorithm SHA256 -LiteralPath $sourceFile.FullName).Hash -ne
                (Get-FileHash -Algorithm SHA256 -LiteralPath $destination).Hash
            ) {
                throw "SHA-256 mismatch after copying WinPE driver '$relativePath'."
            }
        }
        Write-Host "Verified uploaded WinPE drivers." -ForegroundColor Green
    }

    foreach ($path in $ObsoleteFilesToRemove) {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            Remove-Item -LiteralPath $path -Force
            Write-Host "Removed obsolete WinPE file: $path" -ForegroundColor Green
        }
    }
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
    Write-Host "IronDeploy boot.wim rebuild" -ForegroundColor Cyan
    Write-Host "Source: $PSScriptRoot"
    Write-Host "Image:  $BootWim"
    Write-Host (
        "The deployment script will only be copied; it will never be executed."
    )

    Assert-RequiredFiles
    Mount-BootWim
    Copy-AndVerifyWinPEFiles
    Commit-AndUnmountBootWim

    $image = Get-Item -LiteralPath $BootWim
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $BootWim).Hash
    New-Item -ItemType Directory -Path $DistRoot -Force | Out-Null
    Copy-Item -LiteralPath $BootWim -Destination $PublishedWim -Force
    $publishedHash = (
        Get-FileHash -Algorithm SHA256 -LiteralPath $PublishedWim
    ).Hash
    if ($publishedHash -ne $hash) {
        throw "Published WIM hash differs from the working boot.wim."
    }

    Write-Host ""
    Write-Host "IronDeploy boot.wim rebuilt successfully." `
        -ForegroundColor Green
    Write-Host "Working image: $($image.FullName)"
    Write-Host "Published WIM: $PublishedWim"
    Write-Host "Size:   $($image.Length) bytes"
    Write-Host "SHA256: $hash"
    Write-Host ""
    Write-Host "No ISO was created or modified." -ForegroundColor Yellow
    Write-Host "Closing in 3 seconds..."
    Start-Sleep -Seconds 3
}
catch {
    Write-Host ""
    Write-Host "WIM REBUILD FAILED: $($_.Exception.Message)" `
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
    Write-Host "Closing in 3 seconds..."
    Start-Sleep -Seconds 3
    exit 1
}
