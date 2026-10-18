import socket
import unittest
from unittest.mock import Mock, patch

from ups_monitor.adapters import ForzaBridge, TelemetryError
from ups_monitor.battery_estimation import BATTERY_ESTIMATE_METHOD
from ups_monitor.native_adapter import NativeHidAdapter, forza_monitor_running, make_adapter

PATH = r"\\?\hid#vid_0665&pid_5161#unit#{4d1e55b2-f16f-11cf-88cb-001111000030}"
FRAME = bytes.fromhex("236701206c206701206c200820500f2012c00020e9203c200920a20d")


def metadata(**changes):
    return {"path": PATH, "metadata_open": True, "vendor_id": 0x0665,
            "product_id": 0x5161, "input_report_bytes": 9,
            "output_report_bytes": 9, "feature_report_bytes": 0, **changes}


class NativeAdapterTests(unittest.TestCase):
    def test_reconnection_backoff_is_exponential_capped_and_success_resets(self):
        clock = Mock(return_value=100)
        adapter = NativeHidAdapter(clock=clock)
        with patch("ups_monitor.native_adapter.forza_monitor_running", return_value=False), \
             patch("ups_monitor.native_adapter.enumerate_ups", return_value=[]) as enumerate_hid:
            for delay in (2, 4, 8, 16, 30, 30):
                before = enumerate_hid.call_count
                with self.assertRaises(TelemetryError):
                    adapter.read()
                self.assertEqual(adapter.retry_after_seconds, delay)
                with self.assertRaises(TelemetryError):
                    adapter.read()
                self.assertEqual(enumerate_hid.call_count, before + 1)
                clock.return_value += delay
        transport = Mock()
        transport.query_status.return_value = FRAME
        with patch("ups_monitor.native_adapter.forza_monitor_running", return_value=False), \
             patch("ups_monitor.native_adapter.enumerate_ups", return_value=[metadata()]), \
             patch("ups_monitor.native_adapter.StatusTransport", return_value=transport):
            self.assertFalse(adapter.read()["on_battery"])
        self.assertEqual(adapter.retry_after_seconds, 0)
        self.assertEqual(adapter._failures, 0)

    def test_forza_active_blocks_every_native_query_including_cached_transport(self):
        adapter = NativeHidAdapter()
        existing = Mock()
        with patch("ups_monitor.native_adapter.forza_monitor_running", return_value=True), patch("ups_monitor.native_adapter.enumerate_ups") as enumerate_hid, patch("ups_monitor.native_adapter.StatusTransport") as transport:
            for cached in [None, existing]:
                adapter.transport = cached
                with self.assertRaises(TelemetryError):
                    adapter.read()
            enumerate_hid.assert_not_called()
            transport.assert_not_called()
            existing.query_status.assert_not_called()

    def test_online_frame_matches_recorded_device_measurements(self):
        transport = Mock()
        transport.query_status.return_value = FRAME
        with patch("ups_monitor.native_adapter.forza_monitor_running", return_value=False), patch("ups_monitor.native_adapter.enumerate_ups", return_value=[metadata()]), patch("ups_monitor.native_adapter.StatusTransport", return_value=transport):
            sample = NativeHidAdapter().read()
        self.assertEqual(sample["input_voltage"], 218.1)
        self.assertEqual(sample["output_voltage"], 218.1)
        self.assertEqual(sample["frequency_hz"], 60)
        self.assertEqual(sample["battery_voltage"], 27.41)
        self.assertIs(sample["on_battery"], False)
        self.assertIsNone(sample["battery_percent"])
        self.assertEqual(sample["battery_percent_estimate"], 100)
        self.assertEqual(sample["battery_estimate_method"], BATTERY_ESTIMATE_METHOD)
        self.assertIsNone(sample["runtime_seconds"])
        transport.query_status.assert_called_once_with()

    def test_estimate_uses_raw_qs_precision_before_public_voltage_rounding(self):
        fields = FRAME[1:-1].split(b" ")
        fields[7], fields[8], fields[9] = b"\xDA", b"\x3C", b"\x89"
        transport = Mock()
        transport.query_status.return_value = b"#" + b" ".join(fields) + b"\r"
        with patch("ups_monitor.native_adapter.forza_monitor_running", return_value=False), \
             patch("ups_monitor.native_adapter.enumerate_ups", return_value=[metadata()]), \
             patch("ups_monitor.native_adapter.StatusTransport", return_value=transport):
            sample = NativeHidAdapter().read()
        self.assertIs(sample["on_battery"], True)
        self.assertEqual(sample["load_percent"], 8)
        self.assertEqual(sample["battery_voltage"], 25.65)
        self.assertEqual(sample["battery_percent_estimate"], 86)
        self.assertEqual(sample["battery_estimate_method"], BATTERY_ESTIMATE_METHOD)
        self.assertIsNone(sample["battery_percent"])
        self.assertFalse(any(key.startswith("_") for key in sample))
        transport.query_status.assert_called_once_with()

    def test_inexact_attributes_never_construct_transport(self):
        invalid = [metadata(metadata_open="true"), metadata(vendor_id=1),
                   metadata(product_id=1), metadata(feature_report_bytes=False),
                   metadata(input_report_bytes=9.0), metadata(output_report_bytes=8)]
        with patch("ups_monitor.native_adapter.forza_monitor_running", return_value=False), patch("ups_monitor.native_adapter.StatusTransport") as transport:
            for device in invalid:
                with patch("ups_monitor.native_adapter.enumerate_ups", return_value=[device]):
                    with self.assertRaises(TelemetryError):
                        NativeHidAdapter().read()
            transport.assert_not_called()

    def test_device_selection_requires_exactly_one_collection(self):
        with patch("ups_monitor.native_adapter.forza_monitor_running", return_value=False), patch("ups_monitor.native_adapter.StatusTransport") as transport:
            for devices in [[], [metadata(), metadata()]]:
                with patch("ups_monitor.native_adapter.enumerate_ups", return_value=devices):
                    with self.assertRaises(TelemetryError):
                        NativeHidAdapter().read()
            transport.assert_not_called()

    def test_transport_timeout_and_parse_errors_reset_cached_transport(self):
        for error in [OSError("USB unavailable"), TimeoutError("deadline")]:
            adapter = NativeHidAdapter()
            adapter.transport = Mock()
            adapter.transport.query_status.side_effect = error
            with patch("ups_monitor.native_adapter.forza_monitor_running", return_value=False):
                with self.assertRaises(TelemetryError):
                    adapter.read()
            self.assertIsNone(adapter.transport)
        adapter = NativeHidAdapter()
        adapter.transport = Mock()
        adapter.transport.query_status.return_value = b"invalid\r"
        with patch("ups_monitor.native_adapter.forza_monitor_running", return_value=False):
            with self.assertRaises(TelemetryError):
                adapter.read()
        self.assertIsNone(adapter.transport)

    def test_estimator_is_per_adapter_and_resets_on_failure_oem_conflict_and_long_gap(self):
        clock = Mock(return_value=100)
        adapter = NativeHidAdapter(clock=clock)
        transport = Mock()
        base_sample = {"timestamp": "unused", "battery_voltage": 27.4, "load_percent": 10,
                       "on_battery": False, "fault": False, "raw_status": "Line mode",
                       "battery_percent": None}
        battery_sample = {**base_sample, "battery_voltage": 25.3, "on_battery": True, "raw_status": "Battery mode"}
        with patch("ups_monitor.native_adapter.forza_monitor_running", return_value=False) as forza_active, \
             patch("ups_monitor.native_adapter.enumerate_ups", return_value=[metadata()]), \
             patch("ups_monitor.native_adapter.StatusTransport", return_value=transport), \
             patch("ups_monitor.native_adapter.parse_qs", return_value=base_sample.copy()) as decode:
            self.assertEqual(adapter.read()["battery_percent_estimate"], 100)
            decode.return_value = battery_sample.copy()
            self.assertEqual(NativeHidAdapter(clock=clock).read()["battery_percent_estimate"], 81)
            self.assertEqual(adapter.read()["battery_percent_estimate"], 100)
            clock.return_value += 4
            self.assertEqual(adapter.read()["battery_percent_estimate"], 98)
            transport.query_status.side_effect = OSError("lost connection")
            with self.assertRaises(TelemetryError):
                adapter.read()
            clock.return_value += 2
            transport.query_status.side_effect = None
            self.assertEqual(adapter.read()["battery_percent_estimate"], 81)
            decode.return_value = base_sample.copy()
            clock.return_value += 16
            self.assertEqual(adapter.read()["battery_percent_estimate"], 100)
            forza_active.return_value = True
            with self.assertRaises(TelemetryError):
                adapter.read()
            self.assertIsNone(adapter.transport)
            forza_active.return_value = False
            decode.return_value = battery_sample.copy()
            self.assertEqual(adapter.read()["battery_percent_estimate"], 81)
            self.assertIsNone(adapter.read()["battery_percent"])
            self.assertTrue(all(call.args == () for call in transport.query_status.call_args_list))

    def test_auto_selects_once_and_never_falls_back_on_usb_failure(self):
        with patch("ups_monitor.native_adapter.forza_monitor_running", side_effect=[False, True]), patch("ups_monitor.native_adapter.enumerate_ups") as enumerate_hid:
            adapter = make_adapter("auto", "http://127.0.0.1:15179/Forza")
            self.assertIsInstance(adapter, NativeHidAdapter)
            with self.assertRaises(TelemetryError):
                adapter.read()
            self.assertIsInstance(adapter, NativeHidAdapter)
            enumerate_hid.assert_not_called()
        with patch("ups_monitor.native_adapter.forza_monitor_running", return_value=True):
            adapter = make_adapter("auto", "http://127.0.0.1:15179/Forza")
        self.assertIsInstance(adapter, ForzaBridge)
        with patch.object(adapter, "_read", side_effect=TelemetryError("lost")), patch("ups_monitor.native_adapter.StatusTransport") as transport:
            with self.assertRaises(TelemetryError):
                adapter.read()
            transport.assert_not_called()

    def test_unknown_source_is_rejected(self):
        with self.assertRaises(ValueError):
            make_adapter("typo", "http://127.0.0.1:15179/Forza")

    def test_udp_detection_has_loopback_target_and_timeout(self):
        probe = Mock()
        probe.recv.return_value = b"monitorOK"
        context = Mock()
        context.__enter__ = Mock(return_value=probe)
        context.__exit__ = Mock(return_value=False)
        with patch("ups_monitor.native_adapter.socket.socket", return_value=context):
            self.assertIs(forza_monitor_running(), True)
        probe.settimeout.assert_called_once_with(0.3)
        probe.connect.assert_called_once_with(("127.0.0.1", 51222))
        probe.send.assert_called_once_with(b"VP_LINK")

    def test_udp_uncertainty_and_unexpected_replies_block_native_reads(self):
        for error, unsafe in [(TimeoutError(), True), (PermissionError(13, "denied"), True),
                              (OSError("unknown"), True), (ConnectionResetError(10054, "unreachable"), False),
                              (ConnectionRefusedError(111, "refused"), False)]:
            probe = Mock()
            probe.recv.side_effect = error
            context = Mock()
            context.__enter__ = Mock(return_value=probe)
            context.__exit__ = Mock(return_value=False)
            with patch("ups_monitor.native_adapter.socket.socket", return_value=context):
                self.assertIs(forza_monitor_running(), unsafe)
        for reply in [b"unexpected", b""]:
            probe = Mock()
            probe.recv.return_value = reply
            context = Mock()
            context.__enter__ = Mock(return_value=probe)
            context.__exit__ = Mock(return_value=False)
            with patch("ups_monitor.native_adapter.socket.socket", return_value=context):
                self.assertIs(forza_monitor_running(), True)


if __name__ == "__main__":
    unittest.main()
