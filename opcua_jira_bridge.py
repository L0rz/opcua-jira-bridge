"""
OPC UA → Jira Bridge

Flexibel konfigurierbar über opcua_config.yaml.
Unterstützt:
  - Anonymous / Username / Certificate Auth
  - NodeId-basiertes oder Browse-Path-basiertes Node-Discovery
  - Namespace via URI oder direktem Index
  - Beliebige zusätzliche Datennodes
"""

import asyncio
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import httpx
import yaml
from asyncua import Client, ua
from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [BRIDGE] %(message)s")
log = logging.getLogger("bridge")

# ── Jira Credentials aus .env ──────────────────────────────────────────────
JIRA_URL = os.getenv("JIRA_URL")
JIRA_USER = os.getenv("JIRA_USER")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "RKS")
JIRA_ACCOUNT_ID = os.getenv("JIRA_ACCOUNT_ID")

# State
_recent_alarms: dict[str, float] = {}
_created_tickets: list[dict] = []


# ─────────────────────────────────────────────────────────────────────────────
# Config Loading
# ─────────────────────────────────────────────────────────────────────────────

def load_config(config_path: str = "opcua_config.yaml") -> dict:
    """Lädt die YAML-Konfiguration."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Konfigurationsdatei nicht gefunden: {config_path}")
    with open(path) as f:
        cfg = yaml.safe_load(f)
    log.info("Konfiguration geladen: %s", config_path)
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Namespace Discovery
# ─────────────────────────────────────────────────────────────────────────────

async def resolve_namespace(client: Client, ns_cfg: dict) -> int:
    """Ermittelt den Namespace-Index."""
    mode = ns_cfg.get("discovery_mode", "uri")

    if mode == "index":
        idx = ns_cfg.get("index", 2)
        log.info("Namespace-Index (direkt): %d", idx)
        return idx

    if mode in ("uri", "auto"):
        uri = ns_cfg.get("uri", "")
        if uri:
            try:
                idx = await client.get_namespace_index(uri)
                log.info("Namespace-URI '%s' → Index %d", uri, idx)
                return idx
            except Exception as e:
                if mode == "auto":
                    fallback = ns_cfg.get("index", 2)
                    log.warning("URI-Discovery fehlgeschlagen (%s) → Fallback auf Index %d", e, fallback)
                    return fallback
                raise

    raise ValueError(f"Unbekannter discovery_mode: {mode}")


# ─────────────────────────────────────────────────────────────────────────────
# Node Resolution
# ─────────────────────────────────────────────────────────────────────────────

async def resolve_node(client: Client, node_cfg: dict, ns_idx: int):
    """Löst einen Node anhand der Konfiguration auf."""
    method = node_cfg.get("method", "browse_path")

    if method == "nodeid":
        nodeid_str = node_cfg["nodeid"]
        node = client.get_node(nodeid_str)
        log.debug("Node via NodeId: %s", nodeid_str)
        return node

    if method == "browse_path":
        path = node_cfg.get("browse_path", [])
        root = client.nodes.objects
        node = root
        for segment in path:
            node = await node.get_child([f"{ns_idx}:{segment}"])
        log.debug("Node via Browse-Path %s → %s", path, await node.read_node_id())
        return node

    raise ValueError(f"Unbekannte Node-Methode: {method}")


# ─────────────────────────────────────────────────────────────────────────────
# Jira
# ─────────────────────────────────────────────────────────────────────────────

def _is_duplicate(alarm_key: str, cooldown: int) -> bool:
    now = time.time()
    if alarm_key in _recent_alarms:
        if now - _recent_alarms[alarm_key] < cooldown:
            return True
    _recent_alarms[alarm_key] = now
    return False


async def create_jira_ticket(
    alarm_data: dict,
    cfg: dict,
) -> dict | None:
    """Erstellt ein Jira-Ticket basierend auf den Alarmdaten und der Konfiguration."""
    jira_cfg = cfg.get("jira", {})
    alarm_cfg = cfg.get("alarm", {})

    alarm_message = alarm_data.get("alarm_message", "Unbekannter Alarm")
    error_code = alarm_data.get("error_code", 0)

    # Dedup
    core_msg = alarm_message
    if "] " in core_msg:
        core_msg = core_msg.split("] ", 1)[1]

    cooldown = alarm_cfg.get("dedup_cooldown", 300)
    if _is_duplicate(core_msg, cooldown):
        log.info("Doppel-Ticket verhindert: %s", core_msg)
        return None

    # Priority
    priority_map = alarm_cfg.get("priority_map", {})
    priority = priority_map.get(error_code, priority_map.get("default", "Medium"))

    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    # Summary
    summary_tpl = jira_cfg.get("summary_template", "[OPC UA Alarm] {alarm_message}")
    summary = summary_tpl.format(
        alarm_message=core_msg,
        error_code=error_code,
        node_name=alarm_data.get("_node_name", ""),
    )[:255]

    # Description aus allen Datennodes aufbauen
    desc_rows = f"||Parameter||Wert||\n|Zeitstempel|{ts}|\n"
    nodes_cfg = cfg.get("nodes", {})
    for node_name, node_cfg in nodes_cfg.items():
        if node_name == "alarm_active":
            continue
        if not node_cfg.get("enabled", True):
            continue
        label = node_cfg.get("label", node_name)
        unit = node_cfg.get("unit", "")
        value = alarm_data.get(node_name, "n/a")
        if isinstance(value, float):
            value = f"{value:.2f}"
        desc_rows += f"|{label}|{value} {unit}|\n"

    description = (
        f"*Automatisch erstellt durch OPC UA → Jira Bridge*\n\n"
        f"{desc_rows}\n"
        f"Bitte umgehend prüfen und Maßnahmen einleiten."
    )

    payload = {
        "fields": {
            "project": {"key": JIRA_PROJECT_KEY},
            "summary": summary,
            "description": description,
            "issuetype": {"name": jira_cfg.get("issue_type", "Incident")},
            "priority": {"name": priority},
        }
    }

    labels = jira_cfg.get("labels", [])
    if labels:
        payload["fields"]["labels"] = labels

    if jira_cfg.get("auto_assign", True) and JIRA_ACCOUNT_ID:
        payload["fields"]["assignee"] = {"accountId": JIRA_ACCOUNT_ID}

    custom = jira_cfg.get("custom_fields", {})
    for field_id, value in custom.items():
        payload["fields"][field_id] = value

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
        log.error("Jira API Fehler %d: %s", e.response.status_code, e.response.text[:300])
        return None
    except Exception as e:
        log.error("Jira-Verbindung fehlgeschlagen: %s", e)
        return None


def get_created_tickets() -> list[dict]:
    return list(_created_tickets)


# ─────────────────────────────────────────────────────────────────────────────
# OPC UA Subscription Handler
# ─────────────────────────────────────────────────────────────────────────────

class AlarmHandler:
    def __init__(self, client: Client, data_nodes: dict, cfg: dict):
        self.client = client
        self.data_nodes = data_nodes  # {node_name: node_object}
        self.cfg = cfg
        self.trigger_value = cfg.get("alarm", {}).get("trigger_value", True)

    def datachange_notification(self, node, val, data):
        if val == self.trigger_value or (self.trigger_value is True and val is True):
            log.info("🚨 Alarm erkannt (Wert: %s) — Erstelle Jira-Ticket...", val)
            asyncio.get_event_loop().create_task(self._handle_alarm())

    async def _handle_alarm(self):
        alarm_data = {}
        for node_name, node_obj in self.data_nodes.items():
            try:
                alarm_data[node_name] = await node_obj.read_value()
            except Exception as e:
                log.warning("Konnte Node '%s' nicht lesen: %s", node_name, e)
                alarm_data[node_name] = "n/a"

        await create_jira_ticket(alarm_data, self.cfg)


# ─────────────────────────────────────────────────────────────────────────────
# Client Setup (Auth)
# ─────────────────────────────────────────────────────────────────────────────

async def setup_client(server_cfg: dict) -> Client:
    """Erstellt und konfiguriert den OPC UA Client."""
    endpoint = os.getenv("OPCUA_ENDPOINT") or server_cfg.get("endpoint", "opc.tcp://localhost:4840/")
    client = Client(url=endpoint, timeout=server_cfg.get("timeout", 10))

    auth_mode = server_cfg.get("auth_mode", "anonymous")
    security_policy = server_cfg.get("security_policy", "None")
    security_mode = server_cfg.get("security_mode", "None")

    if auth_mode == "username":
        username = os.getenv("OPCUA_USERNAME") or server_cfg.get("username", "")
        password = os.getenv("OPCUA_PASSWORD") or server_cfg.get("password", "")
        client.set_user(username)
        client.set_password(password)
        log.info("Auth: Username (%s)", username)

    elif auth_mode == "certificate":
        cert_path = server_cfg.get("certificate_path", "")
        key_path = server_cfg.get("private_key_path", "")
        if security_policy != "None" and security_mode != "None":
            await client.set_security_string(
                f"Basic256Sha256,{security_mode},{cert_path},{key_path}"
            )
            log.info("Auth: Certificate + Security %s/%s", security_policy, security_mode)
    else:
        log.info("Auth: Anonymous")

    return client


# ─────────────────────────────────────────────────────────────────────────────
# Main Bridge Loop
# ─────────────────────────────────────────────────────────────────────────────

async def run_bridge(config_path: str = "opcua_config.yaml"):
    cfg = load_config(config_path)
    server_cfg = cfg.get("server", {})
    ns_cfg = cfg.get("namespace", {})
    nodes_cfg = cfg.get("nodes", {})

    endpoint = os.getenv("OPCUA_ENDPOINT") or server_cfg.get("endpoint")
    reconnect = server_cfg.get("reconnect_interval", 5)

    log.info("Bridge gestartet — Verbinde mit: %s", endpoint)

    while True:
        try:
            client = await setup_client(server_cfg)

            async with client:
                log.info("✅ Verbunden mit OPC UA Server")

                # Namespace auflösen
                ns_idx = await resolve_namespace(client, ns_cfg)

                # Alarm-Trigger-Node
                alarm_active_cfg = nodes_cfg.get("alarm_active", {})
                alarm_node = await resolve_node(client, alarm_active_cfg, ns_idx)
                log.info("Alarm-Node gefunden: %s", await alarm_node.read_node_id())

                # Datennodes für Ticket-Inhalt
                data_nodes = {}
                for node_name, node_cfg in nodes_cfg.items():
                    if node_name == "alarm_active":
                        continue
                    if not node_cfg.get("enabled", True):
                        continue
                    try:
                        data_nodes[node_name] = await resolve_node(client, node_cfg, ns_idx)
                        log.info("Daten-Node '%s' gefunden", node_name)
                    except Exception as e:
                        log.warning("Node '%s' nicht gefunden: %s — wird übersprungen", node_name, e)

                # Subscription
                handler = AlarmHandler(client, data_nodes, cfg)
                sub = await client.create_subscription(500, handler)
                await sub.subscribe_data_change(alarm_node)
                log.info("Subscribed auf Alarm-Node — warte auf Alarme...")

                while True:
                    await asyncio.sleep(1)

        except Exception as e:
            log.error("Verbindungsfehler: %s — Retry in %ds...", e, reconnect)
            await asyncio.sleep(reconnect)


if __name__ == "__main__":
    import sys
    config_file = sys.argv[1] if len(sys.argv) > 1 else "opcua_config.yaml"
    asyncio.run(run_bridge(config_file))
