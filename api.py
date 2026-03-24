"""
REST API für die OPC UA → Jira Bridge.

Endpoints:
  GET  /health   — Healthcheck
  GET  /status   — Bridge-Status
  POST /alarm    — Manuell einen Alarm triggern (erstellt Jira-Ticket)
  GET  /tickets  — Liste aller erstellten Tickets
"""

import os
from datetime import datetime

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from opcua_jira_bridge import create_jira_ticket, get_created_tickets

load_dotenv()

app = FastAPI(
    title="OPC UA → Jira Bridge API",
    description="REST API zum Steuern und Überwachen der OPC UA Jira Bridge",
    version="1.0.0",
)

_start_time = datetime.utcnow()


class AlarmRequest(BaseModel):
    message: str = "Manueller Alarm via API"
    error_code: int = 1
    temperature: float = 85.0
    pressure: float = 3.5


@app.get("/health")
async def health():
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}


@app.get("/status")
async def status():
    tickets = get_created_tickets()
    uptime = (datetime.utcnow() - _start_time).total_seconds()
    return {
        "status": "running",
        "uptime_seconds": round(uptime, 1),
        "tickets_created": len(tickets),
        "jira_project": os.getenv("JIRA_PROJECT_KEY", "RKS"),
        "opcua_endpoint": os.getenv("OPCUA_ENDPOINT", "N/A"),
    }


@app.post("/alarm")
async def trigger_alarm(req: AlarmRequest):
    """Manuell einen Alarm triggern → erstellt Jira-Ticket."""
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    alarm_message = f"[{ts}] {req.message}"

    ticket = await create_jira_ticket(
        alarm_message=alarm_message,
        error_code=req.error_code,
        temperature=req.temperature,
        pressure=req.pressure,
    )

    if ticket is None:
        raise HTTPException(
            status_code=429,
            detail="Ticket nicht erstellt (Duplikat oder Jira-Fehler)",
        )

    return {"status": "created", "ticket": ticket}


@app.get("/tickets")
async def list_tickets():
    tickets = get_created_tickets()
    return {"count": len(tickets), "tickets": tickets}


if __name__ == "__main__":
    uvicorn.run(
        "api:app",
        host=os.getenv("API_HOST", "0.0.0.0"),
        port=int(os.getenv("API_PORT", "8080")),
        reload=True,
    )
