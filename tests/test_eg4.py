import unittest

import eg4
from tests.helpers import ROOT  # noqa: F401  (sets sys.path)

# Representative frames. Field counts match the firmware we talk to.
QPIGS = "(000.0 00.0 120.1 59.98 0421 0380 006 390 53.20 010 078 0035 0001.2 0334.5 00.00 00000 00010000 00 00 00412 010"
QPIGS2 = "(01.20 0320.5 00385"
QPGS0 = (
    "(1 92932004102443 B 00 000.0 00.00 120.0 59.99 0410 0370 006 53.10 010 078 "
    "0334.5 020 0820 0740 012 00000010 1 2 060 120 10 01.20 000 0320.5 01.20"
)


def frame(payload: str) -> bytes:
    p = payload.encode()
    return p + eg4.crc_bytes(p) + b"\r"


class CrcTests(unittest.TestCase):
    def test_known_vector(self):
        # CRC-16/XMODEM of "123456789" is 0x31C3
        self.assertEqual(eg4.crc16_xmodem(b"123456789"), 0x31C3)

    def test_escape_rule_bumps_reserved_bytes(self):
        # Find an input whose raw CRC contains a reserved byte and confirm it is bumped.
        for n in range(100000):
            data = f"X{n}".encode()
            hi, lo = divmod(eg4.crc16_xmodem(data), 256)
            if hi in eg4.RESERVED_CRC_BYTES or lo in eg4.RESERVED_CRC_BYTES:
                out = eg4.crc_bytes(data)
                self.assertNotIn(out[0], eg4.RESERVED_CRC_BYTES)
                self.assertNotIn(out[1], eg4.RESERVED_CRC_BYTES)
                return
        self.fail("no test vector found")

    def test_build_command_round_trips(self):
        pkt = eg4.build_command("QPIGS")
        self.assertTrue(pkt.endswith(b"\r"))
        self.assertEqual(pkt[:-3], b"QPIGS")
        self.assertEqual(pkt[-3:-1], eg4.crc_bytes(b"QPIGS"))


class DecodeFrameTests(unittest.TestCase):
    def test_valid_frame(self):
        self.assertEqual(eg4.decode_frame(frame("(ACK"), "T"), "(ACK")

    def test_empty_is_timeout(self):
        with self.assertRaises(TimeoutError):
            eg4.decode_frame(b"", "T")

    def test_bad_crc_rejected_when_strict(self):
        raw = bytearray(frame(QPIGS2))
        raw[3] ^= 0x01  # flip a payload bit
        with self.assertRaises(eg4.FrameError):
            eg4.decode_frame(bytes(raw), "QPIGS2", strict_crc=True)

    def test_bad_crc_tolerated_when_not_strict(self):
        raw = bytearray(frame(QPIGS2))
        raw[3] ^= 0x01
        out = eg4.decode_frame(bytes(raw), "QPIGS2", strict_crc=False)
        self.assertTrue(out.startswith("("))

    def test_nak(self):
        with self.assertRaises(eg4.FrameError):
            eg4.decode_frame(frame("(NAK"), "QPGS1")

    def test_missing_cr(self):
        with self.assertRaises(eg4.FrameError):
            eg4.decode_frame(b"(ACK\x12\x34", "T")


class ParserTests(unittest.TestCase):
    def test_qpigs(self):
        s = eg4.parse_qpigs(QPIGS)
        self.assertEqual(s["output_watts"], 380)
        self.assertEqual(s["battery_soc"], 78)
        self.assertEqual(s["pv_charging_power"], 412)
        self.assertAlmostEqual(s["pv_voltage"], 334.5)

    def test_qpigs2(self):
        s = eg4.parse_qpigs2(QPIGS2)
        self.assertEqual(s["pv2_watts"], 385)

    def test_qpgs(self):
        s = eg4.parse_qpgs(QPGS0, 0)
        self.assertEqual(s["mode"], "B")
        self.assertEqual(s["fault_code"], "00")
        self.assertEqual(s["total_output_watts"], 740)
        self.assertAlmostEqual(s["pv2_current"], 1.2)

    def test_short_frame(self):
        with self.assertRaises(eg4.FrameError):
            eg4.parse_qpgs("(1 2 3", 0)

    def test_qmod(self):
        self.assertEqual(eg4.parse_qmod("(B"), {"code": "B", "name": "battery"})


if __name__ == "__main__":
    unittest.main()
