"""Dump raw frames for a set of PI30 commands. Diagnostic only.

Usage: python probe_inverter.py [COMMAND ...]
"""

import sys

from eg4 import EG4Serial, FrameError, build_command, decode_frame

DEFAULT_COMMANDS = [
    "QPI",  # Protocol ID
    "QVFW",  # Main firmware version
    "QPIRI",  # Rated/configuration information
    "QMOD",  # Current operating mode
    "QPIGS",  # Current operating status
    "QPIGS2",  # Second PV input
    "QPGS0",  # Parallel unit 0
    "QPGS1",  # Parallel unit 1
]


def main(commands):
    link = EG4Serial()
    print("Opening:", link.port, "baud:", link.baud)
    with link:
        for command in commands:
            packet = build_command(command)
            print()
            print("=" * 60)
            print("Command:", command)
            print("TX:", packet.hex(" "))
            # Use the raw transport so we can show the bytes even on failure.
            link.open()
            link._ser.reset_input_buffer()
            link._ser.write(packet)
            link._ser.flush()
            raw = link._ser.read_until(b"\r")
            print("Response length:", len(raw))
            print("RX:", raw.hex(" "))
            try:
                print("Payload:", decode_frame(raw, command, strict_crc=True))
                print("CRC: ok")
            except (FrameError, TimeoutError) as exc:
                print("ERROR:", exc)


if __name__ == "__main__":
    main(sys.argv[1:] or DEFAULT_COMMANDS)
