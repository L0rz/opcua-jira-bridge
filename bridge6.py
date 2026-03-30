"""
OPC UA → Jira Bridge v6
- Auto-Discovery: browst alle ROOMS/*/ALARMS Children automatisch
- Kein hardcoded Alarm-Node-Liste mehr
- Kontext-Nodes (CLIENT_ID, ROOM_ID etc.) pro Room
"""
import asyncio
import logging
import os
import sys
import time
import platform
from datetime import datetime
from pathlib import Path

import httpx
import yaml
from asyncua import Client
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [BRIDGE] %(message)s")
log = logging.getLogger("bridge")

JIRA_URL        = os.getenv("JIRA_URL")
JIRA_USER       = os.getenv("JIRA_USER")
JIRA_API_TOKEN  = os.getenv("JIRA_API_TOKEN")
JIRA_PROJECT    = os.getenv("JIRA_PROJECT_KEY", "RKS")
JIRA_ACCOUNT_ID = os.getenv("JIRA_ACCOUNT_ID")

_open_tickets:   dict[str, dict] = {}
_created_tickets: list[dict]     = []
_recent_alarms:  dict[str, float] = {}


def load_config(config_path: str = "opcua_config_real_server.yaml") -> dict:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")
    with open(path) as f:
        return yaml.safe_load(f)


def _is_duplicate(alarm_key: str, cooldown: int) -> bool:
    now = time.time()
    last = _recent_alarms.get(alarm_key, 0)
    if now - last < cooldown:
        return True
    _recent_alarms[alarm_key] = now
    return False


async def _jira_post(path: str, payload: dict) -> dict | None:
    try:
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                f"{JIRA_URL}{path}", json=payload,
                auth=(JIRA_USER, JIRA_API_TOKEN),
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json() if resp.content else {}
    except httpx.HTTPStatusError as e:
        log.error("Jira API %d: %s", e.response.status_code, e.response.text[:300])
    except Exception as e:
        log.error("Jira connection error: %s", e)
    return None


async def create_jira_ticket(alarm_key: str, alarm_label: str, priority: str,
                              context: dict, jira_cfg: dict, alarm_cfg: dict) -> dict | None:
    cooldown = alarm_cfg.get("dedup_cooldown", 300)
    if _is_duplicate(alarm_key, cooldown):
        log.info("Dedup: skipping %s", alarm_key)
        return None

    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    client_id = context.get("client_id", "n/a")
    room_id   = context.get("room_id", "n/a")

    summary_tpl = jira_cfg.get("summary_template", "[OPC UA] {client_id} / {room_id} - {alarm_label}")
    summary = summary_tpl.format(
        alarm_key=alarm_key, alarm_label=alarm_label,
        client_id=client_id, room_id=room_id,
    )[:255]

    rows = f"||Parameter||Value||\n|Timestamp|{ts}|\n|Alarm Key|{alarm_key}|\n"
    for k, v in context.items():
        rows += f"|{k}|{v}|\n"

    description = (
        f"*Automatically created by OPC UA to Jira Bridge*\n\n"
        f"*Alarm:* {alarm_label}\n\n{rows}\n"
        f"Please check and take corrective action immediately."
    )

    payload: dict = {
        "fields": {
            "project":   {"key": JIRA_PROJECT},
            "summary":   summary,
            "description": description,
            "issuetype": {"name": jira_cfg.get("issue_type", "Incident")},
            "priority":  {"name": priority},
        }
    }
    labels = jira_cfg.get("labels", [])
    if labels:
        payload["fields"]["labels"] = labels
    if jira_cfg.get("auto_assign", True) and JIRA_ACCOUNT_ID:
        payload["fields"]["assignee"] = {"accountId": JIRA_ACCOUNT_ID}

    data = await _jira_post("/rest/api/2/issue", payload)
    if not data:
        return None

    ticket = {
        "key": data["key"], "id": data["id"], "alarm_key": alarm_key,
        "summary": summary, "priority": priority, "created_at": ts,
        "url": f"{JIRA_URL}/browse/{data['key']}",
    }
    _open_tickets[alarm_key] = ticket
    _created_tickets.append(ticket)
    log.info("Ticket created: %s -> %s", data["key"], ticket["url"])
    return ticket


async def resolve_jira_ticket(alarm_key: str, alarm_cfg: dict) -> bool:
    ticket = _open_tickets.get(alarm_key)
    if not ticket:
        return False

    transition_id = str(alarm_cfg.get("resolve_transition_id", "5"))
    comment = alarm_cfg.get("resolve_comment", "Alarm automatically resolved: OPC UA Node returned to FALSE.")
    issue_key = ticket["key"]

    await _jira_post(f"/rest/api/2/issue/{issue_key}/transitions", {"transition": {"id": transition_id}})
    await _jira_post(f"/rest/api/2/issue/{issue_key}/comment", {"body": comment})

    log.info("Ticket %s resolved", issue_key)
    del _open_tickets[alarm_key]
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Auto-Discovery: alle ALARMS unter allen ROOMS finden
# ─────────────────────────────────────────────────────────────────────────────

async def discover_alarm_nodes(client: Client, root_path: str, ns_idx: int) -> list[dict]:
    """
    Browst root_path/DataStructure/ROOMS/*/ALARMS/* und gibt eine Liste
    von { "node": Node, "key": "ROOM1.FEEDBACK_MOTOR1", "label": "FEEDBACK_MOTOR1",
          "room_path": ["ROOMS","ROOM1"], "room_id_node": Node|None } zurück.
    """
    alarms = []
    root = client.get_node(f"ns={ns_idx};s={root_path}.DataStructure.ROOMS")

    try:
        rooms = await root.get_children()
    except Exception as e:
        log.error("Could not browse ROOMS under %s: %s", root_path, e)
        return alarms

    for room_node in rooms:
        room_name = (await room_node.read_browse_name()).Name
        log.info("Found room: %s", room_name)

        # ALARMS Ordner suchen
        alarms_folder = None
        room_id_node = None
        for child in await room_node.get_children():
            child_name = (await child.read_browse_name()).Name
            if child_name == "ALARMS":
                alarms_folder = child
            elif child_name == "MISC":
                # ROOM_ID liegt unter MISC
                for misc_child in await child.get_children():
                    if (await misc_child.read_browse_name()).Name == "ROOM_ID":
                        room_id_node = misc_child

        if not alarms_folder:
            log.warning("No ALARMS folder in room %s", room_name)
            continue

        # Alle Alarm-Nodes unter ALARMS
        alarm_children = await alarms_folder.get_children()
        for alarm_node in alarm_children:
            alarm_name = (await alarm_node.read_browse_name()).Name
            alarm_key = f"{room_name}.{alarm_name}"
            try:
                val = await alarm_node.read_value()
                alarms.append({
                    "node": alarm_node,
                    "key": alarm_key,
                    "label": alarm_name,
                    "room_name": room_name,
                    "room_id_node": room_id_node,
                })
                log.info("  Alarm: [%s] = %s", alarm_key, val)
                await asyncio.sleep(0.1)  # Throttle reads for Elipse E3
            except Exception as e:
                log.warning("  Could not read alarm %s: %s", alarm_key, e)

    return alarms


async def read_context(client: Client, root_path: str, ns_idx: int,
                        room_id_node=None) -> dict:
    """Liest Kontext-Werte (CLIENT_ID, SERVER_DATETIME, COMM_PLC_QUALITY, ROOM_ID)."""
    context = {}
    ctx_nodes = {
        "client_id": f"ns={ns_idx};s={root_path}.DataStructure.CLIENT_ID",
        "server_datetime": f"ns={ns_idx};s={root_path}.DataStructure.SERVER_DATETIME",
        "comm_plc_quality": f"ns={ns_idx};s={root_path}.DataStructure.COMM_PLC_QUALITY",
    }
    for key, nodeid in ctx_nodes.items():
        try:
            node = client.get_node(nodeid)
            context[key] = await node.read_value()
        except Exception:
            context[key] = "n/a"

    if room_id_node:
        try:
            context["room_id"] = await room_id_node.read_value()
        except Exception:
            context["room_id"] = "n/a"

    return context


# ─────────────────────────────────────────────────────────────────────────────
# Subscription Handler
# ─────────────────────────────────────────────────────────────────────────────

class AutoAlarmHandler:
    def __init__(self, client, alarm_map, root_path, ns_idx, cfg):
        """
        alarm_map: { node_id_str: { "key", "label", "room_name", "room_id_node" } }
        """
        self.client     = client
        self.alarm_map  = alarm_map
        self.root_path  = root_path
        self.ns_idx     = ns_idx
        self.jira_cfg   = cfg.get("jira", {})
        self.alarm_cfg  = cfg.get("alarm", {})

    def datachange_notification(self, node, val, data):
        loop = asyncio.get_event_loop()
        node_id = str(node.nodeid) if hasattr(node, 'nodeid') else str(node)
        alarm_info = self.alarm_map.get(node_id)
        if not alarm_info:
            return

        trigger = self.alarm_cfg.get("trigger_value", True)
        alarm_key = alarm_info["key"]

        if val == trigger or (trigger is True and val is True):
            log.info("ALARM [%s] ACTIVE (Value: %s)", alarm_key, val)
            loop.create_task(self._on_alarm(alarm_info))
        else:
            log.info("ALARM [%s] RESOLVED (Value: %s)", alarm_key, val)
            if self.alarm_cfg.get("auto_resolve", True):
                loop.create_task(resolve_jira_ticket(alarm_key, self.alarm_cfg))

    async def _on_alarm(self, alarm_info):
        context = await read_context(
            self.client, self.root_path, self.ns_idx,
            alarm_info.get("room_id_node"),
        )
        priority = self.alarm_cfg.get("default_priority", "High")
        await create_jira_ticket(
            alarm_info["key"], alarm_info["label"], priority,
            context, self.jira_cfg, self.alarm_cfg,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Main Bridge
# ─────────────────────────────────────────────────────────────────────────────

async def run_bridge(config_path: str = "opcua_config_real_server.yaml"):
    cfg        = load_config(config_path)
    server_cfg = cfg.get("server", {})
    ns_cfg     = cfg.get("namespace", {})

    endpoint  = os.getenv("OPCUA_ENDPOINT") or server_cfg.get("endpoint")
    reconnect = server_cfg.get("reconnect_interval", 60)

    # Welche Root-Pfade browsen (z.B. ["SIMULATED", "SWEDLOG"])
    root_paths = cfg.get("root_paths", ["SIMULATED"])

    log.info("Bridge started - Endpoint: %s", endpoint)
    log.info("Root paths to scan: %s", root_paths)

    while True:
        try:
            client = Client(url=endpoint, timeout=30)
            client.session_timeout = 3600000  # 1 Stunde
            # Disable watchdog (Elipse E3 drops watchdog reads)
            # Set very high interval so it never fires
            client._watchdog_intervall = 999999

            auth_mode = server_cfg.get("auth_mode", "anonymous")
            if auth_mode == "username":
                username = os.getenv("OPCUA_USERNAME") or server_cfg.get("username", "")
                password = os.getenv("OPCUA_PASSWORD") or server_cfg.get("password", "")
                client.set_user(username)
                client.set_password(password)
                log.info("Auth: %s", username)

            log.info("Endpoint discovery...")
            endpoints = await client.connect_and_get_server_endpoints()
            log.info("Discovery OK: %d endpoint(s)", len(endpoints))

            async with client:
                log.info("Connected to OPC UA Server")
                ns_idx = ns_cfg.get("index", 2)
                log.info("Namespace index: %d", ns_idx)

                # Auto-discover all alarm nodes under each root path
                all_alarm_nodes = []
                alarm_map = {}  # { node_id_str: alarm_info }

                for root_path in root_paths:
                    log.info("Scanning %s ...", root_path)
                    discovered = await discover_alarm_nodes(client, root_path, ns_idx)
                    for alarm_info in discovered:
                        node = alarm_info["node"]
                        nid = str(node.nodeid)
                        alarm_info["root_path"] = root_path
                        alarm_map[nid] = alarm_info
                        all_alarm_nodes.append(node)

                if not all_alarm_nodes:
                    raise RuntimeError("No alarm nodes found!")

                log.info("Total alarm nodes discovered: %d", len(all_alarm_nodes))

                # Polling statt Subscription (Elipse E3 unterstützt keine Subscriptions)
                poll_interval = cfg.get("alarm", {}).get("poll_interval", 5)
                jira_cfg = cfg.get("jira", {})
                alarm_cfg = cfg.get("alarm", {})
                log.info("Polling %d alarm node(s) every %ds...", len(all_alarm_nodes), poll_interval)

                # Track previous state for edge detection
                prev_state: dict[str, bool] = {}

                while True:
                    for node in all_alarm_nodes:
                        nid = str(node.nodeid)
                        info = alarm_map[nid]
                        try:
                            val = await node.read_value()
                            alarm_key = info["key"]
                            prev = prev_state.get(alarm_key)

                            if val is True and prev is not True:
                                # Rising edge: alarm triggered
                                log.info("ALARM [%s] ACTIVE", alarm_key)
                                context = await read_context(
                                    client, info.get("root_path", root_paths[0]),
                                    ns_idx, info.get("room_id_node"),
                                )
                                await create_jira_ticket(
                                    alarm_key, info["label"],
                                    alarm_cfg.get("default_priority", "High"),
                                    context, jira_cfg, alarm_cfg,
                                )
                            elif val is False and prev is True:
                                # Falling edge: alarm resolved
                                log.info("ALARM [%s] RESOLVED", alarm_key)
                                if alarm_cfg.get("auto_resolve", True):
                                    await resolve_jira_ticket(alarm_key, alarm_cfg)

                            prev_state[alarm_key] = val
                        except Exception as e:
                            log.warning("Could not read %s: %s", info["key"], e)
                        await asyncio.sleep(0.05)  # Small delay between reads

                    await asyncio.sleep(poll_interval)

        except Exception as e:
            log.error("Connection error: %s - Retry in %ds...", e, reconnect)
            await asyncio.sleep(reconnect)


if __name__ == "__main__":
    if platform.system() == "Windows":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    config_file = sys.argv[1] if len(sys.argv) > 1 else "opcua_config_real_server.yaml"
    asyncio.run(run_bridge(config_file))
