"""
OPC UA → Jira Bridge
Exakt gleiches Connect-Pattern wie test_verbose.py (das funktioniert hat)
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
        raise FileNotFoundError(f"Config nicht gefunden: {config_path}")
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
                f"{JIRA_URL}{path}",
                json=payload,
                auth=(JIRA_USER, JIRA_API_TOKEN),
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json() if resp.content else {}
    except httpx.HTTPStatusError as e:
        log.error("Jira API %d: %s", e.response.status_code, e.response.text[:300])
    except Exception as e:
        log.error("Jira Verbindung: %s", e)
    return None


async def create_jira_ticket(alarm_cfg: dict, context: dict, jira_cfg: dict, alarm_cfg_global: dict) -> dict | None:
    alarm_key   = alarm_cfg["key"]
    alarm_label = alarm_cfg["label"]
    alarm_desc  = alarm_cfg.get("description", alarm_label)
    priority    = alarm_cfg.get("priority", "Medium")

    cooldown = alarm_cfg_global.get("dedup_cooldown", 300)
    if _is_duplicate(alarm_key, cooldown):
        log.info("Dedup: Kein neues Ticket für %s", alarm_key)
        return None

    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    client_id = context.get("client_id", "n/a")
    room_id   = context.get("room_id", "n/a")

    summary_tpl = jira_cfg.get("summary_template", "[OPC UA] {client_id} / {room_id} — {alarm_label}")
    summary = summary_tpl.format(
        alarm_key=alarm_key, alarm_label=alarm_label,
        client_id=client_id, room_id=room_id,
    )[:255]

    rows = f"||Parameter||Value||\n|Timestamp|{ts}|\n|Alarm Key|{alarm_key}|\n"
    for k, v in context.items():
        rows += f"|{k}|{v}|\n"

    description = (
        f"*Automatically created by OPC UA to Jira Bridge*\n\n"
        f"*Alarm:* {alarm_desc}\n\n{rows}\n"
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
    log.info("✅ Ticket erstellt: %s → %s", data["key"], ticket["url"])
    return ticket


async def resolve_jira_ticket(alarm_key: str, alarm_cfg_global: dict) -> bool:
    ticket = _open_tickets.get(alarm_key)
    if not ticket:
        return False

    transition_id = str(alarm_cfg_global.get("resolve_transition_id", "5"))
    comment = alarm_cfg_global.get("resolve_comment", "Alarm automatically resolved.")
    issue_key = ticket["key"]

    await _jira_post(f"/rest/api/2/issue/{issue_key}/transitions", {"transition": {"id": transition_id}})
    await _jira_post(f"/rest/api/2/issue/{issue_key}/comment", {"body": comment})

    log.info("✅ Ticket %s auf 'Gelöst' gesetzt", issue_key)
    del _open_tickets[alarm_key]
    return True


class MultiAlarmHandler:
    def __init__(self, client, node_to_alarm, context_nodes, cfg):
        self.client        = client
        self.node_to_alarm = node_to_alarm
        self.context_nodes = context_nodes
        self.jira_cfg      = cfg.get("jira", {})
        self.alarm_cfg     = cfg.get("alarm", {})

    def datachange_notification(self, node, val, data):
        loop = asyncio.get_event_loop()
        node_id = str(node.nodeid) if hasattr(node, 'nodeid') else str(node)
        alarm_def = self.node_to_alarm.get(node_id)
        if not alarm_def:
            return

        trigger = self.alarm_cfg.get("trigger_value", True)
        alarm_key = alarm_def["key"]

        if val == trigger or (trigger is True and val is True):
            log.info("🚨 ALARM [%s] aktiv (Wert: %s)", alarm_key, val)
            loop.create_task(self._on_alarm(alarm_def))
        else:
            log.info("✅ ALARM [%s] gelöst (Wert: %s)", alarm_key, val)
            if self.alarm_cfg.get("auto_resolve", True):
                loop.create_task(resolve_jira_ticket(alarm_key, self.alarm_cfg))

    async def _on_alarm(self, alarm_def):
        context = {}
        for key, node_obj in self.context_nodes.items():
            try:
                context[key] = await node_obj.read_value()
            except Exception as e:
                context[key] = "n/a"
        await create_jira_ticket(alarm_def, context, self.jira_cfg, self.alarm_cfg)


async def run_bridge(config_path: str = "opcua_config_real_server.yaml"):
    cfg        = load_config(config_path)
    server_cfg = cfg.get("server", {})
    ns_cfg     = cfg.get("namespace", {})
    alarms_cfg = cfg.get("alarms", [])
    ctx_cfg    = cfg.get("context_nodes", [])

    endpoint  = os.getenv("OPCUA_ENDPOINT") or server_cfg.get("endpoint")
    reconnect = server_cfg.get("reconnect_interval", 30)

    log.info("Bridge gestartet — Endpoint: %s", endpoint)

    while True:
        try:
            # EXAKT wie test_verbose.py — das funktioniert nachweislich
            client = Client(url=endpoint, timeout=15)
            client.session_timeout = 60000

            auth_mode = server_cfg.get("auth_mode", "anonymous")
            if auth_mode == "username":
                username = os.getenv("OPCUA_USERNAME") or server_cfg.get("username", "")
                password = os.getenv("OPCUA_PASSWORD") or server_cfg.get("password", "")
                client.set_user(username)
                client.set_password(password)
                log.info("Auth: %s", username)

            # Watchdog deaktivieren (Elipse E3 Server trennt die Watchdog-Verbindung)
            client.watchdog_intervall = 0  # Disable

            log.info("Endpoint-Discovery...")
            endpoints = await client.connect_and_get_server_endpoints()
            log.info("Discovery OK: %d Endpoint(s)", len(endpoints))

            log.info("Verbinde...")
            async with client:
                log.info("✅ Verbunden mit OPC UA Server")

                ns_idx = 2  # Bekannt aus Screenshot
                log.info("Namespace-Index: %d", ns_idx)

                # Alarm-Nodes
                alarm_nodes = {}
                alarm_node_objs = []
                for alarm_def in alarms_cfg:
                    try:
                        node = client.get_node(alarm_def["nodeid"])
                        nid = str(node.nodeid)
                        alarm_nodes[nid] = alarm_def
                        alarm_node_objs.append(node)
                        # Test-Read
                        val = await node.read_value()
                        log.info("Alarm-Node [%s] → %s (aktuell: %s)", alarm_def["key"], nid, val)
                    except Exception as e:
                        log.error("Alarm-Node [%s] FEHLER: %s", alarm_def.get("key"), e)

                if not alarm_node_objs:
                    raise RuntimeError("Keine Alarm-Nodes gefunden!")

                # Kontext-Nodes
                context_nodes = {}
                for ctx in ctx_cfg:
                    try:
                        node = client.get_node(ctx["nodeid"])
                        val = await node.read_value()
                        context_nodes[ctx["key"]] = node
                        log.info("Kontext-Node [%s] = %s", ctx["key"], val)
                    except Exception as e:
                        log.warning("Kontext-Node [%s] FEHLER: %s", ctx.get("key"), e)

                # Subscription
                handler = MultiAlarmHandler(client, alarm_nodes, context_nodes, cfg)
                sub = await client.create_subscription(500, handler)
                await sub.subscribe_data_change(alarm_node_objs)
                log.info("🎯 Subscribed auf %d Alarm-Node(s) — warte auf Alarme...", len(alarm_node_objs))

                while True:
                    await asyncio.sleep(1)

        except Exception as e:
            log.error("Verbindungsfehler: %s — Retry in %ds...", e, reconnect)
            await asyncio.sleep(reconnect)


if __name__ == "__main__":
    if platform.system() == "Windows":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    config_file = sys.argv[1] if len(sys.argv) > 1 else "opcua_config_real_server.yaml"
    asyncio.run(run_bridge(config_file))
