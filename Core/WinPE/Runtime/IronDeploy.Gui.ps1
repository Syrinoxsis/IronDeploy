# IronDeploy WPF front-end.
#
# A local, in-process WPF/XAML GUI over IronDeploy.Engine.ps1. No web server,
# no listener, no external runtime: it uses only the .NET Framework and
# PowerShell already present in WinPE (WinPE-NetFX + WinPE-PowerShell), so it
# adds no new attack surface and no new optional components.
#
# Start-IronDeployGui throws if WPF cannot load; deploy.ps1 then restores the
# normal WinPE console without starting another deployment workflow.
#
# The engine runs on a background runspace. It reports log lines and progress
# into a synchronized queue, which a DispatcherTimer drains on the UI thread.
#
# Shared state referenced by event handlers is deliberately script-scoped:
# WPF invokes handler scriptblocks from the dispatcher, where function-local
# variables are not reliably in scope.

$IronDeployGuiXaml = @'
<Window
    xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
    xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
    Title="IronDeploy"
    Height="700" Width="860"
    WindowStartupLocation="CenterScreen"
    ResizeMode="NoResize"
    Background="#FF14141F"
    FontFamily="Segoe UI" FontSize="13">
    <Window.Resources>
        <SolidColorBrush x:Key="PanelBrush" Color="#FF20202E"/>
        <SolidColorBrush x:Key="CardBrush" Color="#FF262636"/>
        <SolidColorBrush x:Key="AccentBrush" Color="#FF4C8BF5"/>
        <SolidColorBrush x:Key="DangerBrush" Color="#FFE5484D"/>
        <SolidColorBrush x:Key="SuccessBrush" Color="#FF3FB950"/>
        <SolidColorBrush x:Key="TextBrush" Color="#FFE6E6EC"/>
        <SolidColorBrush x:Key="MutedBrush" Color="#FF9A9AB0"/>

        <Style TargetType="TextBlock">
            <Setter Property="Foreground" Value="{StaticResource TextBrush}"/>
        </Style>
        <Style x:Key="Label" TargetType="TextBlock">
            <Setter Property="Foreground" Value="{StaticResource MutedBrush}"/>
            <Setter Property="FontSize" Value="11"/>
            <Setter Property="Margin" Value="0,0,0,2"/>
        </Style>
        <Style TargetType="Button">
            <Setter Property="Background" Value="{StaticResource CardBrush}"/>
            <Setter Property="Foreground" Value="{StaticResource TextBrush}"/>
            <Setter Property="BorderThickness" Value="0"/>
            <Setter Property="Padding" Value="18,10"/>
            <Setter Property="FontSize" Value="14"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="Button">
                        <Border x:Name="b" Background="{TemplateBinding Background}"
                                CornerRadius="6" Padding="{TemplateBinding Padding}">
                            <ContentPresenter HorizontalAlignment="Center"
                                              VerticalAlignment="Center"/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True">
                                <Setter TargetName="b" Property="Opacity" Value="0.88"/>
                            </Trigger>
                            <Trigger Property="IsEnabled" Value="False">
                                <Setter TargetName="b" Property="Opacity" Value="0.4"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>
        <Style TargetType="TextBox">
            <Setter Property="Background" Value="#FF1A1A28"/>
            <Setter Property="Foreground" Value="{StaticResource TextBrush}"/>
            <Setter Property="CaretBrush" Value="{StaticResource TextBrush}"/>
            <Setter Property="BorderBrush" Value="#FF3A3A50"/>
            <Setter Property="BorderThickness" Value="1"/>
            <Setter Property="Padding" Value="8,7"/>
            <Setter Property="FontSize" Value="15"/>
        </Style>
    </Window.Resources>

    <Grid>
        <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
        </Grid.RowDefinitions>

        <!-- Header -->
        <Border Grid.Row="0" Background="{StaticResource PanelBrush}" Padding="24,16">
            <Grid>
                <StackPanel>
                    <TextBlock Text="IronDeploy" FontSize="24" FontWeight="SemiBold"/>
                    <TextBlock x:Name="HeaderSubtitle"
                               Text="Windows deployment for this machine"
                               Style="{StaticResource Label}" Margin="0,2,0,0"/>
                </StackPanel>
                <StackPanel Orientation="Horizontal" HorizontalAlignment="Right"
                            VerticalAlignment="Center">
                    <Button x:Name="LanguageRuButton" Content="RU" Width="58"
                            Padding="8,7" Margin="0,0,8,0"/>
                    <Button x:Name="LanguageEnButton" Content="EN" Width="58"
                            Padding="8,7"/>
                </StackPanel>
            </Grid>
        </Border>

        <Grid Grid.Row="1">
            <!-- ================= LOGIN PANEL ================= -->
            <Grid x:Name="LoginPanel" Margin="24">
                <Border Width="460" Padding="28" CornerRadius="10"
                        Background="{StaticResource PanelBrush}"
                        HorizontalAlignment="Center" VerticalAlignment="Center">
                    <StackPanel>
                        <TextBlock x:Name="LoginTitle" Text="Authorize deployment" FontSize="22"
                                   FontWeight="SemiBold"/>
                        <TextBlock x:Name="LoginDescription"
                                   Text="Use an IronAPI account with WinPE deployment access. One login authorizes one deployment."
                                   Style="{StaticResource Label}" TextWrapping="Wrap"
                                   Margin="0,6,0,20"/>
                        <StackPanel x:Name="LoginAccountFields">
                            <TextBlock x:Name="LoginUsernameLabel" Text="USERNAME" Style="{StaticResource Label}"/>
                            <TextBox x:Name="LoginUsernameBox" MaxLength="64"/>
                            <TextBlock x:Name="LoginPasswordLabel" Text="PASSWORD" Style="{StaticResource Label}"
                                       Margin="0,14,0,0"/>
                            <PasswordBox x:Name="LoginPasswordBox"
                                         Background="#FF1A1A28"
                                         Foreground="{StaticResource TextBrush}"
                                         BorderBrush="#FF3A3A50" BorderThickness="1"
                                         Padding="8,7" FontSize="15"/>
                        </StackPanel>
                        <StackPanel x:Name="LoginPinFields" Visibility="Collapsed">
                            <TextBlock x:Name="LoginPinLabel" Text="PIN CODE" Style="{StaticResource Label}"/>
                            <PasswordBox x:Name="LoginPinBox" MaxLength="10"
                                         Background="#FF1A1A28"
                                         Foreground="{StaticResource TextBrush}"
                                         BorderBrush="#FF3A3A50" BorderThickness="1"
                                         Padding="8,7" FontSize="18"/>
                        </StackPanel>
                        <TextBlock x:Name="LoginError" TextWrapping="Wrap"
                                   Foreground="{StaticResource DangerBrush}"
                                   Margin="0,12,0,0" Visibility="Collapsed"/>
                        <Grid Margin="0,22,0,0">
                            <Grid.ColumnDefinitions>
                                <ColumnDefinition Width="Auto"/>
                                <ColumnDefinition Width="*"/>
                            </Grid.ColumnDefinitions>
                            <Button x:Name="LoginRebootButton" Grid.Column="0"
                                    Content="Reboot" Width="100"/>
                            <Button x:Name="LoginButton" Grid.Column="1"
                                    Content="Authorize" Width="150"
                                    HorizontalAlignment="Right"
                                    Background="{StaticResource AccentBrush}"/>
                        </Grid>
                    </StackPanel>
                </Border>
            </Grid>

            <!-- ================= SETUP PANEL ================= -->
            <Grid x:Name="SetupPanel" Margin="24" Visibility="Collapsed">
                <Grid.RowDefinitions>
                    <RowDefinition Height="Auto"/>
                    <RowDefinition Height="*"/>
                    <RowDefinition Height="Auto"/>
                </Grid.RowDefinitions>

                <!-- Wizard progress -->
                <Border Grid.Row="0" Background="{StaticResource PanelBrush}"
                        CornerRadius="8" Padding="16,11" Margin="0,0,0,14">
                    <Grid>
                        <Grid.ColumnDefinitions>
                            <ColumnDefinition Width="Auto"/>
                            <ColumnDefinition Width="34"/>
                            <ColumnDefinition Width="Auto"/>
                            <ColumnDefinition Width="34"/>
                            <ColumnDefinition Width="Auto"/>
                        </Grid.ColumnDefinitions>
                        <TextBlock x:Name="StepOneIndicator" Grid.Column="0"
                                   Text="1  Computer" FontSize="14"
                                   FontWeight="SemiBold"
                                   Foreground="{StaticResource AccentBrush}"/>
                        <TextBlock Grid.Column="1" Text="&#8594;"
                                   HorizontalAlignment="Center"
                                   Foreground="{StaticResource MutedBrush}"/>
                        <TextBlock x:Name="StepTwoIndicator" Grid.Column="2"
                                   Text="2  Software" FontSize="14"
                                   FontWeight="SemiBold"
                                   Foreground="{StaticResource MutedBrush}"/>
                        <TextBlock Grid.Column="3" Text="&#8594;"
                                   HorizontalAlignment="Center"
                                   Foreground="{StaticResource MutedBrush}"/>
                        <TextBlock x:Name="StepThreeIndicator" Grid.Column="4"
                                   Text="3  Drivers &amp; confirmation" FontSize="14"
                                   FontWeight="SemiBold"
                                   Foreground="{StaticResource MutedBrush}"/>
                    </Grid>
                </Border>

                <!-- Step 1: computer, image, and domain -->
                <Grid x:Name="IdentityPage" Grid.Row="1">
                    <Grid.RowDefinitions>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="*"/>
                    </Grid.RowDefinitions>

                    <Border Grid.Row="0" Background="{StaticResource CardBrush}"
                            CornerRadius="8" Padding="16,12" Margin="0,0,0,14">
                        <Grid>
                            <Grid.ColumnDefinitions>
                                <ColumnDefinition Width="*"/>
                                <ColumnDefinition Width="*"/>
                            </Grid.ColumnDefinitions>
                            <StackPanel Grid.Column="0">
                                <TextBlock x:Name="SerialLabel" Text="SERIAL NUMBER" Style="{StaticResource Label}"/>
                                <TextBlock x:Name="SerialText" Text="&#8212;" FontSize="15"/>
                            </StackPanel>
                            <StackPanel Grid.Column="1">
                                <TextBlock x:Name="MacLabel" Text="PRIMARY MAC" Style="{StaticResource Label}"/>
                                <TextBlock x:Name="MacText" Text="&#8212;" FontSize="15"/>
                            </StackPanel>
                        </Grid>
                    </Border>

                    <StackPanel Grid.Row="1">
                        <TextBlock x:Name="ComputerNameLabel" Text="COMPUTER NAME" Style="{StaticResource Label}"/>
                        <TextBox x:Name="NameBox" MaxLength="7"/>
                        <TextBlock x:Name="NameHint" Style="{StaticResource Label}"
                                   Margin="0,4,0,0"
                                   Text="Format: pc + 5 digits (e.g. pc00001)"/>

                        <TextBlock x:Name="SuggestedText" Margin="0,6,0,0"
                                   Foreground="{StaticResource AccentBrush}"/>
                        <TextBlock x:Name="LastDomainText"
                                   Foreground="{StaticResource MutedBrush}" FontSize="12"/>
                        <Button x:Name="KnownDeploymentsButton"
                                Content="Deployments of this computer"
                                HorizontalAlignment="Left" Margin="0,8,0,0"
                                Padding="12,7" Visibility="Collapsed"/>
                        <Popup x:Name="KnownDeploymentsPopup"
                               Placement="Bottom" StaysOpen="False"
                               AllowsTransparency="True" PopupAnimation="Fade">
                            <Border Width="560" MaxHeight="250" Padding="14"
                                    Background="{StaticResource CardBrush}"
                                    BorderBrush="#FF3A3A50" BorderThickness="1"
                                    CornerRadius="6">
                                <StackPanel>
                                    <TextBlock x:Name="KnownDeploymentsTitle" Text="DEPLOYMENTS OF THIS COMPUTER"
                                               Style="{StaticResource Label}"
                                               Margin="0,0,0,8"/>
                                    <ScrollViewer MaxHeight="190"
                                                  VerticalScrollBarVisibility="Auto">
                                        <ItemsControl x:Name="KnownList">
                                            <ItemsControl.ItemTemplate>
                                                <DataTemplate>
                                                    <TextBlock Text="{Binding}"
                                                        Foreground="{StaticResource TextBrush}"
                                                        FontSize="12" Margin="0,3"
                                                        TextWrapping="Wrap"/>
                                                </DataTemplate>
                                            </ItemsControl.ItemTemplate>
                                        </ItemsControl>
                                    </ScrollViewer>
                                </StackPanel>
                            </Border>
                        </Popup>

                        <TextBlock x:Name="WindowsImageLabel" Text="WINDOWS IMAGE" Style="{StaticResource Label}"
                                   Margin="0,14,0,0"/>
                        <ComboBox x:Name="ImageCombo" DisplayMemberPath="Display"
                                  Height="34" FontSize="14" Padding="8,4"/>

                        <CheckBox x:Name="DomainCheck" Margin="0,16,0,0"
                                  Foreground="{StaticResource TextBrush}"
                                  Content="Join Active Directory domain (Offline Domain Join)"/>
                    </StackPanel>
                </Grid>

                <!-- Step 2: optional post-install software -->
                <Grid x:Name="ProgramsPage" Grid.Row="1" Visibility="Collapsed">
                    <Grid.RowDefinitions>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="*"/>
                    </Grid.RowDefinitions>

                    <TextBlock x:Name="ProgramsLabel" Grid.Row="0"
                               Text="POST-INSTALL SOFTWARE"
                               Style="{StaticResource Label}" Margin="0,0,0,4"/>
                    <Border x:Name="ProgramsBorder" Grid.Row="1"
                            Background="{StaticResource CardBrush}"
                            CornerRadius="6" Padding="12,8">
                        <Grid>
                            <TextBlock x:Name="NoProgramsText"
                                       Text="No post-install software is available."
                                       Foreground="{StaticResource MutedBrush}"
                                       VerticalAlignment="Center"
                                       HorizontalAlignment="Center"/>
                            <ScrollViewer VerticalScrollBarVisibility="Auto"
                                          HorizontalScrollBarVisibility="Disabled">
                                <StackPanel x:Name="ProgramsPanel"/>
                            </ScrollViewer>
                        </Grid>
                    </Border>
                </Grid>

                <!-- Step 3: one driver package and destructive-action confirmation -->
                <Grid x:Name="DriversPage" Grid.Row="1" Visibility="Collapsed">
                    <Grid.RowDefinitions>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="*"/>
                        <RowDefinition Height="Auto"/>
                    </Grid.RowDefinitions>

                    <Border Grid.Row="0" Background="{StaticResource CardBrush}"
                            CornerRadius="8" Padding="16,12" Margin="0,0,0,14">
                        <Grid>
                            <Grid.ColumnDefinitions>
                                <ColumnDefinition Width="1.4*"/>
                                <ColumnDefinition Width="*"/>
                                <ColumnDefinition Width="*"/>
                                <ColumnDefinition Width="*"/>
                            </Grid.ColumnDefinitions>
                            <StackPanel Grid.Column="0">
                                <TextBlock x:Name="ConfirmModelLabel" Text="MODEL" Style="{StaticResource Label}"/>
                                <TextBlock x:Name="ConfirmModelText" Text="&#8212;"
                                           FontSize="14" TextTrimming="CharacterEllipsis"/>
                            </StackPanel>
                            <StackPanel Grid.Column="1" Margin="12,0,0,0">
                                <TextBlock x:Name="ConfirmComputerLabel" Text="COMPUTER" Style="{StaticResource Label}"/>
                                <TextBlock x:Name="ConfirmComputerText" Text="&#8212;"
                                           FontSize="14" TextTrimming="CharacterEllipsis"/>
                            </StackPanel>
                            <StackPanel Grid.Column="2" Margin="12,0">
                                <TextBlock x:Name="ConfirmImageLabel" Text="WINDOWS IMAGE" Style="{StaticResource Label}"/>
                                <TextBlock x:Name="ConfirmImageText" Text="&#8212;"
                                           FontSize="14" TextTrimming="CharacterEllipsis"/>
                            </StackPanel>
                            <StackPanel Grid.Column="3">
                                <TextBlock x:Name="ConfirmDomainLabel" Text="DOMAIN JOIN" Style="{StaticResource Label}"/>
                                <TextBlock x:Name="ConfirmDomainText" Text="Disabled"
                                           FontSize="14"/>
                            </StackPanel>
                        </Grid>
                    </Border>

                    <TextBlock x:Name="DriversLabel" Grid.Row="1"
                               Text="DRIVER PACKAGE"
                               Style="{StaticResource Label}" Margin="0,0,0,4"/>
                    <TextBlock x:Name="DriversHint" Grid.Row="2"
                               Text="Select one package, or continue without drivers."
                               Foreground="{StaticResource MutedBrush}"
                               Margin="0,0,0,8"/>
                    <Border Grid.Row="3" Background="{StaticResource CardBrush}"
                            CornerRadius="6" Padding="12,8">
                        <ScrollViewer VerticalScrollBarVisibility="Auto"
                                      HorizontalScrollBarVisibility="Disabled">
                            <StackPanel>
                                <CheckBox x:Name="NoDriversCheck"
                                          Foreground="{StaticResource TextBrush}"
                                          Margin="0,3,0,8"/>
                                <StackPanel x:Name="DriverPackagesPanel"/>
                            </StackPanel>
                        </ScrollViewer>
                    </Border>

                    <Border Grid.Row="4" Background="#33E5484D" CornerRadius="6"
                            Padding="12,10" Margin="0,14,0,0"
                            BorderBrush="{StaticResource DangerBrush}" BorderThickness="1">
                        <CheckBox x:Name="WipeCheck"
                                  Foreground="{StaticResource TextBrush}">
                            <TextBlock x:Name="WipeWarningText" TextWrapping="Wrap"
                                Text="I understand this will PERMANENTLY ERASE disk 0 on this machine."/>
                        </CheckBox>
                    </Border>
                </Grid>

                <!-- Fixed wizard actions -->
                <Grid Grid.Row="2" Margin="0,18,0,0">
                    <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="*"/>
                        <ColumnDefinition Width="Auto"/>
                    </Grid.ColumnDefinitions>
                    <Button x:Name="SetupRebootButton" Grid.Column="0"
                            Content="Reboot" Width="120"
                            HorizontalAlignment="Left"/>
                    <StackPanel Grid.Column="1" Orientation="Horizontal">
                        <Button x:Name="BackButton" Content="Back" Width="120"
                                Margin="0,0,12,0" Visibility="Collapsed"/>
                        <Button x:Name="NextButton" Content="Next" Width="160"
                                IsEnabled="False"
                                Background="{StaticResource AccentBrush}"/>
                        <Button x:Name="DeployButton" Content="Wipe &amp; Deploy"
                                Width="200" IsEnabled="False"
                                Visibility="Collapsed"
                                Background="{StaticResource DangerBrush}"/>
                    </StackPanel>
                </Grid>
            </Grid>

            <!-- ================= PROGRESS PANEL ================= -->
            <Grid x:Name="ProgressPanel" Margin="24" Visibility="Collapsed">
                <Grid.RowDefinitions>
                    <RowDefinition Height="Auto"/>
                    <RowDefinition Height="Auto"/>
                    <RowDefinition Height="*"/>
                    <RowDefinition Height="Auto"/>
                </Grid.RowDefinitions>

                <TextBlock x:Name="ActivityText" Grid.Row="0" FontSize="16"
                           Text="Preparing&#8230;" Margin="0,0,0,10"/>
                <ProgressBar x:Name="DeployProgress" Grid.Row="1" Height="10"
                             Minimum="0" Maximum="100" Value="0"
                             Foreground="{StaticResource AccentBrush}"
                             Background="#FF1A1A28" BorderThickness="0"/>

                <Border Grid.Row="2" Background="#FF0E0E16" CornerRadius="8"
                        Margin="0,14,0,0" Padding="4">
                    <RichTextBox x:Name="LogView" IsReadOnly="True"
                                 Background="Transparent" BorderThickness="0"
                                 Foreground="{StaticResource TextBrush}"
                                 FontFamily="Consolas" FontSize="12"
                                 VerticalScrollBarVisibility="Auto">
                        <RichTextBox.Resources>
                            <Style TargetType="Paragraph">
                                <Setter Property="Margin" Value="0"/>
                            </Style>
                        </RichTextBox.Resources>
                        <FlowDocument/>
                    </RichTextBox>
                </Border>

                <!-- Result bar (success / failure) -->
                <Border x:Name="ResultBar" Grid.Row="3" CornerRadius="8"
                        Padding="16,14" Margin="0,14,0,0" Visibility="Collapsed"
                        Background="{StaticResource CardBrush}">
                    <Grid>
                        <Grid.ColumnDefinitions>
                            <ColumnDefinition Width="*"/>
                            <ColumnDefinition Width="Auto"/>
                        </Grid.ColumnDefinitions>
                        <StackPanel Grid.Column="0" VerticalAlignment="Center">
                            <TextBlock x:Name="ResultTitle" FontSize="16"
                                       FontWeight="SemiBold"/>
                            <TextBlock x:Name="ResultMessage" TextWrapping="Wrap"
                                       Foreground="{StaticResource MutedBrush}"
                                       Margin="0,2,0,0"/>
                            <TextBlock x:Name="CountdownText"
                                       Foreground="{StaticResource MutedBrush}"
                                       Margin="0,2,0,0"/>
                        </StackPanel>
                        <StackPanel Grid.Column="1" Orientation="Horizontal"
                                    VerticalAlignment="Center">
                            <Button x:Name="CancelRebootButton" Content="Cancel"
                                    Width="120" Margin="0,0,12,0"
                                    Visibility="Collapsed"/>
                            <Button x:Name="RebootButton" Content="Reboot now"
                                    Width="160" Visibility="Collapsed"
                                    Background="{StaticResource SuccessBrush}"/>
                        </StackPanel>
                    </Grid>
                </Border>
            </Grid>

            <!-- ================= PREFLIGHT OVERLAY ================= -->
            <Grid x:Name="PreflightOverlay" Background="#EE14141F"
                  Visibility="Collapsed">
                <StackPanel HorizontalAlignment="Center" VerticalAlignment="Center"
                            Width="520">
                    <TextBlock x:Name="PreflightStatus" FontSize="16"
                               HorizontalAlignment="Center"
                               Text="Detecting hardware and connecting to the share&#8230;"/>
                    <ProgressBar IsIndeterminate="True" Height="6" Margin="0,16,0,0"
                                 Foreground="{StaticResource AccentBrush}"
                                 Background="#FF1A1A28" BorderThickness="0"/>
                    <TextBlock x:Name="PreflightError" TextWrapping="Wrap"
                               Foreground="{StaticResource DangerBrush}"
                               Margin="0,16,0,0" TextAlignment="Center"
                               Visibility="Collapsed"/>
                    <StackPanel Orientation="Horizontal" HorizontalAlignment="Center"
                                Margin="0,16,0,0">
                        <Button x:Name="PreflightRetry" Content="Retry" Width="140"
                                Margin="0,0,12,0" Visibility="Collapsed"
                                Background="{StaticResource AccentBrush}"/>
                        <Button x:Name="PreflightRebootButton" Content="Reboot" Width="140"
                                Visibility="Collapsed"/>
                    </StackPanel>
                </StackPanel>
            </Grid>
        </Grid>
    </Grid>
</Window>
'@

# Runs a script body on a fresh background runspace with the supplied variables
# pre-set. Returns the PowerShell/handle/runspace triple for later disposal.
function Start-IronGuiRunspace {
    param(
        [Parameter(Mandatory = $true)]
        [hashtable]$Variables,

        [Parameter(Mandatory = $true)]
        [string]$ScriptBody
    )

    $runspace = [runspacefactory]::CreateRunspace()
    $runspace.ApartmentState = "MTA"
    $runspace.ThreadOptions = "ReuseThread"
    $runspace.Open()

    foreach ($key in $Variables.Keys) {
        $runspace.SessionStateProxy.SetVariable($key, $Variables[$key])
    }

    $powershell = [powershell]::Create()
    $powershell.Runspace = $runspace
    [void]$powershell.AddScript($ScriptBody)
    $handle = $powershell.BeginInvoke()

    return [pscustomobject]@{
        PowerShell = $powershell
        Handle = $handle
        Runspace = $runspace
    }
}

function Complete-IronGuiRunspace {
    param($Job)

    if ($null -eq $Job) { return }
    try { $Job.PowerShell.EndInvoke($Job.Handle) } catch {}
    try { $Job.PowerShell.Dispose() } catch {}
    try { $Job.Runspace.Dispose() } catch {}
}

# Hides the shared WinPE console only after WPF is fully initialized. The
# console is restored on fatal GUI errors so diagnostics remain available.
function Set-IronDeployConsoleVisible {
    param(
        [Parameter(Mandatory = $true)]
        [bool]$Visible
    )

    try {
        if (-not ("IronDeploy.NativeConsole" -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace IronDeploy {
    public static class NativeConsole {
        [DllImport("kernel32.dll")]
        public static extern IntPtr GetConsoleWindow();

        [DllImport("user32.dll")]
        public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    }
}
'@ | Out-Null
        }

        $ConsoleWindow = [IronDeploy.NativeConsole]::GetConsoleWindow()
        if ($ConsoleWindow -eq [IntPtr]::Zero) {
            return $false
        }

        $ShowCommand = if ($Visible) { 5 } else { 0 } # SW_SHOW / SW_HIDE
        [void][IronDeploy.NativeConsole]::ShowWindow(
            $ConsoleWindow,
            $ShowCommand
        )
        return $true
    } catch {
        # Console visibility is a UX enhancement, never a deployment blocker.
        return $false
    }
}

# Shows the GUI and drives a deployment. Returns "reboot".
# Throws if WPF cannot load or the UI encounters a fatal error.
function Start-IronDeployGui {
    Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase, System.Xaml

    $script:IronGuiEnginePath = Join-Path $PSScriptRoot "IronDeploy.Engine.ps1"
    if (!(Test-Path -LiteralPath $script:IronGuiEnginePath -PathType Leaf)) {
        throw "IronDeploy engine not found: $script:IronGuiEnginePath"
    }

    $reader = New-Object System.Xml.XmlNodeReader ([xml]$IronDeployGuiXaml)
    $script:IronGuiWindow = [System.Windows.Markup.XamlReader]::Load($reader)
    $script:IronGuiAuthPolicy = Get-IronDeploymentAuthorizationPolicy

    $script:IronGuiUi = @{}
    foreach ($name in @(
        "HeaderSubtitle", "LanguageRuButton", "LanguageEnButton",
        "LoginTitle", "LoginDescription", "LoginUsernameLabel", "LoginPasswordLabel",
        "LoginPinLabel", "LoginAccountFields", "LoginPinFields", "LoginPinBox",
        "LoginPanel", "LoginUsernameBox", "LoginPasswordBox", "LoginButton",
        "LoginError", "LoginRebootButton",
        "SetupPanel", "ProgressPanel", "PreflightOverlay",
        "IdentityPage", "ProgramsPage", "DriversPage",
        "StepOneIndicator", "StepTwoIndicator", "StepThreeIndicator",
        "SerialLabel", "MacLabel", "ComputerNameLabel", "WindowsImageLabel",
        "SerialText", "MacText", "NameBox", "NameHint",
        "SuggestedText", "LastDomainText", "KnownList",
        "KnownDeploymentsButton", "KnownDeploymentsPopup", "KnownDeploymentsTitle",
        "ImageCombo", "DomainCheck", "WipeCheck",
        "ConfirmComputerLabel", "ConfirmImageLabel", "ConfirmDomainLabel", "WipeWarningText",
        "ConfirmModelLabel", "ConfirmModelText",
        "ConfirmComputerText", "ConfirmImageText", "ConfirmDomainText",
        "ProgramsLabel", "ProgramsBorder", "ProgramsPanel", "NoProgramsText",
        "DriversLabel", "DriversHint", "NoDriversCheck", "DriverPackagesPanel",
        "BackButton", "NextButton", "DeployButton", "SetupRebootButton",
        "ActivityText", "DeployProgress", "LogView",
        "ResultBar", "ResultTitle", "ResultMessage", "CountdownText",
        "RebootButton", "CancelRebootButton",
        "PreflightStatus", "PreflightError", "PreflightRetry", "PreflightRebootButton"
    )) {
        $script:IronGuiUi[$name] = $script:IronGuiWindow.FindName($name)
    }

    # Shared state ------------------------------------------------------------
    $script:IronGuiState = [hashtable]::Synchronized(@{})
    $script:IronGuiState.Result = "reboot"
    $script:IronGuiState.Deploying = $false
    $script:IronGuiState.Finished = $false
    $script:IronGuiState.AllowClose = $false
    $script:IronGuiState.DeploymentAccessToken = ""
    $script:IronGuiState.AuthMode = [string]$script:IronGuiAuthPolicy.Mode
    $script:IronGuiState.AutoRebootCancelled = $false
    $script:IronGuiState.LoginErrorKey = $null
    $script:IronGuiState.CurrentActivity = ""
    $script:IronGuiPreflightJob = $null
    $script:IronGuiDeployJob = $null
    $script:IronGuiLoginJob = $null
    $script:IronGuiCountdown = 15
    $script:IronGuiProgramChecks = @()
    $script:IronGuiDriverChecks = @()
    $script:IronGuiChangingDriverSelection = $false

    $script:IronGuiLanguage = if (
        [Globalization.CultureInfo]::CurrentUICulture.TwoLetterISOLanguageName -eq "ru"
    ) {
        "ru"
    } else {
        "en"
    }

    # UI strings stay in the GUI layer. API values, image/program/driver names, and
    # raw server errors are deliberately not translated.
    $script:IronGuiTranslations = @{
        en = @{
            HeaderSubtitle = "Windows deployment for this machine"
            LoginTitle = "Authorize deployment"
            LoginDescription = "Use an IronAPI account with WinPE deployment access. One login authorizes one deployment."
            LoginPinDescription = "Enter the shared deployment PIN. One successful authorization permits one deployment."
            LoginNoneDescription = "No operator credentials are required. Requesting a short-lived deployment token..."
            LoginUsernameLabel = "USERNAME"
            LoginPasswordLabel = "PASSWORD"
            LoginPinLabel = "PIN CODE"
            LoginRebootButton = "Reboot"
            LoginButton = "Authorize"
            LoginRequired = "Enter a deployment username and password."
            LoginPinRequired = "Enter a 6-10 digit deployment PIN."
            LoginAuthorizing = "Authorizing..."
            StepOneIndicator = "1  Computer"
            StepTwoIndicator = "2  Software"
            StepThreeIndicator = "3  Drivers & confirmation"
            SerialLabel = "SERIAL NUMBER"
            MacLabel = "PRIMARY MAC"
            ComputerNameLabel = "COMPUTER NAME"
            NameFormat = "Format: pc + 5 digits (e.g. pc00001)"
            NameInvalid = "Invalid name. Expected format: pc00001"
            SuggestedName = "Suggested name: {0}"
            LastDomainName = "Last name in domain: {0}"
            KnownDeploymentsButton = "Deployments of this computer ({0})  {1}"
            KnownDeploymentsTitle = "DEPLOYMENTS OF THIS COMPUTER"
            KnownDeployment = "Previously deployed as {0} ({1}, deployment #{2})"
            WindowsImageLabel = "WINDOWS IMAGE"
            DomainCheck = "Join Active Directory domain (Offline Domain Join)"
            ConfirmModelLabel = "MODEL"
            ConfirmComputerLabel = "COMPUTER"
            ConfirmImageLabel = "WINDOWS IMAGE"
            ConfirmDomainLabel = "DOMAIN JOIN"
            Enabled = "Enabled"
            Disabled = "Disabled"
            ProgramsLabel = "POST-INSTALL SOFTWARE"
            ProgramsCount = "POST-INSTALL SOFTWARE ({0})"
            NoProgramsText = "No post-install software is available."
            DriversLabel = "DRIVER PACKAGE"
            DriversHint = "Select one package, or continue without drivers."
            NoDriversOption = "Do not install drivers"
            WipeWarningText = "I understand this will PERMANENTLY ERASE disk 0 on this machine."
            SetupRebootButton = "Reboot"
            BackButton = "Back"
            NextButton = "Next"
            DeployButton = "Wipe & Deploy"
            ActivityText = "Preparing..."
            CancelRebootButton = "Cancel"
            RebootButton = "Reboot now"
            Reboot = "Reboot"
            PreflightLoading = "Detecting hardware and loading deployment options..."
            PreflightFailed = "Preparation failed"
            PreflightRetry = "Retry"
            PreflightRebootButton = "Reboot"
            RebootCountdown = "Rebooting into Windows in {0} seconds..."
            RebootCancelled = "Automatic reboot cancelled."
            DeploymentComplete = "Deployment complete"
            DeploymentCompleteMessage = "Windows was applied successfully. The machine will reboot to finish setup."
            DeploymentFailed = "Deployment failed"
        }
        ru = @{
            HeaderSubtitle = "Установка Windows на этот компьютер"
            LoginTitle = "Авторизация развёртывания"
            LoginDescription = "Используйте учётную запись IronAPI с доступом к развёртыванию WinPE. Один вход разрешает одно развёртывание."
            LoginPinDescription = "Введите общий PIN-код развёртывания. Успешная авторизация разрешает одну установку."
            LoginNoneDescription = "Учётные данные оператора не требуются. Запрашивается временный токен развёртывания..."
            LoginUsernameLabel = "ИМЯ ПОЛЬЗОВАТЕЛЯ"
            LoginPasswordLabel = "ПАРОЛЬ"
            LoginPinLabel = "PIN-КОД"
            LoginRebootButton = "Перезагрузить"
            LoginButton = "Войти"
            LoginRequired = "Введите имя пользователя и пароль для развёртывания."
            LoginPinRequired = "Введите PIN-код развёртывания из 6–10 цифр."
            LoginAuthorizing = "Авторизация..."
            StepOneIndicator = "1  Компьютер"
            StepTwoIndicator = "2  Программы"
            StepThreeIndicator = "3  Драйверы и подтверждение"
            SerialLabel = "СЕРИЙНЫЙ НОМЕР"
            MacLabel = "ОСНОВНОЙ MAC-АДРЕС"
            ComputerNameLabel = "ИМЯ КОМПЬЮТЕРА"
            NameFormat = "Формат: pc + 5 цифр (например, pc00001)"
            NameInvalid = "Недопустимое имя. Ожидаемый формат: pc00001"
            SuggestedName = "Предлагаемое имя: {0}"
            LastDomainName = "Последнее имя в домене: {0}"
            KnownDeploymentsButton = "Развёртывания этого компьютера ({0})  {1}"
            KnownDeploymentsTitle = "РАЗВЁРТЫВАНИЯ ЭТОГО КОМПЬЮТЕРА"
            KnownDeployment = "Ранее развёрнут как {0} ({1}, развёртывание №{2})"
            WindowsImageLabel = "ОБРАЗ WINDOWS"
            DomainCheck = "Присоединить к домену Active Directory (Offline Domain Join)"
            ConfirmModelLabel = "МОДЕЛЬ"
            ConfirmComputerLabel = "КОМПЬЮТЕР"
            ConfirmImageLabel = "ОБРАЗ WINDOWS"
            ConfirmDomainLabel = "ПРИСОЕДИНЕНИЕ К ДОМЕНУ"
            Enabled = "Включено"
            Disabled = "Отключено"
            ProgramsLabel = "ПРОГРАММЫ ПОСЛЕ УСТАНОВКИ"
            ProgramsCount = "ПРОГРАММЫ ПОСЛЕ УСТАНОВКИ ({0})"
            NoProgramsText = "Нет доступных программ для установки."
            DriversLabel = "ПАКЕТ ДРАЙВЕРОВ"
            DriversHint = "Выберите один пакет либо продолжите без установки драйверов."
            NoDriversOption = "Не устанавливать драйверы"
            WipeWarningText = "Я понимаю, что это БЕЗВОЗВРАТНО УДАЛИТ все данные с диска 0 этого компьютера."
            SetupRebootButton = "Перезагрузить"
            BackButton = "Назад"
            NextButton = "Далее"
            DeployButton = "Стереть и установить"
            ActivityText = "Подготовка..."
            CancelRebootButton = "Отмена"
            RebootButton = "Перезагрузить сейчас"
            Reboot = "Перезагрузить"
            PreflightLoading = "Определение оборудования и загрузка параметров развёртывания..."
            PreflightFailed = "Не удалось выполнить подготовку"
            PreflightRetry = "Повторить"
            PreflightRebootButton = "Перезагрузить"
            RebootCountdown = "Перезагрузка в Windows через {0} сек."
            RebootCancelled = "Автоматическая перезагрузка отменена."
            DeploymentComplete = "Развёртывание завершено"
            DeploymentCompleteMessage = "Windows успешно применена. Компьютер будет перезагружен для завершения установки."
            DeploymentFailed = "Ошибка развёртывания"
        }
    }

    $script:IronGuiGetText = {
        param([string]$Key, [object[]]$Arguments)

        $value = [string]$script:IronGuiTranslations[$script:IronGuiLanguage][$Key]
        if ([string]::IsNullOrEmpty($value)) {
            $value = [string]$script:IronGuiTranslations.en[$Key]
        }
        if ($null -ne $Arguments -and $Arguments.Count -gt 0) {
            return ($value -f $Arguments)
        }
        return $value
    }

    $script:IronGuiBrushes = @{
        info  = New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.Color]::FromRgb(0xC8, 0xC8, 0xD6))
        ok    = New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.Color]::FromRgb(0x3F, 0xB9, 0x50))
        warn  = New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.Color]::FromRgb(0xE3, 0xB3, 0x41))
        error = New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.Color]::FromRgb(0xE5, 0x48, 0x4D))
        step  = New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.Color]::FromRgb(0x4C, 0x8B, 0xF5))
    }

    $script:IronGuiTranslateActivity = {
        param([string]$Activity)

        if ($script:IronGuiLanguage -ne "ru" -or [string]::IsNullOrWhiteSpace($Activity)) {
            return $Activity
        }
        $activities = @{
            "Reading hardware identity" = "Чтение сведений об оборудовании"
            "Registering deployment" = "Регистрация развёртывания"
            "Connecting deployment share" = "Подключение сетевой папки развёртывания"
            "Selecting Windows image" = "Выбор образа Windows"
            "Checking deployment files" = "Проверка файлов развёртывания"
            "Provisioning Offline Domain Join" = "Подготовка Offline Domain Join"
            "Wiping and partitioning disk 0" = "Очистка и разметка диска 0"
            "Applying Windows image" = "Применение образа Windows"
            "Injecting drivers" = "Добавление драйверов"
            "Saving deployment state" = "Сохранение состояния развёртывания"
            "Generating unattend.xml" = "Создание unattend.xml"
            "Applying unattend.xml" = "Применение unattend.xml"
            "Applying Offline Domain Join" = "Применение Offline Domain Join"
            "Copying post-install scripts" = "Копирование сценариев после установки"
            "Creating UEFI boot files" = "Создание загрузочных файлов UEFI"
            "Ready to boot into Windows" = "Готово к загрузке Windows"
        }
        if ($activities.ContainsKey($Activity)) {
            return [string]$activities[$Activity]
        }
        if ($Activity -match '^Applying Windows image \((.+)\)$') {
            return "Применение образа Windows ($($Matches[1]))"
        }
        return $Activity
    }

    $script:IronGuiApplyLanguage = {
        $textControls = @(
            "HeaderSubtitle", "LoginTitle", "LoginDescription",
            "LoginUsernameLabel", "LoginPasswordLabel", "LoginPinLabel", "StepOneIndicator",
            "StepTwoIndicator", "StepThreeIndicator", "SerialLabel", "MacLabel", "ComputerNameLabel",
            "KnownDeploymentsTitle", "WindowsImageLabel", "ConfirmModelLabel",
            "ConfirmComputerLabel",
            "ConfirmImageLabel", "ConfirmDomainLabel", "NoProgramsText",
            "DriversLabel", "DriversHint", "WipeWarningText"
        )
        foreach ($name in $textControls) {
            $script:IronGuiUi[$name].Text = & $script:IronGuiGetText $name
        }
        $script:IronGuiUi.LoginDescription.Text = switch (
            [string]$script:IronGuiState.AuthMode
        ) {
            "pin" { & $script:IronGuiGetText "LoginPinDescription" }
            "none" { & $script:IronGuiGetText "LoginNoneDescription" }
            default { & $script:IronGuiGetText "LoginDescription" }
        }

        $contentControls = @(
            "LoginRebootButton", "DomainCheck", "SetupRebootButton", "BackButton",
            "NextButton", "DeployButton", "CancelRebootButton", "PreflightRetry",
            "PreflightRebootButton"
        )
        foreach ($name in $contentControls) {
            $script:IronGuiUi[$name].Content = & $script:IronGuiGetText $name
        }

        $accent = $script:IronGuiWindow.Resources["AccentBrush"]
        $card = $script:IronGuiWindow.Resources["CardBrush"]
        $script:IronGuiUi.LanguageRuButton.Background = if (
            $script:IronGuiLanguage -eq "ru"
        ) { $accent } else { $card }
        $script:IronGuiUi.LanguageEnButton.Background = if (
            $script:IronGuiLanguage -eq "en"
        ) { $accent } else { $card }

        $script:IronGuiUi.LoginButton.Content = if ($null -ne $script:IronGuiLoginJob) {
            & $script:IronGuiGetText "LoginAuthorizing"
        } else {
            & $script:IronGuiGetText "LoginButton"
        }
        if ($null -ne $script:IronGuiState.LoginErrorKey) {
            $script:IronGuiUi.LoginError.Text = & $script:IronGuiGetText `
                ([string]$script:IronGuiState.LoginErrorKey)
        }

        & $script:IronGuiUpdateDeployButton
        if ([string]$script:IronGuiState.SuggestedName -match "^pc\d{5}$") {
            $script:IronGuiUi.SuggestedText.Text = & $script:IronGuiGetText `
                "SuggestedName" @($script:IronGuiState.SuggestedName)
        }
        if ([string]$script:IronGuiState.LastDomainName -match "^pc\d{5}$") {
            $script:IronGuiUi.LastDomainText.Text = & $script:IronGuiGetText `
                "LastDomainName" @($script:IronGuiState.LastDomainName)
        }

        $programCount = @($script:IronGuiProgramChecks).Count
        $script:IronGuiUi.ProgramsLabel.Text = if ($programCount -gt 0) {
            & $script:IronGuiGetText "ProgramsCount" @($programCount)
        } else {
            & $script:IronGuiGetText "ProgramsLabel"
        }

        $script:IronGuiUi.NoDriversCheck.Content = & $script:IronGuiGetText `
            "NoDriversOption"

        $knownNames = @(
            @($script:IronGuiState.KnownDeployments) | ForEach-Object {
                & $script:IronGuiGetText "KnownDeployment" @(
                    $_.computer_name, $_.matched_by, $_.deployment_id
                )
            }
        )
        $script:IronGuiUi.KnownList.ItemsSource = $knownNames
        if ($knownNames.Count -gt 0) {
            $script:IronGuiUi.KnownDeploymentsButton.Content = & $script:IronGuiGetText `
                "KnownDeploymentsButton" @($knownNames.Count, [char]0x25BE)
        }

        $script:IronGuiUi.ConfirmDomainText.Text = if (
            [bool]$script:IronGuiUi.DomainCheck.IsChecked
        ) {
            & $script:IronGuiGetText "Enabled"
        } else {
            & $script:IronGuiGetText "Disabled"
        }

        if ($null -ne $script:IronGuiPreflightJob) {
            $script:IronGuiUi.PreflightStatus.Text = & $script:IronGuiGetText "PreflightLoading"
        } elseif ($script:IronGuiUi.PreflightError.Visibility -eq "Visible") {
            $script:IronGuiUi.PreflightStatus.Text = & $script:IronGuiGetText "PreflightFailed"
        }

        if (-not [string]::IsNullOrWhiteSpace([string]$script:IronGuiState.CurrentActivity)) {
            $script:IronGuiUi.ActivityText.Text = & $script:IronGuiTranslateActivity `
                ([string]$script:IronGuiState.CurrentActivity)
        } else {
            $script:IronGuiUi.ActivityText.Text = & $script:IronGuiGetText "ActivityText"
        }

        if ($script:IronGuiState.Finished) {
            if ($script:IronGuiState.LastResultSuccess) {
                $script:IronGuiUi.ResultTitle.Text = & $script:IronGuiGetText "DeploymentComplete"
                $script:IronGuiUi.ResultMessage.Text = & $script:IronGuiGetText "DeploymentCompleteMessage"
                $script:IronGuiUi.RebootButton.Content = & $script:IronGuiGetText "RebootButton"
                $script:IronGuiUi.CountdownText.Text = if ($script:IronGuiState.AutoRebootCancelled) {
                    & $script:IronGuiGetText "RebootCancelled"
                } else {
                    & $script:IronGuiGetText "RebootCountdown" @($script:IronGuiCountdown)
                }
            } else {
                $script:IronGuiUi.ResultTitle.Text = & $script:IronGuiGetText "DeploymentFailed"
                $script:IronGuiUi.RebootButton.Content = & $script:IronGuiGetText "Reboot"
            }
        } else {
            $script:IronGuiUi.RebootButton.Content = & $script:IronGuiGetText "RebootButton"
        }
    }

    # WinPE deployment authorization ----------------------------------------
    $script:IronGuiLoginBody = @'
. $EnginePath
try {
    $authorization = New-IronDeploymentAuthorization `
        -Mode $Mode `
        -Username $Username `
        -Password $Password `
        -Pin $Pin
    $Sync.DeploymentAccessToken = [string]$authorization.AccessToken
    $Sync.LoginOk = $true
} catch {
    $Sync.LoginOk = $false
    $Sync.LoginError = $_.Exception.Message
} finally {
    $Password = $null
    $Pin = $null
    $Sync.LoginDone = $true
}
'@

    $script:IronGuiLoginTimer = New-Object System.Windows.Threading.DispatcherTimer
    $script:IronGuiLoginTimer.Interval = [TimeSpan]::FromMilliseconds(120)
    $script:IronGuiLoginTimer.Add_Tick({
        if (-not $script:IronGuiState.LoginDone) { return }
        $script:IronGuiLoginTimer.Stop()
        Complete-IronGuiRunspace $script:IronGuiLoginJob
        $script:IronGuiLoginJob = $null
        $script:IronGuiUi.LoginPasswordBox.Password = ""
        $script:IronGuiUi.LoginPinBox.Password = ""
        $script:IronGuiUi.LoginButton.IsEnabled = $true
        $script:IronGuiUi.LoginButton.Content = & $script:IronGuiGetText "LoginButton"

        if ($script:IronGuiState.LoginOk) {
            $script:IronGuiState.LoginErrorKey = $null
            $script:IronGuiUi.LoginError.Visibility = "Collapsed"
            $script:IronGuiUi.LoginPanel.Visibility = "Collapsed"
            $script:IronGuiUi.SetupPanel.Visibility = "Visible"
            $script:IronGuiUi.PreflightOverlay.Visibility = "Visible"
            & $script:IronGuiStartPreflight
            $script:IronGuiPreflightTimer.Start()
        } else {
            $script:IronGuiState.LoginErrorKey = $null
            $script:IronGuiUi.LoginError.Text = [string]$script:IronGuiState.LoginError
            $script:IronGuiUi.LoginError.Visibility = "Visible"
            if ($script:IronGuiState.AuthMode -eq "none") {
                $script:IronGuiUi.LoginButton.Visibility = "Visible"
            }
            if ($script:IronGuiState.AuthMode -eq "pin") {
                $script:IronGuiUi.LoginPinBox.Focus()
            } elseif ($script:IronGuiState.AuthMode -eq "account") {
                $script:IronGuiUi.LoginPasswordBox.Focus()
            }
        }
    })

    $script:IronGuiStartLogin = {
        if (
            -not $script:IronGuiUi.LoginButton.IsEnabled -or
            $null -ne $script:IronGuiLoginJob
        ) {
            return
        }
        $mode = [string]$script:IronGuiState.AuthMode
        $username = ""
        $password = ""
        $pin = ""
        if ($mode -eq "account") {
            $username = ([string]$script:IronGuiUi.LoginUsernameBox.Text).Trim()
            $password = [string]$script:IronGuiUi.LoginPasswordBox.Password
            if ([string]::IsNullOrWhiteSpace($username) -or $password.Length -eq 0) {
                $script:IronGuiState.LoginErrorKey = "LoginRequired"
                $script:IronGuiUi.LoginError.Text = & $script:IronGuiGetText "LoginRequired"
                $script:IronGuiUi.LoginError.Visibility = "Visible"
                return
            }
        } elseif ($mode -eq "pin") {
            $pin = [string]$script:IronGuiUi.LoginPinBox.Password
            if ($pin -notmatch "^[0-9]{6,10}$") {
                $script:IronGuiState.LoginErrorKey = "LoginPinRequired"
                $script:IronGuiUi.LoginError.Text = & $script:IronGuiGetText "LoginPinRequired"
                $script:IronGuiUi.LoginError.Visibility = "Visible"
                return
            }
        }
        $script:IronGuiState.LoginDone = $false
        $script:IronGuiState.LoginOk = $false
        $script:IronGuiState.LoginError = ""
        $script:IronGuiState.LoginErrorKey = $null
        $script:IronGuiUi.LoginError.Visibility = "Collapsed"
        $script:IronGuiUi.LoginButton.IsEnabled = $false
        $script:IronGuiUi.LoginButton.Content = & $script:IronGuiGetText "LoginAuthorizing"
        $script:IronGuiLoginJob = Start-IronGuiRunspace `
            -Variables @{
                Sync = $script:IronGuiState
                EnginePath = $script:IronGuiEnginePath
                Username = $username
                Password = $password
                Pin = $pin
                Mode = $mode
            } `
            -ScriptBody $script:IronGuiLoginBody
        $password = $null
        $pin = $null
        $script:IronGuiLoginTimer.Start()
    }

    $script:IronGuiUi.LoginButton.Add_Click({ & $script:IronGuiStartLogin })
    $script:IronGuiUi.LoginPasswordBox.Add_KeyDown({
        param($eventSender, $eventArgs)
        if ($eventArgs.Key -eq [System.Windows.Input.Key]::Enter) {
            & $script:IronGuiStartLogin
        }
    })
    $script:IronGuiUi.LoginPinBox.Add_KeyDown({
        param($eventSender, $eventArgs)
        if ($eventArgs.Key -eq [System.Windows.Input.Key]::Enter) {
            & $script:IronGuiStartLogin
        }
    })
    switch ([string]$script:IronGuiState.AuthMode) {
        "pin" {
            $script:IronGuiUi.LoginAccountFields.Visibility = "Collapsed"
            $script:IronGuiUi.LoginPinFields.Visibility = "Visible"
        }
        "none" {
            $script:IronGuiUi.LoginAccountFields.Visibility = "Collapsed"
            $script:IronGuiUi.LoginPinFields.Visibility = "Collapsed"
            $script:IronGuiUi.LoginButton.Visibility = "Collapsed"
        }
        default {
            $script:IronGuiUi.LoginAccountFields.Visibility = "Visible"
            $script:IronGuiUi.LoginPinFields.Visibility = "Collapsed"
        }
    }
    $script:IronGuiUi.LoginRebootButton.Add_Click({
        $script:IronGuiState.Result = "reboot"
        $script:IronGuiState.AllowClose = $true
        $script:IronGuiWindow.Close()
    })

    $script:IronGuiAppendLog = {
        param([string]$Level, [string]$Message)
        $brush = $script:IronGuiBrushes[$Level]
        if ($null -eq $brush) { $brush = $script:IronGuiBrushes["info"] }
        $paragraph = New-Object System.Windows.Documents.Paragraph
        $paragraph.Margin = New-Object System.Windows.Thickness(0)
        $run = New-Object System.Windows.Documents.Run($Message)
        $run.Foreground = $brush
        $paragraph.Inlines.Add($run)
        $script:IronGuiUi.LogView.Document.Blocks.Add($paragraph)
        $script:IronGuiUi.LogView.ScrollToEnd()
    }

    # Wizard validation and navigation ---------------------------------------
    $script:IronGuiUpdateDeployButton = {
        $nameText = ([string]$script:IronGuiUi.NameBox.Text).Trim()
        $nameOk = $nameText -match "^(?i:pc)\d{5}$"
        $imageOk = $null -ne $script:IronGuiUi.ImageCombo.SelectedItem
        $driverChoiceOk = (
            [bool]$script:IronGuiUi.NoDriversCheck.IsChecked -or
            @(
                $script:IronGuiDriverChecks | Where-Object {
                    [bool]$_.IsChecked
                }
            ).Count -eq 1
        )
        $confirmOk = [bool]$script:IronGuiUi.WipeCheck.IsChecked
        $script:IronGuiUi.NextButton.IsEnabled = (
            $nameOk -and $imageOk -and -not $script:IronGuiState.Deploying
        )
        $script:IronGuiUi.DeployButton.IsEnabled = (
            $nameOk -and $imageOk -and $driverChoiceOk -and
            $confirmOk -and -not $script:IronGuiState.Deploying
        )

        if ($nameText.Length -gt 0 -and -not $nameOk) {
            $script:IronGuiUi.NameHint.Text = & $script:IronGuiGetText "NameInvalid"
            $script:IronGuiUi.NameHint.Foreground = $script:IronGuiBrushes["error"]
        } else {
            $script:IronGuiUi.NameHint.Text = & $script:IronGuiGetText "NameFormat"
            $script:IronGuiUi.NameHint.Foreground = [System.Windows.Media.Brushes]::Gray
        }
    }

    $script:IronGuiShowWizardStep = {
        param([ValidateSet(1, 2, 3)][int]$Step)

        $firstStep = $Step -eq 1
        $secondStep = $Step -eq 2
        $thirdStep = $Step -eq 3
        $script:IronGuiState.WizardStep = $Step
        $script:IronGuiUi.IdentityPage.Visibility = if ($firstStep) {
            "Visible"
        } else {
            "Collapsed"
        }
        $script:IronGuiUi.ProgramsPage.Visibility = if ($secondStep) {
            "Visible"
        } else {
            "Collapsed"
        }
        $script:IronGuiUi.DriversPage.Visibility = if ($thirdStep) {
            "Visible"
        } else {
            "Collapsed"
        }
        $script:IronGuiUi.StepOneIndicator.Foreground = if ($firstStep) {
            $script:IronGuiBrushes["step"]
        } else {
            $script:IronGuiBrushes["info"]
        }
        $script:IronGuiUi.StepTwoIndicator.Foreground = if ($secondStep) {
            $script:IronGuiBrushes["step"]
        } else {
            $script:IronGuiBrushes["info"]
        }
        $script:IronGuiUi.StepThreeIndicator.Foreground = if ($thirdStep) {
            $script:IronGuiBrushes["step"]
        } else {
            $script:IronGuiBrushes["info"]
        }
        $script:IronGuiUi.SetupRebootButton.Visibility = if ($firstStep) {
            "Visible"
        } else {
            "Collapsed"
        }
        $script:IronGuiUi.NextButton.Visibility = if ($thirdStep) {
            "Collapsed"
        } else {
            "Visible"
        }
        $script:IronGuiUi.BackButton.Visibility = if ($firstStep) {
            "Collapsed"
        } else {
            "Visible"
        }
        $script:IronGuiUi.DeployButton.Visibility = if ($thirdStep) {
            "Visible"
        } else {
            "Collapsed"
        }
    }

    $script:IronGuiDriverChecked = {
        param($eventSender, $eventArgs)
        if ($script:IronGuiChangingDriverSelection) { return }
        $script:IronGuiChangingDriverSelection = $true
        try {
            $script:IronGuiUi.NoDriversCheck.IsChecked = $false
            foreach ($driverCheck in $script:IronGuiDriverChecks) {
                if ($driverCheck -ne $eventSender) {
                    $driverCheck.IsChecked = $false
                }
            }
        } finally {
            $script:IronGuiChangingDriverSelection = $false
        }
        & $script:IronGuiUpdateDeployButton
    }

    $script:IronGuiDriverUnchecked = {
        param($eventSender, $eventArgs)
        if ($script:IronGuiChangingDriverSelection) { return }
        $hasSelectedPackage = @(
            $script:IronGuiDriverChecks | Where-Object {
                [bool]$_.IsChecked
            }
        ).Count -gt 0
        if (-not $hasSelectedPackage) {
            $script:IronGuiUi.NoDriversCheck.IsChecked = $true
        }
        & $script:IronGuiUpdateDeployButton
    }

    $script:IronGuiUi.NoDriversCheck.Add_Checked({
        if ($script:IronGuiChangingDriverSelection) { return }
        $script:IronGuiChangingDriverSelection = $true
        try {
            foreach ($driverCheck in $script:IronGuiDriverChecks) {
                $driverCheck.IsChecked = $false
            }
        } finally {
            $script:IronGuiChangingDriverSelection = $false
        }
        & $script:IronGuiUpdateDeployButton
    })
    $script:IronGuiUi.NoDriversCheck.Add_Unchecked({
        if ($script:IronGuiChangingDriverSelection) { return }
        $hasSelectedPackage = @(
            $script:IronGuiDriverChecks | Where-Object {
                [bool]$_.IsChecked
            }
        ).Count -gt 0
        if (-not $hasSelectedPackage) {
            $script:IronGuiUi.NoDriversCheck.IsChecked = $true
        }
        & $script:IronGuiUpdateDeployButton
    })

    $script:IronGuiUi.NameBox.Add_TextChanged($script:IronGuiUpdateDeployButton)
    $script:IronGuiUi.ImageCombo.Add_SelectionChanged($script:IronGuiUpdateDeployButton)
    $script:IronGuiUi.WipeCheck.Add_Checked($script:IronGuiUpdateDeployButton)
    $script:IronGuiUi.WipeCheck.Add_Unchecked($script:IronGuiUpdateDeployButton)
    $script:IronGuiUi.KnownDeploymentsButton.Add_Click({
        $script:IronGuiUi.KnownDeploymentsPopup.PlacementTarget = `
            $script:IronGuiUi.KnownDeploymentsButton
        $script:IronGuiUi.KnownDeploymentsPopup.IsOpen = $true
    })
    $script:IronGuiUi.NextButton.Add_Click({
        if (-not $script:IronGuiUi.NextButton.IsEnabled) {
            return
        }

        if ($script:IronGuiState.WizardStep -eq 1) {
            $selectedImage = $script:IronGuiUi.ImageCombo.SelectedItem
            if ($null -eq $selectedImage) { return }
            $script:IronGuiUi.KnownDeploymentsPopup.IsOpen = $false
            $script:IronGuiUi.ConfirmComputerText.Text = (
                [string]$script:IronGuiUi.NameBox.Text
            ).Trim().ToLowerInvariant()
            $script:IronGuiUi.ConfirmImageText.Text = [string]$selectedImage.Display
            $script:IronGuiUi.ConfirmDomainText.Text = if (
                [bool]$script:IronGuiUi.DomainCheck.IsChecked
            ) {
                & $script:IronGuiGetText "Enabled"
            } else {
                & $script:IronGuiGetText "Disabled"
            }
            & $script:IronGuiShowWizardStep 2
        } elseif ($script:IronGuiState.WizardStep -eq 2) {
            $script:IronGuiUi.WipeCheck.IsChecked = $false
            & $script:IronGuiShowWizardStep 3
        }
        & $script:IronGuiUpdateDeployButton
    })
    $script:IronGuiUi.BackButton.Add_Click({
        $script:IronGuiUi.WipeCheck.IsChecked = $false
        if ($script:IronGuiState.WizardStep -eq 3) {
            & $script:IronGuiShowWizardStep 2
        } else {
            & $script:IronGuiShowWizardStep 1
        }
        & $script:IronGuiUpdateDeployButton
    })

    # --- Preflight -----------------------------------------------------------
    $script:IronGuiPreflightBody = @'
. $EnginePath
Set-IronDeploymentAuthorization -AccessToken $AccessToken
try {
    $hw = Get-IronDeployHardwareIdentity
    $Sync.Serial = $hw.SerialNumber
    $Sync.Mac = $hw.MacAddress
    $Sync.Model = $hw.Model
    $sug = Get-IronDeployNameSuggestion -SerialNumber $hw.SerialNumber -MacAddress $hw.MacAddress
    $Sync.LastDomainName = [string]$sug.LastDomainName
    $Sync.SuggestedName = [string]$sug.SuggestedName
    $Sync.KnownDeployments = @($sug.KnownComputerNames)
    $Sync.Images = @(
        Get-IronDeployImageList | ForEach-Object {
            [pscustomobject]@{
                Name = $_.Name
                Display = ("{0}   ({1:N2} GB)" -f $_.Name, ($_.Length / 1GB))
            }
        }
    )
    $Sync.Programs = @(
        Get-IronDeployProgramList | ForEach-Object {
            [pscustomobject]@{
                Name = $_.Name
                Display = $_.Display
            }
        }
    )
    $Sync.DriverPackages = @(
        Get-IronDeployDriverPackageList | ForEach-Object {
            [pscustomobject]@{
                Vendor = $_.Vendor
                Model = $_.Model
                RelativePath = $_.RelativePath
                Length = $_.Length
                InfCount = $_.InfCount
            }
        }
    )
    $Sync.PreflightOk = $true
} catch {
    $Sync.PreflightOk = $false
    $Sync.PreflightError = $_.Exception.Message
} finally {
    $Sync.PreflightDone = $true
}
'@

    $script:IronGuiStartPreflight = {
        $script:IronGuiState.PreflightDone = $false
        $script:IronGuiState.PreflightOk = $false
        $script:IronGuiState.PreflightError = $null
        $script:IronGuiUi.PreflightStatus.Text = & $script:IronGuiGetText "PreflightLoading"
        $script:IronGuiUi.PreflightError.Visibility = "Collapsed"
        $script:IronGuiUi.PreflightRetry.Visibility = "Collapsed"
        $script:IronGuiUi.PreflightRebootButton.Visibility = "Collapsed"
        $script:IronGuiUi.KnownDeploymentsPopup.IsOpen = $false
        $script:IronGuiUi.KnownDeploymentsButton.Visibility = "Collapsed"
        $script:IronGuiPreflightJob = Start-IronGuiRunspace `
            -Variables @{
                Sync = $script:IronGuiState
                EnginePath = $script:IronGuiEnginePath
                AccessToken = [string]$script:IronGuiState.DeploymentAccessToken
            } `
            -ScriptBody $script:IronGuiPreflightBody
    }

    $script:IronGuiPreflightTimer = New-Object System.Windows.Threading.DispatcherTimer
    $script:IronGuiPreflightTimer.Interval = [TimeSpan]::FromMilliseconds(150)
    $script:IronGuiPreflightTimer.Add_Tick({
        if (-not $script:IronGuiState.PreflightDone) { return }
        $script:IronGuiPreflightTimer.Stop()
        Complete-IronGuiRunspace $script:IronGuiPreflightJob
        $script:IronGuiPreflightJob = $null

        if ($script:IronGuiState.PreflightOk) {
            $script:IronGuiUi.SerialText.Text = [string]$script:IronGuiState.Serial
            $script:IronGuiUi.MacText.Text = [string]$script:IronGuiState.Mac
            $script:IronGuiUi.ConfirmModelText.Text = if (
                [string]::IsNullOrWhiteSpace([string]$script:IronGuiState.Model)
            ) {
                [string][char]0x2014
            } else {
                [string]$script:IronGuiState.Model
            }
            $script:IronGuiUi.ConfirmModelText.ToolTip =
                $script:IronGuiUi.ConfirmModelText.Text
            $script:IronGuiUi.ImageCombo.ItemsSource = @($script:IronGuiState.Images)
            if (@($script:IronGuiState.Images).Count -gt 0) {
                $script:IronGuiUi.ImageCombo.SelectedIndex = 0
            }

            if ([string]$script:IronGuiState.SuggestedName -match "^pc\d{5}$") {
                $script:IronGuiUi.SuggestedText.Text = & $script:IronGuiGetText `
                    "SuggestedName" @($script:IronGuiState.SuggestedName)
                if (-not ([string]$script:IronGuiUi.NameBox.Text).Trim()) {
                    $script:IronGuiUi.NameBox.Text = [string]$script:IronGuiState.SuggestedName
                }
            }
            if ([string]$script:IronGuiState.LastDomainName -match "^pc\d{5}$") {
                $script:IronGuiUi.LastDomainText.Text = & $script:IronGuiGetText `
                    "LastDomainName" @($script:IronGuiState.LastDomainName)
            }
            $script:IronGuiUi.ProgramsPanel.Children.Clear()
            $script:IronGuiProgramChecks = @()
            foreach ($program in @($script:IronGuiState.Programs)) {
                $programCheck = New-Object System.Windows.Controls.CheckBox
                $programText = New-Object System.Windows.Controls.TextBlock
                $programText.Text = [string]$program.Display
                $programText.TextWrapping = "Wrap"
                $programCheck.Content = $programText
                $programCheck.Tag = [string]$program.Name
                $programCheck.Foreground = $script:IronGuiBrushes["info"]
                $programCheck.Margin = New-Object System.Windows.Thickness(0, 3, 0, 3)
                [void]$script:IronGuiUi.ProgramsPanel.Children.Add($programCheck)
                $script:IronGuiProgramChecks += $programCheck
            }
            if ($script:IronGuiProgramChecks.Count -gt 0) {
                $script:IronGuiUi.ProgramsLabel.Text = & $script:IronGuiGetText `
                    "ProgramsCount" @($script:IronGuiProgramChecks.Count)
                $script:IronGuiUi.ProgramsPanel.Visibility = "Visible"
                $script:IronGuiUi.NoProgramsText.Visibility = "Collapsed"
            } else {
                $script:IronGuiUi.ProgramsLabel.Text = & $script:IronGuiGetText "ProgramsLabel"
                $script:IronGuiUi.ProgramsPanel.Visibility = "Collapsed"
                $script:IronGuiUi.NoProgramsText.Visibility = "Visible"
            }

            $script:IronGuiChangingDriverSelection = $true
            try {
                $script:IronGuiUi.DriverPackagesPanel.Children.Clear()
                $script:IronGuiDriverChecks = @()
                $driverGroups = @(
                    @($script:IronGuiState.DriverPackages) |
                        Sort-Object -Property Vendor, Model |
                        Group-Object -Property Vendor
                )
                foreach ($driverGroup in $driverGroups) {
                    $vendorExpander = New-Object System.Windows.Controls.Expander
                    $vendorExpander.IsExpanded = $true
                    $vendorExpander.Margin = New-Object System.Windows.Thickness(0, 2, 0, 5)

                    $vendorHeader = New-Object System.Windows.Controls.TextBlock
                    $vendorHeader.Text = "{0} ({1})" -f `
                        ([string]$driverGroup.Name),
                        ([int]$driverGroup.Count)
                    $vendorHeader.Foreground = $script:IronGuiBrushes["info"]
                    $vendorHeader.FontWeight = [System.Windows.FontWeights]::SemiBold
                    $vendorExpander.Header = $vendorHeader

                    $vendorPackagesPanel = New-Object System.Windows.Controls.StackPanel
                    foreach ($driverPackage in @($driverGroup.Group)) {
                        $driverCheck = New-Object System.Windows.Controls.CheckBox
                        $driverText = New-Object System.Windows.Controls.TextBlock
                        $driverText.Text = "{0}   ({1:N1} MB, {2} INF)" -f `
                            ([string]$driverPackage.Model),
                            ([long]$driverPackage.Length / 1MB),
                            ([int]$driverPackage.InfCount)
                        $driverText.TextWrapping = "Wrap"
                        $driverCheck.Content = $driverText
                        $driverCheck.Tag = [string]$driverPackage.RelativePath
                        $driverCheck.Foreground = $script:IronGuiBrushes["info"]
                        $driverCheck.Margin = New-Object System.Windows.Thickness(18, 3, 0, 3)
                        $driverCheck.Add_Checked($script:IronGuiDriverChecked)
                        $driverCheck.Add_Unchecked($script:IronGuiDriverUnchecked)
                        [void]$vendorPackagesPanel.Children.Add($driverCheck)
                        $script:IronGuiDriverChecks += $driverCheck
                    }
                    $vendorExpander.Content = $vendorPackagesPanel
                    [void]$script:IronGuiUi.DriverPackagesPanel.Children.Add(
                        $vendorExpander
                    )
                }
                $script:IronGuiUi.NoDriversCheck.Content = `
                    & $script:IronGuiGetText "NoDriversOption"
                $script:IronGuiUi.NoDriversCheck.IsChecked = $true
            } finally {
                $script:IronGuiChangingDriverSelection = $false
            }

            $knownNames = @(
                @($script:IronGuiState.KnownDeployments) | ForEach-Object {
                    & $script:IronGuiGetText "KnownDeployment" @(
                        $_.computer_name, $_.matched_by, $_.deployment_id
                    )
                }
            )
            $script:IronGuiUi.KnownList.ItemsSource = $knownNames
            if ($knownNames.Count -gt 0) {
                $script:IronGuiUi.KnownDeploymentsButton.Content = & $script:IronGuiGetText `
                    "KnownDeploymentsButton" @($knownNames.Count, [char]0x25BE)
                $script:IronGuiUi.KnownDeploymentsButton.Visibility = "Visible"
            }

            $script:IronGuiUi.PreflightOverlay.Visibility = "Collapsed"
            & $script:IronGuiUpdateDeployButton
        } else {
            $script:IronGuiUi.PreflightStatus.Text = & $script:IronGuiGetText "PreflightFailed"
            $script:IronGuiUi.PreflightError.Text = [string]$script:IronGuiState.PreflightError
            $script:IronGuiUi.PreflightError.Visibility = "Visible"
            $script:IronGuiUi.PreflightRetry.Visibility = "Visible"
            $script:IronGuiUi.PreflightRebootButton.Visibility = "Visible"
        }
    })

    $script:IronGuiUi.PreflightRetry.Add_Click({
        & $script:IronGuiStartPreflight
        $script:IronGuiPreflightTimer.Start()
    })
    $script:IronGuiUi.PreflightRebootButton.Add_Click({
        $script:IronGuiState.Result = "reboot"
        $script:IronGuiState.AllowClose = $true
        $script:IronGuiWindow.Close()
    })

    # --- Deployment ----------------------------------------------------------
    $script:IronGuiDeployBody = @'
. $EnginePath
Set-IronDeploymentAuthorization -AccessToken $AccessToken
Set-IronDeployCallbacks `
    -OnLog { param($lvl, $msg) $Sync.Queue.Enqueue([pscustomobject]@{ Kind = "log"; Level = $lvl; Message = $msg }) } `
    -OnProgress { param($pct, $act) $Sync.Queue.Enqueue([pscustomobject]@{ Kind = "progress"; Percent = $pct; Activity = $act }) }
try {
    Invoke-IronDeployment `
        -ComputerName $ComputerName `
        -UseDomainJoin:$UseDomainJoin `
        -SelectedImageName $SelectedImageName `
        -SelectedProgramNames @($SelectedProgramNames) `
        -SelectedDriverPackage $SelectedDriverPackage |
        Out-Null
    $Sync.Queue.Enqueue([pscustomobject]@{ Kind = "done"; Success = $true })
} catch {
    $errorMessage = $_.Exception.Message
    try { Send-DeploymentError -Message $errorMessage } catch {}
    $Sync.Queue.Enqueue([pscustomobject]@{ Kind = "done"; Success = $false; Error = $errorMessage })
} finally {
    $Sync.Finished = $true
}
'@

    $script:IronGuiRebootTimer = New-Object System.Windows.Threading.DispatcherTimer
    $script:IronGuiRebootTimer.Interval = [TimeSpan]::FromSeconds(1)
    $script:IronGuiRebootTimer.Add_Tick({
        $script:IronGuiCountdown -= 1
        if ($script:IronGuiCountdown -le 0) {
            $script:IronGuiRebootTimer.Stop()
            $script:IronGuiState.Result = "reboot"
            $script:IronGuiState.AllowClose = $true
            $script:IronGuiWindow.Close()
            return
        }
        $script:IronGuiUi.CountdownText.Text = & $script:IronGuiGetText `
            "RebootCountdown" @($script:IronGuiCountdown)
    })

    $script:IronGuiShowResult = {
        param([bool]$Success, [string]$ErrorMessage)
        $script:IronGuiState.Finished = $true
        $script:IronGuiState.LastResultSuccess = $Success
        $script:IronGuiState.LastResultError = $ErrorMessage
        $script:IronGuiUi.ResultBar.Visibility = "Visible"
        if ($Success) {
            $script:IronGuiUi.ResultBar.Background = New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.Color]::FromRgb(0x14, 0x3A, 0x1E))
            $script:IronGuiUi.ResultTitle.Text = & $script:IronGuiGetText "DeploymentComplete"
            $script:IronGuiUi.ResultTitle.Foreground = $script:IronGuiBrushes["ok"]
            $script:IronGuiUi.ResultMessage.Text = & $script:IronGuiGetText "DeploymentCompleteMessage"
            $script:IronGuiUi.RebootButton.Visibility = "Visible"
            $script:IronGuiUi.CancelRebootButton.Visibility = "Visible"
            $script:IronGuiCountdown = 15
            $script:IronGuiUi.CountdownText.Text = & $script:IronGuiGetText `
                "RebootCountdown" @($script:IronGuiCountdown)
            $script:IronGuiRebootTimer.Start()
        } else {
            $script:IronGuiUi.ResultBar.Background = New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.Color]::FromRgb(0x3A, 0x14, 0x16))
            $script:IronGuiUi.ResultTitle.Text = & $script:IronGuiGetText "DeploymentFailed"
            $script:IronGuiUi.ResultTitle.Foreground = $script:IronGuiBrushes["error"]
            $script:IronGuiUi.ResultMessage.Text = $ErrorMessage
            $script:IronGuiUi.CountdownText.Text = ""
            $script:IronGuiUi.RebootButton.Content = & $script:IronGuiGetText "Reboot"
            $script:IronGuiUi.RebootButton.Background = `
                $script:IronGuiBrushes["error"]
            $script:IronGuiUi.RebootButton.Visibility = "Visible"
        }
    }

    $script:IronGuiDrainTimer = New-Object System.Windows.Threading.DispatcherTimer
    $script:IronGuiDrainTimer.Interval = [TimeSpan]::FromMilliseconds(120)
    $script:IronGuiDrainTimer.Add_Tick({
        while ($script:IronGuiState.Queue.Count -gt 0) {
            $item = $script:IronGuiState.Queue.Dequeue()
            switch ($item.Kind) {
                "log" { & $script:IronGuiAppendLog $item.Level $item.Message }
                "progress" {
                    $script:IronGuiUi.DeployProgress.Value = [double]$item.Percent
                    $script:IronGuiState.CurrentActivity = [string]$item.Activity
                    $script:IronGuiUi.ActivityText.Text = & $script:IronGuiTranslateActivity `
                        ([string]$item.Activity)
                }
                "done" {
                    $script:IronGuiDrainTimer.Stop()
                    Complete-IronGuiRunspace $script:IronGuiDeployJob
                    $script:IronGuiDeployJob = $null
                    if ($item.Success) {
                        $script:IronGuiUi.DeployProgress.Value = 100
                        & $script:IronGuiShowResult $true $null
                    } else {
                        & $script:IronGuiShowResult $false ([string]$item.Error)
                    }
                }
            }
        }
    })

    $script:IronGuiUi.DeployButton.Add_Click({
        if (
            $script:IronGuiState.WizardStep -ne 3 -or
            -not [bool]$script:IronGuiUi.WipeCheck.IsChecked -or
            -not $script:IronGuiUi.DeployButton.IsEnabled
        ) {
            return
        }
        $computerName = ([string]$script:IronGuiUi.NameBox.Text).Trim().ToLowerInvariant()
        $selectedImage = $script:IronGuiUi.ImageCombo.SelectedItem
        if ($null -eq $selectedImage) { return }
        $selectedImageName = [string]$selectedImage.Name
        $useDomainJoin = [bool]$script:IronGuiUi.DomainCheck.IsChecked
        $selectedProgramNames = @(
            $script:IronGuiProgramChecks |
                Where-Object { [bool]$_.IsChecked } |
                ForEach-Object { [string]$_.Tag }
        )
        $checkedDrivers = @(
            $script:IronGuiDriverChecks | Where-Object {
                [bool]$_.IsChecked
            }
        )
        if (
            (
                [bool]$script:IronGuiUi.NoDriversCheck.IsChecked -and
                $checkedDrivers.Count -ne 0
            ) -or (
                -not [bool]$script:IronGuiUi.NoDriversCheck.IsChecked -and
                $checkedDrivers.Count -ne 1
            )
        ) {
            return
        }
        $selectedDriverPackage = if ($checkedDrivers.Count -eq 1) {
            [string]$checkedDrivers[0].Tag
        } else {
            ""
        }

        $script:IronGuiState.Deploying = $true
        $script:IronGuiState.Queue = [System.Collections.Queue]::Synchronized((New-Object System.Collections.Queue))
        $script:IronGuiState.Finished = $false

        $script:IronGuiUi.DeployButton.IsEnabled = $false
        $script:IronGuiUi.SetupPanel.Visibility = "Collapsed"
        $script:IronGuiUi.ProgressPanel.Visibility = "Visible"

        $script:IronGuiDeployJob = Start-IronGuiRunspace `
            -Variables @{
                Sync = $script:IronGuiState
                EnginePath = $script:IronGuiEnginePath
                AccessToken = [string]$script:IronGuiState.DeploymentAccessToken
                ComputerName = $computerName
                UseDomainJoin = $useDomainJoin
                SelectedImageName = $selectedImageName
                SelectedProgramNames = $selectedProgramNames
                SelectedDriverPackage = $selectedDriverPackage
            } `
            -ScriptBody $script:IronGuiDeployBody

        $script:IronGuiDrainTimer.Start()
    })

    $script:IronGuiUi.SetupRebootButton.Add_Click({
        $script:IronGuiState.Result = "reboot"
        $script:IronGuiState.AllowClose = $true
        $script:IronGuiWindow.Close()
    })
    $script:IronGuiUi.RebootButton.Add_Click({
        $script:IronGuiRebootTimer.Stop()
        $script:IronGuiState.Result = "reboot"
        $script:IronGuiState.AllowClose = $true
        $script:IronGuiWindow.Close()
    })
    $script:IronGuiUi.CancelRebootButton.Add_Click({
        $script:IronGuiRebootTimer.Stop()
        $script:IronGuiState.AutoRebootCancelled = $true
        $script:IronGuiUi.CountdownText.Text = & $script:IronGuiGetText "RebootCancelled"
        $script:IronGuiUi.CancelRebootButton.Visibility = "Collapsed"
    })
    $script:IronGuiUi.LanguageRuButton.Add_Click({
        if ($script:IronGuiLanguage -ne "ru") {
            $script:IronGuiLanguage = "ru"
            & $script:IronGuiApplyLanguage
        }
    })
    $script:IronGuiUi.LanguageEnButton.Add_Click({
        if ($script:IronGuiLanguage -ne "en") {
            $script:IronGuiLanguage = "en"
            & $script:IronGuiApplyLanguage
        }
    })
    # GUI-only workflow: close only through an explicit reboot action or the
    # success countdown. The window chrome must not become an implicit reboot.
    $script:IronGuiWindow.Add_Closing({
        param($eventSender, $eventArgs)
        if (-not $script:IronGuiState.AllowClose) {
            $eventArgs.Cancel = $true
        }
    })
    $script:IronGuiAutoAuthorizationStarted = $false
    $script:IronGuiWindow.Add_ContentRendered({
        if (
            $script:IronGuiState.AuthMode -eq "none" -and
            -not $script:IronGuiAutoAuthorizationStarted
        ) {
            $script:IronGuiAutoAuthorizationStarted = $true
            & $script:IronGuiStartLogin
        }
    })

    # Show authorization first, hide the shared console, then show the WPF window.
    # Restore the console only if WPF itself throws a fatal exception.
    $GuiCompletedNormally = $false
    try {
        & $script:IronGuiShowWizardStep 1
        & $script:IronGuiApplyLanguage
        [void](Set-IronDeployConsoleVisible -Visible $false)
        if ($script:IronGuiState.AuthMode -eq "pin") {
            $script:IronGuiUi.LoginPinBox.Focus()
        } elseif ($script:IronGuiState.AuthMode -eq "account") {
            $script:IronGuiUi.LoginUsernameBox.Focus()
        }
        [void]$script:IronGuiWindow.ShowDialog()
        $GuiCompletedNormally = $true
    } finally {
        if (-not $GuiCompletedNormally) {
            [void](Set-IronDeployConsoleVisible -Visible $true)
        }
    }

    return [string]$script:IronGuiState.Result
}
