@echo off
chcp 65001 >nul

mkdir C:\IronDeploy 2>nul

echo ======================================== > C:\IronDeploy\SetupComplete.log
echo IronDeploy SetupComplete started >> C:\IronDeploy\SetupComplete.log
echo Date: %date% %time% >> C:\IronDeploy\SetupComplete.log
echo ======================================== >> C:\IronDeploy\SetupComplete.log

powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Windows\Setup\Scripts\postinstall.ps1 >> C:\IronDeploy\SetupComplete.log 2>&1

echo ExitCode: %ERRORLEVEL% >> C:\IronDeploy\SetupComplete.log
echo IronDeploy SetupComplete finished >> C:\IronDeploy\SetupComplete.log

if exist C:\IronDeploy\postinstall-ok.txt if exist C:\IronDeploy\postinstall-reboot-required.txt (
    echo Post-install requested reboot; scheduling it after SetupComplete. >> C:\IronDeploy\SetupComplete.log
    shutdown.exe /r /t 15 /d p:2:4 /c "IronDeploy post-install completed"
)

exit /b 0
