@echo off
echo ============================================================
echo  OPC UA Jira Bridge - Windows Service Installation
echo ============================================================
echo.

:: NSSM herunterladen falls nicht vorhanden
if not exist "C:\nssm\nssm.exe" (
    echo Downloading NSSM...
    mkdir C:\nssm 2>nul
    curl -L -o C:\nssm\nssm.zip https://nssm.cc/release/nssm-2.24.zip
    powershell -Command "Expand-Archive -Path 'C:\nssm\nssm.zip' -DestinationPath 'C:\nssm\temp' -Force"
    copy "C:\nssm\temp\nssm-2.24\win64\nssm.exe" "C:\nssm\nssm.exe"
    rmdir /s /q "C:\nssm\temp"
    del "C:\nssm\nssm.zip"
    echo NSSM installed to C:\nssm\nssm.exe
)

echo.
echo Installing service...

C:\nssm\nssm.exe install OpcUaJiraBridge "C:\opcua-jira-bridge\.venv\Scripts\python.exe"
C:\nssm\nssm.exe set OpcUaJiraBridge AppParameters "C:\opcua-jira-bridge\bridge6.py opcua_config_real_server.yaml"
C:\nssm\nssm.exe set OpcUaJiraBridge AppDirectory "C:\opcua-jira-bridge"
C:\nssm\nssm.exe set OpcUaJiraBridge DisplayName "OPC UA to Jira Bridge"
C:\nssm\nssm.exe set OpcUaJiraBridge Description "Monitors OPC UA alarm nodes and creates Jira tickets automatically"
C:\nssm\nssm.exe set OpcUaJiraBridge Start SERVICE_AUTO_START
C:\nssm\nssm.exe set OpcUaJiraBridge AppStdout "C:\opcua-jira-bridge\logs\bridge.log"
C:\nssm\nssm.exe set OpcUaJiraBridge AppStderr "C:\opcua-jira-bridge\logs\bridge.log"
C:\nssm\nssm.exe set OpcUaJiraBridge AppRotateFiles 1
C:\nssm\nssm.exe set OpcUaJiraBridge AppRotateBytes 5242880
C:\nssm\nssm.exe set OpcUaJiraBridge AppRestartDelay 30000

:: Log-Ordner anlegen
mkdir "C:\opcua-jira-bridge\logs" 2>nul

echo.
echo ============================================================
echo  Service installed! Commands:
echo    Start:   nssm start OpcUaJiraBridge
echo    Stop:    nssm stop OpcUaJiraBridge
echo    Status:  nssm status OpcUaJiraBridge
echo    Logs:    type C:\opcua-jira-bridge\logs\bridge.log
echo    Remove:  nssm remove OpcUaJiraBridge confirm
echo ============================================================
echo.

:: Starten
C:\nssm\nssm.exe start OpcUaJiraBridge
echo Service started!
pause
