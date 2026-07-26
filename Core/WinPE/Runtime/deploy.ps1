# IronDeploy v0.6 - WinPE deployment launcher.
#
# Loads the deployment engine and its WPF GUI. The engine performs the
# destructive work and never reboots; this launcher reboots when the GUI asks
# it to. If WPF cannot start, the script leaves a fatal error in the normal
# WinPE command prompt and never starts an alternative deployment workflow.

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

if (
    $env:SystemDrive -ine "X:" -or
    !(Test-Path -LiteralPath "HKLM:\SYSTEM\CurrentControlSet\Control\MiniNT")
) {
    throw "IronDeploy deploy.ps1 may only run inside Windows PE."
}

try {
    # The GUI requires STA and startnet.cmd explicitly launches it that way.
    $Apartment = [System.Threading.Thread]::CurrentThread.GetApartmentState()
    if ($Apartment -ne [System.Threading.ApartmentState]::STA) {
        throw "PowerShell is not running in STA mode; the IronDeploy GUI requires STA."
    }

    # The engine validates deploy.config.ps1 when loaded. The GUI restores the
    # console before rethrowing any fatal WPF error.
    . (Join-Path $PSScriptRoot "IronDeploy.Engine.ps1")
    . (Join-Path $PSScriptRoot "IronDeploy.Gui.ps1")
    $Result = Start-IronDeployGui
} catch {
    if (Get-Command Set-IronDeployConsoleVisible -ErrorAction SilentlyContinue) {
        [void](Set-IronDeployConsoleVisible -Visible $true)
    }
    Write-Host ""
    Write-Host "FATAL: IronDeploy GUI could not continue." -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host "No fallback deployment was started. The WinPE console is available." `
        -ForegroundColor Yellow
    exit 1
}

if ($Result -eq "reboot") {
    wpeutil reboot
    if ($LASTEXITCODE -ne 0) {
        [void](Set-IronDeployConsoleVisible -Visible $true)
        Write-Host "ERROR: Failed to reboot into Windows" -ForegroundColor Red
        exit 1
    }
    exit 0
}

[void](Set-IronDeployConsoleVisible -Visible $true)
Write-Host "FATAL: IronDeploy GUI returned unexpected result '$Result'." `
    -ForegroundColor Red
exit 1
