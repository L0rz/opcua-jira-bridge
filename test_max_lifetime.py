"""
Test #9: Persistent + batch-read + MAXIMUM SecureChannel lifetime.

Critical finding: E3 NEVER cleans up dead sessions after crash.
Server must be restarted manually. This means:
  - Every connect consumes a permanent "slot"
  - After ~12 connects → server blocks ALL new connections
  - Reconnect strategies are USELESS
  - We get ONE connection — it must last forever

Strategy:
  - Request maximum SecureChannel lifetime (24h) at connect time
  - Request maximum session timeout (24h)
  - Disable KeepAlive/renewal threads (they crash on E3)
  - Batch-read every 30s
  - Log the ACTUAL negotiated lifetime the server returns
  - See how long it actually survives

Key question: Does E3 accept a long SecureChannel lifetime?
If yes → problem solved, no renewal needed.
If no → we need asyncua's renewal or server-side config change.

Usage:
  python test_max_lifetime.py
  python test_max_lifetime.py --ipv4
  python test_max_lifetime.py --lifetime-hours 4
"""

import argparse
import logging
import time
import sys
from opcua import Client, ua

# Verbose logging to see SecureChannel negotiation
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
# Enable opcua debug temporarily to see channel negotiation
opcua_logger = logging.getLogger("opcua")
opcua_logger.setLevel(logging.DEBUG)

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


def inspect_secure_channel(client):
    """Try to read the negotiated SecureChannel parameters."""
    info = {}
    try:
        uaclient = client.uaclient
        # The internal secure channel timeout that was negotiated
        if hasattr(uaclient, '_security_token'):
            token = uaclient._security_token
            if hasattr(token, 'RevisedLifetime'):
                info['revised_lifetime_ms'] = token.RevisedLifetime
            if hasattr(token, 'CreatedAt'):
                info['created_at'] = str(token.CreatedAt)
        # Check what we requested
        info['requested_timeout_ms'] = client.secure_channel_timeout
        info['requested_session_timeout_ms'] = client.session_timeout
    except Exception as e:
        info['error'] = str(e)
    
    # Also try to get it from the open secure channel response
    try:
        if hasattr(uaclient, '_open_secure_channel_response'):
            resp = uaclient._open_secure_channel_response
            info['response_lifetime'] = resp.SecurityToken.RevisedLifetime
    except Exception:
        pass
    
    return info


def main():
    parser = argparse.ArgumentParser(description="Max lifetime OPC UA test")
    parser.add_argument("--ipv4", action="store_true",
                        help=f"Force IPv4 ({ENDPOINT_IPV4})")
    parser.add_argument("--interval", type=int, default=30,
                        help="Poll interval in seconds (default: 30)")
    parser.add_argument("--lifetime-hours", type=int, default=24,
                        help="Requested SecureChannel lifetime in hours (default: 24)")
    args = parser.parse_args()

    endpoint = ENDPOINT_IPV4 if args.ipv4 else ENDPOINT_HOSTNAME
    lifetime_ms = args.lifetime_hours * 3600 * 1000

    print("=" * 72)
    print(f"  Max Lifetime Test")
    print(f"  Endpoint          : {endpoint}")
    print(f"  Poll interval     : {args.interval}s")
    print(f"  Requested lifetime: {args.lifetime_hours}h ({lifetime_ms}ms)")
    print(f"  Strategy          : request max lifetime, NO renewal, see what happens")
    print("=" * 72)

    # ── Connect with maximum timeouts ─────────────────────────────
    client = Client(endpoint, timeout=30)
    client.session_timeout = lifetime_ms           # 24h session
    client.secure_channel_timeout = lifetime_ms    # 24h secure channel
    client.set_user(USERNAME)
    client.set_password(PASSWORD)

    # Disable the KeepAlive thread that does renewal (it crashes on E3)
    # python-opcua starts a KeepAlive thread that calls open_secure_channel(renew=True)
    # We want to prevent that entirely
    if hasattr(client, '_watchdog_interval'):
        client._watchdog_interval = 999999  # effectively disable

    print("\nConnecting (requesting maximum lifetime)...")
    print("Watch the DEBUG output for 'RevisedLifetime' in the server response!\n")

    try:
        client.connect()
    except Exception as e:
        print(f"❌ Connect failed: {e}")
        sys.exit(1)

    # ── Inspect what the server actually granted ──────────────────
    print("\n" + "=" * 72)
    channel_info = inspect_secure_channel(client)
    print(f"  SECURE CHANNEL NEGOTIATION RESULT:")
    for k, v in channel_info.items():
        if 'lifetime' in k.lower() or 'timeout' in k.lower():
            ms = v
            if isinstance(ms, (int, float)):
                print(f"    {k}: {ms}ms = {ms/1000:.0f}s = {ms/60000:.1f}min = {ms/3600000:.2f}h")
            else:
                print(f"    {k}: {v}")
        else:
            print(f"    {k}: {v}")
    print("=" * 72)

    # Now reduce opcua logging to WARNING for clean poll output
    opcua_logger.setLevel(logging.WARNING)

    # ── Disable KeepAlive thread if running ───────────────────────
    # python-opcua may have already started a KeepAlive thread
    try:
        if hasattr(client, 'keepalive') and client.keepalive:
            client.keepalive.stop()
            print("  ⚠️  Stopped KeepAlive thread (renewal disabled)")
    except Exception:
        pass

    # ── Poll loop ─────────────────────────────────────────────────
    print(f"\nPolling every {args.interval}s. NO renewal, NO disconnect.")
    print("Watching for natural death...\n")

    start = time.monotonic()
    count = 0
    last_active = None

    try:
        while True:
            count += 1
            elapsed = time.monotonic() - start

            try:
                values = batch_read(client, NODEIDS)
                active = sorted([name for nid, name in zip(NODEIDS, ALARM_NAMES)
                                 if values.get(nid)])
                changed = "" if active == last_active else " ← CHANGED"
                last_active = active

                # Milestone markers
                milestone = ""
                if elapsed > 0 and int(elapsed) % 300 < args.interval:  # every 5 min
                    milestone = f" ⏱️ {elapsed/60:.0f}min"

                print(f"  [{count:4d}] {time.strftime('%H:%M:%S')} ({elapsed:7.0f}s = "
                      f"{elapsed/60:5.1f}min) OK  active={active or 'none'}"
                      f"{changed}{milestone}")

            except Exception as e:
                duration = time.monotonic() - start
                print(f"\n  💥 DIED after {duration:.0f}s ({duration/60:.1f} min)")
                print(f"     Error: {e}")
                print(f"     Polls completed: {count}")
                print(f"\n  This is the actual SecureChannel lifetime the E3 enforces.")
                print(f"  If this is still ~20 min, the server ignores our lifetime request.")
                break

            time.sleep(args.interval)

    except KeyboardInterrupt:
        elapsed = time.monotonic() - start
        print(f"\n  Stopped after {elapsed:.0f}s ({elapsed/60:.1f} min), {count} polls")
        print(f"  Connection was STILL ALIVE! ✅")


if __name__ == "__main__":
    main()
