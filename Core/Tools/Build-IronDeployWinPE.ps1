#Requires -Version 5.1

[CmdletBinding()]
param(
    [ValidateSet("Iso", "Wim", "Both")]
    [string]$Target,
    [switch]$Initialize,
    [switch]$UpdatePxeBundle,
    [switch]$UseExistingMount,
    [switch]$RecoverInvalidMount,
    # Package the current boot.wim into the ISO without copying the runtime
    # files into it first. Only use this right after a successful -Target Wim
    # build, when boot.wim is already up to date. Without it, -Target Iso does
    # a full pass (mount, copy runtime files, commit) so the ISO can never be
    # built from a stale or freshly initialized boot.wim.
    [switch]$SkipWimUpdate
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$IronDeployRoot = Split-Path $PSScriptRoot -Parent
$BuildRoot = Join-Path $IronDeployRoot "WinPE\Build"

function Read-BuildTarget {
    while ($true) {
        Write-Host "Select IronDeploy WinPE rebuild target:" -ForegroundColor Cyan
        Write-Host "  1. ISO only"
        Write-Host "  2. WIM only"
        Write-Host "  3. Both WIM and ISO"

        $choice = Read-Host "Enter 1, 2, or 3"
        switch ($choice.Trim()) {
            "1" { return "Iso" }
            "2" { return "Wim" }
            "3" { return "Both" }
            default {
                Write-Host "Invalid selection: $choice" -ForegroundColor Yellow
            }
        }
    }
}

if ([string]::IsNullOrWhiteSpace($Target)) {
    $Target = Read-BuildTarget
}

if ($UpdatePxeBundle -and $Target -eq "Iso") {
    throw "-UpdatePxeBundle requires -Target Wim or -Target Both."
}

if ($SkipWimUpdate) {
    if ($Target -ne "Iso") {
        throw "-SkipWimUpdate is only supported with -Target Iso."
    }
    if ($Initialize) {
        throw (
            "-SkipWimUpdate cannot be combined with -Initialize: " +
            "initialization resets boot.wim, so the ISO would be built from " +
            "a stock image without the IronDeploy runtime."
        )
    }
}

if ($Initialize) {
    & (Join-Path $BuildRoot "Initialize-IronDeployWinPE.ps1")
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

$buildArguments = @{}
if ($RecoverInvalidMount) {
    $buildArguments.RecoverInvalidMount = $true
}

if ($Target -eq "Wim") {
    if ($UseExistingMount) {
        $buildArguments.UseExistingMount = $true
    }
    & (Join-Path $BuildRoot "Rebuild-IronDeployWim.ps1") @buildArguments
}
elseif ($Target -eq "Iso") {
    if ($UseExistingMount) {
        throw "-UseExistingMount is only supported with -Target Wim."
    }
    if ($RecoverInvalidMount -and $SkipWimUpdate) {
        throw "-RecoverInvalidMount cannot be combined with -SkipWimUpdate."
    }
    # By default -Target Iso updates boot.wim before packaging it, so the ISO
    # always contains the current runtime. -SkipWimUpdate opts out of that for
    # a fast repackage right after a -Target Wim build.
    if ($SkipWimUpdate) {
        $buildArguments.SkipWimUpdate = $true
    }
    & (Join-Path $BuildRoot "Rebuild-IronDeployIso.ps1") @buildArguments
}
else {
    if ($UseExistingMount) {
        throw "-UseExistingMount is only supported with -Target Wim."
    }
    & (Join-Path $BuildRoot "Rebuild-IronDeployIso.ps1") @buildArguments
}

$buildExitCode = $LASTEXITCODE
if ($buildExitCode -ne 0) {
    exit $buildExitCode
}

$DistRoot = Join-Path $IronDeployRoot "dist"
$PublishedWim = Join-Path $DistRoot "IronDeploy_PE.wim"
if ($UpdatePxeBundle) {
    $pxeWim = Join-Path $DistRoot "PXE\irondeploy\boot.wim"
    if (-not (Test-Path -LiteralPath (Split-Path $pxeWim -Parent))) {
        throw "PXE bundle is missing: $(Split-Path $pxeWim -Parent)"
    }
    Copy-Item -LiteralPath $PublishedWim -Destination $pxeWim -Force
    if (
        (Get-FileHash -LiteralPath $PublishedWim -Algorithm SHA256).Hash -ne
        (Get-FileHash -LiteralPath $pxeWim -Algorithm SHA256).Hash
    ) {
        throw "PXE boot.wim hash differs from the published WIM."
    }
    Write-Host "PXE bundle updated: $pxeWim" -ForegroundColor Green
}

$artifactPaths = @()
if ($Target -in @("Wim", "Both")) {
    $artifactPaths += $PublishedWim
}
if ($Target -in @("Iso", "Both")) {
    $artifactPaths += Join-Path $DistRoot "IronDeploy_PE.iso"
}
$hashLines = foreach ($artifactPath in $artifactPaths) {
    $hash = Get-FileHash -LiteralPath $artifactPath -Algorithm SHA256
    "{0}  {1}" -f $hash.Hash, (Split-Path $artifactPath -Leaf)
}
[IO.File]::WriteAllLines(
    (Join-Path $DistRoot "SHA256SUMS.txt"),
    $hashLines,
    [Text.UTF8Encoding]::new($false)
)

$gitCommit = $null
try {
    $gitCommit = (& git -C $IronDeployRoot rev-parse HEAD 2>$null).Trim()
}
catch {
    $gitCommit = $null
}
[ordered]@{
    built_at_utc = [DateTime]::UtcNow.ToString("o")
    target = $Target
    git_commit = $gitCommit
    artifacts = @($artifactPaths | ForEach-Object {
        Split-Path $_ -Leaf
    })
} |
    ConvertTo-Json -Depth 3 |
    Set-Content `
        -LiteralPath (Join-Path $DistRoot "build-info.json") `
        -Encoding UTF8

Write-Host "Build metadata written under $DistRoot" -ForegroundColor Green
exit 0
