"""
opcua_poller.py — OPC UA Alarm Poller (persistent sync connection + debounce)

Uses python-opcua (sync) with a single persistent connection.
Elipse E3 doesn't tolerate repeated connect/disconnect cycles (blocks after ~12),
but persistent connections work indefinitely.

Reads all alarm nodes periodically, applies debounce, writes stable state changes
to SQLite (shared with jira_worker.py).
"""

import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from opcua import Client, ua

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [POLLER] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("poller")

# Suppress noisy opcua library logging
logging.getLogger("opcua").setLevel(logging.WARNING)

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "opcua_config_real_server.yaml"
DB_FILE = BASE_DIR / "alarms.db"

# Defaults
DEFAULT_POLL_INTERVAL = 30
DEFAULT_DEBOUNCE_SECONDS = 30
RECONNECT_MIN = 300   # 5 min — give E3 server time to clean up zombie sessions
RECONNECT_MAX = 600   # 10 min max


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
        log.info("ALARM ON  : %s → new event inserted", alarm_key)
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
            log.info("ALARM OFF : %s → resolved_pending (ticket %s)", alarm_key, open_row["ticket_key"])
        else:
            conn.execute(
                "INSERT INTO alarm_events (alarm_key, value, status, created_at) VALUES (?,?,?,?)",
                (alarm_key, False, "ignored", now),
            )
            log.info("ALARM OFF : %s → no open ticket, ignored", alarm_key)

    conn.commit()


# ── Debounce tracker ──────────────────────────────────────────────────────────

class DebounceTracker:
    def __init__(self, debounce_seconds: float) -> None:
        self.debounce_seconds = debounce_seconds
        self._pending: dict[str, dict] = {}
        self._committed: dict[str, bool] = {}

    def update(self, alarm_key: str, value: bool) -> None:
        pending = self._pending.get(alarm_key)
        if pending is None or pending["value"] != value:
            self._pending[alarm_key] = {"value": value, "since": time.monotonic()}

    def flush(self, conn: sqlite3.Connection) -> int:
        if self.debounce_seconds <= 0:
            return self._flush_all(conn)

        now = time.monotonic()
        changes = 0
        for alarm_key, pending in list(self._pending.items()):
            if (now - pending["since"]) < self.debounce_seconds:
                continue
            value = pending["value"]
            if self._committed.get(alarm_key) != value:
                write_state_change(conn, alarm_key, value)
                self._committed[alarm_key] = value
                changes += 1
            del self._pending[alarm_key]
        return changes

    def _flush_all(self, conn: sqlite3.Connection) -> int:
        changes = 0
        for alarm_key, pending in list(self._pending.items()):
            value = pending["value"]
            if self._committed.get(alarm_key) != value:
                write_state_change(conn, alarm_key, value)
                self._committed[alarm_key] = value
                changes += 1
            del self._pending[alarm_key]
        return changes

    def seed(self, alarm_key: str, value: bool) -> None:
        self._committed[alarm_key] = value

    @property
    def pending_count(self) -> int:
        return len(self._pending)


# ── OPC UA connection ─────────────────────────────────────────────────────────

def create_client(endpoint: str, username: str, password: str) -> Client:
    client = Client(endpoint, timeout=10)
    client.session_timeout = 86400000  # request 24h — never timeout if possible
    client.set_user(username)
    client.set_password(password)
    return client


def discover_alarm_nodes(client: Client, root_paths: list[str], ns_index: int) -> list[dict]:
    """Browse ROOMS/*/ALARMS/* under each root_path."""
    discovered: list[dict] = []

    for root_path in root_paths:
        log.info("Browsing root path: %s", root_path)
        try:
            objects = client.get_objects_node()
            root_node = objects.get_child(f"{ns_index}:{root_path}")
        except Exception as exc:
            log.warning("Root path '%s' not found: %s", root_path, exc)
            continue

        try:
            ds_node = root_node.get_child(f"{ns_index}:DataStructure")
            rooms_node = ds_node.get_child(f"{ns_index}:ROOMS")
        except Exception as exc:
            log.warning("No DataStructure/ROOMS under '%s': %s", root_path, exc)
            continue

        for room_node in rooms_node.get_children():
            room_name = room_node.get_browse_name().Name

            try:
                alarms_node = room_node.get_child(f"{ns_index}:ALARMS")
            except Exception:
                continue

            for alarm_node in alarms_node.get_children():
                alarm_name = alarm_node.get_browse_name().Name
                nodeid = alarm_node.nodeid.to_string()
                alarm_key = f"{room_name}.{alarm_name}"

                discovered.append({
                    "nodeid": nodeid,
                    "alarm_key": alarm_key,
                    "label": alarm_name,
                    "room_name": room_name,
                    "root_path": root_path,
                })

    log.info("Discovered %d alarm nodes across %d root path(s)", len(discovered), len(root_paths))
    return discovered


def read_all_values(client: Client, nodeids: list[str]) -> dict[str, bool] | None:
    """Read all alarm node values in a single OPC UA request. Returns {nodeid: value} or None."""
    try:
        nodes = [client.get_node(nid) for nid in nodeids]
        # Batch read: single request for all 24 nodes
        results = client.get_values(nodes)
        return {nodeids[i]: results[i] for i in range(len(nodeids))}
    except AttributeError:
        # Fallback if get_values not available in this opcua version
        try:
            values = [n.get_value() for n in [client.get_node(nid) for nid in nodeids]]
            return {nodeids[i]: values[i] for i in range(len(nodeids))}
        except Exception as exc:
            log.error("Read failed: %s", exc)
            return None
    except Exception as exc:
        log.error("Read failed: %s", exc)
        return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
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
    poll_interval: int = int(cfg.get("alarm", {}).get("poll_interval", DEFAULT_POLL_INTERVAL))
    debounce_seconds: float = float(cfg.get("alarm", {}).get("debounce_seconds", DEFAULT_DEBOUNCE_SECONDS))

    log.info("Starting OPC UA Poller (persistent sync connection)")
    log.info("  Endpoint      : %s", endpoint)
    log.info("  Root paths    : %s", root_paths)
    log.info("  Poll interval : %ds", poll_interval)
    log.info("  Debounce      : %ds", debounce_seconds)
    log.info("  DB            : %s", DB_FILE)

    db = db_connect()
    ensure_schema(db)

    # Initialize debounce tracker
    debounce = DebounceTracker(debounce_seconds)

    # Outer reconnect loop
    backoff = RECONNECT_MIN

    while True:
        client = create_client(endpoint, username, password)

        try:
            log.info("Connecting to %s ...", endpoint)
            client.connect()
            log.info("Connected!")
            backoff = RECONNECT_MIN  # reset on success

            # Load or discover alarm nodes
            alarm_nodes = load_cached_nodes(db)
            if not alarm_nodes:
                log.info("No cached nodes — discovering...")
                alarm_nodes = discover_alarm_nodes(client, root_paths, ns_index)
                if not alarm_nodes:
                    log.error("No alarm nodes found — check config")
                    sys.exit(1)
                save_nodes(db, alarm_nodes)

            alarm_nodeids = [n["nodeid"] for n in alarm_nodes]
            node_map = {n["nodeid"]: n for n in alarm_nodes}
            log.info("Monitoring %d alarm nodes", len(alarm_nodes))

            # Seed debounce with current DB state
            for node in alarm_nodes:
                current = get_current_state(db, node["alarm_key"])
                if current is not None:
                    debounce.seed(node["alarm_key"], current)

            # Inner poll loop — runs until connection dies
            # NEVER proactively disconnect — each disconnect poisons the E3 server
            poll_count = 0
            session_start = time.monotonic()

            while True:
                poll_count += 1

                values = read_all_values(client, alarm_nodeids)
                if values is None:
                    session_age = time.monotonic() - session_start
                    log.warning("Read failed after %.0fs — connection dead, will reconnect", session_age)
                    break  # exit to reconnect loop

                # Feed into debounce
                for nodeid, value in values.items():
                    node_info = node_map.get(nodeid)
                    if node_info:
                        try:
                            debounce.update(node_info["alarm_key"], bool(value))
                        except Exception:
                            pass

                # Flush stable changes
                flushed = debounce.flush(db)
                if flushed:
                    log.info("Debounce flush: %d stable state change(s) written", flushed)

                # Heartbeat every 10 polls
                if poll_count % 10 == 0:
                    session_age = time.monotonic() - session_start
                    log.info("♥ Poll #%d OK (session: %.0fs) — %d pending debounce",
                             poll_count, session_age, debounce.pending_count)

                time.sleep(poll_interval)

        except KeyboardInterrupt:
            log.info("Shutting down (not disconnecting — E3 cleans up on its own)...")
            break

        except Exception as exc:
            log.error("Connection error: %s", exc)

        # DO NOT call client.disconnect() — it creates SecureStream_Delete errors
        # that poison the E3 server. Just drop the connection and wait.
        log.info("Waiting %ds for server to clean up before reconnect...", backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, RECONNECT_MAX)

    db.close()


if __name__ == "__main__":
    main()



