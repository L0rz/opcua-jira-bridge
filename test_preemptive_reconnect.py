"""
Test #7: Persistent connection + batch-read + preemptive reconnect before
SecureChannel timeout.

Strategy:
  - Keep persistent connection (proven stable for ~21 min)
  - Batch-read every 30s (proven to reduce server load)
  - Proactively disconnect + reconnect every ~15 min (BEFORE the ~20 min
    SecureChannel timeout kills the connection)
  - Wait a few seconds between disconnect and reconnect (let E3 clean up)

This combines the best of both worlds:
  - Persistent = fewer connects (E3 blocks after ~12)
  - Preemptive = never hits SecureChannel timeout

Usage:
  python test_preemptive_reconnect.py                        # defaults
  python test_preemptive_reconnect.py --reconnect-min 10     # reconnect every 10 min
  python test_preemptive_reconnect.py --interval 15          # read every 15s
  python test_preemptive_reconnect.py --ipv4                 # force IPv4
  python test_preemptive_reconnect.py --cooldown 10          # 10s pause between sessions
"""

import argparse
import logging
import time
from opcua import Client, ua

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logging.getLogger("opcua").setLevel(logging.WARNING)

ENDPOINT_HOSTNAME = "opc.tcp://Deltafrigo:48010"
ENDPOINT_IPV4 = "opc.tcp://192.168.15.10:48010"
USERNAME = "OPC"
PASSWORD = "OPC"

ALARM_PREFIX = "ns=2;s=SIMULATED.DataStructure.ROOMS.ROOM1.ALARMS."
ALARM_NAMES = [
    "MAINTENANCE", "GENERAL_OVERLOAD", "PUMP_OVERLOAD",
    "ROOM_TEMP_MEASURING_MODE", "FEEDBACK_MOTOR1", "FEEDBACK_MOTOR2",
    "FEEDBACK_MOTOR3", "TOP_AISLE_TEMP_MEASURING_MODE",
    "BOTTOM_AISLE_TEMP_MEASURING_MODE", "AISLE_TEMP_MEASURING_MODE",
    "MIX_TEMP_FAILURE", "ROOM_PRESSURE_FAILURE", "ROOM_PRESSURE_LOW",
    "ROOM_PRESSURE_HIGH", "MIX_DEVIATION",
    "INITIAL_PRESSURE_TEST_DIDNT_PASS", "UNLOCKED_DOOR",
    "UNLOCKED_LEFT_DOOR", "UNLOCKED_RIGHT_DOOR", "UNSEALED_DOOR",
    "UNSEALED_LEFT_DOOR", "UNSEALED_RIGHT_DOOR",
    "INITIAL_PRESSURE_TEST_PRESSURE_NOT_REACHING", "PHASE_LOSS",
]
NODEIDS = [ALARM_PREFIX + name for name in ALARM_NAMES]


def batch_read(client, nodeids):
    """Single ReadRequest for all 24 nodes."""
    params = ua.ReadParameters()
    for nid_str in nodeids:
        rv = ua.ReadValueId()
        rv.NodeId = ua.NodeId.from_string(nid_str)
        rv.AttributeId = ua.AttributeIds.Value
        params.NodesToRead.append(rv)
    results = client.uaclient.read(params)
    return {nodeids[i]: results[i].Value.Value for i in range(len(nodeids))}


def create_client(endpoint, session_timeout_ms=86400000):
    """Create a fresh OPC UA client."""
    client = Client(endpoint, timeout=10)
    client.session_timeout = session_timeout_ms
    client.set_user(USERNAME)
    client.set_password(PASSWORD)
    return client


def safe_disconnect(client):
    """Best-effort disconnect. Never raise."""
    try:
        client.disconnect()
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="Preemptive reconnect OPC UA test")
    parser.add_argument("--ipv4", action="store_true",
                        help=f"Force IPv4 ({ENDPOINT_IPV4})")
    parser.add_argument("--interval", type=int, default=30,
                        help="Poll interval in seconds (default: 30)")
    parser.add_argument("--reconnect-min", type=int, default=15,
                        help="Preemptive reconnect after N minutes (default: 15)")
    parser.add_argument("--cooldown", type=int, default=5,
                        help="Seconds to wait between disconnect and reconnect (default: 5)")
    parser.add_argument("--max-sessions", type=int, default=0,
                        help="Stop after N reconnect cycles (0 = infinite)")
    args = parser.parse_args()

    endpoint = ENDPOINT_IPV4 if args.ipv4 else ENDPOINT_HOSTNAME
    reconnect_seconds = args.reconnect_min * 60

    print("=" * 72)
    print(f"  Preemptive Reconnect Test (python-opcua sync + batch-read)")
    print(f"  Endpoint       : {endpoint}")
    print(f"  Poll interval  : {args.interval}s")
    print(f"  Reconnect every: {args.reconnect_min} min ({reconnect_seconds}s)")
    print(f"  Cooldown       : {args.cooldown}s between sessions")
    print(f"  Nodes          : {len(NODEIDS)}")
    print("=" * 72)

    total_start = time.monotonic()
    total_polls = 0
    total_fails = 0
    session_num = 0
    last_active = None

    try:
        while True:
            session_num += 1
            if args.max_sessions and session_num > args.max_sessions:
                break

            # ── Connect ──────────────────────────────────────────────
            print(f"\n{'─'*72}")
            print(f"  SESSION #{session_num} — connecting to {endpoint} ...")
            
            client = create_client(endpoint)
            try:
                t0 = time.monotonic()
                client.connect()
                conn_ms = (time.monotonic() - t0) * 1000
                print(f"  Connected in {conn_ms:.0f}ms. "
                      f"Will reconnect in {args.reconnect_min} min.")
                print(f"{'─'*72}")
            except Exception as e:
                print(f"  ❌ Connect FAILED: {e}")
                print(f"  Waiting 60s before retry...")
                time.sleep(60)
                continue

            # ── Poll loop (until reconnect timer) ─────────────────
            session_start = time.monotonic()
            session_polls = 0
            session_fails = 0

            while True:
                session_age = time.monotonic() - session_start
                total_age = time.monotonic() - total_start

                # Time to reconnect?
                if session_age >= reconnect_seconds:
                    print(f"\n  ⏰ Session #{session_num} lived {session_age:.0f}s "
                          f"(limit: {reconnect_seconds}s) — preemptive reconnect")
                    break

                total_polls += 1
                session_polls += 1

                try:
                    values = batch_read(client, NODEIDS)
                    active = sorted([name for nid, name in zip(NODEIDS, ALARM_NAMES)
                                     if values.get(nid)])
                    changed = "" if active == last_active else " ← CHANGED"
                    last_active = active

                    remaining = reconnect_seconds - session_age
                    print(f"  [{total_polls:4d}] {time.strftime('%H:%M:%S')} "
                          f"S{session_num}#{session_polls:3d} "
                          f"({total_age:7.0f}s total, {remaining:4.0f}s left) "
                          f"OK  active={active or 'none'}{changed}")

                    session_fails = 0  # reset on success

                except Exception as e:
                    session_fails += 1
                    total_fails += 1
                    print(f"  [{total_polls:4d}] {time.strftime('%H:%M:%S')} "
                          f"S{session_num}#{session_polls:3d} "
                          f"({total_age:7.0f}s total) "
                          f"FAIL: {e}")

                    if session_fails >= 3:
                        print(f"  ❌ 3 consecutive fails in session — forcing reconnect")
                        break

                time.sleep(args.interval)

            # ── Disconnect + cooldown ─────────────────────────────
            print(f"  Disconnecting session #{session_num} "
                  f"({session_polls} polls, {session_fails} fails)...")
            safe_disconnect(client)

            print(f"  Cooldown {args.cooldown}s (let E3 clean up)...")
            time.sleep(args.cooldown)

    except KeyboardInterrupt:
        print("\n\nStopped by user.")
        safe_disconnect(client)

    # ── Summary ───────────────────────────────────────────────────
    total_elapsed = time.monotonic() - total_start
    print()
    print("=" * 72)
    print(f"  SUMMARY")
    print(f"  Total duration : {total_elapsed:.0f}s ({total_elapsed/60:.1f} min)")
    print(f"  Sessions       : {session_num}")
    print(f"  Total polls    : {total_polls} ({total_fails} failed)")
    print(f"  Success rate   : {(total_polls-total_fails)/max(total_polls,1)*100:.1f}%")
    print(f"  Endpoint       : {endpoint}")
    print(f"  Reconnect every: {args.reconnect_min} min")
    print("=" * 72)


if __name__ == "__main__":
    main()
