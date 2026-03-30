"""
Testet verschiedene Verbindungsmethoden zum OPC UA Server.
Führt alle Varianten durch und zeigt welche funktioniert.

Verwendung: python test_connection.py [opc.tcp://host:port]
"""
import asyncio
import sys
from asyncua import Client
from asyncua.ua import MessageSecurityMode
from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256

URL = sys.argv[1] if len(sys.argv) > 1 else "opc.tcp://localhost:48010"


async def try_connect(label: str, client: Client):
    print(f"\n▶ Teste: {label}")
    try:
        await client.connect()
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
        await client.disconnect()
        return True
    except Exception as e:
        print(f"  ❌ {e}")
        try:
            await client.disconnect()
        except:
            pass
        return False


async def main():
    print(f"OPC UA Verbindungstest → {URL}\n{'='*60}")

    # Variante 1: Anonymous
    c = Client(url=URL, timeout=10)
    if await try_connect("Anonymous", c):
        return

    # Variante 2: Username/Password — Security None
    c = Client(url=URL, timeout=10)
    c.set_user("OPC")
    c.set_password("OPC")
    if await try_connect("Username OPC/OPC (Security: None)", c):
        return

    # Variante 3: Username/Password — explizit kein Security Mode setzen
    c = Client(url=URL, timeout=10)
    c.set_user("OPC")
    c.set_password("OPC")
    c.set_security_string("None,None,,,")
    if await try_connect("Username OPC/OPC + explicit None security", c):
        return

    # Variante 4: Session-Timeout erhöhen
    c = Client(url=URL, timeout=30)
    c.set_user("OPC")
    c.set_password("OPC")
    c.session_timeout = 30000
    if await try_connect("Username OPC/OPC + 30s session timeout", c):
        return

    # Variante 5: Ohne application_uri
    c = Client(url=URL, timeout=10)
    c.set_user("OPC")
    c.set_password("OPC")
    c.application_uri = "urn:opcua-jira-bridge:client"
    if await try_connect("Username OPC/OPC + custom application_uri", c):
        return

    print("\n❌ Alle Varianten fehlgeschlagen.")
    print("Bitte Ausgabe an Jarvis schicken für weitere Diagnose.")


if __name__ == "__main__":
    asyncio.run(main())
