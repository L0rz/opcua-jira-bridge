"""
jira_worker.py — Jira Ticket Sync Worker (NO OPC UA / asyncua)

Polls SQLite for new alarm events and creates/resolves Jira tickets via httpx.
Runs independently of opcua_poller.py — both share alarms.db.
"""

import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv
import os

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [JIRA] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("jira")

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "opcua_config_real_server.yaml"
DB_FILE = BASE_DIR / "alarms.db"
ENV_FILE = BASE_DIR / ".env"

# Load .env
load_dotenv(ENV_FILE)

POLL_SLEEP = 5  # seconds between DB polls


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


def fetch_new_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM alarm_events WHERE status='new' ORDER BY id ASC"
    ).fetchall()


def fetch_resolved_pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM alarm_events WHERE status='resolved_pending' ORDER BY id ASC"
    ).fetchall()


def get_open_ticket_for_alarm(conn: sqlite3.Connection, alarm_key: str) -> str | None:
    """Return the ticket_key of the latest open (ticket_created) event for this alarm."""
    row = conn.execute(
        "SELECT ticket_key FROM alarm_events "
        "WHERE alarm_key=? AND status='ticket_created' "
        "ORDER BY id DESC LIMIT 1",
        (alarm_key,),
    ).fetchone()
    return row["ticket_key"] if row else None


def mark_event_ticket_created(
    conn: sqlite3.Connection, event_id: int, ticket_key: str
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE alarm_events SET status='ticket_created', ticket_key=?, processed_at=? WHERE id=?",
        (ticket_key, now, event_id),
    )
    conn.execute(
        "UPDATE alarm_state SET open_ticket_key=? WHERE alarm_key=("
        "SELECT alarm_key FROM alarm_events WHERE id=?)",
        (ticket_key, event_id),
    )
    conn.commit()


def mark_event_resolved(conn: sqlite3.Connection, event_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE alarm_events SET status='resolved', processed_at=? WHERE id=?",
        (now, event_id),
    )
    # Clear open_ticket_key in alarm_state
    conn.execute(
        "UPDATE alarm_state SET open_ticket_key=NULL WHERE alarm_key=("
        "SELECT alarm_key FROM alarm_events WHERE id=?)",
        (event_id,),
    )
    conn.commit()


def mark_event_ignored(conn: sqlite3.Connection, event_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE alarm_events SET status='ignored', processed_at=? WHERE id=?",
        (now, event_id),
    )
    conn.commit()


def get_node_info(conn: sqlite3.Connection, alarm_key: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM alarm_nodes WHERE alarm_key=?", (alarm_key,)
    ).fetchone()
    return dict(row) if row else None


# ── Jira API ──────────────────────────────────────────────────────────────────

class JiraClient:
    def __init__(
        self,
        base_url: str,
        user: str,
        api_token: str,
        project_key: str,
        account_id: str,
        issue_type: str,
        labels: list[str],
        summary_template: str,
        resolve_transition_id: str,
        resolve_comment: str,
        auto_resolve: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.project_key = project_key
        self.account_id = account_id
        self.issue_type = issue_type
        self.labels = labels
        self.summary_template = summary_template
        self.resolve_transition_id = resolve_transition_id
        self.resolve_comment = resolve_comment
        self.auto_resolve = auto_resolve

        self._client = httpx.Client(
            auth=(user, api_token),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=30,
        )

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def create_ticket(
        self,
        alarm_key: str,
        label: str,
        room_name: str | None,
        root_path: str | None,
        alarm_value: bool,
    ) -> str | None:
        """Create a Jira Incident ticket. Returns ticket key (e.g. 'RKS-42') or None."""
        room = room_name or alarm_key.split(".")[0]
        alarm_label = label or alarm_key.split(".")[-1]

        # Build summary using template variables
        summary = self.summary_template.format(
            alarm_key=alarm_key,
            alarm_label=alarm_label,
            room=room,
            room_id=room,
            client_id=root_path or "OPC-UA",
        )

        description = (
            f"*OPC UA Alarm Triggered*\n\n"
            f"| Field | Value |\n"
            f"| Alarm Key | {alarm_key} |\n"
            f"| Room | {room} |\n"
            f"| Root Path | {root_path or 'N/A'} |\n"
            f"| Label | {alarm_label} |\n"
            f"| Triggered At | {datetime.now(timezone.utc).isoformat()} |\n\n"
            f"This ticket was automatically created by the OPC UA → Jira bridge."
        )

        payload = {
            "fields": {
                "project": {"key": self.project_key},
                "summary": summary,
                "description": description,
                "issuetype": {"name": self.issue_type},
                "labels": self.labels,
                "assignee": {"accountId": self.account_id},
                "priority": {"name": "High"},
            }
        }

        try:
            resp = self._client.post(self._url("/rest/api/2/issue"), json=payload)
            resp.raise_for_status()
            ticket_key = resp.json()["key"]
            log.info("Created Jira ticket %s for alarm %s", ticket_key, alarm_key)
            return ticket_key
        except httpx.HTTPStatusError as exc:
            log.error(
                "Jira create failed (%s): %s — %s",
                exc.response.status_code,
                alarm_key,
                exc.response.text[:300],
            )
            return None
        except Exception as exc:
            log.error("Jira create error for %s: %s", alarm_key, exc)
            return None

    def resolve_ticket(self, ticket_key: str, comment: str | None = None) -> bool:
        """Transition ticket to resolved and optionally add a comment."""
        if not self.auto_resolve:
            log.info("Auto-resolve disabled — skipping %s", ticket_key)
            return False

        # Add comment first
        resolve_comment = comment or self.resolve_comment
        if resolve_comment:
            try:
                self._client.post(
                    self._url(f"/rest/api/2/issue/{ticket_key}/comment"),
                    json={"body": resolve_comment},
                )
            except Exception as exc:
                log.warning("Failed to add comment to %s: %s", ticket_key, exc)

        # Transition
        payload = {"transition": {"id": self.resolve_transition_id}}
        try:
            resp = self._client.post(
                self._url(f"/rest/api/2/issue/{ticket_key}/transitions"),
                json=payload,
            )
            if resp.status_code == 204:
                log.info("Resolved Jira ticket %s", ticket_key)
                return True
            else:
                log.warning(
                    "Transition %s returned %s: %s",
                    ticket_key,
                    resp.status_code,
                    resp.text[:200],
                )
                return False
        except Exception as exc:
            log.error("Jira resolve error for %s: %s", ticket_key, exc)
            return False

    def close(self) -> None:
        self._client.close()


# ── Worker logic ──────────────────────────────────────────────────────────────

def process_new_events(
    conn: sqlite3.Connection,
    jira: JiraClient,
) -> None:
    events = fetch_new_events(conn)
    if not events:
        return

    log.info("Processing %d new alarm event(s)", len(events))

    for event in events:
        alarm_key = event["alarm_key"]
        event_id = event["id"]

        # Dedup check: is there already an open ticket for this alarm?
        open_ticket = get_open_ticket_for_alarm(conn, alarm_key)
        if open_ticket:
            log.info(
                "Dedup: alarm %s already has open ticket %s — ignoring event %d",
                alarm_key,
                open_ticket,
                event_id,
            )
            mark_event_ignored(conn, event_id)
            continue

        # Get node metadata for rich ticket content
        node_info = get_node_info(conn, alarm_key)
        label = node_info["label"] if node_info else alarm_key.split(".")[-1]
        room_name = node_info["room_name"] if node_info else None
        root_path = node_info["root_path"] if node_info else None

        # Create Jira ticket
        ticket_key = jira.create_ticket(
            alarm_key=alarm_key,
            label=label,
            room_name=room_name,
            root_path=root_path,
            alarm_value=bool(event["value"]),
        )

        if ticket_key:
            mark_event_ticket_created(conn, event_id, ticket_key)
        else:
            log.warning("Ticket creation failed for event %d (%s) — will retry", event_id, alarm_key)


def process_resolved_pending(
    conn: sqlite3.Connection,
    jira: JiraClient,
) -> None:
    events = fetch_resolved_pending(conn)
    if not events:
        return

    log.info("Processing %d resolved_pending event(s)", len(events))

    for event in events:
        event_id = event["id"]
        alarm_key = event["alarm_key"]
        ticket_key = event["ticket_key"]

        if not ticket_key:
            log.warning("Event %d has no ticket_key — marking resolved directly", event_id)
            mark_event_resolved(conn, event_id)
            continue

        comment = (
            f"Alarm automatically resolved: OPC UA node '{alarm_key}' returned to FALSE. "
            f"Resolved at {datetime.now(timezone.utc).isoformat()}."
        )

        success = jira.resolve_ticket(ticket_key, comment=comment)
        if success:
            mark_event_resolved(conn, event_id)
        else:
            log.warning(
                "Failed to resolve ticket %s for event %d — will retry",
                ticket_key,
                event_id,
            )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # Load config
    if not CONFIG_FILE.exists():
        log.error("Config file not found: %s", CONFIG_FILE)
        sys.exit(1)

    with open(CONFIG_FILE) as f:
        cfg = yaml.safe_load(f)

    # Jira credentials from .env (dotenv)
    jira_url = os.getenv("JIRA_URL", "https://frigotec.atlassian.net")
    jira_user = os.getenv("JIRA_USER", "ml@helo.systems")
    jira_token = os.getenv("JIRA_API_TOKEN", "")
    jira_project = os.getenv("JIRA_PROJECT_KEY", "RKS")
    jira_account_id = os.getenv(
        "JIRA_ACCOUNT_ID", "712020:61ed9993-1bd0-4fe5-b049-005d72601888"
    )

    if not jira_token:
        log.error("JIRA_API_TOKEN not set in .env — cannot start")
        sys.exit(1)

    # Jira config from yaml (with .env overrides for credentials)
    jira_cfg = cfg.get("jira", {})
    alarm_cfg = cfg.get("alarm", {})

    issue_type = jira_cfg.get("issue_type", "Incident")
    labels = jira_cfg.get("labels", ["opcua", "automated", "alarm"])
    summary_template = jira_cfg.get(
        "summary_template", "[OPC UA] {client_id} / {room_id} — {alarm_label}"
    )
    resolve_transition_id = str(alarm_cfg.get("resolve_transition_id", "5"))
    resolve_comment = alarm_cfg.get(
        "resolve_comment",
        "Alarm automatically resolved: OPC UA Node returned to FALSE.",
    )
    auto_resolve = bool(alarm_cfg.get("auto_resolve", True))

    log.info("Starting Jira Worker")
    log.info("  Jira URL      : %s", jira_url)
    log.info("  Project       : %s", jira_project)
    log.info("  Auto-resolve  : %s (transition ID: %s)", auto_resolve, resolve_transition_id)
    log.info("  Poll interval : %ds", POLL_SLEEP)
    log.info("  DB            : %s", DB_FILE)

    # Init DB
    conn = db_connect()
    ensure_schema(conn)

    # Init Jira client
    jira = JiraClient(
        base_url=jira_url,
        user=jira_user,
        api_token=jira_token,
        project_key=jira_project,
        account_id=jira_account_id,
        issue_type=issue_type,
        labels=labels,
        summary_template=summary_template,
        resolve_transition_id=resolve_transition_id,
        resolve_comment=resolve_comment,
        auto_resolve=auto_resolve,
    )

    log.info("Worker ready — watching alarms.db for events")

    try:
        while True:
            try:
                process_new_events(conn, jira)
                process_resolved_pending(conn, jira)
            except sqlite3.Error as exc:
                log.error("DB error: %s — retrying in %ds", exc, POLL_SLEEP)
            except Exception as exc:
                log.exception("Unexpected error in worker loop: %s", exc)

            time.sleep(POLL_SLEEP)
    except KeyboardInterrupt:
        log.info("Worker stopped by user")
    finally:
        jira.close()
        conn.close()


if __name__ == "__main__":
    main()
