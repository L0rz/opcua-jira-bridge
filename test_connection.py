"""
Testet verschiedene Verbindungsmethoden zum OPC UA Server.
Führt alle Varianten durch und zeigt welche funktioniert.

Verwendung: python test_connection.py [opc.tcp://host:port]
"""
import asyncio
import sys
from asyncua import Client

URL = sys.argv[1] if len(sys.argv) > 1 else "opc.tcp://localhost:48010"


async def try_connect(label: str, setup_fn):
    print(f"\n▶ Teste: {label}")
    try:
        client = setup_fn()
        async with client:
            ns = await client.get_namespace_array()
            print(f"  ✅ ERFOLG! Namespaces: {len(ns)}")
            for i, n in enumerate(ns):
                print(f"     [{i}] {n}")

            print("  🌳 Objects:")
            for child in await client.nodes.objects.get_children():
                name = await child.read_browse_name()
                nid  = await child.read_node_id()
                print(f"     {name.Name:30s} → {nid}")
            return True
    except Exception as e:
        print(f"  ❌ Fehler: {e}")
        return False


async def main():
    print(f"OPC UA Verbindungstest → {URL}\n{'='*60}")

    variants = [
        ("Anonymous", lambda: Client(url=URL, timeout=10)),
        ("Username OPC/OPC", lambda: _with_user(Client(url=URL, timeout=10), "OPC", "OPC")),
        ("Username opc/opc (lowercase)", lambda: _with_user(Client(url=URL, timeout=10), "opc", "opc")),
        ("Username admin/admin", lambda: _with_user(Client(url=URL, timeout=10), "admin", "admin")),
        ("Username Administrator/''", lambda: _with_user(Client(url=URL, timeout=10), "Administrator", "")),
    ]

    for label, setup in variants:
        ok = await try_connect(label, setup)
        if ok:
            print(f"\n✅ FUNKTIONIERT: {label}")
            print("→ Bitte diese Auth-Methode in opcua_config_real_server.yaml eintragen!")
            break
    else:
        print("\n❌ Keine Verbindungsmethode hat funktioniert.")
        print("Mögliche Ursachen:")
        print("  - Falscher Port oder Endpoint-Pfad")
        print("  - Server erwartet Zertifikat-Authentifizierung")
        print("  - Firewall blockiert")
        print("  - Server läuft nicht / anderer Dienst auf Port 48010")

        # Prüfe ob Port offen ist
        import socket
        host = URL.replace("opc.tcp://", "").split(":")[0]
        port = int(URL.split(":")[-1])
        s = socket.socket()
        s.settimeout(3)
        try:
            s.connect((host, port))
            print(f"\n  ✅ Port {port} ist offen — Server antwortet, aber lehnt ab")
        except:
            print(f"\n  ❌ Port {port} nicht erreichbar — Server läuft nicht oder Firewall")
        finally:
            s.close()


def _with_user(client, user, password):
    client.set_user(user)
    client.set_password(password)
    return client


if __name__ == "__main__":
    asyncio.run(main())
