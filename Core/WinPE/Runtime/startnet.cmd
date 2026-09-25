@echo off
wpeinit
rem TEMPORARY WINPE DRIVER UPLOAD: remove this line with the stop-gap feature.
powershell.exe -ExecutionPolicy Bypass -File X:\IronDeploy\Load-WinPEDrivers.ps1
powershell.exe -STA -ExecutionPolicy Bypass -File X:\IronDeploy\deploy.ps1
