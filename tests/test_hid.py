import ctypes
import subprocess
import unittest
from unittest.mock import Mock, patch

from ups_monitor.hid_windows import HIDD_ATTRIBUTES, HIDP_CAPS, StatusTransport, _cooldown_until, _query_status_once, _read_report

PATH = r"\\?\hid#vid_0665&pid_5161#unit#{4d1e55b2-f16f-11cf-88cb-001111000030}"


class HidTests(unittest.TestCase):
    def setUp(self):
        _cooldown_until.clear()

    def test_only_status_query_is_allowed_before_launching_worker(self):
        transport = StatusTransport(PATH)
        with patch("ups_monitor.hid_windows.subprocess.run") as run:
            for command in [b"S01\r", b"Q1\r", b"QS\rS01\r", "QS\r", b"QS"]:
                with self.assertRaises(ValueError):
                    transport.query_status(command)
            run.assert_not_called()

    def test_worker_is_bounded_and_returns_raw_bytes(self):
        with patch("ups_monitor.hid_windows.subprocess.run") as run:
            run.return_value = Mock(returncode=0, stdout=b"#0123456789\r", stderr=b"")
            self.assertEqual(StatusTransport(PATH).query_status(), b"#0123456789\r")
            self.assertEqual(run.call_args.kwargs["timeout"], 3)
            self.assertEqual(run.call_args.args[0][-2:], ["--qs-worker", PATH])
        with patch("ups_monitor.hid_windows.subprocess.run", side_effect=subprocess.TimeoutExpired("hid", 3)):
            with self.assertRaises(TimeoutError):
                StatusTransport(PATH).query_status()

    def fake_apis(self, *, invalid_caps=False):
        hid, kernel = Mock(), Mock()
        kernel.CreateFileW.return_value = 10
        kernel.CreateEventW.return_value = 20
        kernel.CreateMutexW.return_value = 30
        kernel.WaitForSingleObject.return_value = 0
        def attributes(handle, pointer):
            value = ctypes.cast(pointer, ctypes.POINTER(HIDD_ATTRIBUTES)).contents
            value.VendorID, value.ProductID = 0x0665, 0x5161
            return True
        hid.HidD_GetAttributes.side_effect = attributes
        hid.HidD_GetPreparsedData.return_value = True
        def caps(preparsed, pointer):
            value = ctypes.cast(pointer, ctypes.POINTER(HIDP_CAPS)).contents
            value.InputReportByteLength = 8 if invalid_caps else 9
            value.OutputReportByteLength = 9
            value.FeatureReportByteLength = 0
            return 0x00110000
        hid.HidP_GetCaps.side_effect = caps
        hid.HidD_SetOutputReport.return_value = True
        hid.HidD_FlushQueue.return_value = True
        return Mock(), hid, kernel

    def test_exact_report_framing_and_handles_are_closed(self):
        apis = self.fake_apis()
        with patch("ups_monitor.hid_windows._apis", return_value=apis), patch("ups_monitor.hid_windows._read_report", side_effect=[b"#0123456", b"789\r\x00\x00\x00\x00"]):
            self.assertEqual(_query_status_once(PATH), b"#0123456789\r")
        args = apis[1].HidD_SetOutputReport.call_args.args
        self.assertEqual(bytes(args[1]), b"\x00QS\r\x00\x00\x00\x00\x00")
        self.assertEqual(args[2], 9)
        self.assertEqual(apis[2].CreateFileW.call_args.args[1:6], (0xC0000000, 3, None, 3, 0x40000000))
        self.assertEqual(apis[2].CloseHandle.call_count, 3)
        apis[1].HidD_FlushQueue.assert_called_once_with(10)
        apis[2].ReleaseMutex.assert_called_once_with(30)
        self.assertLessEqual(apis[2].WaitForSingleObject.call_args.args[1], 500)

    def test_changed_caps_never_send_report(self):
        apis = self.fake_apis(invalid_caps=True)
        with patch("ups_monitor.hid_windows._apis", return_value=apis):
            with self.assertRaises(OSError):
                _query_status_once(PATH)
        apis[1].HidD_SetOutputReport.assert_not_called()
        self.assertEqual(apis[2].CloseHandle.call_count, 2)
        apis[2].ReleaseMutex.assert_called_once_with(30)

    def test_mutex_contention_and_abandonment_never_send(self):
        for result in [258, 128]:
            apis = self.fake_apis()
            apis[2].WaitForSingleObject.return_value = result
            with patch("ups_monitor.hid_windows._apis", return_value=apis):
                with self.assertRaises(OSError):
                    _query_status_once(PATH)
            apis[1].HidD_SetOutputReport.assert_not_called()
            apis[2].CreateFileW.assert_not_called()
            self.assertEqual(apis[2].ReleaseMutex.call_count, 1 if result == 128 else 0)
            apis[2].CloseHandle.assert_called_once_with(30)

    def test_failed_worker_cooldown_survives_new_transport_and_does_not_extend(self):
        with patch("ups_monitor.hid_windows.time.monotonic", return_value=100), patch("ups_monitor.hid_windows.subprocess.run") as run:
            run.return_value = Mock(returncode=1, stdout=b"", stderr=b"timeout")
            with self.assertRaises(OSError):
                StatusTransport(PATH).query_status()
        with patch("ups_monitor.hid_windows.time.monotonic", return_value=103), patch("ups_monitor.hid_windows.subprocess.run") as run:
            with self.assertRaises(TimeoutError):
                StatusTransport(PATH).query_status()
            run.assert_not_called()
            self.assertEqual(_cooldown_until[PATH.casefold()], 105)
        with patch("ups_monitor.hid_windows.time.monotonic", return_value=105), patch("ups_monitor.hid_windows.subprocess.run") as run:
            run.return_value = Mock(returncode=0, stdout=b"#ok\r", stderr=b"")
            self.assertEqual(StatusTransport(PATH).query_status(), b"#ok\r")

    def test_queue_flush_failure_never_sends_and_releases_resources(self):
        apis = self.fake_apis()
        apis[1].HidD_FlushQueue.return_value = False
        with patch("ups_monitor.hid_windows._apis", return_value=apis), patch("ups_monitor.hid_windows.ctypes.get_last_error", return_value=5):
            with self.assertRaises(OSError):
                _query_status_once(PATH)
        apis[1].HidD_SetOutputReport.assert_not_called()
        self.assertEqual(apis[2].CloseHandle.call_count, 3)
        apis[2].ReleaseMutex.assert_called_once_with(30)

    def test_pending_timeout_cancels_and_drains_before_return(self):
        kernel = Mock()
        kernel.ReadFile.return_value = False
        kernel.WaitForSingleObject.return_value = 258
        with patch("ups_monitor.hid_windows.ctypes.get_last_error", return_value=997):
            with self.assertRaises(TimeoutError):
                _read_report(kernel, 10, 20, 0)
        kernel.CancelIoEx.assert_called_once()
        self.assertIs(kernel.GetOverlappedResult.call_args.args[-1], True)


if __name__ == "__main__":
    unittest.main()
