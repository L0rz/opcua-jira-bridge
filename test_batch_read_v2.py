"""
Test: persistent connection + batch read + explicit secure channel renewal.
"""
import json
import time
import logging
from opcua import Client, ua

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("opcua").setLevel(logging.WARNING)

ENDPOINT = "opc.tcp://Deltafrigo:48010"
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
    nodeid_objects = [ua.NodeId.from_string(nid) for nid in nodeids]
    params = ua.ReadParameters()
    for nid in nodeid_objects:
        rv = ua.ReadValueId()
        rv.NodeId = nid
        rv.AttributeId = ua.AttributeIds.Value
        params.NodesToRead.append(rv)
    results = client.uaclient.read(params)
    return {nodeids[i]: results[i].Value.Value for i in range(len(nodeids))}


def main():
    client = Client(ENDPOINT, timeout=10)
    client.session_timeout = 86400000  # 24h
    # SecureChannel renewal: python-opcua's KeepAlive thread calls
    # open_secure_channel(renew=True) at secure_channel_timeout/2 interval
    client.secure_channel_timeout = 600000  # 10 min → renewal at 5 min
    client.set_user(USERNAME)
    client.set_password(PASSWORD)

    print("Connecting...")
    client.connect()
    print(f"Connected! secure_channel_timeout={client.secure_channel_timeout}ms")
    print(f"Batch-reading {len(NODEIDS)} nodes every 30s")
    print("KeepAlive thread should renew SecureChannel every ~5min")

    try:
        count = 0
        start = time.monotonic()
        while True:
            count += 1
            elapsed = time.monotonic() - start
            try:
                values = batch_read(client, NODEIDS)
                active = [name for nid, name in zip(NODEIDS, ALARM_NAMES) if values[nid]]
                print(f"[{count}] {time.strftime('%H:%M:%S')} ({elapsed:.0f}s) OK — active: {active or 'none'}")
            except Exception as e:
                print(f"[{count}] {time.strftime('%H:%M:%S')} ({elapsed:.0f}s) FAILED: {e}")
                break
            time.sleep(30)
    except KeyboardInterrupt:
        print(f"Stopped after {count} polls ({time.monotonic()-start:.0f}s)")


if __name__ == "__main__":
    main()
