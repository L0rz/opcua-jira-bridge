"""
OPC UA → Jira Bridge v8
- Connect-per-poll: connect → batch read → disconnect → wait → repeat
- No persistent session needed (works with short server timeouts)
- Caches discovered alarm node IDs after first browse
"""
import asyncio
import logging
import os
import sys
import time
import platform
from datetime import datetime, timezone
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

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    client_id = context.get("client_id", "n/a")
    room_id   = context.get("room_id", "n/a")

    summary = jira_cfg.get("summary_template", "[OPC UA] {client_id} / {room_id} - {alarm_label}").format(
        alarm_key=alarm_key, alarm_label=alarm_label, client_id=client_id, room_id=room_id,
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


async def quick_connect(endpoint: str, server_cfg: dict) -> Client:
    """Connect via discovery handshake, return client. Caller must disconnect."""
    client = Client(url=endpoint, timeout=10)
    client.session_timeout = 30000
    client._watchdog_intervall = 999999
    if server_cfg.get("auth_mode") == "username":
        u = os.getenv("OPCUA_USERNAME") or server_cfg.get("username", "")
        p = os.getenv("OPCUA_PASSWORD") or server_cfg.get("password", "")
        client.set_user(u)
        client.set_password(p)
    await client.connect()
    return client


async def discover_alarms(client: Client, root_path: str, ns_idx: int) -> list[dict]:
    """Browse ROOMS/*/ALARMS/* — returns list with nodeid strings."""
    alarms = []
    rooms_node = client.get_node(f"ns={ns_idx};s={root_path}.DataStructure.ROOMS")
    try:
        rooms = await rooms_node.get_children()
    except Exception as e:
        log.error("Cannot browse ROOMS: %s", e)
        return alarms

    for room in rooms:
        room_name = (await room.read_browse_name()).Name
        alarms_folder = None
        room_id_nodeid = None
        for child in await room.get_children():
            name = (await child.read_browse_name()).Name
            if name == "ALARMS":
                alarms_folder = child
            elif name == "MISC":
                for mc in await child.get_children():
                    if (await mc.read_browse_name()).Name == "ROOM_ID":
                        rid = mc.nodeid
                        room_id_nodeid = f"ns={rid.NamespaceIndex};s={rid.Identifier}"
        if not alarms_folder:
            continue
        for alarm_node in await alarms_folder.get_children():
            alarm_name = (await alarm_node.read_browse_name()).Name
            nid = alarm_node.nodeid
            alarms.append({
                "nodeid": f"ns={nid.NamespaceIndex};s={nid.Identifier}",
                "key": f"{room_name}.{alarm_name}",
                "label": alarm_name,
                "room_name": room_name,
                "room_id_nodeid": room_id_nodeid,
                "root_path": root_path,
            })
        log.info("Room %s: %d alarms", room_name, sum(1 for a in alarms if a["room_name"] == room_name))
    return alarms


async def run_bridge(config_path: str = "opcua_config_real_server.yaml"):
    cfg = load_config(config_path)
    server_cfg = cfg.get("server", {})
    ns_cfg     = cfg.get("namespace", {})
    alarm_cfg  = cfg.get("alarm", {})
    jira_cfg   = cfg.get("jira", {})
    root_paths = cfg.get("root_paths", ["SIMULATED"])
    poll_interval = alarm_cfg.get("poll_interval", 5)
    endpoint   = os.getenv("OPCUA_ENDPOINT") or server_cfg.get("endpoint")
    ns_idx     = ns_cfg.get("index", 2)

    log.info("Bridge v8 started - Endpoint: %s", endpoint)
    log.info("Mode: CONNECT-PER-POLL every %ds", poll_interval)

    # Phase 1: Discover alarm nodes (one-time)
    cached_alarms: list[dict] = []
    while not cached_alarms:
        try:
            log.info("Discovering alarm nodes...")
            client = await quick_connect(endpoint, server_cfg)
            try:
                for rp in root_paths:
                    cached_alarms.extend(await discover_alarms(client, rp, ns_idx))
            finally:
                await client.disconnect()

            if not cached_alarms:
                log.warning("No alarms found, retrying in 60s...")
                await asyncio.sleep(60)
        except Exception as e:
            log.error("Discovery failed: %s — retry in 60s", e)
            await asyncio.sleep(60)

    log.info("Discovered %d alarm nodes. Starting poll loop.", len(cached_alarms))

    # Build nodeid lists
    alarm_nodeids = [a["nodeid"] for a in cached_alarms]
    alarm_keys    = [a["key"] for a in cached_alarms]

    # Context node IDs (for Jira ticket content)
    context_nodeids: dict[str, dict[str, str]] = {}
    for a in cached_alarms:
        rp = a["root_path"]
        if rp not in context_nodeids:
            context_nodeids[rp] = {
                "client_id":      f"ns={ns_idx};s={rp}.DataStructure.CLIENT_ID",
                "server_datetime": f"ns={ns_idx};s={rp}.DataStructure.SERVER_DATETIME",
                "comm_plc_quality": f"ns={ns_idx};s={rp}.DataStructure.COMM_PLC_QUALITY",
            }

    # Phase 2: Poll loop
    prev_state: dict[str, bool] = {}
    poll_count = 0
    consecutive_errors = 0

    while True:
        try:
            # Quick connect
            client = await quick_connect(endpoint, server_cfg)
            try:
                # Batch read all alarm nodes in ONE request
                nodes = [client.get_node(nid) for nid in alarm_nodeids]
                values = await client.read_values(nodes)
            finally:
                # Always disconnect cleanly
                try:
                    await client.disconnect()
                except:
                    pass

            consecutive_errors = 0
            poll_count += 1

            # Process values AFTER disconnect (all sequential, no background tasks)
            for i, val in enumerate(values):
                alarm_key = alarm_keys[i]
                prev = prev_state.get(alarm_key)

                if val is True and prev is not True:
                    log.info("ALARM [%s] ACTIVE", alarm_key)
                    context = {
                        "alarm_key": alarm_key,
                        "room": cached_alarms[i].get("room_name", "n/a"),
                        "root_path": cached_alarms[i].get("root_path", "n/a"),
                    }
                    await create_jira_ticket(alarm_key, cached_alarms[i]["label"],
                        alarm_cfg.get("default_priority", "High"),
                        context, jira_cfg, alarm_cfg)

                elif val is False and prev is True:
                    log.info("ALARM [%s] RESOLVED", alarm_key)
                    if alarm_cfg.get("auto_resolve", True):
                        await resolve_jira_ticket(alarm_key, alarm_cfg)

                prev_state[alarm_key] = val

            if poll_count % 20 == 0:
                log.info("Poll #%d OK (%d nodes)", poll_count, len(alarm_nodeids))

        except Exception as e:
            consecutive_errors += 1
            wait = min(60 * consecutive_errors, 300)  # Exponential backoff, max 5 min
            log.error("Poll error: %s — retry in %ds (errors: %d)", e, wait, consecutive_errors)
            await asyncio.sleep(wait)
            continue

        await asyncio.sleep(poll_interval)


async def _create_ticket_with_context(endpoint, server_cfg, alarm_info, ns_idx, context_nodeids, jira_cfg, alarm_cfg):
    """Background: connect, read context, create ticket."""
    context = {}
    try:
        client = await quick_connect(endpoint, server_cfg)
        try:
            rp = alarm_info["root_path"]
            for key, nid in context_nodeids.get(rp, {}).items():
                try:
                    context[key] = await client.get_node(nid).read_value()
                except:
                    context[key] = "n/a"
            if alarm_info.get("room_id_nodeid"):
                try:
                    context["room_id"] = await client.get_node(alarm_info["room_id_nodeid"]).read_value()
                except:
                    context["room_id"] = "n/a"
        finally:
            try:
                await client.disconnect()
            except:
                pass
    except Exception as e:
        log.warning("Could not read context: %s", e)

    await create_jira_ticket(
        alarm_info["key"], alarm_info["label"],
        alarm_cfg.get("default_priority", "High"),
        context, jira_cfg, alarm_cfg)


if __name__ == "__main__":
    if platform.system() == "Windows":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    config_file = sys.argv[1] if len(sys.argv) > 1 else "opcua_config_real_server.yaml"
    asyncio.run(run_bridge(config_file))
