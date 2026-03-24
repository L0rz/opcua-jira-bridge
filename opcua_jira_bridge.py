"""
OPC UA → Jira Bridge

Subscribed auf den AlarmActive-Node des OPC UA Servers.
Wenn ein Alarm erkannt wird:
  1. Liest AlarmMessage, ErrorCode, Temperature, Pressure
  2. Erstellt ein Jira-Ticket im konfigurierten Projekt
  3. Verhindert Doppel-Tickets (Cooldown + Message-Dedup)
"""

import asyncio
import logging
import os
import time
from datetime import datetime

import httpx
from asyncua import Client, ua
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [BRIDGE] %(message)s")
log = logging.getLogger("bridge")

OPCUA_ENDPOINT = os.getenv("OPCUA_ENDPOINT", "opc.tcp://localhost:4840/freeopcua/server/")
JIRA_URL = os.getenv("JIRA_URL")
JIRA_USER = os.getenv("JIRA_USER")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "RKS")
JIRA_ACCOUNT_ID = os.getenv("JIRA_ACCOUNT_ID")

# Dedup: min seconds between tickets for same message
DEDUP_COOLDOWN = 300  # 5 Minuten
_recent_alarms: dict[str, float] = {}
_created_tickets: list[dict] = []

PRIORITY_MAP = {
    0: "Low",       # OK (shouldn't trigger, but fallback)
    1: "Medium",    # Warning
    2: "High",      # Critical
    3: "Highest",   # Emergency
}


def _is_duplicate(alarm_message: str) -> bool:
    """Check if this alarm was already reported recently."""
    # Normalize: strip timestamp prefix for dedup
    core_msg = alarm_message
    if "] " in core_msg:
        core_msg = core_msg.split("] ", 1)[1]

    now = time.time()
    if core_msg in _recent_alarms:
        if now - _recent_alarms[core_msg] < DEDUP_COOLDOWN:
            return True
    _recent_alarms[core_msg] = now
    return False


async def create_jira_ticket(
    alarm_message: str,
    error_code: int,
    temperature: float,
    pressure: float,
) -> dict | None:
    """Create a Jira ticket for an OPC UA alarm."""
    if _is_duplicate(alarm_message):
        log.info("Doppel-Ticket verhindert: %s", alarm_message)
        return None

    priority = PRIORITY_MAP.get(error_code, "Medium")
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    # Clean message for summary
    summary_msg = alarm_message
    if "] " in summary_msg:
        summary_msg = summary_msg.split("] ", 1)[1]

    summary = f"[OPC UA Alarm] {summary_msg}"
    description = (
        f"*Automatisch erstellt durch OPC UA → Jira Bridge*\n\n"
        f"||Parameter||Wert||\n"
        f"|Zeitstempel|{ts}|\n"
        f"|Alarm|{alarm_message}|\n"
        f"|Error Code|{error_code} ({priority})|\n"
        f"|Temperatur|{temperature:.1f} °C|\n"
        f"|Druck|{pressure:.2f} bar|\n\n"
        f"Bitte umgehend prüfen und Maßnahmen einleiten."
    )

    payload = {
        "fields": {
            "project": {"key": JIRA_PROJECT_KEY},
            "summary": summary[:255],
            "description": description,
            "issuetype": {"name": "Incident"},
            "priority": {"name": priority},
        }
    }

    if JIRA_ACCOUNT_ID:
        payload["fields"]["assignee"] = {"accountId": JIRA_ACCOUNT_ID}

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{JIRA_URL}/rest/api/2/issue",
                json=payload,
                auth=(JIRA_USER, JIRA_API_TOKEN),
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            ticket = {
                "key": data["key"],
                "id": data["id"],
                "summary": summary,
                "priority": priority,
                "error_code": error_code,
                "created_at": ts,
                "url": f"{JIRA_URL}/browse/{data['key']}",
            }
            _created_tickets.append(ticket)
            log.info("✅ Jira-Ticket erstellt: %s → %s", data["key"], ticket["url"])
            return ticket
    except httpx.HTTPStatusError as e:
        log.error("Jira API Fehler %d: %s", e.response.status_code, e.response.text)
        return None
    except Exception as e:
        log.error("Jira-Verbindung fehlgeschlagen: %s", e)
        return None


def get_created_tickets() -> list[dict]:
    return list(_created_tickets)


class AlarmHandler:
    """Subscription handler for OPC UA data changes."""

    def __init__(self, client: Client, ns_idx: int):
        self.client = client
        self.ns_idx = ns_idx

    def datachange_notification(self, node, val, data):
        """Called when AlarmActive changes."""
        if val is True:
            log.info("🚨 Alarm erkannt! Erstelle Jira-Ticket...")
            asyncio.get_event_loop().create_task(self._handle_alarm())

    async def _handle_alarm(self):
        try:
            root = self.client.nodes.objects
            plant = await root.get_child([f"{self.ns_idx}:IndustrialPlant"])

            temp = await (await plant.get_child([f"{self.ns_idx}:Temperature"])).read_value()
            pressure = await (await plant.get_child([f"{self.ns_idx}:Pressure"])).read_value()
            error_code = await (await plant.get_child([f"{self.ns_idx}:ErrorCode"])).read_value()
            alarm_msg = await (await plant.get_child([f"{self.ns_idx}:AlarmMessage"])).read_value()

            await create_jira_ticket(alarm_msg, error_code, temp, pressure)
        except Exception as e:
            log.error("Fehler beim Alarm-Handling: %s", e)


async def run_bridge():
    """Main bridge loop — connects to OPC UA and subscribes to alarms."""
    log.info("Verbinde mit OPC UA Server: %s", OPCUA_ENDPOINT)

    while True:
        try:
            async with Client(url=OPCUA_ENDPOINT) as client:
                log.info("Verbunden mit OPC UA Server")

                # Find namespace
                ns_idx = await client.get_namespace_index("http://opcua-jira-bridge.test")
                log.info("Namespace Index: %d", ns_idx)

                # Get AlarmActive node
                root = client.nodes.objects
                plant = await root.get_child([f"{ns_idx}:IndustrialPlant"])
                alarm_node = await plant.get_child([f"{ns_idx}:AlarmActive"])

                # Subscribe
                handler = AlarmHandler(client, ns_idx)
                sub = await client.create_subscription(500, handler)
                await sub.subscribe_data_change(alarm_node)
                log.info("Subscribed auf AlarmActive — warte auf Alarme...")

                # Keep alive
                while True:
                    await asyncio.sleep(1)

        except Exception as e:
            log.error("Verbindung verloren: %s — Retry in 5s...", e)
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(run_bridge())
