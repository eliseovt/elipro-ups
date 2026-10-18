import socket
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import Mock, patch

from ups_monitor.adapters import ForzaBridge, TelemetryError
from ups_monitor.battery_estimation import BATTERY_ESTIMATE_METHOD
from ups_monitor.native_adapter import (ForzaProbeReason, ForzaProbeResult, NativeHidAdapter,
                                        forza_monitor_running, make_adapter, probe_forza_monitor)

PATH = r"\\?\hid#vid_0665&pid_5161#unit#{4d1e55b2-f16f-11cf-88cb-001111000030}"
FRAME = bytes.fromhex("236701206c206701206c200820500f2012c00020e9203c200920a20d")
ABSENT = ForzaProbeResult("absent", ForzaProbeReason.PORT_UNREACHABLE)
ACTIVE = ForzaProbeResult("active", ForzaProbeReason.RECOGNIZED_REPLY)
UNKNOWN = ForzaProbeResult("unknown", ForzaProbeReason.TIMEOUT)


def metadata(**changes):
    return {"path": PATH, "metadata_open": True, "vendor_id": 0x0665,
            "product_id": 0x5161, "input_report_bytes": 9,
            "output_report_bytes": 9, "feature_report_bytes": 0, **changes}


class NativeAdapterTests(unittest.TestCase):
    def test_reconnection_backoff_is_exponential_capped_and_success_resets(self):
        clock = Mock(return_value=100)
        adapter = NativeHidAdapter(clock=clock)
        with patch("ups_monitor.native_adapter.probe_forza_monitor", return_value=ABSENT), \
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
        with patch("ups_monitor.native_adapter.probe_forza_monitor", return_value=ABSENT), \
             patch("ups_monitor.native_adapter.enumerate_ups", return_value=[metadata()]), \
             patch("ups_monitor.native_adapter.StatusTransport", return_value=transport):
            self.assertFalse(adapter.read()["on_battery"])
        self.assertEqual(adapter.retry_after_seconds, 0)
        self.assertEqual(adapter._failures, 0)

    def test_forza_active_blocks_every_native_query_including_cached_transport(self):
        adapter = NativeHidAdapter()
        existing = Mock()
        with patch("ups_monitor.native_adapter.probe_forza_monitor", return_value=ACTIVE) as probe, \
             patch("ups_monitor.native_adapter.time.sleep") as sleep, \
             patch("ups_monitor.native_adapter.enumerate_ups") as enumerate_hid, \
             patch("ups_monitor.native_adapter.StatusTransport") as transport:
            for cached in [None, existing]:
                probe.reset_mock()
                adapter.transport = cached
                with self.assertRaisesRegex(TelemetryError, "ForzaTracker está activo"):
                    adapter.read()
                probe.assert_called_once_with()
            enumerate_hid.assert_not_called()
            transport.assert_not_called()
            existing.query_status.assert_not_called()
            sleep.assert_not_called()

    def test_uncertain_probe_recovers_inside_read_without_touching_usb_before_absence(self):
        adapter = NativeHidAdapter()
        transport = Mock()
        transport.query_status.return_value = FRAME
        order = []
        def probe():
            order.append("probe")
            return UNKNOWN if len(order) == 1 else ABSENT
        def enumerate_hid():
            order.append("enumerate")
            return [metadata()]
        with patch("ups_monitor.native_adapter.probe_forza_monitor", side_effect=probe) as check, \
             patch("ups_monitor.native_adapter.time.sleep") as sleep, \
             patch("ups_monitor.native_adapter.enumerate_ups", side_effect=enumerate_hid), \
             patch("ups_monitor.native_adapter.StatusTransport", return_value=transport):
            sample = adapter.read()
        self.assertEqual(order, ["probe", "probe", "enumerate"])
        self.assertEqual(check.call_count, 2)
        sleep.assert_called_once_with(0.1)
        self.assertEqual(sample["battery_percent_estimate"], 100)
        transport.query_status.assert_called_once_with()
        self.assertEqual(adapter._failures, 0)

    def test_retry_stops_immediately_when_oem_is_confirmed(self):
        adapter = NativeHidAdapter()
        with patch("ups_monitor.native_adapter.probe_forza_monitor", side_effect=[UNKNOWN, ACTIVE]) as probe, \
             patch("ups_monitor.native_adapter.time.sleep") as sleep, \
             patch("ups_monitor.native_adapter.enumerate_ups") as enumerate_hid, \
             patch("ups_monitor.native_adapter.StatusTransport") as transport:
            with self.assertRaisesRegex(TelemetryError, "ForzaTracker está activo"):
                adapter.read()
        self.assertEqual(probe.call_count, 2)
        sleep.assert_called_once_with(0.1)
        enumerate_hid.assert_not_called()
        transport.assert_not_called()

    def test_uncertainty_retries_only_three_times_blocks_cached_usb_and_retains_reason(self):
        adapter = NativeHidAdapter()
        for cached in (None, Mock()):
            with self.subTest(cached=cached is not None), \
                 patch("ups_monitor.native_adapter.probe_forza_monitor", return_value=ForzaProbeResult(
                     "unknown", ForzaProbeReason.TIMEOUT, winerror=10060)) as probe, \
                 patch("ups_monitor.native_adapter.time.sleep") as sleep, \
                 patch("ups_monitor.native_adapter.enumerate_ups") as enumerate_hid, \
                 patch("ups_monitor.native_adapter.StatusTransport") as transport, \
                 patch.object(adapter._battery_estimator, "reset") as reset:
                adapter.transport = cached
                with self.assertRaises(TelemetryError) as raised:
                    adapter.read()
                message = str(raised.exception)
                self.assertIn("No se pudo confirmar que el USB esté libre", message)
                self.assertIn("Elipro reintentará", message)
                self.assertIn("timeout", message)
                self.assertIn("winerror=10060", message)
                self.assertNotIn("ForzaTracker está activo", message)
                self.assertEqual(probe.call_count, 3)
                self.assertEqual(sleep.call_args_list, [unittest.mock.call(0.1), unittest.mock.call(0.1)])
                enumerate_hid.assert_not_called()
                transport.assert_not_called()
                reset.assert_called_once_with()
                self.assertIsNone(adapter.transport)
                if cached is not None:
                    cached.query_status.assert_not_called()

    def test_no_absence_cache_and_later_unknown_or_active_cannot_reuse_transport(self):
        for next_result, expected_calls in ((UNKNOWN, 4), (ACTIVE, 2)):
            with self.subTest(state=next_result.state):
                adapter = NativeHidAdapter()
                transport = Mock()
                transport.query_status.return_value = FRAME
                with patch("ups_monitor.native_adapter.probe_forza_monitor", side_effect=[ABSENT] + [next_result] * 3) as probe, \
                     patch("ups_monitor.native_adapter.time.sleep"), \
                     patch("ups_monitor.native_adapter.enumerate_ups", return_value=[metadata()]) as enumerate_hid, \
                     patch("ups_monitor.native_adapter.StatusTransport", return_value=transport):
                    adapter.read()
                    with self.assertRaises(TelemetryError):
                        adapter.read()
                self.assertEqual(probe.call_count, expected_calls)
                enumerate_hid.assert_called_once_with()
                transport.query_status.assert_called_once_with()
                self.assertIsNone(adapter.transport)

    def test_online_frame_matches_recorded_device_measurements(self):
        transport = Mock()
        transport.query_status.return_value = FRAME
        with patch("ups_monitor.native_adapter.probe_forza_monitor", return_value=ABSENT), patch("ups_monitor.native_adapter.enumerate_ups", return_value=[metadata()]), patch("ups_monitor.native_adapter.StatusTransport", return_value=transport):
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
        with patch("ups_monitor.native_adapter.probe_forza_monitor", return_value=ABSENT), \
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
        with patch("ups_monitor.native_adapter.probe_forza_monitor", return_value=ABSENT), patch("ups_monitor.native_adapter.StatusTransport") as transport:
            for device in invalid:
                with patch("ups_monitor.native_adapter.enumerate_ups", return_value=[device]):
                    with self.assertRaises(TelemetryError):
                        NativeHidAdapter().read()
            transport.assert_not_called()

    def test_device_selection_requires_exactly_one_collection(self):
        with patch("ups_monitor.native_adapter.probe_forza_monitor", return_value=ABSENT), patch("ups_monitor.native_adapter.StatusTransport") as transport:
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
            with patch("ups_monitor.native_adapter.probe_forza_monitor", return_value=ABSENT):
                with self.assertRaises(TelemetryError):
                    adapter.read()
            self.assertIsNone(adapter.transport)
        adapter = NativeHidAdapter()
        adapter.transport = Mock()
        adapter.transport.query_status.return_value = b"invalid\r"
        with patch("ups_monitor.native_adapter.probe_forza_monitor", return_value=ABSENT):
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
        with patch("ups_monitor.native_adapter.probe_forza_monitor", return_value=ABSENT) as forza_active, \
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
            forza_active.return_value = ACTIVE
            with self.assertRaises(TelemetryError):
                adapter.read()
            self.assertIsNone(adapter.transport)
            forza_active.return_value = ABSENT
            decode.return_value = battery_sample.copy()
            self.assertEqual(adapter.read()["battery_percent_estimate"], 81)
            self.assertIsNone(adapter.read()["battery_percent"])
            self.assertTrue(all(call.args == () for call in transport.query_status.call_args_list))

    def test_auto_selects_once_and_never_falls_back_on_usb_failure(self):
        with patch("ups_monitor.native_adapter.forza_monitor_running", return_value=False), \
             patch("ups_monitor.native_adapter.probe_forza_monitor", return_value=ACTIVE), \
             patch("ups_monitor.native_adapter.enumerate_ups") as enumerate_hid:
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

    def test_auto_preserves_conservative_selection_and_explicit_bridge_does_not_probe(self):
        for result, expected_type in ((ABSENT, NativeHidAdapter), (ACTIVE, ForzaBridge), (UNKNOWN, ForzaBridge)):
            with self.subTest(state=result.state), \
                 patch("ups_monitor.native_adapter.probe_forza_monitor", return_value=result) as probe:
                self.assertIsInstance(make_adapter("auto", "http://127.0.0.1:15179/Forza"), expected_type)
                probe.assert_called_once_with()
        with patch("ups_monitor.native_adapter.probe_forza_monitor") as probe:
            self.assertIsInstance(make_adapter("bridge", "http://127.0.0.1:15179/Forza"), ForzaBridge)
            probe.assert_not_called()

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

    def test_probe_result_is_immutable_and_only_exact_oem_reply_confirms_active(self):
        with self.assertRaises(FrozenInstanceError):
            ACTIVE.state = "absent"
        for reply, state, reason in ((b"monitorOK", "active", ForzaProbeReason.RECOGNIZED_REPLY),
                                      (b"", "unknown", ForzaProbeReason.EMPTY_REPLY),
                                      (b"monitorOK\x00", "unknown", ForzaProbeReason.UNEXPECTED_REPLY),
                                      (b"other service", "unknown", ForzaProbeReason.UNEXPECTED_REPLY)):
            with self.subTest(reply=reply):
                channel = Mock()
                channel.recv.return_value = reply
                context = Mock(__enter__=Mock(return_value=channel), __exit__=Mock(return_value=False))
                with patch("ups_monitor.native_adapter.socket.socket", return_value=context):
                    result = probe_forza_monitor()
                self.assertEqual((result.state, result.reason), (state, reason))
                context.__exit__.assert_called_once()

    def test_probe_classifies_query_error_and_preserves_numeric_diagnostics(self):
        windows_closed = OSError("closed")
        windows_closed.winerror = 10061
        for error, state, reason in ((TimeoutError(), "unknown", ForzaProbeReason.TIMEOUT),
                                     (PermissionError(13, "private details"), "unknown", ForzaProbeReason.PERMISSION_DENIED),
                                     (OSError(5, "private details"), "unknown", ForzaProbeReason.SOCKET_ERROR),
                                     (ConnectionResetError(10054, "closed"), "absent", ForzaProbeReason.PORT_UNREACHABLE),
                                     (ConnectionRefusedError(111, "closed"), "absent", ForzaProbeReason.PORT_UNREACHABLE),
                                     (windows_closed, "absent", ForzaProbeReason.PORT_UNREACHABLE)):
            for operation in ("connect", "send", "recv"):
                with self.subTest(operation=operation, error=type(error).__name__):
                    channel = Mock()
                    getattr(channel, operation).side_effect = error
                    context = Mock(__enter__=Mock(return_value=channel), __exit__=Mock(return_value=False))
                    with patch("ups_monitor.native_adapter.socket.socket", return_value=context):
                        result = probe_forza_monitor()
                    self.assertEqual((result.state, result.reason), (state, reason))
                    self.assertEqual(result.errno, error.errno)
                    self.assertEqual(result.winerror, getattr(error, "winerror", None))
                    self.assertNotIn("private details", repr(result))

    def test_socket_factory_configuration_and_close_failures_never_confirm_absence(self):
        for operation in ("factory", "enter", "settimeout", "exit"):
            with self.subTest(operation=operation):
                channel = Mock()
                channel.recv.return_value = b"monitorOK"
                context = Mock(__enter__=Mock(return_value=channel), __exit__=Mock(return_value=False))
                factory = Mock(return_value=context)
                error = OSError(10061, "not a confirmed query response")
                if operation == "factory":
                    factory.side_effect = error
                elif operation == "enter":
                    context.__enter__.side_effect = error
                elif operation == "settimeout":
                    channel.settimeout.side_effect = error
                else:
                    context.__exit__.side_effect = error
                with patch("ups_monitor.native_adapter.socket.socket", factory):
                    result = probe_forza_monitor()
                self.assertEqual((result.state, result.reason), ("unknown", ForzaProbeReason.SOCKET_ERROR))
                self.assertEqual(result.errno, 10061)

    def test_query_error_followed_by_close_error_remains_uncertain_and_blocks_usb(self):
        for query_error in (TimeoutError(), ConnectionRefusedError(111, "query closed")):
            with self.subTest(query_error=type(query_error).__name__):
                channel = Mock()
                channel.recv.side_effect = query_error
                context = Mock(__enter__=Mock(return_value=channel),
                               __exit__=Mock(side_effect=OSError(10061, "close failed")))
                with patch("ups_monitor.native_adapter.socket.socket", return_value=context), \
                     patch("ups_monitor.native_adapter.time.sleep") as sleep, \
                     patch("ups_monitor.native_adapter.enumerate_ups") as enumerate_hid, \
                     patch("ups_monitor.native_adapter.StatusTransport") as transport:
                    result = probe_forza_monitor()
                    self.assertEqual((result.state, result.reason, result.errno),
                                     ("unknown", ForzaProbeReason.SOCKET_ERROR, 10061))
                    with self.assertRaisesRegex(TelemetryError, "socket_error, errno=10061"):
                        NativeHidAdapter().read()
                self.assertEqual(context.__enter__.call_count, 4)
                self.assertEqual(context.__exit__.call_count, 4)
                self.assertEqual(sleep.call_count, 2)
                enumerate_hid.assert_not_called()
                transport.assert_not_called()

    def test_boolean_wrapper_is_false_only_for_confirmed_absence(self):
        for result, expected in ((ABSENT, False), (ACTIVE, True), (UNKNOWN, True)):
            with self.subTest(state=result.state), \
                 patch("ups_monitor.native_adapter.probe_forza_monitor", return_value=result):
                self.assertIs(forza_monitor_running(), expected)

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
