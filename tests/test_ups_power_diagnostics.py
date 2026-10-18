"""Read-only diagnostics preserve evidence and refuse concurrent readers."""
import copy
import io
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from ups_monitor import entrypoint, runtime, ups_power_diagnostics as diagnostic
from ups_monitor.instance_guard import InstanceGuardError


FRAME = bytes.fromhex("23 62 00 20 6C 20 62 01 20 6C 20 07 20 4F FA 20 12 C0 00 20 E9 20 3C 20 09 20 A2 0D")
DEVICE = {"path": r"\\?\hid#vid_0665&pid_5161#unit", "metadata_open": True,
          "vendor_id": 0x0665, "product_id": 0x5161, "version_number": 3,
          "input_report_bytes": 9, "output_report_bytes": 9, "feature_report_bytes": 0,
          "manufacturer": "INNO TECH", "product": "USB to Serial"}


class UpsPowerDiagnosticTests(unittest.TestCase):
    def capture(self, devices=None, raw=FRAME, occupied=False):
        with patch.object(diagnostic, "MonitorInstanceGuard"), \
                patch.object(diagnostic, "forza_monitor_running", return_value=occupied), \
                patch.object(diagnostic, "enumerate_ups_bounded", return_value=devices if devices is not None else [DEVICE]), \
                patch.object(diagnostic, "StatusTransport") as transport:
            transport.return_value.query_status.return_value = raw
            report = diagnostic.capture_compatibility()
            return report, transport

    def test_communication_is_not_physical_control_validation(self):
        report, transport = self.capture()
        self.assertEqual(report["communication"], "compatible")
        self.assertEqual(report["protocol"], "T")
        self.assertEqual(report["raw_responses"][0]["raw_hex"], FRAME.hex())
        self.assertEqual(report["raw_responses"][0]["raw_text"], repr(FRAME))
        self.assertFalse(report["available"])
        self.assertEqual(report["physical_validation"], "pending")
        self.assertEqual(report["manual_restart"], "unvalidated")
        self.assertNotIn("path", report["device"])
        transport.return_value.query_status.assert_called_once_with()
        self.assertEqual(transport.return_value.method_calls, [unittest.mock.call.query_status()])

    def test_wrong_revision_or_descriptors_never_send_a_query(self):
        for changes in ({"version_number": 4}, {"input_report_bytes": 8}, {"metadata_open": False}, {"vendor_id": True}):
            report, transport = self.capture([{**DEVICE, **changes}])
            self.assertEqual(report["validation"], "incompatible")
            transport.assert_not_called()

    def test_unsupported_qs_is_preserved_as_incompatible(self):
        report, _ = self.capture(raw=b"N\r")
        self.assertEqual(report["validation"], "incompatible")
        self.assertEqual(report["raw_responses"][0]["raw_hex"], "4e0d")

    def test_oem_or_an_existing_monitor_prevents_reading(self):
        with self.assertRaises(OSError):
            self.capture(occupied=True)
        with patch.object(diagnostic, "MonitorInstanceGuard") as guard, \
                patch.object(diagnostic, "enumerate_ups_bounded") as enumerate_device:
            guard.return_value.__enter__.side_effect = InstanceGuardError("monitor active")
            with self.assertRaises(InstanceGuardError):
                diagnostic.capture_compatibility()
            enumerate_device.assert_not_called()

    def test_dispatch_and_worker_roles_are_fixed_for_source_and_package(self):
        with patch.object(diagnostic, "main", return_value=0) as main:
            self.assertEqual(entrypoint.main(["ups-power-diagnostic", "--help"]), 0)
            main.assert_called_once_with(["--help"])
        with patch("sys.frozen", False, create=True):
            self.assertEqual(runtime.child_command("ups-power-diagnostic")[1:], ["-m", "ups_monitor.ups_power_diagnostics"])
        with patch("sys.frozen", True, create=True):
            self.assertEqual(runtime.child_command("control-worker", "path", "profile", "cancel")[1:],
                             ["control-worker", "path", "profile", "cancel"])


if __name__ == "__main__":
    unittest.main()
