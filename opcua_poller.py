"""
opcua_poller.py — OPC UA Alarm Poller (NO Jira/httpx)

Connects to OPC UA server, discovers alarm nodes, polls them on an interval,
and writes state changes to SQLite (shared with jira_worker.py).
"""

import asyncio
import logging
import platform
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from asyncua import Client, ua

# ── Windows event loop fix ────────────────────────────────────────────────────
if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [POLLER] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("poller")

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "opcua_config_real_server.yaml"
DB_FILE = BASE_DIR / "alarms.db"


# ── DB helpers ────────────────────────────────────────────────────────────────

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS alarm_nodes (
            id           INTEGER PRIMARY KEY,
            nodeid       TEXT UNIQUE NOT NULL,
            alarm_key    TEXT NOT NULL,
            label        TEXT NOT NULL,
            room_name    TEXT,
            root_path    TEXT,
            discovered_at TEXT
        );

        CREATE TABLE IF NOT EXISTS alarm_events (
            id           INTEGER PRIMARY KEY,
            alarm_key    TEXT NOT NULL,
            value        BOOLEAN NOT NULL,
            status       TEXT DEFAULT 'new',
            ticket_key   TEXT,
            created_at   TEXT NOT NULL,
            processed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS alarm_state (
            alarm_key       TEXT PRIMARY KEY,
            current_value   BOOLEAN,
            last_changed    TEXT,
            open_ticket_key TEXT
        );
    """)
    conn.commit()


def load_cached_nodes(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM alarm_nodes").fetchall()
    return [dict(r) for r in rows]


def save_nodes(conn: sqlite3.Connection, nodes: list[dict]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """INSERT INTO alarm_nodes (nodeid, alarm_key, label, room_name, root_path, discovered_at)
           VALUES (:nodeid, :alarm_key, :label, :room_name, :root_path, :discovered_at)
           ON CONFLICT(nodeid) DO UPDATE SET
               alarm_key=excluded.alarm_key,
               label=excluded.label,
               room_name=excluded.room_name,
               root_path=excluded.root_path""",
        [{**n, "discovered_at": now} for n in nodes],
    )
    conn.commit()
    log.info("Cached %d alarm nodes in DB", len(nodes))


def get_current_state(conn: sqlite3.Connection, alarm_key: str) -> bool | None:
    row = conn.execute(
        "SELECT current_value FROM alarm_state WHERE alarm_key=?", (alarm_key,)
    ).fetchone()
    if row is None:
        return None
    val = row["current_value"]
    if val is None:
        return None
    return bool(val)


def write_state_change(
    conn: sqlite3.Connection,
    alarm_key: str,
    new_value: bool,
    open_ticket_key: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()

    # Update alarm_state
    conn.execute(
        """INSERT INTO alarm_state (alarm_key, current_value, last_changed, open_ticket_key)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(alarm_key) DO UPDATE SET
               current_value=excluded.current_value,
               last_changed=excluded.last_changed""",
        (alarm_key, new_value, now, open_ticket_key),
    )

    # Determine event status
    if new_value:
        # Alarm triggered → new event for jira_worker
        status = "new"
        conn.execute(
            "INSERT INTO alarm_events (alarm_key, value, status, created_at) VALUES (?,?,?,?)",
            (alarm_key, True, status, now),
        )
        log.info("ALARM ON  : %s → new event inserted", alarm_key)
    else:
        # Alarm cleared → mark open ticket as resolved_pending if exists
        open_row = conn.execute(
            "SELECT id, ticket_key FROM alarm_events "
            "WHERE alarm_key=? AND status='ticket_created' "
            "ORDER BY id DESC LIMIT 1",
            (alarm_key,),
        ).fetchone()

        if open_row:
            conn.execute(
                "UPDATE alarm_events SET status='resolved_pending' WHERE id=?",
                (open_row["id"],),
            )
            log.info("ALARM OFF : %s → resolved_pending (ticket %s)", alarm_key, open_row["ticket_key"])
        else:
            # No open ticket — insert resolved event directly (ignored by jira_worker)
            conn.execute(
                "INSERT INTO alarm_events (alarm_key, value, status, created_at) VALUES (?,?,?,?)",
                (alarm_key, False, "ignored", now),
            )
            log.info("ALARM OFF : %s → no open ticket, ignored", alarm_key)

    conn.commit()


# ── OPC UA discovery ──────────────────────────────────────────────────────────

async def discover_alarm_nodes(
    client: Client,
    root_paths: list[str],
    ns_index: int,
) -> list[dict]:
    """Browse ROOMS/*/ALARMS/* under each root_path and return node descriptors."""
    discovered: list[dict] = []

    for root_path in root_paths:
        log.info("Browsing root path: %s", root_path)
        try:
            root_node = await client.nodes.root.get_child(
                ["0:Objects", f"{ns_index}:{root_path}"]
            )
        except Exception as exc:
            log.warning("Root path '%s' not found: %s", root_path, exc)
            continue

        # Browse ROOMS
        try:
            rooms_node = await root_node.get_child(f"{ns_index}:ROOMS")
        except Exception as exc:
            log.warning("No ROOMS under '%s': %s", root_path, exc)
            continue

        room_children = await rooms_node.get_children()
        for room_node in room_children:
            room_name = (await room_node.read_browse_name()).Name
            log.debug("  Room: %s", room_name)

            # Browse ALARMS under room
            try:
                alarms_node = await room_node.get_child(f"{ns_index}:ALARMS")
            except Exception:
                log.debug("  No ALARMS under room %s", room_name)
                continue

            alarm_children = await alarms_node.get_children()
            for alarm_node in alarm_children:
                alarm_name = (await alarm_node.read_browse_name()).Name
                nodeid = alarm_node.nodeid.to_string()
                alarm_key = f"{room_name}.{alarm_name}"

                discovered.append(
                    {
                        "nodeid": nodeid,
                        "alarm_key": alarm_key,
                        "label": alarm_name,
                        "room_name": room_name,
                        "root_path": root_path,
                    }
                )
                log.debug("    Alarm: %s → %s", alarm_key, nodeid)

    log.info("Discovered %d alarm nodes across %d root path(s)", len(discovered), len(root_paths))
    return discovered


# ── Single poll cycle ─────────────────────────────────────────────────────────

async def poll_once(
    endpoint: str,
    username: str,
    password: str,
    alarm_nodes: list[dict],
    conn: sqlite3.Connection,
) -> None:
    """Connect, batch-read all alarm values, disconnect, write changes to DB."""
    alarm_nodeids = [n["nodeid"] for n in alarm_nodes]
    node_map = {n["nodeid"]: n for n in alarm_nodes}

    client = Client(url=endpoint, timeout=10)
    client.session_timeout = 30000
    client._watchdog_intervall = 999999  # noqa: SLF001 — intentional E3 quirk
    client.set_user(username)
    client.set_password(password)

    try:
        await client.connect()
        log.debug("Connected to %s", endpoint)

        nodes = [client.get_node(nid) for nid in alarm_nodeids]
        values = await client.read_values(nodes)

        await client.disconnect()
        await asyncio.sleep(2)  # Let socket fully close (Elipse E3 requirement)

    except Exception as exc:
        log.error("OPC UA poll failed: %s", exc)
        try:
            await client.disconnect()
        except Exception:
            pass
        await asyncio.sleep(2)
        return

    # Process values
    changes = 0
    for nodeid, value in zip(alarm_nodeids, values):
        node_info = node_map[nodeid]
        alarm_key = node_info["alarm_key"]

        # Coerce to bool, skip None/bad values
        if value is None:
            continue
        try:
            bool_value = bool(value)
        except Exception:
            log.warning("Cannot convert value for %s: %r", alarm_key, value)
            continue

        prev = get_current_state(conn, alarm_key)

        if prev != bool_value:
            write_state_change(conn, alarm_key, bool_value)
            changes += 1

    if changes:
        log.info("Poll complete — %d state change(s) written", changes)
    else:
        log.debug("Poll complete — no changes")


# ── Discovery cycle ───────────────────────────────────────────────────────────

async def discover_and_cache(
    endpoint: str,
    username: str,
    password: str,
    root_paths: list[str],
    ns_index: int,
    conn: sqlite3.Connection,
) -> list[dict]:
    client = Client(url=endpoint, timeout=10)
    client.session_timeout = 30000
    client._watchdog_intervall = 999999  # noqa: SLF001
    client.set_user(username)
    client.set_password(password)

    try:
        await client.connect()
        log.info("Connected for discovery")
        discovered = await discover_alarm_nodes(client, root_paths, ns_index)
        await client.disconnect()
        await asyncio.sleep(2)
    except Exception as exc:
        log.error("Discovery failed: %s", exc)
        try:
            await client.disconnect()
        except Exception:
            pass
        await asyncio.sleep(2)
        return []

    if discovered:
        save_nodes(conn, discovered)

    return discovered


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    # Load config
    if not CONFIG_FILE.exists():
        log.error("Config file not found: %s", CONFIG_FILE)
        sys.exit(1)

    with open(CONFIG_FILE) as f:
        cfg = yaml.safe_load(f)

    endpoint: str = cfg["server"]["endpoint"]
    username: str = cfg["server"].get("username", "OPC")
    password: str = cfg["server"].get("password", "OPC")
    ns_index: int = cfg.get("namespace", {}).get("index", 2)
    root_paths: list[str] = cfg.get("root_paths", ["SIMULATED"])
    poll_interval: int = int(cfg.get("alarm", {}).get("poll_interval", 10))

    log.info("Starting OPC UA Poller")
    log.info("  Endpoint    : %s", endpoint)
    log.info("  Root paths  : %s", root_paths)
    log.info("  Poll interval: %ds", poll_interval)
    log.info("  DB          : %s", DB_FILE)

    # Init DB
    conn = db_connect()
    ensure_schema(conn)

    # Load or discover alarm nodes
    alarm_nodes = load_cached_nodes(conn)

    if alarm_nodes:
        log.info("Loaded %d cached alarm nodes from DB", len(alarm_nodes))
    else:
        log.info("No cached nodes — starting discovery...")
        alarm_nodes = await discover_and_cache(
            endpoint, username, password, root_paths, ns_index, conn
        )
        if not alarm_nodes:
            log.error("Discovery returned no nodes — check server and root_paths config")
            sys.exit(1)

    log.info("Monitoring %d alarm nodes", len(alarm_nodes))

    # Main poll loop
    while True:
        try:
            await poll_once(endpoint, username, password, alarm_nodes, conn)
        except Exception as exc:
            log.exception("Unexpected error in poll loop: %s", exc)

        await asyncio.sleep(poll_interval)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Poller stopped by user")
