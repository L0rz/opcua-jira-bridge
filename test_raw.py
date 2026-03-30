"""
Raw Socket Test - schaut was der Server auf Port 48010 wirklich spricht
Probiert verschiedene Endpoint-URL Varianten im HEL
"""
import socket
import struct
import sys

host = "localhost"
port = 48010

if len(sys.argv) > 1:
    parts = sys.argv[1].replace("opc.tcp://", "").split(":")
    host = parts[0]
    port = int(parts[1]) if len(parts) > 1 else port

print(f"Raw Test → {host}:{port}\n{'='*60}")


def make_hel(endpoint_url: str) -> bytes:
    url_bytes = endpoint_url.encode("utf-8")
    url_len = struct.pack("<I", len(url_bytes))
    body = (
        struct.pack("<I", 0)           # ProtocolVersion
        + struct.pack("<I", 65536)     # ReceiveBufferSize
        + struct.pack("<I", 65536)     # SendBufferSize
        + struct.pack("<I", 0)         # MaxMessageSize
        + struct.pack("<I", 0)         # MaxChunkCount
        + url_len
        + url_bytes
    )
    header = b"HELF" + struct.pack("<I", 8 + len(body))
    return header + body


def send_hel(endpoint_url: str) -> str:
    hel = make_hel(endpoint_url)
    s = socket.socket()
    s.settimeout(5)
    try:
        s.connect((host, port))
        s.send(hel)
        response = s.recv(4096)
        if response[:3] == b"ACK":
            return "✅ ACK — Server akzeptiert diese URL!"
        elif response[:3] == b"ERR":
            code = struct.unpack("<I", response[8:12])[0] if len(response) >= 12 else 0
            return f"❌ ERR 0x{code:08X}"
        else:
            return f"❓ Unbekannt: {response[:8]}"
    except socket.timeout:
        return "❌ Timeout"
    except Exception as e:
        return f"❌ {e}"
    finally:
        s.close()


# Verschiedene URL-Varianten testen
variants = [
    f"opc.tcp://{host}:{port}",
    f"opc.tcp://{host}:{port}/",
    f"opc.tcp://{host.upper()}:{port}",
    f"opc.tcp://{host.lower()}:{port}",
    f"opc.tcp://localhost:{port}",
    f"opc.tcp://127.0.0.1:{port}",
    f"opc.tcp://{host}:{port}/OPCUA/SimulationServer",
    f"opc.tcp://{host}:{port}/UA/Server",
    f"opc.tcp://{host}:{port}/opcua",
]

for url in variants:
    result = send_hel(url)
    print(f"  {result:45s} ← {url}")
    if "ACK" in result:
        print(f"\n✅ FUNKTIONIERENDE URL: {url}")
        print(f"→ Bitte .env und opcua_config_real_server.yaml auf diese URL setzen!")
        break

print("\nasyncua Version:")
try:
    import asyncua
    print(f"  {asyncua.__version__}")
except:
    print("  unbekannt")
