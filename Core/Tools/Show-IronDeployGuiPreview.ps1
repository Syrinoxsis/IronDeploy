#Requires -Version 5.1

<#
.SYNOPSIS
Renders the IronDeploy WinPE GUI on a normal Windows machine, with sample data.

.DESCRIPTION
A safe, cosmetic preview of IronDeploy.Gui.ps1. It loads only the XAML and
fills it with fake hardware/image data so the layout, colours, and screen
transitions can be reviewed without WinPE.

It never loads the deployment engine, never connects the SMB share, and never
touches any disk. The "Wipe & Deploy" button here only plays a scripted fake
progress animation; reboot actions simply close the preview window.

Run it in STA mode:

    powershell -STA -ExecutionPolicy Bypass -File .\Tools\Show-IronDeployGuiPreview.ps1
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

if ([System.Threading.Thread]::CurrentThread.GetApartmentState() -ne [System.Threading.ApartmentState]::STA) {
    Write-Host "WPF needs STA. Re-run with:" -ForegroundColor Yellow
    Write-Host "  powershell -STA -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -ForegroundColor Yellow
    return
}

$IronDeployRoot = Split-Path $PSScriptRoot -Parent
$GuiPath = Join-Path $IronDeployRoot "WinPE\Runtime\IronDeploy.Gui.ps1"
if (!(Test-Path -LiteralPath $GuiPath -PathType Leaf)) {
    throw "GUI script not found: $GuiPath"
}

# Dot-sourcing the GUI file only defines $IronDeployGuiXaml and its functions;
# it has no side effects (Add-Type lives inside Start-IronDeployGui).
. $GuiPath

Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase, System.Xaml

$reader = New-Object System.Xml.XmlNodeReader ([xml]$IronDeployGuiXaml)
$window = [System.Windows.Markup.XamlReader]::Load($reader)
Set-IronGuiWindowBounds -Window $window

$ui = @{}
foreach ($name in @(
    "LoginPanel", "SetupPanel", "ProgressPanel", "PreflightOverlay",
    "SerialText", "MacText", "NameBox", "SuggestedText", "LastDomainText",
    "KnownList", "KnownDeploymentsButton", "KnownDeploymentsPopup",
    "ImageCombo", "DiskCombo", "DomainCheck", "DeployButton", "SetupRebootButton",
    "ActivityText", "DeployProgress", "LogView",
    "ResultBar", "ResultTitle", "ResultMessage", "CountdownText",
    "RebootButton", "CancelRebootButton"
)) {
    $ui[$name] = $window.FindName($name)
}

# --- Sample data -------------------------------------------------------------
$ui.LoginPanel.Visibility = "Collapsed"
$ui.SetupPanel.Visibility = "Visible"
$ui.PreflightOverlay.Visibility = "Collapsed"
$ui.SerialText.Text = "5CD1234ABC"
$ui.MacText.Text = "00-1A-2B-3C-4D-5E"
$ui.SuggestedText.Text = "Suggested name: pc00042"
$ui.LastDomainText.Text = "Last name in domain: pc00041"
$ui.NameBox.Text = "pc00042"
$ui.KnownList.ItemsSource = @(
    "Previously deployed as pc00007 (serial_number, deployment #7)",
    "Previously deployed as pc00018 (mac_address, deployment #18)",
    "Previously deployed as pc00031 (serial_number, deployment #31)"
)
$ui.KnownDeploymentsButton.Content = "Deployments of this computer (3)  $([char]0x25BE)"
$ui.KnownDeploymentsButton.Visibility = "Visible"
$ui.ImageCombo.ItemsSource = @(
    [pscustomobject]@{ Name = "Win11_Pro.wim";  Display = "Win11_Pro.wim   (16.20 GB)" }
    [pscustomobject]@{ Name = "Win11_Ent.wim";  Display = "Win11_Ent.wim   (17.05 GB)" }
    [pscustomobject]@{ Name = "Win10_LTSC.wim"; Display = "Win10_LTSC.wim   (11.80 GB)" }
)
$ui.ImageCombo.SelectedIndex = 0
$ui.DiskCombo.ItemsSource = @(
    [pscustomobject]@{ Number = 0; Display = "#0 - NVMe Samsung PM9B1 - 476.94 GiB" }
    [pscustomobject]@{ Number = 1; Display = "#1 - SATA WDC WD10SPZX - 931.51 GiB" }
)
$ui.DiskCombo.SelectedIndex = 0
$ui.DomainCheck.IsChecked = $true
$ui.DeployButton.IsEnabled = $true
$ui.DeployButton.Content = "Wipe & Deploy (PREVIEW)"

$brushOk    = New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.Color]::FromRgb(0x3F, 0xB9, 0x50))
$brushStep  = New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.Color]::FromRgb(0x4C, 0x8B, 0xF5))
$brushInfo  = New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.Color]::FromRgb(0xC8, 0xC8, 0xD6))

$script:previewLog = @(
    @{ p = 4;   c = $brushStep; t = "[STEP] Register deployment start" }
    @{ p = 6;   c = $brushOk;   t = "[OK] Deployment #42 registered: status begin" }
    @{ p = 10;  c = $brushStep; t = "[STEP] Connect SMB share, attempt 1/3" }
    @{ p = 14;  c = $brushOk;   t = "[OK] Selected Windows image: Win11_Pro.wim (index 4)" }
    @{ p = 22;  c = $brushStep; t = "[STEP] DiskPart wipe and partition" }
    @{ p = 30;  c = $brushStep; t = "[STEP] Apply Windows image" }
    @{ p = 62;  c = $brushStep; t = "[STEP] Add drivers to offline Windows" }
    @{ p = 80;  c = $brushStep; t = "[STEP] Apply unattend.xml" }
    @{ p = 95;  c = $brushStep; t = "[STEP] Create UEFI boot files" }
    @{ p = 100; c = $brushOk;   t = "IronDeploy finished successfully." }
)
$script:previewIndex = 0

$script:previewTimer = New-Object System.Windows.Threading.DispatcherTimer
$script:previewTimer.Interval = [TimeSpan]::FromMilliseconds(500)
$script:previewTimer.Add_Tick({
    if ($script:previewIndex -ge $script:previewLog.Count) {
        $script:previewTimer.Stop()
        $ui.ResultBar.Visibility = "Visible"
        $ui.ResultBar.Background = New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.Color]::FromRgb(0x14, 0x3A, 0x1E))
        $ui.ResultTitle.Text = "Deployment complete (PREVIEW)"
        $ui.ResultTitle.Foreground = $brushOk
        $ui.ResultMessage.Text = "This is a cosmetic preview. Nothing was changed on this machine."
        $ui.CountdownText.Text = ""
        $ui.RebootButton.Visibility = "Visible"
        $ui.RebootButton.Content = "Close"
        return
    }
    $entry = $script:previewLog[$script:previewIndex]
    $script:previewIndex += 1
    $ui.DeployProgress.Value = [double]$entry.p
    $ui.ActivityText.Text = $entry.t
    $paragraph = New-Object System.Windows.Documents.Paragraph
    $paragraph.Margin = New-Object System.Windows.Thickness(0)
    $run = New-Object System.Windows.Documents.Run($entry.t)
    $run.Foreground = $entry.c
    $paragraph.Inlines.Add($run)
    $ui.LogView.Document.Blocks.Add($paragraph)
    $ui.LogView.ScrollToEnd()
})

$ui.DeployButton.Add_Click({
    $ui.SetupPanel.Visibility = "Collapsed"
    $ui.ProgressPanel.Visibility = "Visible"
    $script:previewTimer.Start()
})
$ui.KnownDeploymentsButton.Add_Click({
    $ui.KnownDeploymentsPopup.PlacementTarget = $ui.KnownDeploymentsButton
    $ui.KnownDeploymentsPopup.IsOpen = $true
})
$ui.SetupRebootButton.Add_Click({ $window.Close() })
$ui.RebootButton.Add_Click({ $window.Close() })
$ui.CancelRebootButton.Add_Click({ $window.Close() })

[void]$window.ShowDialog()
