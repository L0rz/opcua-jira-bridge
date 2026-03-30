"""
OPC UA → Jira Bridge v7
- Polling statt Subscriptions (Elipse E3 kompatibel)
- Auto-Discovery aller ROOMS/*/ALARMS/*
- Edge-Detection (nur bei Zustandswechsel reagieren)
- Kein Test-Read bei Discovery
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
    with open(Path(config_path)) as f:
        return yaml.safe_load(f)


def _is_duplicate(alarm_key: str, cooldown: int) -> bool:
    now = time.time()
    if now - _recent_alarms.get(alarm_key, 0) < cooldown:
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
        log.error("Jira error: %s", e)
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

    summary = jira_cfg.get("summary_template", "[OPC UA] {client_id} / {room_id} - {alarm_label}").format(
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
            "project": {"key": JIRA_PROJECT}, "summary": summary,
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

    data = await _jira_post("/rest/api/2/issue", payload)
    if not data:
        return None

    ticket = {
        "key": data["key"], "alarm_key": alarm_key,
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
    issue_key = ticket["key"]
    tid = str(alarm_cfg.get("resolve_transition_id", "5"))
    comment = alarm_cfg.get("resolve_comment", "Alarm automatically resolved: OPC UA Node returned to FALSE.")
    await _jira_post(f"/rest/api/2/issue/{issue_key}/transitions", {"transition": {"id": tid}})
    await _jira_post(f"/rest/api/2/issue/{issue_key}/comment", {"body": comment})
    log.info("Ticket %s resolved", issue_key)
    del _open_tickets[alarm_key]
    return True


async def discover_alarms(client: Client, root_path: str, ns_idx: int) -> list[dict]:
    """Browse ROOMS/*/ALARMS/* — NO reads, only browse."""
    alarms = []
    rooms_node = client.get_node(f"ns={ns_idx};s={root_path}.DataStructure.ROOMS")
    try:
        rooms = await rooms_node.get_children()
    except Exception as e:
        log.error("Could not browse ROOMS: %s", e)
        return alarms

    for room in rooms:
        room_name = (await room.read_browse_name()).Name
        log.info("Room: %s", room_name)
        alarms_folder = None
        room_id_node = None
        for child in await room.get_children():
            name = (await child.read_browse_name()).Name
            if name == "ALARMS":
                alarms_folder = child
            elif name == "MISC":
                for mc in await child.get_children():
                    if (await mc.read_browse_name()).Name == "ROOM_ID":
                        room_id_node = mc
        if not alarms_folder:
            continue
        for alarm_node in await alarms_folder.get_children():
            alarm_name = (await alarm_node.read_browse_name()).Name
            alarms.append({
                "node": alarm_node,
                "key": f"{room_name}.{alarm_name}",
                "label": alarm_name,
                "room_name": room_name,
                "room_id_node": room_id_node,
                "root_path": root_path,
            })
            log.info("  -> %s", alarm_name)
    return alarms


async def read_context(client: Client, root_path: str, ns_idx: int, room_id_node=None) -> dict:
    ctx = {}
    for key, suffix in [("client_id", "CLIENT_ID"), ("server_datetime", "SERVER_DATETIME"),
                         ("comm_plc_quality", "COMM_PLC_QUALITY")]:
        try:
            ctx[key] = await client.get_node(f"ns={ns_idx};s={root_path}.DataStructure.{suffix}").read_value()
        except:
            ctx[key] = "n/a"
    if room_id_node:
        try:
            ctx["room_id"] = await room_id_node.read_value()
        except:
            ctx["room_id"] = "n/a"
    return ctx


async def run_bridge(config_path: str = "opcua_config_real_server.yaml"):
    cfg = load_config(config_path)
    server_cfg = cfg.get("server", {})
    ns_cfg     = cfg.get("namespace", {})
    alarm_cfg  = cfg.get("alarm", {})
    jira_cfg   = cfg.get("jira", {})
    root_paths = cfg.get("root_paths", ["SIMULATED"])
    reconnect  = server_cfg.get("reconnect_interval", 60)
    poll_interval = alarm_cfg.get("poll_interval", 5)
    endpoint   = os.getenv("OPCUA_ENDPOINT") or server_cfg.get("endpoint")

    log.info("Bridge v7 started - Endpoint: %s", endpoint)
    log.info("Mode: POLLING every %ds", poll_interval)
    log.info("Root paths: %s", root_paths)

    while True:
        try:
            client = Client(url=endpoint, timeout=30)
            client.session_timeout = 3600000
            client._watchdog_intervall = 999999

            if server_cfg.get("auth_mode") == "username":
                u = os.getenv("OPCUA_USERNAME") or server_cfg.get("username", "")
                p = os.getenv("OPCUA_PASSWORD") or server_cfg.get("password", "")
                client.set_user(u)
                client.set_password(p)
                log.info("Auth: %s", u)

            # Discovery
            endpoints = await client.connect_and_get_server_endpoints()
            log.info("Discovery: %d endpoint(s)", len(endpoints))

            async with client:
                log.info("Connected")
                ns_idx = ns_cfg.get("index", 2)

                # Discover alarm nodes (browse only, no reads)
                all_alarms = []
                for rp in root_paths:
                    all_alarms.extend(await discover_alarms(client, rp, ns_idx))

                if not all_alarms:
                    raise RuntimeError("No alarm nodes found!")

                log.info("Discovered %d alarm nodes — starting poll loop", len(all_alarms))

                # Polling loop with edge detection
                prev_state: dict[str, bool] = {}

                while True:
                    for info in all_alarms:
                        node = info["node"]
                        alarm_key = info["key"]
                        try:
                            val = await node.read_value()
                            prev = prev_state.get(alarm_key)

                            if val is True and prev is not True:
                                log.info("ALARM [%s] ACTIVE", alarm_key)
                                context = await read_context(
                                    client, info["root_path"], ns_idx, info.get("room_id_node"))
                                await create_jira_ticket(
                                    alarm_key, info["label"],
                                    alarm_cfg.get("default_priority", "High"),
                                    context, jira_cfg, alarm_cfg)

                            elif val is False and prev is True:
                                log.info("ALARM [%s] RESOLVED", alarm_key)
                                if alarm_cfg.get("auto_resolve", True):
                                    await resolve_jira_ticket(alarm_key, alarm_cfg)

                            prev_state[alarm_key] = val
                        except Exception as e:
                            log.warning("Read error [%s]: %s", alarm_key, e)
                            break  # Exit inner loop, will trigger reconnect
                        await asyncio.sleep(0.05)

                    await asyncio.sleep(poll_interval)

        except Exception as e:
            log.error("Connection error: %s — Retry in %ds", e, reconnect)
            await asyncio.sleep(reconnect)


if __name__ == "__main__":
    if platform.system() == "Windows":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    config_file = sys.argv[1] if len(sys.argv) > 1 else "opcua_config_real_server.yaml"
    asyncio.run(run_bridge(config_file))
