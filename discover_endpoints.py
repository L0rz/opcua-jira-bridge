"""
Discovert alle verfügbaren Endpoints und Namespaces des OPC UA Servers.
Hilft bei Verbindungsproblemen.

Verwendung: python discover_endpoints.py [opc.tcp://host:port]
"""
import asyncio
import sys
from asyncua import Client
from asyncua.ua import SecurityPolicyType

async def discover(url: str):
    print(f"\n🔍 Endpoint Discovery: {url}\n")

    # 1. Endpoints ohne Auth discovern
    try:
        endpoints = await Client.get_best_endpoint(url)
        print("Best endpoint:", endpoints)
    except Exception as e:
        print(f"get_best_endpoint fehlgeschlagen: {e}")

    try:
        from asyncua.client.client import Client as C
        tmp = C(url=url)
        eps = await tmp.connect_and_get_server_endpoints()
        print(f"\n📋 Verfügbare Endpoints ({len(eps)}):")
        for ep in eps:
            print(f"  URL:            {ep.EndpointUrl}")
            print(f"  SecurityPolicy: {ep.SecurityPolicyUri.split('#')[-1]}")
            print(f"  SecurityMode:   {ep.SecurityMode}")
            print(f"  Transport:      {ep.TransportProfileUri.split('/')[-1]}")
            print()
    except Exception as e:
        print(f"Endpoint-Liste fehlgeschlagen: {e}")

    # 2. Mit Username verbinden und Namespaces lesen
    print("🔗 Verbinde mit Username OPC/OPC...")
    client = Client(url=url, timeout=10)
    client.set_user("OPC")
    client.set_password("OPC")

    try:
        async with client:
            print("✅ Verbunden!\n")

            ns_array = await client.get_namespace_array()
            print(f"📦 Namespaces ({len(ns_array)}):")
            for i, ns in enumerate(ns_array):
                print(f"  [{i}] {ns}")

            print("\n🌳 Root/Objects Children:")
            objects = client.nodes.objects
            children = await objects.get_children()
            for child in children:
                name = await child.read_browse_name()
                nid = await child.read_node_id()
                print(f"  {name.Name:30s} → {nid}")

    except Exception as e:
        print(f"❌ Verbindung fehlgeschlagen: {e}")


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "opc.tcp://localhost:48010"
    asyncio.run(discover(url))
