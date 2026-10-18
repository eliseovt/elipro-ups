"""Battery observations with mocked USB; never query real hardware or power APIs."""
import contextlib
import io
import json
import sys
import unittest
from unittest.mock import Mock, patch

from ups_monitor import battery_diagnostics as diagnostic, entrypoint, runtime
from ups_monitor.instance_guard import AlreadyRunningError, MonitorInstanceGuard


PATH = r"\\?\hid#vid_0665&pid_5161#unit#{4d1e55b2-f16f-11cf-88cb-001111000030}"
QS = bytes.fromhex("236701206c206701206c200820500f2012c00020e9203c200920a20d")


def metadata(**changes):
    return {"path": PATH, "metadata_open": True, "vendor_id": 0x0665, "product_id": 0x5161,
            "input_report_bytes": 9, "output_report_bytes": 9, "feature_report_bytes": 0, **changes}


class BatteryDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.guard = self.enterContext(patch.object(diagnostic, "MonitorInstanceGuard"))
        self.oem = self.enterContext(patch.object(diagnostic, "forza_monitor_running", return_value=False))
        self.enumerate = self.enterContext(patch.object(diagnostic, "enumerate_ups_bounded", return_value=[metadata()]))
        self.constructor = self.enterContext(patch.object(diagnostic, "StatusTransport"))
        self.transport = self.constructor.return_value
        self.transport.query_status.return_value = QS
        self.transport.query_battery_diagnostic.side_effect = [b"(123\r", b"95\r"]
        self.sleep = self.enterContext(patch.object(diagnostic.time, "sleep"))

    def test_capture_preserves_raw_qs_and_vendor_replies_without_inventing_capacity(self):
        report = diagnostic.capture_battery_samples(label="network")
        sample = report["samples"][0]
        self.assertEqual(report["physical_validation"], "pending")
        self.assertEqual(report["estimation"], "not_computed")
        self.assertEqual(sample["capture_label"], "network")
        self.assertEqual(sample["qs"]["raw_hex"], QS.hex(" "))
        self.assertEqual(sample["qs"]["decoded"]["battery_voltage"], 27.41)
        self.assertIsNone(sample["qs"]["decoded"]["battery_percent"])
        self.assertEqual(sample["bt"]["raw_text"], "(123\r")
        self.assertIsNone(sample["bt"]["decoded"])
        self.assertEqual(sample["bt"]["format_status"], "unverified_format")
        self.assertEqual(sample["bl"]["decoded"], {"value": 95, "unit": "percent", "validation": "format_only"})
        self.guard.return_value.__enter__.assert_called_once()
        self.guard.return_value.__exit__.assert_called_once()
        self.sleep.assert_not_called()

    def test_vendor_query_errors_never_discard_a_successful_qs(self):
        self.transport.query_battery_diagnostic.side_effect = [TimeoutError("BT deadline"), OSError("BL unavailable")]
        sample = diagnostic.capture_battery_samples()["samples"][0]
        self.assertEqual(sample["qs"]["decoded"]["battery_voltage"], 27.41)
        self.assertIsNone(sample["qs"]["error"])
        self.assertIn("BT deadline", sample["bt"]["error"])
        self.assertIn("BL unavailable", sample["bl"]["error"])
        self.assertIsNone(sample["bl"]["decoded"])

    def test_observed_negative_battery_replies_are_retained_as_unsupported(self):
        self.transport.query_battery_diagnostic.side_effect = [b"N\r", b"N\r"]
        sample = diagnostic.capture_battery_samples(label="battery")["samples"][0]
        self.assertEqual(sample["qs"]["decoded"]["battery_voltage"], 27.41)
        self.assertIsNone(sample["qs"]["decoded"]["battery_percent"])
        for name in ("bt", "bl"):
            self.assertEqual(sample[name]["raw_hex"], "4e 0d")
            self.assertEqual(sample[name]["raw_text"], "N\r")
            self.assertEqual(sample[name]["format_status"], "unsupported")
            self.assertIsNone(sample[name]["decoded"])
            self.assertIsNone(sample[name]["error"])

    def test_qs_failure_keeps_raw_bytes_and_skips_additional_queries(self):
        self.transport.query_status.return_value = b"#invalid\r"
        sample = diagnostic.capture_battery_samples()["samples"][0]
        self.assertEqual(sample["qs"]["raw_text"], "#invalid\r")
        self.assertIsNotNone(sample["qs"]["error"])
        self.assertEqual(sample["bt"]["format_status"], "skipped")
        self.transport.query_battery_diagnostic.assert_not_called()

    def test_bl_strict_format_and_bounds_preserve_invalid_reply_without_clamping(self):
        for raw in (b"0\r", b"100\r", b"---\r", b"101\r", b"-1\r", b" 95\r", b"95.0\r",
                    b"(95\r", b"95%\r", b"9\r9\r", b"9" * 257 + b"\r", b"\xff\r"):
            with self.subTest(raw=raw[:20]):
                self.transport.query_battery_diagnostic.side_effect = [b"unknown\r", raw]
                query = diagnostic.capture_battery_samples()["samples"][0]["bl"]
                self.assertEqual(query["raw_hex"], raw.hex(" "))
                if raw in (b"0\r", b"100\r"):
                    self.assertEqual(query["decoded"]["value"], int(raw[:-1]))
                    self.assertIsNone(query["error"])
                elif raw == b"---\r":
                    self.assertEqual(query["format_status"], "unavailable")
                    self.assertIsNone(query["decoded"])
                else:
                    self.assertIsNotNone(query["error"])
                    self.assertIsNone(query["decoded"])

    def test_samples_interval_and_labels_are_bounded_before_guard_or_usb(self):
        for arguments in ({"samples": 0}, {"samples": 121}, {"samples": True}, {"interval": 0.9},
                          {"interval": float("inf")}, {"interval": float("nan")}, {"label": "arbitrary"}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                diagnostic.capture_battery_samples(**arguments)
        self.guard.assert_not_called()
        self.enumerate.assert_not_called()
        self.constructor.assert_not_called()
        self.transport.query_battery_diagnostic.side_effect = [b"(1\r", b"1\r", b"(2\r", b"2\r"]
        report = diagnostic.capture_battery_samples(samples=2, interval=2, label="recovery")
        self.assertEqual(len(report["samples"]), 2)
        self.sleep.assert_called_once_with(2)
        self.assertTrue(all(sample["capture_label"] == "recovery" for sample in report["samples"]))

    def test_active_forza_and_unverifiable_metadata_abort_before_querying(self):
        self.oem.return_value = True
        with self.assertRaises(OSError):
            diagnostic.capture_battery_samples()
        self.enumerate.assert_not_called()
        self.oem.return_value = False
        for devices in ([], [metadata(), metadata()], [metadata(metadata_open=False)],
                        [metadata(vendor_id=True)], [metadata(product_id=1)],
                        [metadata(input_report_bytes=8)], [metadata(output_report_bytes=9.0)],
                        [metadata(feature_report_bytes=False)]):
            self.enumerate.return_value = devices
            with self.subTest(devices=devices), self.assertRaises(OSError):
                diagnostic.capture_battery_samples()
        self.constructor.assert_not_called()

    def test_oem_starting_after_qs_is_rechecked_and_preserves_qs(self):
        self.oem.side_effect = [False, False, True, True]
        sample = diagnostic.capture_battery_samples()["samples"][0]
        self.assertEqual(sample["qs"]["decoded"]["battery_voltage"], 27.41)
        self.assertIn("ForzaTracker", sample["bt"]["error"])
        self.transport.query_battery_diagnostic.assert_not_called()

    def test_real_monitor_guard_excludes_diagnostic_before_oem_or_usb(self):
        ownership = Mock(abandoned=False)
        with patch("ups_monitor.instance_guard._WindowsOwnership", return_value=ownership), \
             patch("ups_monitor.instance_guard._FileOwnership", return_value=ownership), \
             patch.object(diagnostic, "MonitorInstanceGuard", MonitorInstanceGuard):
            with MonitorInstanceGuard():
                with self.assertRaises(AlreadyRunningError):
                    diagnostic.capture_battery_samples()
        self.oem.assert_not_called()
        self.enumerate.assert_not_called()
        self.constructor.assert_not_called()

    def test_diagnostic_constructs_no_store_notifications_monitor_or_power_actions(self):
        with patch("ups_monitor.core.Store") as store, patch("ups_monitor.core.PolicyEngine") as policy, \
             patch("ups_monitor.windows_notifications.WindowsNotifier") as notices, \
             patch("ups_monitor.server.Monitor") as monitor, patch("ups_monitor.server.windows_action") as action:
            diagnostic.capture_battery_samples()
        for forbidden in (store, policy, notices, monitor, action):
            forbidden.assert_not_called()

    def test_cli_dispatch_output_and_source_frozen_roles_are_explicit(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(entrypoint.main(["battery-diagnostic", "--label", "battery"]), 0)
        self.assertEqual(json.loads(output.getvalue())["samples"][0]["capture_label"], "battery")
        with patch.object(sys, "frozen", False, create=True):
            self.assertEqual(runtime.child_command("battery-diagnostic", "--samples", "2"),
                             [sys.executable, "-m", "ups_monitor.battery_diagnostics", "--samples", "2"])
        with patch.object(sys, "frozen", True, create=True):
            self.assertEqual(runtime.child_command("battery-worker", PATH, "BL"),
                             [sys.executable, "battery-worker", PATH, "BL"])
        for arguments in (["--samples", "121"], ["--interval", "nan"], ["--interval", "0.1"], ["--label", "other"]):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                diagnostic.parse_args(arguments)

    def test_cli_failure_is_json_and_does_not_claim_a_completed_capture(self):
        self.guard.return_value.__enter__.side_effect = AlreadyRunningError("Monitor activo")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(diagnostic.main([]), 1)
        report = json.loads(output.getvalue())
        self.assertEqual(report["samples"], [])
        self.assertIn("Monitor activo", report["error"])
        self.enumerate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
