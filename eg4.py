"""EG4 6500EX-48 (PI30 protocol) serial access.

Frame format, both directions:  <payload><crc16-xmodem, 2 bytes><CR>
Responses start with "(". PI30 adjusts any CRC byte equal to 0x28 "(",
0x0D or 0x0A by adding one, so that a CRC byte can never be mistaken for a
delimiter; both sender and receiver must apply the same rule.

Parsing is split from I/O so the parsers can be unit-tested with recorded
frames and the connection can be held open for the collector's lifetime.
"""

import struct
import time

import serial

import settings

MODES = {
    "P": "power_on",
    "S": "standby",
    "L": "line",
    "B": "battery",
    "F": "fault",
    "H": "power_saving",
    "D": "shutdown",
    "C": "charge",
    "Y": "bypass",
    "E": "eco",
}

RESERVED_CRC_BYTES = (0x28, 0x0D, 0x0A)


class FrameError(ValueError):
    """Malformed or corrupt frame from the inverter."""


# --------------------------------------------------------------------- framing


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


def crc_bytes(data: bytes) -> bytes:
    """CRC as the two bytes PI30 puts on the wire, escape rule applied."""
    hi, lo = struct.pack(">H", crc16_xmodem(data))
    if hi in RESERVED_CRC_BYTES:
        hi += 1
    if lo in RESERVED_CRC_BYTES:
        lo += 1
    return bytes((hi, lo))


def build_command(command: str) -> bytes:
    payload = command.encode("ascii")
    return payload + crc_bytes(payload) + b"\r"


def decode_frame(raw: bytes, command: str = "?", strict_crc: bool = True) -> str:
    """Strip CRC and CR, verify the CRC, return the payload text."""
    if not raw:
        raise TimeoutError(f"No response to {command}")
    if not raw.endswith(b"\r") or len(raw) < 4:
        raise FrameError(f"Malformed response to {command}: {raw!r}")

    payload, received = raw[:-3], raw[-3:-1]
    expected = crc_bytes(payload)

    if received != expected:
        message = (
            f"CRC mismatch on {command}: got {received.hex()} "
            f"expected {expected.hex()} payload={payload!r}"
        )
        if strict_crc:
            raise FrameError(message)
        print(message, flush=True)

    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise FrameError(f"Non-ASCII payload for {command}: {payload!r}") from exc

    if not text.startswith("("):
        raise FrameError(f"Unexpected {command} response: {text}")
    if text.startswith("(NAK"):
        raise FrameError(f"Inverter rejected {command}")
    return text


# --------------------------------------------------------------------- parsers


def _fields(response: str, command: str, minimum: int):
    fields = response[1:].split()
    if len(fields) < minimum:
        raise FrameError(f"Unexpected {command} field count: {len(fields)}: {fields}")
    return fields


def parse_qmod(response: str) -> dict:
    code = response[1:].strip()
    return {"code": code, "name": MODES.get(code, "unknown")}


def parse_qpigs(response: str) -> dict:
    f = _fields(response, "QPIGS", 21)
    return {
        "grid_voltage": float(f[0]),
        "grid_frequency": float(f[1]),
        "output_voltage": float(f[2]),
        "output_frequency": float(f[3]),
        "output_va": int(f[4]),
        "output_watts": int(f[5]),
        "load_percent": int(f[6]),
        "bus_voltage": int(f[7]),
        "battery_voltage": float(f[8]),
        "battery_charge_current": int(f[9]),
        "battery_soc": int(f[10]),
        "temperature_c": int(f[11]),
        "pv_current": float(f[12]),
        "pv_voltage": float(f[13]),
        "battery_voltage_scc": float(f[14]),
        "battery_discharge_current": int(f[15]),
        "device_status": f[16],
        "battery_voltage_offset": f[17],
        "eeprom_version": f[18],
        "pv_charging_power": int(f[19]),
        "device_status_2": f[20],
    }


def parse_qpigs2(response: str) -> dict:
    f = _fields(response, "QPIGS2", 3)
    return {
        "pv2_current": float(f[0]),
        "pv2_voltage": float(f[1]),
        "pv2_watts": int(f[2]),
    }


def parse_qpgs(response: str, unit: int = 0) -> dict:
    f = _fields(response, f"QPGS{unit}", 29)
    return {
        "parallel_id": int(f[0]),
        "serial_number": f[1],
        "mode": f[2],
        "fault_code": f[3],
        "grid_voltage": float(f[4]),
        "grid_frequency": float(f[5]),
        "output_voltage": float(f[6]),
        "output_frequency": float(f[7]),
        "output_va": int(f[8]),
        "output_watts": int(f[9]),
        "load_percent": int(f[10]),
        "battery_voltage": float(f[11]),
        "battery_charge_current": int(f[12]),
        "battery_soc": int(f[13]),
        "pv1_voltage": float(f[14]),
        "total_charging_current": int(f[15]),
        "total_output_va": int(f[16]),
        "total_output_watts": int(f[17]),
        "total_load_percent": int(f[18]),
        "device_status": f[19],
        "output_mode": f[20],
        "charger_source_priority": f[21],
        "max_charger_current": int(f[22]),
        "max_charger_range": int(f[23]),
        "max_ac_charger_current": int(f[24]),
        "pv1_current": float(f[25]),
        "battery_discharge_current": int(f[26]),
        "pv2_voltage": float(f[27]),
        "pv2_current": float(f[28]),
    }


# ------------------------------------------------------------------ connection


class EG4Serial:
    """A serial link that stays open across queries and heals itself.

    Use as a context manager or call close() when done. Any I/O error closes
    the port so the next query reopens it.
    """

    def __init__(
        self,
        port=settings.SERIAL_PORT,
        baud=settings.SERIAL_BAUD,
        timeout=settings.SERIAL_TIMEOUT,
        command_gap=settings.SERIAL_COMMAND_GAP,
        strict_crc=settings.SERIAL_STRICT_CRC,
    ):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.command_gap = command_gap
        self.strict_crc = strict_crc
        self._ser = None
        self._last_command_at = 0.0

    # -- lifecycle
    def open(self):
        if self._ser is None or not self._ser.is_open:
            self._ser = serial.Serial(
                self.port,
                self.baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout,
                write_timeout=self.timeout,
            )
        return self

    def close(self):
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    # -- transport
    def query(self, command: str) -> str:
        self.open()
        wait = self.command_gap - (time.monotonic() - self._last_command_at)
        if wait > 0:
            time.sleep(wait)
        try:
            self._ser.reset_input_buffer()
            self._ser.write(build_command(command))
            self._ser.flush()
            raw = self._ser.read_until(b"\r")
        except (serial.SerialException, OSError):
            self.close()
            raise
        finally:
            self._last_command_at = time.monotonic()
        return decode_frame(raw, command, self.strict_crc)

    # -- typed queries
    def get_mode(self):
        return parse_qmod(self.query("QMOD"))

    def get_status(self):
        return parse_qpigs(self.query("QPIGS"))

    def get_pv2_status(self):
        return parse_qpigs2(self.query("QPIGS2"))

    def get_parallel_status(self, unit: int):
        if unit not in (0, 1):
            raise ValueError("unit must be 0 or 1")
        return parse_qpgs(self.query(f"QPGS{unit}"), unit)

    def get_all_parallel_status(self):
        return [self.get_parallel_status(0), self.get_parallel_status(1)]


if __name__ == "__main__":
    with EG4Serial() as link:
        print("Mode:", link.get_mode())
        print()
        print("Status:")
        for key, value in link.get_status().items():
            print(f"{key}: {value}")
