"""
Test: Sync OPC UA read using python-opcua (opcua) instead of asyncua.
Cleaner secure channel teardown — may fix Elipse E3 SecureStream_Delete errors.
"""
import json
import sys
from opcua import Client

def main():
    endpoint = sys.argv[1]
    username = sys.argv[2]
    password = sys.argv[3]
    nodeids = json.loads(sys.argv[4])

    client = Client(endpoint, timeout=10)
    client.session_timeout = 30000
    client.set_user(username)
    client.set_password(password)

    try:
        client.connect()
        nodes = [client.get_node(nid) for nid in nodeids]
        values = [n.get_value() for n in nodes]
        result = {nodeids[i]: values[i] for i in range(len(nodeids))}
        print(json.dumps(result))
    finally:
        try:
            client.disconnect()
        except Exception:
            pass

if __name__ == "__main__":
    main()
