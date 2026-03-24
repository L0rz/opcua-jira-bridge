# OPC UA → Jira Bridge

Automatische Erstellung von Jira-Tickets aus OPC UA Alarmen einer simulierten Industrieanlage.

## Architektur

```
┌─────────────────┐     OPC UA      ┌──────────────────┐     REST     ┌─────────────┐
│  OPC UA Server   │◄──────────────►│  OPC UA Bridge    │────────────►│  Jira Cloud  │
│  (Simulation)    │   Subscribe     │  (Client)         │  API v2     │  (Tickets)   │
└─────────────────┘                 └──────────────────┘             └─────────────┘
                                           │
                                    ┌──────┴───────┐
                                    │   REST API    │
                                    │  (FastAPI)    │
                                    │  :8080        │
                                    └──────────────┘
```

### Komponenten

| Komponente | Datei | Beschreibung |
|---|---|---|
| OPC UA Server | `opcua_server.py` | Simuliert Industrieanlage mit Temp, Druck, Alarmen |
| Jira Bridge | `opcua_jira_bridge.py` | Subscribed auf Alarme, erstellt Jira-Incidents |
| REST API | `api.py` | FastAPI für Status, manuelle Alarme, Ticket-Liste |
| Test | `test_demo.py` | Verbindungstest mit Jira (erstellt + löscht Ticket) |

### OPC UA Nodes

| Node | Typ | Beschreibung |
|---|---|---|
| `Temperature` | Float | Aktuelle Temperatur (°C) |
| `Pressure` | Float | Aktueller Druck (bar) |
| `ErrorCode` | Int | 0=OK, 1=Warning, 2=Critical, 3=Emergency |
| `AlarmActive` | Bool | `true` wenn Alarm aktiv |
| `AlarmMessage` | String | Beschreibung des Alarms |

## Quick Start

### Voraussetzungen

- Python 3.11+
- Docker + Docker Compose (optional)

### 1. Setup

```bash
git clone <repo-url>
cd opcua-jira-bridge

# .env anlegen
cp .env.example .env
# .env mit Jira-Credentials ausfüllen

# Dependencies
pip install -r requirements.txt
```

### 2. Jira-Verbindung testen

```bash
python test_demo.py
```

### 3. Lokal starten

```bash
# Terminal 1: OPC UA Server
python opcua_server.py

# Terminal 2: Bridge (erstellt Tickets bei Alarmen)
python opcua_jira_bridge.py

# Terminal 3: REST API (optional)
python api.py
```

### 4. Mit Docker

```bash
docker-compose up --build
```

## REST API

Base URL: `http://localhost:8080`

### `GET /health`

Healthcheck.

```json
{"status": "healthy", "timestamp": "2026-03-24T13:00:00"}
```

### `GET /status`

Bridge-Status mit Uptime und Ticket-Zähler.

```json
{
  "status": "running",
  "uptime_seconds": 3600.0,
  "tickets_created": 5,
  "jira_project": "RKS",
  "opcua_endpoint": "opc.tcp://localhost:4840/freeopcua/server/"
}
```

### `POST /alarm`

Manuell einen Alarm triggern → erstellt Jira-Ticket.

**Body:**
```json
{
  "message": "Manueller Test-Alarm",
  "error_code": 2,
  "temperature": 92.5,
  "pressure": 1.2
}
```

**Response:**
```json
{
  "status": "created",
  "ticket": {
    "key": "RKS-42",
    "url": "https://frigotec.atlassian.net/browse/RKS-42",
    "priority": "High"
  }
}
```

### `GET /tickets`

Liste aller erstellten Tickets.

```json
{
  "count": 3,
  "tickets": [...]
}
```

## Duplikat-Vermeidung

Die Bridge verhindert Doppel-Tickets durch:
- **Message-Dedup:** Gleiche Alarm-Nachricht wird nur alle 5 Minuten als neues Ticket erstellt
- **Timestamp-Stripping:** Zeitstempel werden für den Vergleich entfernt

## Alarm-Szenarien (Simulation)

Der Test-Server simuliert folgende Störungen:

| ErrorCode | Priority | Beispiel |
|---|---|---|
| 1 | Medium | Temperatur über Grenzwert, Kompressor Vibration |
| 2 | High | Druckverlust kritisch, Kühlmittelstand niedrig |
| 3 | Highest | Notabschaltung, Stromversorgung instabil |

## Projektstruktur

```
opcua-jira-bridge/
├── .env.example          # Environment Template
├── .gitignore
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── README.md
├── opcua_server.py       # OPC UA Test-Server
├── opcua_jira_bridge.py  # Bridge (Client → Jira)
├── api.py                # REST API (FastAPI)
└── test_demo.py          # Jira Verbindungstest
```

## Lizenz

MIT
