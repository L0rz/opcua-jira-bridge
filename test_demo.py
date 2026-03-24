"""
Test-Demo: Prüft die Jira-Verbindung und erstellt/löscht ein Test-Ticket.

Usage: python test_demo.py
"""

import asyncio
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

JIRA_URL = os.getenv("JIRA_URL")
JIRA_USER = os.getenv("JIRA_USER")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "RKS")


async def test_jira_connection():
    """Test 1: Verbindung zu Jira prüfen."""
    print("=" * 60)
    print("OPC UA → Jira Bridge — Verbindungstest")
    print("=" * 60)

    async with httpx.AsyncClient() as client:
        # Test 1: Server erreichbar?
        print("\n[1/4] Teste Jira-Verbindung...")
        try:
            resp = await client.get(
                f"{JIRA_URL}/rest/api/2/myself",
                auth=(JIRA_USER, JIRA_API_TOKEN),
                timeout=15,
            )
            resp.raise_for_status()
            user = resp.json()
            print(f"  ✅ Verbunden als: {user.get('displayName', 'N/A')} ({user.get('emailAddress', 'N/A')})")
        except Exception as e:
            print(f"  ❌ Verbindung fehlgeschlagen: {e}")
            sys.exit(1)

        # Test 2: Projekt existiert?
        print(f"\n[2/4] Prüfe Projekt {JIRA_PROJECT_KEY}...")
        try:
            resp = await client.get(
                f"{JIRA_URL}/rest/api/2/project/{JIRA_PROJECT_KEY}",
                auth=(JIRA_USER, JIRA_API_TOKEN),
                timeout=15,
            )
            resp.raise_for_status()
            project = resp.json()
            print(f"  ✅ Projekt gefunden: {project.get('name', 'N/A')} ({JIRA_PROJECT_KEY})")
        except Exception as e:
            print(f"  ❌ Projekt nicht gefunden: {e}")
            sys.exit(1)

        # Test 3: Ticket erstellen
        print("\n[3/4] Erstelle Test-Ticket...")
        try:
            payload = {
                "fields": {
                    "project": {"key": JIRA_PROJECT_KEY},
                    "summary": "[OPC UA Test] Bridge Verbindungstest — bitte ignorieren",
                    "description": (
                        "Automatischer Test der OPC UA → Jira Bridge.\n"
                        "Dieses Ticket wird sofort wieder gelöscht."
                    ),
                    "issuetype": {"name": "Incident"},
                    "priority": {"name": "Low"},
                }
            }
            resp = await client.post(
                f"{JIRA_URL}/rest/api/2/issue",
                json=payload,
                auth=(JIRA_USER, JIRA_API_TOKEN),
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            resp.raise_for_status()
            ticket = resp.json()
            ticket_key = ticket["key"]
            ticket_id = ticket["id"]
            print(f"  ✅ Test-Ticket erstellt: {ticket_key} ({JIRA_URL}/browse/{ticket_key})")
        except httpx.HTTPStatusError as e:
            print(f"  ❌ Ticket-Erstellung fehlgeschlagen: {e.response.status_code}")
            print(f"     Response: {e.response.text}")
            sys.exit(1)
        except Exception as e:
            print(f"  ❌ Fehler: {e}")
            sys.exit(1)

        # Test 4: Ticket löschen
        print(f"\n[4/4] Lösche Test-Ticket {ticket_key}...")
        try:
            resp = await client.delete(
                f"{JIRA_URL}/rest/api/2/issue/{ticket_key}",
                auth=(JIRA_USER, JIRA_API_TOKEN),
                timeout=15,
            )
            resp.raise_for_status()
            print(f"  ✅ Test-Ticket {ticket_key} gelöscht")
        except Exception as e:
            print(f"  ⚠️  Ticket löschen fehlgeschlagen (ggf. manuell löschen): {e}")

    print("\n" + "=" * 60)
    print("✅ Alle Tests bestanden — Jira-Integration funktioniert!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(test_jira_connection())
