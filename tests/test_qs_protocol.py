import unittest
from datetime import datetime, timezone

from ups_monitor.qs_protocol import MAX_FRAME_BYTES, QsProtocolError, parse_qs


# Anonymized measurement bytes recovered from an existing vendor log, not USB.
LOG_SAMPLE = bytes.fromhex(
    "23 62 00 20 6C 20 62 01 20 6C 20 07 20 4F FA 20 "
    "12 C0 00 20 E9 20 3C 20 09 20 A2 0D")
LIVE_SAMPLE = bytes.fromhex(
    "23 67 01 20 6C 20 67 01 20 6C 20 08 20 50 0F 20 "
    "12 C0 00 20 E9 20 3C 20 09 20 A2 0D")


def field_frame(index, replacement):
    fields = LOG_SAMPLE[1:-1].split(b" ")
    fields[index] = replacement
    return b"#" + b" ".join(fields) + b"\r"


def status_frame(value):
    escapes = {13: b"\x28\x00", 17: b"\x28\x01", 19: b"\x28\x02",
               10: b"\x28\x03", 32: b"\x28\x04"}
    return field_frame(9, escapes.get(value, bytes([value])))


class QsProtocolTests(unittest.TestCase):
    def test_zero_frequency_is_only_valid_for_output_off_shutdown(self):
        fields = LOG_SAMPLE[1:-1].split(b" ")
        fields[2], fields[5], fields[6], fields[9] = b"\x00\x00", b"\x00\x00", b"\x00\x00\x00", b"\x0b"
        off = b"#" + b" ".join(fields) + b"\r"
        parsed = parse_qs(off)
        self.assertEqual(parsed["frequency_hz"], 0)
        self.assertEqual(parsed["output_voltage"], 0)
        self.assertEqual(parsed["raw_status"], "Shutdown mode")
        self.assertIsNone(parsed["on_battery"])
        fields[9] = b"\x09"
        with self.assertRaises(QsProtocolError):
            parse_qs(b"#" + b" ".join(fields) + b"\r")

    def test_recorded_measurements_and_unknown_capabilities(self):
        before = datetime.now(timezone.utc)
        sample = parse_qs(LOG_SAMPLE)
        after = datetime.now(timezone.utc)
        self.assertLessEqual(before, datetime.fromisoformat(sample["timestamp"]))
        self.assertLessEqual(datetime.fromisoformat(sample["timestamp"]), after)
        self.assertEqual(sample["input_voltage"], 207.5)
        self.assertEqual(sample["output_voltage"], 207.5)
        self.assertEqual(sample["frequency_hz"], 60.0)
        self.assertEqual(sample["battery_voltage"], 27.41)
        self.assertEqual(sample["load_percent"], 7)
        self.assertIs(sample["on_battery"], False)
        self.assertIs(sample["low_battery"], False)
        self.assertIs(sample["fault"], False)
        self.assertEqual(sample["raw_status"], "Line mode")
        self.assertEqual(sample["raw_flags"], {"status_hex": "09", "ratings_hex": "A2"})
        for field in ("battery_percent", "runtime_seconds", "temperature_c", "estimated_watts"):
            self.assertIsNone(sample[field])
        self.assertIs(sample["watts_is_estimate"], False)

    def test_each_status_flag_and_precautionary_inhibition(self):
        cases = [
            (0x89, "Battery mode", True, False, False),
            (0xC9, "Battery mode", True, True, False),
            (0x29, "AVR mode", False, False, False),
            (0x99, "Fault mode", None, False, True),
            (0x8D, "Battery test mode", None, False, False),
            (0x8B, "Shutdown mode", None, False, False),
        ]
        for flags, mode, battery, low, fault in cases:
            with self.subTest(flags=flags):
                sample = parse_qs(status_frame(flags))
                self.assertEqual(sample["raw_status"], mode)
                self.assertIs(sample["on_battery"], battery)
                self.assertIs(sample["low_battery"], low)
                self.assertIs(sample["fault"], fault)

    def test_known_escapes_including_escaped_space_and_cr(self):
        for raw in (0x0D, 0x11, 0x13, 0x0A, 0x20):
            with self.subTest(raw=raw):
                self.assertEqual(parse_qs(status_frame(raw))["raw_flags"]["status_hex"], f"{raw:02X}")

    def test_literal_escape_prefix_is_valid_data(self):
        sample = parse_qs(status_frame(0x28))
        self.assertEqual(sample["raw_status"], "AVR mode")
        self.assertEqual(sample["raw_flags"]["status_hex"], "28")
        self.assertIs(sample["on_battery"], False)
        self.assertIs(sample["low_battery"], False)
        self.assertEqual(parse_qs(field_frame(5, b"\x50\x28"))["frequency_hz"], 59.9)
        self.assertEqual(parse_qs(field_frame(0, b"\x28\x06"))["input_voltage"], 84.8)

    def test_live_capture_matches_independently_reported_values(self):
        sample = parse_qs(LIVE_SAMPLE)
        self.assertEqual(sample["input_voltage"], 218.1)
        self.assertEqual(sample["output_voltage"], 218.1)
        self.assertEqual(sample["load_percent"], 8)
        self.assertEqual(sample["battery_voltage"], 27.41)
        self.assertEqual(sample["frequency_hz"], 60.0)

    def test_optional_battery_precision_preserves_curve_input_without_changing_default_measurement(self):
        fields = LIVE_SAMPLE[1:-1].split(b" ")
        fields[7], fields[8], fields[9] = b"\xDA", b"\x3C", b"\x89"
        frame = b"#" + b" ".join(fields) + b"\r"
        public = parse_qs(frame)
        precise = parse_qs(frame, preserve_battery_precision=True)
        self.assertEqual(public["battery_voltage"], 25.65)
        self.assertEqual(precise["battery_voltage"], 218 * 60 / 510)
        self.assertIs(precise["on_battery"], True)
        self.assertEqual(set(public), set(precise))
        self.assertEqual({key: value for key, value in public.items() if key not in ("timestamp", "battery_voltage")},
                         {key: value for key, value in precise.items() if key not in ("timestamp", "battery_voltage")})

    def test_bad_or_unescaped_control_bytes_are_rejected(self):
        for encoded in (b"\x28\x05", b"\x28\x28", b"\x0d", b"\x0a", b"\x11", b"\x13"):
            with self.subTest(encoded=encoded), self.assertRaises(QsProtocolError):
                parse_qs(field_frame(9, encoded))

    def test_denominator_and_implausible_measurements_are_rejected(self):
        for index, value in ((5, b"\x00\x00"), (6, b"\x00\x00\x00"),
                             (0, b"\xff\xff"), (4, b"\xff"),
                             (7, b"\x00"), (8, b"\xff")):
            with self.subTest(index=index), self.assertRaises(QsProtocolError):
                parse_qs(field_frame(index, value))

    def test_zero_input_is_valid_only_for_confirmed_battery_or_inhibited_modes(self):
        zero_input = field_frame(0, b"\x00\x00")
        with self.assertRaises(QsProtocolError):
            parse_qs(zero_input)
        self.assertEqual(parse_qs(zero_input.replace(b" \x09 ", b" \x89 "))["input_voltage"], 0)
        with self.assertRaises(QsProtocolError):
            parse_qs(field_frame(2, b"\x00\x00"))

    def test_complete_framing_and_exact_field_widths_are_required(self):
        invalid = [b"", LOG_SAMPLE[:-1], LOG_SAMPLE[1:], b"(" + LOG_SAMPLE[1:],
                   LOG_SAMPLE + b"\x00", LOG_SAMPLE + b"\r", LOG_SAMPLE[:-2] + b"\r",
                   LOG_SAMPLE.replace(b" \x07 ", b"  \x07 "),
                   b"#" + b"x" * MAX_FRAME_BYTES + b"\r",
                   field_frame(0, b"\x62"), field_frame(4, b"\x00\x07"),
                   field_frame(6, b"\x12\xc0"), field_frame(10, b"")]
        for frame in invalid:
            with self.subTest(frame=frame), self.assertRaises(QsProtocolError):
                parse_qs(frame)
        for value in (None, LOG_SAMPLE.decode("latin1"), bytearray(LOG_SAMPLE)):
            with self.subTest(value=type(value).__name__), self.assertRaises(QsProtocolError):
                parse_qs(value)

    def test_every_partial_record_is_rejected(self):
        for length in range(len(LOG_SAMPLE)):
            with self.subTest(length=length), self.assertRaises(QsProtocolError):
                parse_qs(LOG_SAMPLE[:length])


if __name__ == "__main__":
    unittest.main()
