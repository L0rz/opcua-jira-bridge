"""
OPC UA Verbindungstest — verwendet async with (wie die echte Bridge)
Verwendung: python test_connection.py [opc.tcp://host:port]
"""
import asyncio
import sys
import socket
from asyncua import Client

URL = sys.argv[1] if len(sys.argv) > 1 else "opc.tcp://localhost:48010"


async def try_connect(label: str, client: Client):
    print(f"\n▶ {label}")
    try:
        async with client:
            print(f"  ✅ VERBUNDEN!")
            ns = await client.get_namespace_array()
            print(f"  Namespaces ({len(ns)}):")
            for i, n in enumerate(ns):
                print(f"     [{i}] {n}")
            print("  Objects:")
            for child in await client.nodes.objects.get_children():
                name = await child.read_browse_name()
                nid  = await child.read_node_id()
                print(f"     {name.Name:30s} → {nid}")
            return True
    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {e}")
        return False


async def main():
    # Erst TCP-Erreichbarkeit prüfen
    host = URL.replace("opc.tcp://", "").split(":")[0]
    port = int(URL.split(":")[-1])
    print(f"OPC UA Verbindungstest → {URL}")
    print(f"{'='*60}")
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect((host, port))
        print(f"✅ TCP Port {port} erreichbar\n")
    except Exception as e:
        print(f"❌ TCP Port {port} NICHT erreichbar: {e}")
        print("→ OPC UA Server läuft nicht oder falsche IP/Port")
        return
    finally:
        s.close()

    # Variante 1: Anonymous
    if await try_connect("Anonymous", Client(url=URL, timeout=10)):
        return

    # Variante 2: Username OPC/OPC
    c = Client(url=URL, timeout=10)
    c.set_user("OPC")
    c.set_password("OPC")
    if await try_connect("Username OPC/OPC", c):
        return

    # Variante 3: Username OPC/OPC + langer Session-Timeout
    c = Client(url=URL, timeout=10)
    c.set_user("OPC")
    c.set_password("OPC")
    c.session_timeout = 60000  # 60 Sekunden
    if await try_connect("Username OPC/OPC + 60s session timeout", c):
        return

    # Variante 4: Anonymous + langer Session-Timeout
    c = Client(url=URL, timeout=10)
    c.session_timeout = 60000
    if await try_connect("Anonymous + 60s session timeout", c):
        return

    # Variante 5: Anderer Hostname statt localhost
    import socket as s2
    hostname = s2.gethostname()
    url2 = URL.replace("localhost", hostname)
    if url2 != URL:
        c = Client(url=url2, timeout=10)
        c.set_user("OPC")
        c.set_password("OPC")
        if await try_connect(f"Username OPC/OPC @ {url2}", c):
            return

    print("\n❌ Alle Varianten fehlgeschlagen.")
    print("Bitte komplette Ausgabe an Jarvis schicken.")


if __name__ == "__main__":
    asyncio.run(main())
