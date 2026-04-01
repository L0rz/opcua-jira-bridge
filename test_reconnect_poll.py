"""
Test #6: Connect-per-Poll with python-opcua (sync) + Batch-Read + IPv4.

Tests untested hypotheses from Trilium checklist:
  #7  — Force IPv4 (instead of IPv6 link-local fe80::...)
  #11 — Longer poll interval (60s default)
  NEW — Connect-per-poll with python-opcua sync (only tested with asyncua before)

Pattern: connect → batch-read → disconnect → sleep → repeat
Each connection lives <2 seconds. No SecureChannel renewal needed.

Usage:
  python test_reconnect_poll.py                    # hostname (default)
  python test_reconnect_poll.py --ipv4             # force IPv4: 192.168.15.10
  python test_reconnect_poll.py --interval 30      # faster polling
  python test_reconnect_poll.py --no-disconnect    # skip explicit disconnect
  python test_reconnect_poll.py --ipv4 --interval 30 --no-disconnect
"""

import argparse
import logging
import time
from datetime import datetime
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
    """Single ReadRequest for all 24 nodes — one network round-trip."""
    params = ua.ReadParameters()
    for nid_str in nodeids:
        rv = ua.ReadValueId()
        rv.NodeId = ua.NodeId.from_string(nid_str)
        rv.AttributeId = ua.AttributeIds.Value
        params.NodesToRead.append(rv)
    results = client.uaclient.read(params)
    return {nodeids[i]: results[i].Value.Value for i in range(len(nodeids))}


def do_poll(endpoint, username, password, nodeids, do_disconnect=True):
    """Connect, batch-read, disconnect. Returns (values_dict, connect_ms, read_ms)."""
    client = Client(endpoint, timeout=10)
    client.session_timeout = 30000  # 30s is fine — we disconnect immediately anyway
    client.set_user(username)
    client.set_password(password)

    t0 = time.monotonic()
    client.connect()
    t1 = time.monotonic()

    values = batch_read(client, nodeids)
    t2 = time.monotonic()

    if do_disconnect:
        try:
            client.disconnect()
        except Exception:
            pass  # best-effort disconnect

    connect_ms = (t1 - t0) * 1000
    read_ms = (t2 - t1) * 1000
    return values, connect_ms, read_ms


def main():
    parser = argparse.ArgumentParser(description="Connect-per-poll OPC UA test")
    parser.add_argument("--ipv4", action="store_true",
                        help=f"Force IPv4 endpoint ({ENDPOINT_IPV4})")
    parser.add_argument("--interval", type=int, default=60,
                        help="Poll interval in seconds (default: 60)")
    parser.add_argument("--no-disconnect", action="store_true",
                        help="Skip explicit disconnect (let server clean up)")
    parser.add_argument("--max-polls", type=int, default=0,
                        help="Stop after N polls (0 = infinite)")
    args = parser.parse_args()

    endpoint = ENDPOINT_IPV4 if args.ipv4 else ENDPOINT_HOSTNAME
    do_disconnect = not args.no_disconnect

    print("=" * 70)
    print(f"  Connect-per-Poll Test (python-opcua sync + batch-read)")
    print(f"  Endpoint    : {endpoint}")
    print(f"  IPv4 forced : {args.ipv4}")
    print(f"  Interval    : {args.interval}s")
    print(f"  Disconnect  : {do_disconnect}")
    print(f"  Nodes       : {len(NODEIDS)}")
    print(f"  Max polls   : {args.max_polls or 'infinite'}")
    print("=" * 70)

    # Stats
    success_count = 0
    fail_count = 0
    total_connect_ms = 0
    total_read_ms = 0
    start = time.monotonic()
    last_active = None

    count = 0
    try:
        while True:
            count += 1
            if args.max_polls and count > args.max_polls:
                break

            elapsed = time.monotonic() - start
            try:
                values, connect_ms, read_ms = do_poll(
                    endpoint, USERNAME, PASSWORD, NODEIDS, do_disconnect
                )
                success_count += 1
                total_connect_ms += connect_ms
                total_read_ms += read_ms

                active = sorted([name for nid, name in zip(NODEIDS, ALARM_NAMES)
                                 if values.get(nid)])

                # Show change indicator
                changed = "" if active == last_active else " ← CHANGED"
                last_active = active

                avg_conn = total_connect_ms / success_count
                avg_read = total_read_ms / success_count

                print(f"[{count:3d}] {time.strftime('%H:%M:%S')} ({elapsed:6.0f}s) "
                      f"OK  conn={connect_ms:5.0f}ms read={read_ms:4.0f}ms "
                      f"(avg: {avg_conn:.0f}/{avg_read:.0f}ms) "
                      f"active={active or 'none'}{changed}")

            except Exception as e:
                fail_count += 1
                print(f"[{count:3d}] {time.strftime('%H:%M:%S')} ({elapsed:6.0f}s) "
                      f"FAIL #{fail_count}: {e}")

                # If we get blocked, increase wait
                if fail_count >= 3:
                    extra_wait = min(fail_count * 30, 300)
                    print(f"  → {fail_count} consecutive fails, extra wait {extra_wait}s "
                          f"(server may be rate-limiting)")
                    time.sleep(extra_wait)
                    fail_count = 0  # reset after cooldown

            time.sleep(args.interval)

    except KeyboardInterrupt:
        pass

    # Summary
    elapsed = time.monotonic() - start
    print()
    print("=" * 70)
    print(f"  SUMMARY")
    print(f"  Duration     : {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print(f"  Polls        : {count} ({success_count} OK, {fail_count} failed)")
    print(f"  Success rate : {success_count/max(count,1)*100:.1f}%")
    if success_count:
        print(f"  Avg connect  : {total_connect_ms/success_count:.0f}ms")
        print(f"  Avg read     : {total_read_ms/success_count:.0f}ms")
    print(f"  Endpoint     : {endpoint}")
    print("=" * 70)


if __name__ == "__main__":
    main()
