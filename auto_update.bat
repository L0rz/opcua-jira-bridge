@echo off
:: OPC UA Jira Bridge - Auto Updater
:: Runs via Windows Task Scheduler (e.g. every hour)
:: Checks GitHub for updates, pulls, restarts service if changed

cd /d C:\opcua-jira-bridge

:: Save current commit hash
for /f %%i in ('git rev-parse HEAD') do set BEFORE=%%i

:: Pull latest
git pull origin master --quiet 2>nul

:: Check if something changed
for /f %%i in ('git rev-parse HEAD') do set AFTER=%%i

if "%BEFORE%"=="%AFTER%" (
    echo %date% %time% - No updates >> logs\update.log
    exit /b 0
)

echo %date% %time% - Updated from %BEFORE% to %AFTER% >> logs\update.log

:: Install any new dependencies
.venv\Scripts\pip install -r requirements.txt --quiet 2>nul

:: Restart service
C:\nssm\nssm.exe restart OpcUaJiraBridge
echo %date% %time% - Service restarted >> logs\update.log
