# Windows Setup — OPC UA → Jira Bridge

Bridge läuft auf derselben Maschine wie der OPC UA Server (localhost).

## Voraussetzungen

- Python 3.11+ → https://www.python.org/downloads/
  ⚠️ Bei Installation: **"Add Python to PATH"** aktivieren!

## 1. Projektordner auf Windows kopieren

Den ganzen Ordner `opcua-jira-bridge/` auf die Windows-Maschine (z.B. `C:\opcua-jira-bridge\`).
Relevante Dateien:
- `opcua_jira_bridge.py`
- `opcua_config_real_server.yaml`
- `.env`
- `requirements.txt`

## 2. Venv & Dependencies

```cmd
cd C:\opcua-jira-bridge
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Starten

```cmd
cd C:\opcua-jira-bridge
.venv\Scripts\activate
python opcua_jira_bridge.py opcua_config_real_server.yaml
```

## 4. Als Windows-Dienst (optional, Autostart)

Mit NSSM (Non-Sucking Service Manager):
```cmd
nssm install OpcUaJiraBridge "C:\opcua-jira-bridge\.venv\Scripts\python.exe"
nssm set OpcUaJiraBridge AppParameters "C:\opcua-jira-bridge\opcua_jira_bridge.py opcua_config_real_server.yaml"
nssm set OpcUaJiraBridge AppDirectory "C:\opcua-jira-bridge"
nssm start OpcUaJiraBridge
```

## Erwartete Ausgabe beim Start

```
2026-xx-xx [BRIDGE] Bridge gestartet — Endpoint: opc.tcp://localhost:48010
2026-xx-xx [BRIDGE] ✅ Verbunden mit OPC UA Server
2026-xx-xx [BRIDGE] Namespace-Index: 2
2026-xx-xx [BRIDGE] Alarm-Node [FEEDBACK_MOTOR1] → ns=2;s=SIMULATED...
2026-xx-xx [BRIDGE] Alarm-Node [PHASE_LOSS] → ns=2;s=SIMULATED...
2026-xx-xx [BRIDGE] Alarm-Node [PUMP_OVERLOAD] → ns=2;s=SIMULATED...
2026-xx-xx [BRIDGE] Subscribed auf 3 Alarm-Node(s) — warte auf Alarme...
```

Nach ~5 Sekunden (Toggle-Intervall):
```
2026-xx-xx [BRIDGE] 🚨 ALARM [PHASE_LOSS] aktiv (Wert: True)
2026-xx-xx [BRIDGE] ✅ Ticket erstellt: RKS-8 → https://frigotec.atlassian.net/browse/RKS-8
```
