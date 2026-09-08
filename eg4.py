import serial
import struct

PORT = "/dev/ttyUSB0"
BAUD = 2400


def crc16_xmodem(data: bytes) -> int:
    crc = 0x0000

    for byte in data:
        crc ^= byte << 8

        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF

    return crc


def build_command(command: str) -> bytes:
    payload = command.encode("ascii")
    crc = crc16_xmodem(payload)
    return payload + struct.pack(">H", crc) + b"\r"


def query(command: str) -> str:
    packet = build_command(command)

    with serial.Serial(
        PORT,
        BAUD,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=3,
    ) as ser:
        ser.reset_input_buffer()
        ser.write(packet)
        ser.flush()
        response = ser.read_until(b"\r")

    if not response:
        raise TimeoutError(f"No response to {command}")

    if not response.endswith(b"\r") or len(response) < 4:
        raise ValueError(f"Malformed response to {command}: {response!r}")

    payload = response[:-3]
    return payload.decode("ascii")


def get_mode():
    response = query("QMOD")

    if not response.startswith("("):
        raise ValueError(f"Unexpected QMOD response: {response}")

    code = response[1:]

    modes = {
        "P": "power_on",
        "S": "standby",
        "L": "line",
        "B": "battery",
        "F": "fault",
        "H": "power_saving",
    }

    return {
        "code": code,
        "name": modes.get(code, "unknown"),
    }


def get_status():
    response = query("QPIGS")

    if not response.startswith("("):
        raise ValueError(f"Unexpected QPIGS response: {response}")

    fields = response[1:].split()

    if len(fields) < 21:
        raise ValueError(
            f"Unexpected QPIGS field count: {len(fields)}: {fields}"
        )

    return {
        "grid_voltage": float(fields[0]),
        "grid_frequency": float(fields[1]),
        "output_voltage": float(fields[2]),
        "output_frequency": float(fields[3]),
        "output_va": int(fields[4]),
        "output_watts": int(fields[5]),
        "load_percent": int(fields[6]),
        "bus_voltage": int(fields[7]),
        "battery_voltage": float(fields[8]),
        "battery_charge_current": int(fields[9]),
        "battery_soc": int(fields[10]),
        "temperature_c": int(fields[11]),
        "pv_current": float(fields[12]),
        "pv_voltage": float(fields[13]),
        "battery_voltage_scc": float(fields[14]),
        "battery_discharge_current": int(fields[15]),
        "device_status": fields[16],
        "battery_voltage_offset": fields[17],
        "eeprom_version": fields[18],
        "pv_charging_power": int(fields[19]),
        "device_status_2": fields[20],
    }


def get_parallel_status(unit: int):
    if unit not in (0, 1):
        raise ValueError("unit must be 0 or 1")

    response = query(f"QPGS{unit}")

    if not response.startswith("("):
        raise ValueError(f"Unexpected QPGS{unit} response: {response}")

    fields = response[1:].split()

    if len(fields) < 29:
        raise ValueError(
            f"Unexpected QPGS{unit} field count: {len(fields)}: {fields}"
        )

    return {
        "parallel_id": int(fields[0]),
        "serial_number": fields[1],
        "mode": fields[2],
        "fault_code": fields[3],

        "grid_voltage": float(fields[4]),
        "grid_frequency": float(fields[5]),

        "output_voltage": float(fields[6]),
        "output_frequency": float(fields[7]),
        "output_va": int(fields[8]),
        "output_watts": int(fields[9]),
        "load_percent": int(fields[10]),

        "battery_voltage": float(fields[11]),
        "battery_charge_current": int(fields[12]),
        "battery_soc": int(fields[13]),

        "pv1_voltage": float(fields[14]),
        "total_charging_current": int(fields[15]),

        "total_output_va": int(fields[16]),
        "total_output_watts": int(fields[17]),
        "total_load_percent": int(fields[18]),

        "device_status": fields[19],
        "output_mode": fields[20],
        "charger_source_priority": fields[21],

        "max_charger_current": int(fields[22]),
        "max_charger_range": int(fields[23]),
        "max_ac_charger_current": int(fields[24]),

        "pv1_current": float(fields[25]),
        "battery_discharge_current": int(fields[26]),
        "pv2_voltage": float(fields[27]),
        "pv2_current": float(fields[28]),
    }


def get_all_parallel_status():
    return [
        get_parallel_status(0),
        get_parallel_status(1),
    ]


if __name__ == "__main__":
    print("Mode:")
    print(get_mode())

    print()
    print("Status:")

    status = get_status()

    for key, value in status.items():
        print(f"{key}: {value}")
