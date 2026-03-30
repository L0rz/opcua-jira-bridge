"""
Single-shot OPC UA reader. Called by opcua_poller as subprocess.
Reads all alarm nodes, prints JSON to stdout, exits cleanly.
"""
import asyncio
import json
import platform
import sys
from asyncua import Client

if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def main():
    endpoint = sys.argv[1]
    username = sys.argv[2]
    password = sys.argv[3]
    nodeids = json.loads(sys.argv[4])

    client = Client(url=endpoint, timeout=10)
    client.session_timeout = 30000
    client._watchdog_intervall = 999999
    client.set_user(username)
    client.set_password(password)

    await client.connect()
    nodes = [client.get_node(nid) for nid in nodeids]
    values = await client.read_values(nodes)
    await client.disconnect()

    # Output as JSON
    result = {nodeids[i]: values[i] for i in range(len(nodeids))}
    print(json.dumps(result))


asyncio.run(main())
