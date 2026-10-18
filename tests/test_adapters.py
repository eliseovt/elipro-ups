import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from ups_monitor.adapters import ForzaBridge, NoRedirect, TelemetryError, normalize, number


def payload(**overrides):
    info = {
        "currentTime": {"time": int(datetime.now(timezone.utc).timestamp() * 1000)},
        "workMode": "Line mode", "inputVoltage": "220.0", "outputVoltage": 220,
        "outputFrequency": 60, "outputLoadPercent": 30, "batteryCapacity": 95,
        "batteryVoltage": 27, "batteryRemainTime": 0, "temperature": 0,
    }
    info.update(overrides)
    return {"protocolType": "PMV", "workInfo": info}


class AdapterTests(unittest.TestCase):
    def test_unsupported_measurements_remain_unknown_instead_of_zero(self):
        sample = normalize(payload(batteryRemainTime=100, temperature=25))
        self.assertIsNone(sample["runtime_seconds"])
        self.assertIsNone(sample["temperature_c"])
        self.assertIsNone(sample["estimated_watts"])
        self.assertFalse(sample["watts_is_estimate"])
        self.assertFalse(sample["on_battery"])

    def test_only_recognized_battery_mode_becomes_true(self):
        self.assertIs(normalize(payload(workMode="Battery mode"))["on_battery"], True)
        for mode in [None, "", "Fault mode", "true", True, 1]:
            self.assertIsNone(normalize(payload(workMode=mode))["on_battery"])
        self.assertIs(normalize(payload(workMode="Fault mode"))["fault"], True)

    def test_protocol_must_match_and_warning_must_be_exact(self):
        changed = payload()
        changed["protocolType"] = "UNKNOWN"
        with self.assertRaises(TelemetryError):
            normalize(changed)
        for warnings in [["a7"], ["a7:[warning.a7]"]]:
            self.assertIs(normalize(payload(warnings=warnings))["low_battery"], True)
        for warnings in [["a6"], ["a70"], ["not-a7"], ["a7:[warning.a6]"]]:
            self.assertIs(normalize(payload(warnings=warnings))["low_battery"], False)
        for warnings in [None, "a7", [True]]:
            self.assertIsNone(normalize(payload(warnings=warnings))["low_battery"])

    def test_stale_or_unverifiable_telemetry_is_rejected(self):
        for age in [30, -60]:
            stamp = int((datetime.now(timezone.utc) - timedelta(seconds=age)).timestamp() * 1000)
            with self.assertRaises(TelemetryError):
                normalize(payload(currentTime={"time": stamp}))
        for stamp in [None, "", False, "NaN", 0]:
            with self.assertRaises(TelemetryError):
                normalize(payload(currentTime=stamp))

    def test_invalid_measurements_never_turn_into_low_values(self):
        sample = normalize(payload(batteryCapacity="NaN", batteryVoltage=True,
                                   outputFrequency=0, outputLoadPercent=-1))
        for key in ["battery_percent", "battery_voltage", "frequency_hz", "load_percent"]:
            self.assertIsNone(sample[key])
        for value in [True, False, "nan", "inf", float("inf"), None]:
            self.assertIsNone(number(value))

    def test_remote_targets_credentials_and_redirects_are_rejected(self):
        for url in ["https://127.0.0.1/Forza", "http://example.com/Forza",
                    "http://127.0.0.1.evil/Forza", "http://user:pass@localhost/Forza",
                    "http://127.0.0.1/Forza?token=x", "http://127.0.0.1/Forza#fragment"]:
            with self.assertRaises(ValueError):
                ForzaBridge(url)
        with self.assertRaises(TelemetryError):
            NoRedirect().redirect_request(None, None, 302, "found", {}, "http://example.com")

    def test_reader_has_a_path_allowlist_and_bounded_payload(self):
        bridge = ForzaBridge()
        bridge.opener = Mock()
        with self.assertRaises(ValueError):
            bridge._read("control/shutdown")
        bridge.opener.open.assert_not_called()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"x" * 2_000_001
        bridge.opener.open.return_value = response
        with self.assertRaises(TelemetryError):
            bridge._read("monitoringWorkStatus")
        response.read.assert_called_once_with(2_000_001)

    def test_discovery_requires_local_usb_and_failure_rediscovers(self):
        bridge = ForzaBridge()
        bridge._read = Mock(return_value={"portName": "REMOTE1"})
        with self.assertRaises(TelemetryError):
            bridge.read()
        self.assertIsNone(bridge.port_name)
        bridge.port_name = "USB1"
        bridge._read = Mock(side_effect=TelemetryError("disconnected"))
        with self.assertRaises(TelemetryError):
            bridge.read()
        self.assertIsNone(bridge.port_name)


if __name__ == "__main__":
    unittest.main()
