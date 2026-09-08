import serial
import struct
import time


PORT = "/dev/ttyUSB0"
BAUD = 2400

COMMANDS = [
    "QPI",      # Protocol ID
    "QVFW",     # Main firmware version
    "QPIRI",    # Rated/configuration information
    "QMOD",     # Current operating mode
    "QPIGS",    # Current operating status
]


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


def query(ser, command: str):
    packet = build_command(command)

    print()
    print("=" * 60)
    print("Command:", command)
    print("TX:", packet.hex(" "))

    ser.reset_input_buffer()
    ser.write(packet)
    ser.flush()

    response = ser.read_until(b"\r")

    print("Response length:", len(response))
    print("Raw response:", repr(response))

    if not response:
        print("NO RESPONSE")
        return

    print("RX:", response.hex(" "))

    # Strip:
    #   final CR byte
    #   two CRC bytes immediately before it
    if response.endswith(b"\r") and len(response) >= 3:
        payload = response[:-3]
        received_crc = response[-3:-1]
    else:
        payload = response
        received_crc = b""

    print("Payload:", payload.decode("ascii", errors="replace"))

    if received_crc:
        print("Response CRC:", received_crc.hex(" "))


print("Opening:", PORT)
print("Baud:", BAUD)

with serial.Serial(
    PORT,
    BAUD,
    bytesize=serial.EIGHTBITS,
    parity=serial.PARITY_NONE,
    stopbits=serial.STOPBITS_ONE,
    timeout=3,
) as ser:

    for command in COMMANDS:
        query(ser, command)
        time.sleep(0.5)
