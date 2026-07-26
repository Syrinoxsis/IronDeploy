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

exit /b 0
