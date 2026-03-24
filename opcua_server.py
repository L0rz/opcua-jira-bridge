"""
OPC UA Test-Server — Simuliert eine Industrieanlage mit Störungen/Alarmen.

Nodes:
  - Temperature (float)
  - Pressure (float)
  - ErrorCode (int)     0=OK, 1=Warning, 2=Critical, 3=Emergency
  - AlarmActive (bool)
  - AlarmMessage (str)

Alle ~10s wird zufällig ein Alarm ausgelöst.
"""

import asyncio
import random
import logging
from datetime import datetime

from asyncua import Server, ua

logging.basicConfig(level=logging.INFO, format="%(asctime)s [SERVER] %(message)s")
log = logging.getLogger("opcua_server")

ALARM_SCENARIOS = [
    {"error_code": 1, "message": "Temperatur über Grenzwert (>85°C)", "temp_range": (86, 95)},
    {"error_code": 2, "message": "Druckverlust kritisch (<2 bar)", "temp_range": (60, 70), "pressure_range": (0.5, 1.8)},
    {"error_code": 3, "message": "Notabschaltung — Sensor ausgefallen", "temp_range": (0, 0)},
    {"error_code": 1, "message": "Kompressor Vibration erhöht", "temp_range": (70, 80)},
    {"error_code": 2, "message": "Kühlmittelstand niedrig", "temp_range": (75, 90)},
    {"error_code": 3, "message": "Stromversorgung instabil — USV aktiv", "temp_range": (50, 60)},
    {"error_code": 1, "message": "Filter verschmutzt — Wartung nötig", "temp_range": (65, 75)},
    {"error_code": 2, "message": "Leckage erkannt in Leitung 3", "temp_range": (55, 65), "pressure_range": (1.0, 2.5)},
]


async def main():
    server = Server()
    await server.init()
    server.set_endpoint("opc.tcp://0.0.0.0:4840/freeopcua/server/")
    server.set_server_name("OPC UA Alarm Test Server")

    uri = "http://opcua-jira-bridge.test"
    idx = await server.register_namespace(uri)

    objects = server.nodes.objects
    plant = await objects.add_object(idx, "IndustrialPlant")

    temp_node = await plant.add_variable(idx, "Temperature", 22.0)
    pressure_node = await plant.add_variable(idx, "Pressure", 5.0)
    error_node = await plant.add_variable(idx, "ErrorCode", 0)
    alarm_active_node = await plant.add_variable(idx, "AlarmActive", False)
    alarm_msg_node = await plant.add_variable(idx, "AlarmMessage", "")

    for node in [temp_node, pressure_node, error_node, alarm_active_node, alarm_msg_node]:
        await node.set_writable()

    log.info("Server gestartet auf opc.tcp://0.0.0.0:4840/freeopcua/server/")
    log.info("Namespace Index: %d", idx)

    async with server:
        while True:
            # Normal values
            normal_temp = round(random.uniform(18.0, 30.0), 1)
            normal_pressure = round(random.uniform(4.0, 6.0), 2)

            await temp_node.write_value(normal_temp)
            await pressure_node.write_value(normal_pressure)
            await error_node.write_value(0)
            await alarm_active_node.write_value(False)
            await alarm_msg_node.write_value("")

            log.info("Normal: Temp=%.1f°C, Pressure=%.2f bar", normal_temp, normal_pressure)

            await asyncio.sleep(random.uniform(8, 12))

            # Alarm auslösen
            scenario = random.choice(ALARM_SCENARIOS)
            alarm_temp = round(random.uniform(*scenario["temp_range"]), 1)
            alarm_pressure = round(
                random.uniform(*scenario.get("pressure_range", (3.0, 5.0))), 2
            )
            ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            msg = f"[{ts}] {scenario['message']}"

            await temp_node.write_value(alarm_temp)
            await pressure_node.write_value(alarm_pressure)
            await error_node.write_value(scenario["error_code"])
            await alarm_active_node.write_value(True)
            await alarm_msg_node.write_value(msg)

            log.info(
                "🚨 ALARM: ErrorCode=%d | %s | Temp=%.1f°C, Pressure=%.2f bar",
                scenario["error_code"], scenario["message"], alarm_temp, alarm_pressure,
            )

            await asyncio.sleep(random.uniform(8, 12))


if __name__ == "__main__":
    asyncio.run(main())
