@echo off
REM Hydra collector - one-click setup. Double-click this file.
REM Downloads a portable GitHub CLI if needed (no installer), logs in to GitHub (device code),
REM creates the PUBLIC repo "hydra-collector", pushes this folder and starts the first runs.
REM Progress is written to start_log.txt in this folder.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0START.ps1"
echo.
echo Finished. See start_log.txt. This window closes in 60 seconds.
timeout /t 60 >nul
