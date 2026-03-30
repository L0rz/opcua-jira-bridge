"""Test multiple connects in a single Python process"""
import asyncio, sys, platform
from asyncua import Client

if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

URL = sys.argv[1] if len(sys.argv) > 1 else "opc.tcp://Deltafrigo:48010"

async def main():
    for i in range(5):
        print(f"\n--- Connect #{i+1} ---")
        client = Client(url=URL, timeout=10)
        client.session_timeout = 30000
        client._watchdog_intervall = 999999
        client.set_user("OPC")
        client.set_password("OPC")
        try:
            await client.connect()
            print("  Connected!")
            # Simulate bridge8: batch read alarm nodes
            alarm_nodeids = [
                "ns=2;s=SIMULATED.DataStructure.ROOMS.ROOM1.ALARMS.FEEDBACK_MOTOR1",
                "ns=2;s=SIMULATED.DataStructure.ROOMS.ROOM1.ALARMS.PHASE_LOSS",
                "ns=2;s=SIMULATED.DataStructure.ROOMS.ROOM1.ALARMS.PUMP_OVERLOAD",
            ]
            nodes = [client.get_node(nid) for nid in alarm_nodeids]
            values = await client.read_values(nodes)
            print(f"  Read OK: {values}")
            await client.disconnect()
            print("  Disconnected cleanly")
        except Exception as e:
            print(f"  FAILED: {e}")
            try: await client.disconnect()
            except: pass

        print(f"  Waiting 10s...")
        await asyncio.sleep(10)

asyncio.run(main())
