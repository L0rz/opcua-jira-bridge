@echo off
echo ============================================================
echo  OPC UA Jira Bridge - Auto-Update Installer
echo  Creates a scheduled task that checks for updates every hour
echo ============================================================
echo.

:: Git Repo initialisieren falls noch ZIP-Download
cd /d C:\opcua-jira-bridge
git status >nul 2>&1
if errorlevel 1 (
    echo Initializing git repo...
    git init
    git remote add origin https://github.com/L0rz/opcua-jira-bridge.git
    git fetch origin
    git checkout -b master origin/master --force
)

:: Log-Ordner
mkdir logs 2>nul

:: Scheduled Task erstellen (jede Stunde)
schtasks /create /tn "OpcUaJiraBridge-AutoUpdate" /tr "C:\opcua-jira-bridge\auto_update.bat" /sc hourly /ru SYSTEM /f

echo.
echo ============================================================
echo  Auto-Update installed!
echo  - Checks every hour for GitHub updates
echo  - Restarts service automatically if code changed
echo  - Logs: C:\opcua-jira-bridge\logs\update.log
echo.
echo  Manual trigger: schtasks /run /tn "OpcUaJiraBridge-AutoUpdate"
echo  Remove:         schtasks /delete /tn "OpcUaJiraBridge-AutoUpdate" /f
echo ============================================================
pause
