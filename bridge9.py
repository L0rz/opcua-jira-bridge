"""
OPC UA → Jira Bridge v9
Based on test_multi.py pattern (proven to work).
First run: discover + poll. Subsequent runs: poll only.
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


async def create_jira_ticket(alarm_key, alarm_label, priority, context, jira_cfg, alarm_cfg):
    cooldown = alarm_cfg.get("dedup_cooldown", 300)
    if _is_duplicate(alarm_key, cooldown):
        return None

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    client_id = context.get("client_id", "n/a")
    room_id   = context.get("room", "n/a")

    summary = jira_cfg.get("summary_template", "[OPC UA] {client_id} / {room_id} - {alarm_label}").format(
        alarm_key=alarm_key, alarm_label=alarm_label, client_id=client_id, room_id=room_id,
    )[:255]

    rows = f"||Parameter||Value||\n|Timestamp|{ts}|\n|Alarm Key|{alarm_key}|\n"
    for k, v in context.items():
        rows += f"|{k}|{v}|\n"

    payload = {
        "fields": {
            "project": {"key": JIRA_PROJECT}, "summary": summary,
            "description": f"*Automatically created by OPC UA to Jira Bridge*\n\n*Alarm:* {alarm_label}\n\n{rows}",
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

    ticket = {"key": data["key"], "alarm_key": alarm_key, "url": f"{JIRA_URL}/browse/{data['key']}"}
    _open_tickets[alarm_key] = ticket
    log.info("Ticket: %s -> %s", data["key"], ticket["url"])
    return ticket


async def resolve_jira_ticket(alarm_key, alarm_cfg):
    ticket = _open_tickets.get(alarm_key)
    if not ticket:
        return
    issue_key = ticket["key"]
    tid = str(alarm_cfg.get("resolve_transition_id", "5"))
    comment = alarm_cfg.get("resolve_comment", "Alarm automatically resolved.")
    await _jira_post(f"/rest/api/2/issue/{issue_key}/transitions", {"transition": {"id": tid}})
    await _jira_post(f"/rest/api/2/issue/{issue_key}/comment", {"body": comment})
    log.info("Resolved: %s", issue_key)
    del _open_tickets[alarm_key]


async def run_bridge(config_path):
    cfg = load_config(config_path)
    server_cfg = cfg.get("server", {})
    alarm_cfg  = cfg.get("alarm", {})
    jira_cfg   = cfg.get("jira", {})
    root_paths = cfg.get("root_paths", ["SIMULATED"])
    poll_interval = alarm_cfg.get("poll_interval", 30)
    endpoint   = os.getenv("OPCUA_ENDPOINT") or server_cfg.get("endpoint")
    ns_idx     = cfg.get("namespace", {}).get("index", 2)

    log.info("Bridge v9 - Endpoint: %s - Poll: %ds", endpoint, poll_interval)

    # ── Step 1: Discover alarm node IDs (one-time) ──────────────────────────
    alarm_nodeids = []
    alarm_keys = []
    alarm_labels = []
    alarm_rooms = []

    while not alarm_nodeids:
        try:
            # EXACTLY like test_multi: fresh client, connect, work, disconnect
            client = Client(url=endpoint, timeout=10)
            client.session_timeout = 30000
            client._watchdog_intervall = 999999
            client.set_user(server_cfg.get("username", "OPC"))
            client.set_password(server_cfg.get("password", "OPC"))
            await client.connect()
            log.info("Connected for discovery")

            for rp in root_paths:
                rooms_node = client.get_node(f"ns={ns_idx};s={rp}.DataStructure.ROOMS")
                for room in await rooms_node.get_children():
                    room_name = (await room.read_browse_name()).Name
                    for child in await room.get_children():
                        if (await child.read_browse_name()).Name == "ALARMS":
                            for alarm in await child.get_children():
                                aname = (await alarm.read_browse_name()).Name
                                nid = alarm.nodeid
                                alarm_nodeids.append(f"ns={nid.NamespaceIndex};s={nid.Identifier}")
                                alarm_keys.append(f"{room_name}.{aname}")
                                alarm_labels.append(aname)
                                alarm_rooms.append(room_name)

            await client.disconnect()
            log.info("Discovery done: %d alarms. Disconnected.", len(alarm_nodeids))

        except Exception as e:
            log.error("Discovery failed: %s - retry 60s", e)
            try: await client.disconnect()
            except: pass
            await asyncio.sleep(60)

    # ── Step 2: Poll loop (EXACTLY like test_multi) ─────────────────────────
    prev_state: dict[str, bool] = {}
    poll_count = 0
    consecutive_errors = 0

    log.info("Starting poll loop...")
    await asyncio.sleep(10)  # Wait before first poll (like test_multi)

    while True:
        try:
            # EXACTLY like test_multi: fresh client each time
            client = Client(url=endpoint, timeout=10)
            client.session_timeout = 30000
            client._watchdog_intervall = 999999
            client.set_user(server_cfg.get("username", "OPC"))
            client.set_password(server_cfg.get("password", "OPC"))

            await client.connect()
            nodes = [client.get_node(nid) for nid in alarm_nodeids]
            values = await client.read_values(nodes)
            await client.disconnect()

            # Let socket fully close before doing ANYTHING else
            await asyncio.sleep(2)

            consecutive_errors = 0
            poll_count += 1

            # Process AFTER disconnect + cleanup
            for i, val in enumerate(values):
                prev = prev_state.get(alarm_keys[i])
                if val is True and prev is not True:
                    log.info("ALARM [%s] ACTIVE", alarm_keys[i])
                    ctx = {"room": alarm_rooms[i]}
                    await create_jira_ticket(alarm_keys[i], alarm_labels[i],
                        alarm_cfg.get("default_priority", "High"), ctx, jira_cfg, alarm_cfg)
                elif val is False and prev is True:
                    log.info("ALARM [%s] RESOLVED", alarm_keys[i])
                    if alarm_cfg.get("auto_resolve", True):
                        await resolve_jira_ticket(alarm_keys[i], alarm_cfg)
                prev_state[alarm_keys[i]] = val

            if poll_count % 20 == 0:
                log.info("Poll #%d OK", poll_count)

        except Exception as e:
            consecutive_errors += 1
            wait = min(60 * consecutive_errors, 300)
            log.error("Poll error: %s - retry %ds (#%d)", e, wait, consecutive_errors)
            try: await client.disconnect()
            except: pass
            await asyncio.sleep(wait)
            continue

        await asyncio.sleep(poll_interval)


if __name__ == "__main__":
    if platform.system() == "Windows":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    cfg_file = sys.argv[1] if len(sys.argv) > 1 else "opcua_config_real_server.yaml"
    asyncio.run(run_bridge(cfg_file))
