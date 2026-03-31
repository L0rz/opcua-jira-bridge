"""
Test: single persistent connection with periodic reads.
No connect/disconnect cycling — just one session, read in a loop.
"""
import json
import sys
import time
from opcua import Client

def main():
    endpoint = "opc.tcp://Deltafrigo:48010"
    username = "OPC"
    password = "OPC"
    test_node = "ns=2;s=SIMULATED.DataStructure.ROOMS.ROOM1.ALARMS.PUMP_OVERLOAD"

    client = Client(endpoint, timeout=10)
    client.session_timeout = 600000  # request 10 min
    client.set_user(username)
    client.set_password(password)

    print("Connecting...")
    client.connect()
    
    # Check what the server actually gave us
    print(f"Connected! Requesting reads every 30s...")
    
    node = client.get_node(test_node)
    
    try:
        count = 0
        while True:
            count += 1
            try:
                val = node.get_value()
                print(f"[{count}] {time.strftime('%H:%M:%S')} PUMP_OVERLOAD = {val}")
            except Exception as e:
                print(f"[{count}] {time.strftime('%H:%M:%S')} READ FAILED: {e}")
                break
            time.sleep(30)
    except KeyboardInterrupt:
        print("Stopping...")
    finally:
        try:
            client.disconnect()
            print("Disconnected cleanly")
        except:
            pass

if __name__ == "__main__":
    main()
