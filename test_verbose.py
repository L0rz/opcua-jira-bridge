"""
Verbose asyncua Verbindungstest mit maximalem Logging
"""
import asyncio
import logging
import sys
from asyncua import Client

# ALLES loggen
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(name)s] %(message)s")

URL = sys.argv[1] if len(sys.argv) > 1 else "opc.tcp://Deltafrigo:48010"


async def main():
    print(f"\nVerbose Connect → {URL}\n{'='*60}\n")

    client = Client(url=URL, timeout=15)
    client.set_user("OPC")
    client.set_password("OPC")

    # Session timeout hochsetzen
    client.session_timeout = 60000

    # Versuche Endpoints zu holen
    print("--- GetEndpoints ---")
    try:
        endpoints = await client.connect_and_get_server_endpoints()
        for ep in endpoints:
            print(f"  Endpoint: {ep.EndpointUrl}")
            print(f"  Security: {ep.SecurityPolicyUri}")
            print(f"  Mode:     {ep.SecurityMode}")
            for tok in ep.UserIdentityTokens:
                print(f"  Token:    {tok.TokenType} ({tok.PolicyId})")
            print()
    except Exception as e:
        print(f"  GetEndpoints fehlgeschlagen: {e}")
        print("  Versuche direkten Connect...\n")

    print("--- Direct Connect ---")
    try:
        async with client:
            print("✅ VERBUNDEN!")
    except Exception as e:
        print(f"❌ {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
