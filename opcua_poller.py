"""
opcua_poller.py — OPC UA Alarm Poller via Subscriptions (NO Jira/httpx)

Connects to OPC UA server, discovers alarm nodes, subscribes to DataChange
notifications, and writes state changes to SQLite (shared with jira_worker.py).

Subscriptions keep the session alive — no more 30s timeout / rate-limiting.
Auto-reconnects on connection loss with exponential backoff.
"""

import asyncio
import logging
import platform
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from asyncua import Client, ua
from asyncua.common.subscription import DataChangeNotif

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

# Suppress noisy asyncua debug/info output
logging.getLogger("asyncua").setLevel(logging.WARNING)

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "opcua_config_real_server.yaml"
DB_FILE = BASE_DIR / "alarms.db"

# Reconnect backoff: starts at 5s, doubles up to 60s
RECONNECT_MIN = 5
RECONNECT_MAX = 60

# Debounce: alarm must be stable for this many seconds before writing to DB
DEBOUNCE_SECONDS = 30


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
    return bool(val) if val is not None else None


def write_state_change(
    conn: sqlite3.Connection,
    alarm_key: str,
    new_value: bool,
) -> None:
    now = datetime.now(timezone.utc).isoformat()

    # Update alarm_state
    conn.execute(
        """INSERT INTO alarm_state (alarm_key, current_value, last_changed, open_ticket_key)
           VALUES (?, ?, ?, NULL)
           ON CONFLICT(alarm_key) DO UPDATE SET
               current_value=excluded.current_value,
               last_changed=excluded.last_changed""",
        (alarm_key, new_value, now),
    )

    if new_value:
        # Alarm triggered → new event for jira_worker
        conn.execute(
            "INSERT INTO alarm_events (alarm_key, value, status, created_at) VALUES (?,?,?,?)",
            (alarm_key, True, "new", now),
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
            conn.execute(
                "INSERT INTO alarm_events (alarm_key, value, status, created_at) VALUES (?,?,?,?)",
                (alarm_key, False, "ignored", now),
            )
            log.info("ALARM OFF : %s → no open ticket, ignored", alarm_key)

    conn.commit()


# ── Subscription handler ──────────────────────────────────────────────────────

class AlarmSubscriptionHandler:
    """Handles DataChange notifications with debounce.

    An alarm must stay in a new state for DEBOUNCE_SECONDS before it's
    written to the DB. This prevents flapping alarms from flooding the DB
    with events every ~5 seconds.
    """

    def __init__(self, node_map: dict[str, dict], conn: sqlite3.Connection, debounce_seconds: float = DEBOUNCE_SECONDS) -> None:
        self.node_map = node_map
        self.conn = conn
        self.debounce_seconds = debounce_seconds
        # alarm_key → {"value": bool, "since": timestamp}
        # Tracks the last raw value received and when it first appeared
        self._pending: dict[str, dict] = {}
        self._committed_state: dict[str, bool] = {}  # last value written to DB

    def datachange_notification(self, node, val, data: DataChangeNotif) -> None:
        nodeid = node.nodeid.to_string()
        node_info = self.node_map.get(nodeid)
        if node_info is None:
            return

        alarm_key = node_info["alarm_key"]

        try:
            bool_value = bool(val)
        except Exception:
            return

        now = time.monotonic()
        pending = self._pending.get(alarm_key)

        if pending is None or pending["value"] != bool_value:
            # New state or first notification — start debounce timer
            self._pending[alarm_key] = {"value": bool_value, "since": now}
            if pending is not None:
                log.debug("Debounce reset: %s → %s (was %s for %.1fs)",
                          alarm_key, bool_value, pending["value"], now - pending["since"])
        # If same value, just let the timer continue

    def flush_debounced(self) -> int:
        """Check all pending values and write those that have been stable long enough.
        Returns number of state changes written.
        """
        now = time.monotonic()
        changes = 0

        for alarm_key, pending in list(self._pending.items()):
            elapsed = now - pending["since"]
            if elapsed < self.debounce_seconds:
                continue

            bool_value = pending["value"]
            committed = self._committed_state.get(alarm_key)

            if committed != bool_value:
                write_state_change(self.conn, alarm_key, bool_value)
                self._committed_state[alarm_key] = bool_value
                changes += 1

            # Remove from pending — it's been flushed
            del self._pending[alarm_key]

        return changes

    def get_pending_count(self) -> int:
        """Return number of alarms currently in debounce."""
        return len(self._pending)

    def event_notification(self, event) -> None:
        log.debug("Event notification: %s", event)

    def status_change_notification(self, status) -> None:
        log.warning("Subscription status change: %s", status)


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

        try:
            ds_node = await root_node.get_child(f"{ns_index}:DataStructure")
            rooms_node = await ds_node.get_child(f"{ns_index}:ROOMS")
        except Exception as exc:
            log.warning("No DataStructure/ROOMS under '%s': %s", root_path, exc)
            continue

        room_children = await rooms_node.get_children()
        for room_node in room_children:
            room_name = (await room_node.read_browse_name()).Name

            try:
                alarms_node = await room_node.get_child(f"{ns_index}:ALARMS")
            except Exception:
                log.debug("No ALARMS under room %s", room_name)
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

    log.info("Discovered %d alarm nodes across %d root path(s)", len(discovered), len(root_paths))
    return discovered


# ── Connection + subscription cycle ──────────────────────────────────────────

async def run_subscription(
    endpoint: str,
    username: str,
    password: str,
    alarm_nodes: list[dict],
    conn: sqlite3.Connection,
    publishing_interval: float = 500,  # ms
) -> None:
    """
    Connect, subscribe to all alarm nodes, and run until disconnected.
    Raises on connection failure so the caller can reconnect.
    """
    node_map = {n["nodeid"]: n for n in alarm_nodes}
    handler = AlarmSubscriptionHandler(node_map, conn)

    client = Client(url=endpoint, timeout=10)
    client.session_timeout = 3600000  # 1h — subscriptions keep it alive anyway
    client._watchdog_intervall = 999999  # noqa: SLF001
    client.set_user(username)
    client.set_password(password)

    log.info("Connecting to %s ...", endpoint)
    await client.connect()
    log.info("Connected — creating subscription (publishing_interval=%sms)", publishing_interval)

    subscription = await client.create_subscription(publishing_interval, handler)

    nodes = [client.get_node(n["nodeid"]) for n in alarm_nodes]
    monitored_items = await subscription.subscribe_data_change(nodes)
    log.info("Subscribed to %d alarm nodes — waiting for notifications", len(alarm_nodes))

    # Initial read to seed committed state (bypass debounce for initial sync)
    log.info("Initial state read...")
    values = await client.read_values(nodes)
    for i, node_obj in enumerate(alarm_nodes):
        alarm_key = node_obj["alarm_key"]
        try:
            bool_value = bool(values[i])
        except Exception:
            continue
        prev = get_current_state(conn, alarm_key)
        if prev != bool_value:
            write_state_change(conn, alarm_key, bool_value)
        # Seed the handler's committed state so debounce knows current baseline
        handler._committed_state[alarm_key] = bool_value if prev is None else (bool_value if prev != bool_value else prev)
    log.info("Initial state read complete — debounce=%ds", DEBOUNCE_SECONDS)

    # Main loop: flush debounced events + keep-alive
    heartbeat_count = 0
    try:
        while True:
            await asyncio.sleep(5)  # Check debounce every 5s
            heartbeat_count += 1

            # Flush any debounced state changes
            flushed = handler.flush_debounced()
            if flushed:
                log.info("Debounce flush: %d stable state change(s) written to DB", flushed)

            # Keep-alive every 30s (every 6th iteration)
            if heartbeat_count % 6 == 0:
                try:
                    server_state = await client.nodes.server_state.read_value()
                    pending = handler.get_pending_count()
                    if heartbeat_count % 60 == 0:  # Log every ~5 min
                        log.info("♥ Connection alive (server state: %s, pending debounce: %d, uptime: %d checks)",
                                 server_state, pending, heartbeat_count)
                except Exception as exc:
                    log.warning("Keep-alive read failed: %s", exc)
                    raise
    except asyncio.CancelledError:
        log.info("Shutdown requested — disconnecting cleanly...")
        raise
    finally:
        log.info("Cleaning up OPC UA session...")
        try:
            await subscription.delete()
            log.info("Subscription deleted")
        except Exception:
            pass
        try:
            await client.disconnect()
            log.info("Disconnected cleanly from server")
        except Exception:
            pass
        # Give the server a moment to process the disconnect
        await asyncio.sleep(2)


# ── Discovery helper ──────────────────────────────────────────────────────────

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
        return []

    if discovered:
        save_nodes(conn, discovered)

    return discovered


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
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
    publishing_interval: float = float(cfg.get("alarm", {}).get("publishing_interval", 500))

    log.info("Starting OPC UA Poller (Subscription mode)")
    log.info("  Endpoint           : %s", endpoint)
    log.info("  Root paths         : %s", root_paths)
    log.info("  Publishing interval: %sms", publishing_interval)
    log.info("  DB                 : %s", DB_FILE)

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

    log.info("Monitoring %d alarm nodes via subscription", len(alarm_nodes))

    # Reconnect loop with exponential backoff
    backoff = RECONNECT_MIN
    while True:
        try:
            await run_subscription(
                endpoint, username, password, alarm_nodes, conn, publishing_interval
            )
            # If run_subscription returns cleanly (shouldn't happen), reconnect immediately
            backoff = RECONNECT_MIN
        except asyncio.CancelledError:
            log.info("Poller cancelled")
            break
        except Exception as exc:
            log.error("Connection lost: %s — reconnecting in %ds", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)
            log.info("Reconnecting...")
            # Reset backoff after successful reconnect (handled at top of loop)


if __name__ == "__main__":
    import signal

    loop = asyncio.new_event_loop()
    main_task = loop.create_task(main())

    def _shutdown(sig, frame):
        log.info("Received %s — shutting down gracefully...", signal.Signals(sig).name)
        main_task.cancel()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        loop.run_until_complete(main_task)
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()
        log.info("Poller stopped")




