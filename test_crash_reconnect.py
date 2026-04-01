"""
Test #8: Persistent + batch-read + let it crash + reconnect after cooldown.

Key insight from all previous tests:
  - Explicit disconnect POISONS the E3 (SecureStream_Delete errors, blocks after ~12)
  - No disconnect = zombie sessions fill server faster
  - Renewal = E3 cancels the Future → crash
  - BEST RESULT: persistent + batch-read ran 21.5 min until SecureChannel died naturally

Strategy:
  - Stay persistent with batch-read (proven 21.5 min stable)
  - NEVER call client.disconnect()
  - When connection dies (~20 min), catch the error
  - Wait for E3 to clean up the dead session on its own (configurable cooldown)
  - Reconnect with a fresh client
  - ~3 connects per hour, ZERO explicit disconnects

Usage:
  python test_crash_reconnect.py                     # defaults (120s cooldown)
  python test_crash_reconnect.py --cooldown 60       # 60s cooldown between sessions
  python test_crash_reconnect.py --cooldown 180      # 3 min cooldown (conservative)
  python test_crash_reconnect.py --ipv4              # force IPv4
  python test_crash_reconnect.py --interval 15       # faster polling
"""

import argparse
import gc
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


def drop_client(client):
    """Drop client WITHOUT calling disconnect(). 
    Just close the socket and let garbage collection handle the rest.
    This avoids the SecureStream_Delete that poisons E3."""
    try:
        # Close the underlying socket directly — no OPC UA CloseSecureChannel
        if hasattr(client, 'uaclient') and hasattr(client.uaclient, '_uasocket'):
            sock = client.uaclient._uasocket
            if hasattr(sock, '_socket') and sock._socket:
                sock._socket.close()
    except Exception:
        pass
    # Let Python GC clean up the rest
    del client
    gc.collect()


def main():
    parser = argparse.ArgumentParser(description="Crash-reconnect OPC UA test")
    parser.add_argument("--ipv4", action="store_true",
                        help=f"Force IPv4 ({ENDPOINT_IPV4})")
    parser.add_argument("--interval", type=int, default=30,
                        help="Poll interval in seconds (default: 30)")
    parser.add_argument("--cooldown", type=int, default=120,
                        help="Seconds to wait after crash before reconnect (default: 120)")
    parser.add_argument("--max-sessions", type=int, default=0,
                        help="Stop after N sessions (0 = infinite)")
    args = parser.parse_args()

    endpoint = ENDPOINT_IPV4 if args.ipv4 else ENDPOINT_HOSTNAME

    print("=" * 72)
    print(f"  Crash-Reconnect Test (NO explicit disconnect)")
    print(f"  Endpoint       : {endpoint}")
    print(f"  Poll interval  : {args.interval}s")
    print(f"  Post-crash wait: {args.cooldown}s")
    print(f"  Nodes          : {len(NODEIDS)}")
    print(f"  Strategy       : persistent → crash → wait → reconnect")
    print(f"                   NEVER calls client.disconnect()")
    print("=" * 72)

    total_start = time.monotonic()
    total_polls = 0
    total_fails = 0
    session_num = 0
    last_active = None
    session_durations = []

    client = None

    try:
        while True:
            session_num += 1
            if args.max_sessions and session_num > args.max_sessions:
                break

            # ── Connect ──────────────────────────────────────────────
            print(f"\n{'─'*72}")
            print(f"  SESSION #{session_num} — connecting...")

            client = Client(endpoint, timeout=10)
            client.session_timeout = 86400000  # request 24h
            # Do NOT set secure_channel_timeout — let it use default
            # Do NOT enable any KeepAlive/renewal
            client.set_user(USERNAME)
            client.set_password(PASSWORD)

            try:
                t0 = time.monotonic()
                client.connect()
                conn_ms = (time.monotonic() - t0) * 1000
                print(f"  Connected in {conn_ms:.0f}ms. Running until crash...")
                print(f"{'─'*72}")
            except Exception as e:
                print(f"  ❌ Connect FAILED: {e}")
                print(f"  Waiting {args.cooldown}s...")
                drop_client(client)
                client = None
                time.sleep(args.cooldown)
                continue

            # ── Poll until crash ──────────────────────────────────
            session_start = time.monotonic()
            session_polls = 0

            while True:
                session_age = time.monotonic() - session_start
                total_age = time.monotonic() - total_start
                total_polls += 1
                session_polls += 1

                try:
                    values = batch_read(client, NODEIDS)
                    active = sorted([name for nid, name in zip(NODEIDS, ALARM_NAMES)
                                     if values.get(nid)])
                    changed = "" if active == last_active else " ← CHANGED"
                    last_active = active

                    print(f"  [{total_polls:4d}] {time.strftime('%H:%M:%S')} "
                          f"S{session_num}#{session_polls:3d} "
                          f"({total_age:7.0f}s total, session: {session_age:5.0f}s) "
                          f"OK  active={active or 'none'}{changed}")

                except Exception as e:
                    session_duration = time.monotonic() - session_start
                    session_durations.append(session_duration)
                    total_fails += 1

                    print(f"\n  💥 SESSION #{session_num} CRASHED after "
                          f"{session_duration:.0f}s ({session_duration/60:.1f} min)")
                    print(f"     Error: {e}")
                    print(f"     Polls this session: {session_polls}")
                    print(f"     Total polls so far: {total_polls}")

                    # Drop client without disconnect
                    print(f"  🗑️  Dropping client (no disconnect!)...")
                    drop_client(client)
                    client = None

                    print(f"  ⏳ Waiting {args.cooldown}s for E3 to clean up dead session...")
                    time.sleep(args.cooldown)
                    break

                time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\n\nStopped by user.")
        if client:
            drop_client(client)

    # ── Summary ───────────────────────────────────────────────────
    total_elapsed = time.monotonic() - total_start
    print()
    print("=" * 72)
    print(f"  SUMMARY")
    print(f"  Total duration  : {total_elapsed:.0f}s ({total_elapsed/60:.1f} min)")
    print(f"  Sessions        : {session_num}")
    print(f"  Total polls     : {total_polls} ({total_fails} crashes)")
    print(f"  Success rate    : {(total_polls-total_fails)/max(total_polls,1)*100:.1f}%")
    if session_durations:
        avg_dur = sum(session_durations) / len(session_durations)
        print(f"  Avg session life: {avg_dur:.0f}s ({avg_dur/60:.1f} min)")
        print(f"  Session lives   : {', '.join(f'{d:.0f}s' for d in session_durations)}")
    print(f"  Endpoint        : {endpoint}")
    print(f"  Cooldown        : {args.cooldown}s")
    print("=" * 72)


if __name__ == "__main__":
    main()
