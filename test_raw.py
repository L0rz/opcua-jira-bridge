"""
Raw Socket Test - schaut was der Server auf Port 48010 wirklich spricht
"""
import socket
import sys

host = "localhost"
port = 48010

if len(sys.argv) > 1:
    parts = sys.argv[1].replace("opc.tcp://", "").split(":")
    host = parts[0]
    port = int(parts[1]) if len(parts) > 1 else port

print(f"Raw Test → {host}:{port}\n{'='*60}")

# OPC UA Hello Message (HEL)
# Magic: "HELF" + chunk type + message size + protocol version + buffer sizes + endpoint
HEL = (
    b"HELF"          # MessageType = HEL, ChunkType = F (final)
    + b"\x00" * 4   # MessageSize placeholder (wird unten gefüllt)
    + b"\x00\x00\x00\x00"   # ProtocolVersion = 0
    + b"\x00\x00\x04\x00"   # ReceiveBufferSize = 262144 (little endian: 0x00040000)
    + b"\x00\x00\x04\x00"   # SendBufferSize    = 262144
    + b"\x00\x00\x00\x00"   # MaxMessageSize    = 0 (unlimited)
    + b"\x00\x00\x00\x00"   # MaxChunkCount     = 0 (unlimited)
    + b"\x1a\x00"           # EndpointUrl length = 26
    + b"opc.tcp://localhost:48010"  # EndpointUrl
)
# Größe einsetzen (little endian uint32)
import struct
size = len(HEL)
HEL = HEL[:4] + struct.pack("<I", size) + HEL[8:]

print(f"Sende OPC UA HEL ({len(HEL)} bytes)...")

s = socket.socket()
s.settimeout(5)
try:
    s.connect((host, port))
    print("✅ TCP verbunden")
    s.send(HEL)
    response = s.recv(4096)
    print(f"✅ Antwort ({len(response)} bytes): {response[:4]}")
    if response[:3] == b"ACK":
        print("→ Server spricht OPC UA Binary! ACK erhalten.")
        print("→ Problem liegt in asyncua Library")
    elif response[:3] == b"ERR":
        err_code = struct.unpack("<I", response[8:12])[0] if len(response) >= 12 else 0
        print(f"→ Server antwortete mit ERROR: Code 0x{err_code:08X}")
    else:
        print(f"→ Unbekannte Antwort: {response[:20]}")
except socket.timeout:
    print("❌ Timeout — Server antwortet nicht auf OPC UA HEL")
    print("→ Port 48010 ist offen aber spricht kein OPC UA Binary!")
    print("→ Evtl. falscher Port oder Server braucht anderen Transport")
except Exception as e:
    print(f"❌ Fehler: {e}")
finally:
    s.close()

# Auch schauen was asyncua Version ist
print("\nasyncua Version:")
try:
    import asyncua
    print(f"  {asyncua.__version__}")
except:
    print("  unbekannt")
