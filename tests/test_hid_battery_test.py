"""Battery-test HID workers: fake authorization, fake events, fake USB only."""
import ctypes
import io
import json
import unittest
from unittest.mock import Mock, patch

from ups_monitor import hid_windows as hid
from ups_monitor.ups_power import NT2402PRO_CANDIDATE_PROFILE, UpsPowerBlocked, device_fingerprint


PATH = r"\\?\hid#vid_0665&pid_5161#battery-test-fixture"
AUTH = "ups-battery-test-authorization.json"
JOURNAL = "ups-battery-test-operation.json"
OPERATION = "d2fd6166-4a6a-46cb-8071-af22947438a2"
QS_BATTERY = bytes.fromhex(
    "23 67 01 20 6C 20 67 01 20 6C 20 08 20 50 0F 20 "
    "12 C0 00 20 E9 20 3C 20 89 20 A2 0D")


def profile():
    return {**NT2402PRO_CANDIDATE_PROFILE,
        "device_fingerprint": device_fingerprint({**NT2402PRO_CANDIDATE_PROFILE, "path": PATH})}


def fake_apis():
    usb, kernel = Mock(), Mock()
    kernel.CreateFileW.return_value = 10
    kernel.CreateEventW.return_value = 20
    kernel.CreateMutexW.return_value = 30
    kernel.WaitForSingleObject.return_value = 0

    def attributes(handle, pointer):
        value = ctypes.cast(pointer, ctypes.POINTER(hid.HIDD_ATTRIBUTES)).contents
        value.VendorID, value.ProductID, value.VersionNumber = 0x0665, 0x5161, 3
        return True

    def caps(preparsed, pointer):
        value = ctypes.cast(pointer, ctypes.POINTER(hid.HIDP_CAPS)).contents
        value.InputReportByteLength, value.OutputReportByteLength, value.FeatureReportByteLength = 9, 9, 0
        return 0x00110000

    usb.HidD_GetAttributes.side_effect = attributes
    usb.HidD_GetPreparsedData.return_value = True
    usb.HidP_GetCaps.side_effect = caps
    usb.HidD_FlushQueue.return_value = True
    usb.HidD_SetOutputReport.return_value = True
    return Mock(), usb, kernel


class BatteryHidTests(unittest.TestCase):
    def setUp(self):
        hid._cooldown_until.clear()
        hid._active_workers.clear()
        self.owner = Mock()
        self.event_factory = patch.object(hid, "_OperationInterruption", return_value=self.owner).start()
        self.continuity = patch.object(hid, "_require_operation_not_interrupted", return_value=True).start()
        self.authorization = patch("ups_monitor.ups_test_authorization.authorize_battery_test_worker", return_value=True).start()
        self.validation = patch("ups_monitor.ups_battery_test.validate_battery_test_worker_request", return_value=profile()).start()
        self.preflight = patch("ups_monitor.ups_battery_test.validate_battery_test_preflight", return_value=True).start()
        self.addCleanup(patch.stopall)

    def argv(self, selector):
        return [PATH, AUTH, JOURNAL, OPERATION, selector]

    def transport(self):
        return hid.BatteryTestTransport(PATH, AUTH, JOURNAL, OPERATION)

    def test_worker_argument_allowlist_rejects_commands_and_extras_before_gates(self):
        with patch.object(hid, "_apis") as apis:
            for values in ([], [PATH], self.argv("start") + ["extra"], self.argv("S03R0000\r"),
                           self.argv("shutdown"), self.argv("QS"), self.argv("C\r")):
                with self.subTest(values=values), self.assertRaises(SystemExit):
                    hid.battery_test_worker_main(values)
            self.authorization.assert_not_called()
            self.validation.assert_not_called()
            self.continuity.assert_not_called()
            apis.assert_not_called()

    def test_missing_authorization_or_owned_journal_never_opens_usb(self):
        self.authorization.return_value = False
        with patch.object(hid, "_apis") as apis, patch.object(hid, "_run_bounded_worker") as run, patch("sys.stderr"):
            with self.assertRaises(UpsPowerBlocked):
                self.transport().submit_test()
            self.assertEqual(hid.battery_test_worker_main(self.argv("start")), 1)
            self.validation.assert_not_called()
            apis.assert_not_called()
            run.assert_not_called()
        self.authorization.return_value = True
        self.validation.side_effect = UpsPowerBlocked("foreign operation")
        with patch.object(hid, "_apis") as apis, patch("sys.stderr"):
            self.assertEqual(hid.battery_test_worker_main(self.argv("cancel")), 1)
            apis.assert_not_called()

    def test_battery_role_never_uses_cancellation_permission(self):
        with patch("ups_monitor.ups_test_authorization.authorize_test_worker", side_effect=AssertionError("wrong namespace")) as wrong, \
             patch.object(hid, "_run_bounded_worker", return_value=Mock(returncode=0,
                 stdout=b'{"state":"submitted","confirmation":"unconfirmed","reports_written":2}')):
            self.transport().submit_test()
        self.authorization.assert_called_once_with(AUTH, JOURNAL)
        wrong.assert_not_called()

    def test_worker_fixed_reports_and_direct_qs_preflight_for_both_selectors(self):
        for selector, reports in (("start", [b"\x00S03R0000", b"\x00\r\x00\x00\x00\x00\x00\x00\x00"]),
                                  ("cancel", [b"\x00C\r\x00\x00\x00\x00\x00\x00"])):
            apis = fake_apis()
            stdout = Mock(buffer=io.BytesIO())

            def query(path):
                self.assertEqual(path, PATH)
                apis[2].WaitForSingleObject.assert_called_once_with(30, 500)
                apis[2].ReleaseMutex.assert_not_called()
                apis[1].HidD_SetOutputReport.assert_not_called()
                return QS_BATTERY

            with self.subTest(selector=selector), patch.object(hid, "_apis", return_value=apis), \
                 patch.object(hid, "_query_status_once", side_effect=query) as query_mock, \
                 patch.object(hid, "_run_bounded_worker") as run, patch("sys.stdout", stdout):
                self.assertEqual(hid.battery_test_worker_main(self.argv(selector)), 0)
            query_mock.assert_called_once_with(PATH)
            run.assert_not_called()
            self.assertEqual([bytes(call.args[1]) for call in apis[1].HidD_SetOutputReport.call_args_list], reports)
            args = self.preflight.call_args
            self.assertEqual(args.args, (AUTH, JOURNAL, OPERATION, selector))
            self.assertTrue(args.kwargs["sample"]["on_battery"])
            result = json.loads(stdout.buffer.getvalue())
            self.assertEqual(result, {"state": "submitted", "confirmation": "unconfirmed", "reports_written": len(reports)})
            apis[2].ReleaseMutex.assert_called_once()

    def test_nested_qs_does_not_release_outer_exclusion_before_control(self):
        apis, depth, commands = fake_apis(), [0], []

        def acquire(mutex, timeout):
            depth[0] += 1
            return 0

        def release(mutex):
            depth[0] -= 1
            return True

        def write(handle, report, size):
            payload = bytes(report)
            if payload.startswith(b"\x00QS\r"):
                self.assertEqual(depth[0], 2)
                commands.append("QS")
            else:
                self.assertEqual(depth[0], 1)
                commands.append("S" if payload.startswith(b"\x00S") else "CR")
            return True

        apis[2].WaitForSingleObject.side_effect = acquire
        apis[2].ReleaseMutex.side_effect = release
        apis[1].HidD_SetOutputReport.side_effect = write
        chunks = [QS_BATTERY[index:index + 8].ljust(8, b"\x00") for index in range(0, len(QS_BATTERY), 8)]
        with patch.object(hid, "_apis", return_value=apis), patch.object(hid, "_read_report", side_effect=chunks), \
             patch("sys.stdout", Mock(buffer=io.BytesIO())):
            self.assertEqual(hid.battery_test_worker_main(self.argv("start")), 0)
        self.assertEqual(commands, ["QS", "S", "CR"])
        self.assertEqual(depth[0], 0)

    def test_suspend_during_qs_blocks_every_following_control_report(self):
        for selector in ("start", "cancel"):
            apis, interrupted = fake_apis(), [False]

            def check(operation_id):
                if interrupted[0]:
                    raise UpsPowerBlocked("suspended")
                return True

            def query(path):
                interrupted[0] = True
                return QS_BATTERY

            self.continuity.side_effect = check
            with self.subTest(selector=selector), patch.object(hid, "_apis", return_value=apis), \
                 patch.object(hid, "_query_status_once", side_effect=query) as query_mock, patch("sys.stderr"):
                self.assertEqual(hid.battery_test_worker_main(self.argv(selector)), 1)
            query_mock.assert_called_once()
            apis[1].HidD_SetOutputReport.assert_not_called()
            apis[2].ReleaseMutex.assert_called_once()

    def test_suspend_between_start_blocks_prevents_terminating_cr(self):
        apis, interrupted = fake_apis(), [False]

        def check(operation_id):
            if interrupted[0]:
                raise UpsPowerBlocked("suspended between reports")
            return True

        def write(handle, report, size):
            self.assertEqual(bytes(report), b"\x00S03R0000")
            interrupted[0] = True
            return True

        self.continuity.side_effect = check
        apis[1].HidD_SetOutputReport.side_effect = write
        with patch.object(hid, "_apis", return_value=apis), patch.object(hid, "_query_status_once", return_value=QS_BATTERY), \
             patch("sys.stderr"):
            self.assertEqual(hid.battery_test_worker_main(self.argv("start")), 1)
        apis[1].HidD_SetOutputReport.assert_called_once()
        apis[2].ReleaseMutex.assert_called_once()

    def test_partial_write_never_retries_or_invents_ack(self):
        apis = fake_apis()
        apis[1].HidD_SetOutputReport.side_effect = [True, False]
        with patch.object(hid, "_apis", return_value=apis), patch.object(hid, "_query_status_once", return_value=QS_BATTERY) as query, \
             patch.object(hid, "_run_bounded_worker") as run, patch("sys.stderr"):
            self.assertEqual(hid.battery_test_worker_main(self.argv("start")), 1)
        self.assertEqual(apis[1].HidD_SetOutputReport.call_count, 2)
        query.assert_called_once()
        run.assert_not_called()
        apis[2].ReleaseMutex.assert_called_once()

    def test_preflight_rejection_keeps_cancel_and_start_unsent(self):
        self.preflight.side_effect = UpsPowerBlocked("battery low, off output, or expired window")
        for selector in ("start", "cancel"):
            apis = fake_apis()
            with self.subTest(selector=selector), patch.object(hid, "_apis", return_value=apis), \
                 patch.object(hid, "_query_status_once", return_value=QS_BATTERY), patch("sys.stderr"):
                self.assertEqual(hid.battery_test_worker_main(self.argv(selector)), 1)
            apis[1].HidD_SetOutputReport.assert_not_called()

    def test_parent_keeps_one_event_across_both_requests_and_propagates_suspend(self):
        transport = self.transport()
        with patch.object(hid, "_run_bounded_worker") as run:
            for selector, reports, operation in (("start", 2, transport.submit_test),
                    ("cancel", 1, transport.cancel_after_windows_failure)):
                run.return_value = Mock(returncode=0, stdout=json.dumps({"state": "submitted",
                    "confirmation": "unconfirmed", "reports_written": reports}).encode())
                operation()
                self.assertEqual(run.call_args.args[0][-6:], ["--battery-test-worker", *self.argv(selector)])
                self.assertEqual(run.call_args.args[1], PATH.casefold())
            self.event_factory.assert_called_once_with(OPERATION)
            self.assertIs(transport._interruption_event, self.owner)
            transport.signal_interruption()
            self.owner.signal_interruption.assert_called_once_with()
            with self.assertRaises(ValueError):
                transport.cancel_test()
            self.assertEqual(run.call_count, 2)

    def test_parent_checks_interruption_before_spawning_worker(self):
        self.continuity.side_effect = UpsPowerBlocked("suspended")
        with patch.object(hid, "_run_bounded_worker") as run:
            with self.assertRaises(UpsPowerBlocked):
                self.transport().submit_test()
            run.assert_not_called()


class NativeOperationEventTests(unittest.TestCase):
    def test_owner_event_is_manual_reset_and_signaling_does_not_touch_usb(self):
        api = Mock()
        api.CreateEventW.return_value = 99
        with patch.object(hid, "_event_api", return_value=api), \
             patch.object(ctypes, "set_last_error", create=True), patch.object(ctypes, "get_last_error", return_value=0, create=True), \
             patch.object(hid, "_apis") as usb:
            owner = hid._OperationInterruption(OPERATION)
            api.CreateEventW.assert_called_once_with(None, True, False,
                "Global\\EliproUPS_Control_d2fd61664a6a46cb8071af22947438a2")
            owner.signal_interruption()
            api.SetEvent.assert_called_once_with(99)
            owner.close()
            owner.close()
            api.CloseHandle.assert_called_once_with(99)
            usb.assert_not_called()

    def test_worker_opens_existing_event_only_and_requires_unsignaled_status(self):
        for wait in (258, 0, 128, 0xFFFFFFFF):
            api = Mock()
            api.OpenEventW.return_value, api.WaitForSingleObject.return_value = 88, wait
            with self.subTest(wait=wait), patch.object(hid, "_event_api", return_value=api):
                if wait == 258:
                    self.assertIs(hid._require_operation_not_interrupted(OPERATION), True)
                else:
                    with self.assertRaises(UpsPowerBlocked):
                        hid._require_operation_not_interrupted(OPERATION)
            api.OpenEventW.assert_called_once_with(0x00100000, False,
                "Global\\EliproUPS_Control_d2fd61664a6a46cb8071af22947438a2")
            api.CreateEventW.assert_not_called()
            api.SetEvent.assert_not_called()
            api.CloseHandle.assert_called_once_with(88)

    def test_missing_event_and_reused_operation_id_fail_closed(self):
        api = Mock()
        api.OpenEventW.return_value = 0
        with patch.object(hid, "_event_api", return_value=api):
            with self.assertRaises(UpsPowerBlocked):
                hid._require_operation_not_interrupted(OPERATION)
        api.WaitForSingleObject.assert_not_called()
        api.CreateEventW.return_value = 77
        with patch.object(hid, "_event_api", return_value=api), \
             patch.object(ctypes, "set_last_error", create=True), \
             patch.object(ctypes, "get_last_error", return_value=183, create=True):
            with self.assertRaises(OSError):
                hid._OperationInterruption(OPERATION)
        api.CloseHandle.assert_called_once_with(77)
        api.SetEvent.assert_not_called()


if __name__ == "__main__":
    unittest.main()
