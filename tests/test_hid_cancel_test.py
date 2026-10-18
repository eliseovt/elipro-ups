"""The real-test HID role is exercised only with fake gates and fake USB APIs."""
import ctypes
import io
import json
import unittest
from unittest.mock import Mock, patch

from ups_monitor.hid_windows import (
    CancellationTestTransport, HIDD_ATTRIBUTES, HIDP_CAPS, StatusTransport,
    _active_workers, _cooldown_until, _write_control_reports_once,
    cancel_test_worker_main,
)
from ups_monitor.ups_power import (
    NT2402PRO_CANDIDATE_PROFILE, UpsPowerBlocked, VALIDATED_PROFILES, device_fingerprint,
)


PATH = r"\\?\hid#vid_0665&pid_5161#manual-test-unit"
AUTH = "test-authorization.json"
JOURNAL = "test-operation.json"
OPERATION = "2f5e00bf-903d-414a-82a4-57046fa92fb9"
QS_SAMPLE = bytes.fromhex(
    "23 67 01 20 6C 20 67 01 20 6C 20 08 20 50 0F 20 "
    "12 C0 00 20 E9 20 3C 20 09 20 A2 0D")


def candidate_profile():
    device = {**NT2402PRO_CANDIDATE_PROFILE, "path": PATH}
    return {**NT2402PRO_CANDIDATE_PROFILE, "device_fingerprint": device_fingerprint(device)}


def fake_apis(*, revision=3, report_bytes=9):
    hid, kernel = Mock(), Mock()
    kernel.CreateFileW.return_value = 10
    kernel.CreateMutexW.return_value = 30
    kernel.CreateEventW.return_value = 20
    kernel.WaitForSingleObject.return_value = 0

    def attributes(handle, pointer):
        value = ctypes.cast(pointer, ctypes.POINTER(HIDD_ATTRIBUTES)).contents
        value.VendorID, value.ProductID, value.VersionNumber = 0x0665, 0x5161, revision
        return True

    def caps(preparsed, pointer):
        value = ctypes.cast(pointer, ctypes.POINTER(HIDP_CAPS)).contents
        value.InputReportByteLength = value.OutputReportByteLength = report_bytes
        value.FeatureReportByteLength = 0
        return 0x00110000

    hid.HidD_GetAttributes.side_effect = attributes
    hid.HidD_GetPreparsedData.return_value = True
    hid.HidP_GetCaps.side_effect = caps
    hid.HidD_SetOutputReport.return_value = True
    hid.HidD_FlushQueue.return_value = True
    return Mock(), hid, kernel


class CancellationTestHidTests(unittest.TestCase):
    def setUp(self):
        _cooldown_until.clear()
        _active_workers.clear()
        self.authorization = patch("ups_monitor.ups_test_authorization.authorize_test_worker", return_value=True).start()
        self.validation = patch("ups_monitor.ups_cancel_test.validate_test_worker_request", return_value=candidate_profile()).start()
        self.preflight = patch("ups_monitor.ups_cancel_test.validate_cancel_test_preflight", return_value=None, create=True).start()
        self.interruption = patch("ups_monitor.hid_windows._OperationInterruption").start()
        self.continuity = patch("ups_monitor.hid_windows._require_operation_not_interrupted", return_value=True).start()
        self.addCleanup(patch.stopall)

    def transport(self):
        return CancellationTestTransport(PATH, AUTH, JOURNAL, OPERATION)

    def argv(self, selector):
        return [PATH, AUTH, JOURNAL, OPERATION, selector]

    def test_direct_worker_rejects_extra_arguments_and_arbitrary_commands(self):
        with patch("ups_monitor.hid_windows._apis") as apis:
            for args in ([], [PATH], self.argv("start") + ["extra"], self.argv("shutdown"),
                         self.argv("S03R0000\r"), self.argv("QS")):
                with self.subTest(args=args), self.assertRaises(SystemExit):
                    cancel_test_worker_main(args)
            self.authorization.assert_not_called()
            self.validation.assert_not_called()
            apis.assert_not_called()

    def test_no_service_authorization_blocks_parent_and_worker_before_usb(self):
        for rejected in (False, None, 1, {}):
            self.authorization.return_value = rejected
            with self.subTest(authorization=rejected), patch("ups_monitor.hid_windows._apis") as apis, \
                 patch("ups_monitor.hid_windows._run_bounded_worker") as run, patch("sys.stderr"):
                with self.assertRaises(UpsPowerBlocked):
                    self.transport().submit_test()
                self.assertEqual(cancel_test_worker_main(self.argv("start")), 1)
                self.validation.assert_not_called()
                apis.assert_not_called()
                run.assert_not_called()

    def test_authorization_error_does_not_expose_paths_or_open_usb(self):
        self.authorization.side_effect = OSError("sensitive-local-path")
        stderr = io.StringIO()
        with patch("ups_monitor.hid_windows._apis") as apis, patch("sys.stderr", stderr):
            self.assertEqual(cancel_test_worker_main(self.argv("cancel")), 1)
        self.assertNotIn("sensitive-local-path", stderr.getvalue())
        self.validation.assert_not_called()
        apis.assert_not_called()

    def test_invalid_durable_operation_blocks_both_roles_before_usb(self):
        self.validation.side_effect = ValueError("Not an owned pending test operation")
        with patch("ups_monitor.hid_windows._apis") as apis, \
             patch("ups_monitor.hid_windows._run_bounded_worker") as run, patch("sys.stderr"):
            with self.assertRaises(ValueError):
                self.transport().cancel_test()
            self.assertEqual(cancel_test_worker_main(self.argv("cancel")), 1)
            apis.assert_not_called()
            run.assert_not_called()

    def test_foreign_fingerprint_or_candidate_fields_cannot_open_usb(self):
        for profile in (None, {**candidate_profile(), "device_fingerprint": "f" * 64},
                        {**candidate_profile(), "version_number": 4},
                        {**candidate_profile(), "version_number": True},
                        {**candidate_profile(), "profile_id": "unrelated-profile"}):
            self.validation.return_value = profile
            with self.subTest(profile=profile), patch("ups_monitor.hid_windows._apis") as apis, \
                 patch("ups_monitor.hid_windows._run_bounded_worker") as run, patch("sys.stderr"):
                with self.assertRaises(UpsPowerBlocked):
                    self.transport().submit_test()
                self.assertEqual(cancel_test_worker_main(self.argv("start")), 1)
                apis.assert_not_called()
                run.assert_not_called()

    def test_worker_commands_are_fixed_and_never_create_a_validated_profile(self):
        before = dict(VALIDATED_PROFILES)
        for selector, command, reports in (("start", b"S03R0000\r", 2), ("cancel", b"C\r", 1)):
            stdout = Mock(buffer=io.BytesIO())
            expected = {"state": "submitted", "confirmation": "unconfirmed", "reports_written": reports}
            with self.subTest(selector=selector), patch("sys.stdout", stdout), \
                 patch("ups_monitor.hid_windows._write_control_reports_once", return_value=expected) as write:
                self.assertEqual(cancel_test_worker_main(self.argv(selector)), 0)
            self.authorization.assert_called_with(AUTH, JOURNAL)
            self.validation.assert_called_with(AUTH, JOURNAL, OPERATION, selector)
            if selector == "cancel":
                self.assertEqual(write.call_args.args, (PATH, command, candidate_profile()))
                self.assertTrue(callable(write.call_args.kwargs["before_write"]))
            else:
                write.assert_called_once()
                self.assertEqual(write.call_args.args, (PATH, command, candidate_profile()))
            self.assertTrue(callable(write.call_args.kwargs["before_each_report"]))
            self.assertEqual(json.loads(stdout.buffer.getvalue()), expected)
        self.assertEqual(VALIDATED_PROFILES, before)

    def test_final_preflight_runs_after_identity_and_flush_inside_control_mutex(self):
        apis = fake_apis()

        def before_write():
            apis[1].HidD_GetAttributes.assert_called_once()
            apis[1].HidP_GetCaps.assert_called_once()
            apis[1].HidD_FlushQueue.assert_called_once_with(10)
            apis[2].WaitForSingleObject.assert_called_once_with(30, 500)
            apis[2].ReleaseMutex.assert_not_called()
            apis[1].HidD_SetOutputReport.assert_not_called()

        preflight = Mock(side_effect=before_write)
        with patch("ups_monitor.hid_windows._apis", return_value=apis):
            self.assertEqual(_write_control_reports_once(PATH, b"C\r", candidate_profile(),
                                                        before_write=preflight)["reports_written"], 1)
        preflight.assert_called_once_with()
        apis[1].HidD_SetOutputReport.assert_called_once()
        apis[2].ReleaseMutex.assert_called_once_with(30)

    def test_preflight_rejection_or_expired_deadline_never_sends_cancel(self):
        for failure in (UpsPowerBlocked("Salida apagada o avería"), TimeoutError("QS timed out")):
            apis = fake_apis()
            preflight = Mock(side_effect=failure)
            with self.subTest(failure=failure), patch("ups_monitor.hid_windows._apis", return_value=apis):
                with self.assertRaises(type(failure)):
                    _write_control_reports_once(PATH, b"C\r", candidate_profile(), before_write=preflight)
            preflight.assert_called_once_with()
            apis[1].HidD_SetOutputReport.assert_not_called()
            apis[2].ReleaseMutex.assert_called_once()
        apis = fake_apis()
        preflight = Mock()
        with patch("ups_monitor.hid_windows._apis", return_value=apis), \
             patch("ups_monitor.hid_windows.time.monotonic", side_effect=[100, 103]):
            with self.assertRaises(TimeoutError):
                _write_control_reports_once(PATH, b"C\r", candidate_profile(), before_write=preflight)
        preflight.assert_called_once_with()
        apis[1].HidD_SetOutputReport.assert_not_called()

    def test_cancel_worker_uses_one_direct_qs_and_revalidates_its_decoded_sample(self):
        apis = fake_apis()
        stdout = Mock(buffer=io.BytesIO())

        def fresh_query(path):
            self.assertEqual(path, PATH)
            apis[2].WaitForSingleObject.assert_called_once_with(30, 500)
            apis[2].ReleaseMutex.assert_not_called()
            apis[1].HidD_SetOutputReport.assert_not_called()
            return QS_SAMPLE

        with patch("ups_monitor.hid_windows._apis", return_value=apis), \
             patch("ups_monitor.hid_windows._query_status_once", side_effect=fresh_query) as query, \
             patch("ups_monitor.hid_windows._run_bounded_worker") as run, patch("sys.stdout", stdout):
            self.assertEqual(cancel_test_worker_main(self.argv("cancel")), 0)
        query.assert_called_once_with(PATH)
        run.assert_not_called()
        self.preflight.assert_called_once()
        self.assertEqual(self.preflight.call_args.args, (AUTH, JOURNAL, OPERATION))
        decoded = self.preflight.call_args.kwargs["sample"]
        self.assertEqual(decoded["raw_flags"]["status_hex"], "09")
        self.assertGreater(decoded["output_voltage"], 0)
        self.assertIs(decoded["fault"], False)
        self.assertEqual(bytes(apis[1].HidD_SetOutputReport.call_args.args[1]),
                         b"\x00C\r\x00\x00\x00\x00\x00\x00")
        self.assertEqual(json.loads(stdout.buffer.getvalue())["confirmation"], "unconfirmed")

    def test_nested_status_query_keeps_outer_mutex_until_one_cancel_is_written(self):
        apis = fake_apis()
        depth, commands = [0], []

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
            elif payload.startswith(b"\x00C\r"):
                self.assertEqual(depth[0], 1)
                commands.append("C")
            else:
                self.fail("Unexpected HID command in cancellation preflight")
            return True

        apis[2].WaitForSingleObject.side_effect = acquire
        apis[2].ReleaseMutex.side_effect = release
        apis[1].HidD_SetOutputReport.side_effect = write
        chunks = [QS_SAMPLE[index:index + 8].ljust(8, b"\x00") for index in range(0, len(QS_SAMPLE), 8)]
        with patch("ups_monitor.hid_windows._apis", return_value=apis), \
             patch("ups_monitor.hid_windows._read_report", side_effect=chunks), \
             patch("ups_monitor.hid_windows._run_bounded_worker") as run, \
             patch("sys.stdout", Mock(buffer=io.BytesIO())):
            self.assertEqual(cancel_test_worker_main(self.argv("cancel")), 0)
        self.assertEqual(commands, ["QS", "C"])
        self.assertEqual(depth[0], 0)
        self.assertEqual(apis[2].ReleaseMutex.call_count, 2)
        run.assert_not_called()

    def test_off_output_fault_or_failed_qs_preflight_cannot_send_c(self):
        base = QS_SAMPLE[1:-1].split(b" ")
        off = list(base)
        off[2], off[5], off[6], off[9] = b"\x00\x00", b"\x00\x00", b"\x00\x00\x00", b"\x0b"
        fault = list(base)
        fault[9] = b"\x19"
        frames = [b"#" + b" ".join(fields) + b"\r" for fields in (off, fault)]

        def check_preflight(*args, sample):
            if sample["output_voltage"] <= 0 or sample["fault"] is not False:
                raise UpsPowerBlocked("Salida apagada o falla en lectura previa")

        self.preflight.side_effect = check_preflight
        for frame in frames:
            apis = fake_apis()
            with self.subTest(frame=frame), patch("ups_monitor.hid_windows._apis", return_value=apis), \
                 patch("ups_monitor.hid_windows._query_status_once", return_value=frame) as query, patch("sys.stderr"):
                self.assertEqual(cancel_test_worker_main(self.argv("cancel")), 1)
            query.assert_called_once_with(PATH)
            apis[1].HidD_SetOutputReport.assert_not_called()
            apis[2].ReleaseMutex.assert_called_once()
        apis = fake_apis()
        with patch("ups_monitor.hid_windows._apis", return_value=apis), \
             patch("ups_monitor.hid_windows._query_status_once", side_effect=TimeoutError("QS failed")) as query, \
             patch("sys.stderr"):
            self.assertEqual(cancel_test_worker_main(self.argv("cancel")), 1)
        query.assert_called_once_with(PATH)
        apis[1].HidD_SetOutputReport.assert_not_called()

    def test_candidate_write_fragments_under_one_mutex_without_reading_an_ack(self):
        apis = fake_apis()
        with patch("ups_monitor.hid_windows._apis", return_value=apis), \
             patch("ups_monitor.hid_windows._read_report") as read:
            result = _write_control_reports_once(PATH, b"S03R0000\r", candidate_profile())
        self.assertEqual(result, {"state": "submitted", "confirmation": "unconfirmed", "reports_written": 2})
        self.assertEqual([bytes(call.args[1]) for call in apis[1].HidD_SetOutputReport.call_args_list],
                         [b"\x00S03R0000", b"\x00\r\x00\x00\x00\x00\x00\x00\x00"])
        apis[2].CreateMutexW.assert_called_once()
        apis[2].ReleaseMutex.assert_called_once_with(30)
        self.assertEqual(apis[2].CloseHandle.call_count, 2)
        apis[2].ReadFile.assert_not_called()
        read.assert_not_called()

    def test_partial_write_and_expired_deadline_never_retry(self):
        apis = fake_apis()
        apis[1].HidD_SetOutputReport.side_effect = [True, False]
        with patch("ups_monitor.hid_windows._apis", return_value=apis):
            with self.assertRaisesRegex(OSError, "no se repetirá"):
                _write_control_reports_once(PATH, b"S03R0000\r", candidate_profile())
        self.assertEqual(apis[1].HidD_SetOutputReport.call_count, 2)
        apis[2].ReleaseMutex.assert_called_once()
        apis = fake_apis()
        with patch("ups_monitor.hid_windows._apis", return_value=apis), \
             patch("ups_monitor.hid_windows.time.monotonic", side_effect=[100, 100, 103]):
            with self.assertRaises(TimeoutError):
                _write_control_reports_once(PATH, b"S03R0000\r", candidate_profile())
        apis[1].HidD_SetOutputReport.assert_called_once()
        apis[2].ReleaseMutex.assert_called_once()

    def test_changed_hardware_or_arbitrary_command_does_not_write(self):
        for apis in (fake_apis(revision=4), fake_apis(report_bytes=8)):
            with patch("ups_monitor.hid_windows._apis", return_value=apis):
                with self.assertRaises(OSError):
                    _write_control_reports_once(PATH, b"C\r", candidate_profile())
            apis[1].HidD_SetOutputReport.assert_not_called()
        with patch("ups_monitor.hid_windows._apis") as apis:
            for command in (b"QS\r", b"S03R0001\r", b"C\rQS\r", b"S00R0000\r"):
                with self.assertRaises(ValueError):
                    _write_control_reports_once(PATH, command, candidate_profile())
            apis.assert_not_called()

    def test_parent_has_fixed_isolated_role_and_rechecks_every_request(self):
        transport = self.transport()
        with patch("ups_monitor.hid_windows.child_command", side_effect=lambda role, *args: [role, *args]), \
             patch("ups_monitor.hid_windows._run_bounded_worker") as run:
            for selector, reports, operation in (("start", 2, transport.submit_test), ("cancel", 1, transport.cancel_test)):
                result = {"state": "submitted", "confirmation": "unconfirmed", "reports_written": reports}
                run.return_value = Mock(returncode=0, stdout=json.dumps(result).encode())
                self.assertEqual(operation(), result)
                self.assertEqual(run.call_args.args, (["cancel-test-worker", *self.argv(selector)], PATH.casefold()))
            self.assertEqual(self.authorization.call_count, 2)
            self.assertEqual(self.validation.call_count, 2)
            self.authorization.return_value = False
            with self.assertRaises(UpsPowerBlocked):
                transport.cancel_test()
            self.assertEqual(run.call_count, 2)

    def test_failed_worker_shares_status_cooldown_and_cannot_be_retried(self):
        with patch("ups_monitor.hid_windows.child_command", side_effect=lambda role, *args: [role, *args]), \
             patch("ups_monitor.hid_windows.time.monotonic", return_value=100), \
             patch("ups_monitor.hid_windows._run_bounded_worker", side_effect=TimeoutError("uncertain worker")) as run:
            with self.assertRaises(TimeoutError):
                self.transport().submit_test()
            for operation in (self.transport().submit_test, self.transport().cancel_test,
                              StatusTransport(PATH).query_status):
                with self.assertRaises(TimeoutError):
                    operation()
            run.assert_called_once()
            self.assertEqual(_cooldown_until[PATH.casefold()], 105)

    def test_worker_result_is_only_a_complete_unconfirmed_submission(self):
        invalid = ({"state": "armed", "confirmation": "ACK", "reports_written": 2},
                   {"state": "submitted", "confirmation": "unconfirmed", "reports_written": 1},
                   {"state": "submitted", "confirmation": "unconfirmed", "reports_written": True},
                   ["ACK"])
        with patch("ups_monitor.hid_windows.child_command", side_effect=lambda role, *args: [role, *args]), \
             patch("ups_monitor.hid_windows._run_bounded_worker") as run:
            for result in invalid:
                _cooldown_until.clear()
                run.return_value = Mock(returncode=0, stdout=json.dumps(result).encode())
                with self.subTest(result=result), self.assertRaises(OSError):
                    self.transport().submit_test()


if __name__ == "__main__":
    unittest.main()
