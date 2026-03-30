"""
OPC UA Low-Level Discovery Test
"""
import asyncio
import sys
from asyncua import Client
from asyncua.ua import ua_binary
import asyncua.ua as ua

URL = sys.argv[1] if len(sys.argv) > 1 else "opc.tcp://localhost:48010"


async def main():
    print(f"Discovery Test → {URL}\n{'='*60}")

    # 1. Endpoint Discovery (lightweight, kein Login nötig)
    print("\n1. GetEndpoints (Discovery Service)...")
    try:
        client = Client(url=URL, timeout=15)
        # get_server_endpoints nutzt nur den Discovery-Channel
        endpoints = await client.connect_and_get_server_endpoints()
        print(f"   ✅ {len(endpoints)} Endpoint(s) gefunden:")
        for ep in endpoints:
            print(f"   URL:      {ep.EndpointUrl}")
            print(f"   Security: {ep.SecurityPolicyUri.split('#')[-1]} / {ep.SecurityMode}")
            print(f"   Auth:     {[t.TokenType for t in ep.UserIdentityTokens]}")
            print()
    except Exception as e:
        print(f"   ❌ {type(e).__name__}: {e}")

    # 2. FindServers
    print("\n2. FindServers...")
    try:
        from asyncua.client.client import Client as C
        servers = await C.find_servers(URL)
        print(f"   ✅ {len(servers)} Server gefunden:")
        for s in servers:
            print(f"   Name: {s.ApplicationName.Text}")
            print(f"   URI:  {s.ApplicationUri}")
            for url in s.DiscoveryUrls:
                print(f"   Discovery URL: {url}")
    except Exception as e:
        print(f"   ❌ {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
