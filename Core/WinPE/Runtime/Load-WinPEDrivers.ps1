# TEMPORARY WINPE DRIVER UPLOAD
# This stop-gap loader and the WinPEDrivers directory are intentionally separate
# from IronDeploy.Engine.ps1. Remove this file and its one startnet.cmd call when
# native WinPE driver management replaces the feature.

$driverRoot = "X:\IronDeploy\WinPEDrivers"

if (-not (Test-Path -LiteralPath $driverRoot -PathType Container)) {
    Write-Host "No uploaded WinPE boot drivers were found."
    exit 0
}

$driverInfs = @(
    Get-ChildItem -LiteralPath $driverRoot -Filter "*.inf" -File -Recurse |
        Sort-Object FullName
)

if ($driverInfs.Count -eq 0) {
    Write-Warning "The uploaded WinPE driver directory contains no INF files."
    exit 0
}

$loadedCount = 0
$failedCount = 0
foreach ($driverInf in $driverInfs) {
    Write-Host "Loading WinPE driver: $($driverInf.FullName)"
    & drvload.exe $driverInf.FullName
    if ($LASTEXITCODE -eq 0) {
        $loadedCount++
    }
    else {
        $failedCount++
        Write-Warning (
            "drvload failed with exit code $LASTEXITCODE for " +
            $driverInf.FullName
        )
    }
}

Write-Host "WinPE drivers loaded: $loadedCount; failed: $failedCount."

# A newly loaded network adapter may not have existed during the first wpeinit.
if ($loadedCount -gt 0) {
    Write-Host "Reinitializing WinPE networking after driver loading."
    & wpeutil.exe InitializeNetwork
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "WinPE network reinitialization returned $LASTEXITCODE."
    }
}

# Driver failures remain non-fatal so the existing WinPE UI can show diagnostics
# and adapters supported by inbox drivers can still deploy normally.
exit 0
