import socket
import unittest
from unittest.mock import Mock, patch

from ups_monitor.adapters import ForzaBridge, TelemetryError
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
        self.assertIsNone(sample["runtime_seconds"])
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
