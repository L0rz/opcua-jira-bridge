"""
opcua_poller_sub.py — OPC UA Alarm Poller via Subscriptions (NO Jira/httpx)

Connects to the OPC UA server, discovers alarm nodes, subscribes to DataChange
notifications, and writes debounced state changes to SQLite (shared with
jira_worker.py).

Subscriptions keep the session alive — no repeated connect/disconnect cycling.
Auto-reconnects on connection loss with exponential backoff.

Changes vs. commit 20877bb:
  * asyncua library logging suppressed to WARNING (no more PublishResult dumps)
  * Debounce: a changed value must stay stable for `debounce_seconds`
    before it is committed — prevents ticket spam from flapping alarms.
    Configurable via alarm.debounce_seconds in opcua_config_real_server.yaml
    (default 30s).
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
from asyncua import Client

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

# Suppress noisy asyncua internal logging (PublishResult / NotificationMessage dumps).
for _noisy in ("asyncua", "asyncua.client", "asyncua.common", "asyncua.client.ua_client"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# ── Paths & defaults ──────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "opcua_config_real_server.yaml"
DB_FILE = BASE_DIR / "alarms.db"

RECONNECT_MIN = 5                 # s — reconnect backoff start
RECONNECT_MAX = 60                # s — reconnect backoff cap
DEFAULT_PUBLISHING_INTERVAL = 500  # ms
DEFAULT_DEBOUNCE_SECONDS = 30      # s — value must stay stable this long before commit
FLUSH_INTERVAL = 1.0              # s — debounce flush / keep-alive tick


# ── DB helpers ────────────────────────────────────────────────────────────────

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS alarm_nodes (
            id INTEGER PRIMARY KEY,
            nodeid TEXT UNIQUE NOT NULL,
            alarm_key TEXT NOT NULL,
            label TEXT NOT NULL,
            room_name TEXT,
            root_path TEXT,
            discovered_at TEXT
        );

        CREATE TABLE IF NOT EXISTS alarm_events (
            id INTEGER PRIMARY KEY,
            alarm_key TEXT NOT NULL,
            value BOOLEAN NOT NULL,
            status TEXT DEFAULT 'new',
            ticket_key TEXT,
            created_at TEXT NOT NULL,
            processed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS alarm_state (
            alarm_key TEXT PRIMARY KEY,
            current_value BOOLEAN,
            last_changed TEXT,
            open_ticket_key TEXT
        );
        """
    )
    conn.commit()


def load_cached_nodes(conn: sqlite3.Connection) -> list:
    rows = conn.execute("SELECT * FROM alarm_nodes").fetchall()
    return [dict(r) for r in rows]


def save_nodes(conn: sqlite3.Connection, nodes: list) -> None:
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


def get_current_state(conn: sqlite3.Connection, alarm_key: str):
    row = conn.execute(
        "SELECT current_value FROM alarm_state WHERE alarm_key=?", (alarm_key,)
    ).fetchone()
    if row is None:
        return None
    val = row["current_value"]
    return bool(val) if val is not None else None


def write_state_change(conn: sqlite3.Connection, alarm_key: str, new_value: bool) -> None:
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """INSERT INTO alarm_state (alarm_key, current_value, last_changed, open_ticket_key)
           VALUES (?, ?, ?, NULL)
           ON CONFLICT(alarm_key) DO UPDATE SET
               current_value=excluded.current_value,
               last_changed=excluded.last_changed""",
        (alarm_key, new_value, now),
    )

    if new_value:
        conn.execute(
            "INSERT INTO alarm_events (alarm_key, value, status, created_at) VALUES (?,?,?,?)",
            (alarm_key, True, "new", now),
        )
        log.info("ALARM ON  : %s -> new event inserted", alarm_key)
    else:
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
            log.info(
                "ALARM OFF : %s -> resolved_pending (ticket %s)",
                alarm_key, open_row["ticket_key"],
            )
        else:
            conn.execute(
                "INSERT INTO alarm_events (alarm_key, value, status, created_at) VALUES (?,?,?,?)",
                (alarm_key, False, "ignored", now),
            )
            log.info("ALARM OFF : %s -> no open ticket, ignored", alarm_key)

    conn.commit()


# ── Subscription handler (with debounce) ──────────────────────────────────────

class AlarmSubscriptionHandler:
    """Handles DataChange notifications and applies debounce before committing.

    A value that differs from the committed state starts a debounce timer.
    Only if it is still different after `debounce_seconds` (flushed by the main
    loop) is it written to the DB. Flapping that reverts within the window is
    discarded — no ticket spam.
    """

    def __init__(self, node_map: dict, conn: sqlite3.Connection, debounce_seconds: float) -> None:
        self.node_map = node_map
        self.conn = conn
        self.debounce_seconds = debounce_seconds
        # alarm_key -> (pending_value: bool, since: float monotonic seconds)
        self.pending: dict = {}

    def datachange_notification(self, node, val, data) -> None:
        nodeid = node.nodeid.to_string()
        info = self.node_map.get(nodeid)
        if info is None:
            log.warning("Notification for unknown node: %s", nodeid)
            return

        alarm_key = info["alarm_key"]
        try:
            bool_value = bool(val)
        except Exception:
            log.warning("Cannot convert value for %s: %r", alarm_key, val)
            return

        committed = get_current_state(self.conn, alarm_key)
        if bool_value == committed:
            # Reverted to committed state before debounce elapsed -> cancel pending.
            if self.pending.pop(alarm_key, None) is not None:
                log.debug("%s reverted to %s before debounce — pending cleared", alarm_key, bool_value)
            return

        # Value differs from committed -> (re)start the debounce timer.
        self.pending[alarm_key] = (bool_value, time.monotonic())
        log.debug("%s pending -> %s (debounce %ss)", alarm_key, bool_value, self.debounce_seconds)

    def flush(self) -> int:
        """Commit pending changes stable for >= debounce_seconds. Returns count."""
        if not self.pending:
            return 0
        now = time.monotonic()
        committed = 0
        for alarm_key, (value, since) in list(self.pending.items()):
            if now - since >= self.debounce_seconds:
                write_state_change(self.conn, alarm_key, value)
                self.pending.pop(alarm_key, None)
                committed += 1
        return committed

    def status_change_notification(self, status) -> None:
        log.warning("Subscription status change: %s", status)


# ── OPC UA discovery ──────────────────────────────────────────────────────────

async def discover_alarm_nodes(client: Client, root_paths: list, ns_index: int) -> list:
    """Browse ROOMS/*/ALARMS/* under each root_path and return node descriptors."""
    discovered: list = []

    for root_path in root_paths:
        log.info("Browsing root path: %s", root_path)
        try:
            root_node = await client.nodes.root.get_child(["0:Objects", f"{ns_index}:{root_path}"])
        except Exception as exc:
            log.warning("Root path '%s' not found: %s", root_path, exc)
            continue

        try:
            ds_node = await root_node.get_child(f"{ns_index}:DataStructure")
            rooms_node = await ds_node.get_child(f"{ns_index}:ROOMS")
        except Exception as exc:
            log.warning("No DataStructure/ROOMS under '%s': %s", root_path, exc)
            continue

        for room_node in await rooms_node.get_children():
            room_name = (await room_node.read_browse_name()).Name
            try:
                alarms_node = await room_node.get_child(f"{ns_index}:ALARMS")
            except Exception:
                log.debug("No ALARMS under room %s", room_name)
                continue

            for alarm_node in await alarms_node.get_children():
                alarm_name = (await alarm_node.read_browse_name()).Name
                discovered.append(
                    {
                        "nodeid": alarm_node.nodeid.to_string(),
                        "alarm_key": f"{room_name}.{alarm_name}",
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
    alarm_nodes: list,
    conn: sqlite3.Connection,
    publishing_interval: float,
    debounce_seconds: float,
) -> None:
    """Connect, subscribe to all alarm nodes, and run until disconnected.

    Raises on connection failure so the caller can reconnect.
    """
    node_map = {n["nodeid"]: n for n in alarm_nodes}
    handler = AlarmSubscriptionHandler(node_map, conn, debounce_seconds)

    client = Client(url=endpoint, timeout=10)
    client.session_timeout = 3600000          # 1h — subscriptions keep it alive anyway
    client._watchdog_intervall = 999999       # noqa: SLF001
    client.set_user(username)
    client.set_password(password)

    log.info("Connecting to %s ...", endpoint)
    await client.connect()
    log.info(
        "Connected — creating subscription (publishing_interval=%sms, debounce=%ss)",
        publishing_interval, debounce_seconds,
    )

    subscription = await client.create_subscription(publishing_interval, handler)
    nodes = [client.get_node(n["nodeid"]) for n in alarm_nodes]
    await subscription.subscribe_data_change(nodes)
    log.info("Subscribed to %d alarm nodes — waiting for notifications", len(alarm_nodes))

    # Initial read to seed current state (committed immediately, no debounce on startup).
    log.info("Initial state read...")
    values = await client.read_values(nodes)
    for i, n in enumerate(alarm_nodes):
        try:
            bool_value = bool(values[i])
        except Exception:
            continue
        if get_current_state(conn, n["alarm_key"]) != bool_value:
            write_state_change(conn, n["alarm_key"], bool_value)
    log.info("Initial state read complete")

    # Run: flush debounced changes every tick; periodic keep-alive connection check.
    tick = 0
    try:
        while True:
            await asyncio.sleep(FLUSH_INTERVAL)
            committed = handler.flush()
            if committed:
                log.info("Debounce flush committed %d change(s)", committed)
            tick += 1
            if tick % 10 == 0:
                try:
                    await client.check_connection()
                except Exception as exc:
                    log.warning("Connection check failed: %s", exc)
                    raise
    finally:
        try:
            await subscription.delete()
        except Exception:
            pass
        try:
            await client.disconnect()
        except Exception:
            pass
        log.info("Disconnected")


# ── Discovery helper ──────────────────────────────────────────────────────────

async def discover_and_cache(
    endpoint: str,
    username: str,
    password: str,
    root_paths: list,
    ns_index: int,
    conn: sqlite3.Connection,
) -> list:
    client = Client(url=endpoint, timeout=10)
    client.session_timeout = 30000
    client._watchdog_intervall = 999999       # noqa: SLF001
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

    server = cfg.get("server", {})
    endpoint = server["endpoint"]
    username = server.get("username", "OPC")
    password = server.get("password", "OPC")
    ns_index = cfg.get("namespace", {}).get("index", 2)
    root_paths = cfg.get("root_paths", ["SIMULATED"])
    alarm_cfg = cfg.get("alarm", {})
    publishing_interval = float(alarm_cfg.get("publishing_interval", DEFAULT_PUBLISHING_INTERVAL))
    debounce_seconds = float(alarm_cfg.get("debounce_seconds", DEFAULT_DEBOUNCE_SECONDS))

    log.info("Starting OPC UA Poller (Subscription mode + debounce)")
    log.info("  Endpoint            : %s", endpoint)
    log.info("  Root paths          : %s", root_paths)
    log.info("  Publishing interval : %sms", publishing_interval)
    log.info("  Debounce            : %ss", debounce_seconds)
    log.info("  DB                  : %s", DB_FILE)

    conn = db_connect()
    ensure_schema(conn)

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

    backoff = RECONNECT_MIN
    while True:
        try:
            await run_subscription(
                endpoint, username, password, alarm_nodes, conn,
                publishing_interval, debounce_seconds,
            )
            backoff = RECONNECT_MIN
        except asyncio.CancelledError:
            log.info("Poller cancelled")
            break
        except Exception as exc:
            log.error("Connection lost: %s — reconnecting in %ds", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)
            log.info("Reconnecting...")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Poller stopped by user")
