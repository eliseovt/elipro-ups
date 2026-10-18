"""Production worker ownership, live preflight and S4 guards; all APIs fake."""
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from ups_monitor import hid_windows as hid
from ups_monitor.ups_power import OperationJournal, UpsPowerBlocked
from tests.test_hid_cancel_test import fake_apis, QS_SAMPLE
from tests.test_ups_power import DEVICE, unreliable_profile


class ProductionControlGuardTests(unittest.TestCase):
    def setUp(self):
        test_root = Path(__file__).resolve().parents[1] / ".test-data"
        test_root.mkdir(exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=test_root)
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name).resolve()
        self.path = self.data_dir / "ups-power-operation.json"
        self.operation_id = str(uuid.uuid4())
        self.profile = unreliable_profile()
        self.current = datetime.now(timezone.utc)
        self.journal = OperationJournal(self.path)
        self.operation = {"schema_version": 1, "owner": "elipro-ups-power", "state": "cancelling",
                          "operation_id": self.operation_id, "owner_pid": os.getpid(),
                          "profile_id": self.profile["profile_id"], "device_fingerprint": self.profile["device_fingerprint"],
                          "action": "shutdown", "delay_seconds": 180, "manual_restart": True,
                          "created_at": (self.current - timedelta(seconds=10)).isoformat(),
                          "submitted_at": (self.current - timedelta(seconds=5)).isoformat(),
                          "submission_complete": True, "cancel_attempted": True,
                          "started_monotonic": 100, "reserve_seconds": 600}
        self.journal.write(self.operation)
        hid._cooldown_until.clear()
        self.authorization = patch.object(hid, "_require_control_service_authorization", return_value=None).start()
        patch("ups_monitor.ups_test_authorization._registered_data_dir", return_value=self.data_dir).start()
        self.status = patch("ups_monitor.windows_service.manage", return_value={
            "installed": True, "state": "running", "pid": os.getpid()}).start()
        patch("ups_monitor.ups_power.validated_control_profile", return_value=self.profile).start()
        self.continuity = patch.object(hid, "_require_operation_not_interrupted", return_value=True).start()
        patch.object(hid.time, "monotonic", return_value=110).start()
        self.addCleanup(patch.stopall)

    def arguments(self, selector="cancel"):
        values = [DEVICE["path"], self.profile["profile_id"], selector]
        if selector == "shutdown":
            values.append("180")
        return [*values, str(self.path), self.operation_id]

    def start_operation(self):
        self.operation.update(state="arming", submission_complete=False, cancel_attempted=False)
        self.journal.write(self.operation)

    def test_legacy_roles_and_arbitrary_context_shapes_are_denied_before_usb(self):
        with patch.object(hid, "_apis") as apis:
            for arguments in ([DEVICE["path"], self.profile["profile_id"], "cancel"],
                              [DEVICE["path"], self.profile["profile_id"], "shutdown", "180"],
                              self.arguments("load.on"), self.arguments() + ["extra"]):
                with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                    hid.control_worker_main(arguments)
        apis.assert_not_called()

    def test_fresh_qs_and_durable_single_claim_precede_cancel_inside_device_mutex(self):
        apis = fake_apis()
        stdout = Mock(buffer=io.BytesIO())

        def query(path):
            self.assertEqual(path, DEVICE["path"])
            apis[2].WaitForSingleObject.assert_called_once_with(30, 500)
            apis[2].ReleaseMutex.assert_not_called()
            apis[1].HidD_SetOutputReport.assert_not_called()
            self.assertNotIn("worker_claimed_for", self.journal.load())
            return QS_SAMPLE

        def output(handle, report, length):
            self.assertEqual(self.journal.load()["worker_claimed_for"], "cancel")
            self.assertEqual(bytes(report), b"\x00C\r\x00\x00\x00\x00\x00\x00")
            self.assertEqual(length, 9)
            return True

        apis[1].HidD_SetOutputReport.side_effect = output
        with patch.object(hid, "_apis", return_value=apis), patch.object(hid, "_query_status_once", side_effect=query) as read, \
                patch.object(hid, "_run_bounded_worker") as nested, patch("sys.stdout", stdout):
            self.assertEqual(hid.control_worker_main(self.arguments()), 0)
        read.assert_called_once_with(DEVICE["path"])
        nested.assert_not_called()
        apis[1].HidD_SetOutputReport.assert_called_once()
        apis[2].ReleaseMutex.assert_called_once_with(30)
        self.assertEqual(json.loads(stdout.buffer.getvalue())["confirmation"], "unconfirmed")
        with patch.object(hid, "_apis") as apis, patch("sys.stderr", io.StringIO()):
            self.assertEqual(hid.control_worker_main(self.arguments()), 1)
        apis.assert_not_called()

    def test_interruption_during_fresh_qs_prevents_claim_and_every_control_report(self):
        apis = fake_apis()
        signalled = [False]

        def guard(operation_id):
            self.assertEqual(operation_id, self.operation_id)
            if signalled[0]:
                raise UpsPowerBlocked("S4 interruption")
            return True

        def query(path):
            signalled[0] = True
            return QS_SAMPLE

        self.continuity.side_effect = guard
        with patch.object(hid, "_apis", return_value=apis), patch.object(hid, "_query_status_once", side_effect=query), \
                patch("sys.stderr", io.StringIO()):
            self.assertEqual(hid.control_worker_main(self.arguments()), 1)
        apis[1].HidD_SetOutputReport.assert_not_called()
        self.assertNotIn("worker_claimed_for", self.journal.load())
        apis[2].ReleaseMutex.assert_called_once()

    def test_interruption_between_shutdown_blocks_never_sends_cr_or_retries(self):
        self.start_operation()
        apis = fake_apis()
        signalled = [False]

        def guard(operation_id):
            if signalled[0]:
                raise UpsPowerBlocked("S4 interruption")
            return True

        def output(handle, report, length):
            self.assertEqual(bytes(report), b"\x00S03R0000")
            self.assertEqual(self.journal.load()["worker_claimed_for"], "shutdown")
            signalled[0] = True
            return True

        self.continuity.side_effect = guard
        apis[1].HidD_SetOutputReport.side_effect = output
        battery_qs = QS_SAMPLE.replace(b"\x3C\x20\x09\x20", b"\x3C\x20\x89\x20")
        with patch.object(hid, "_apis", return_value=apis), patch.object(hid, "_query_status_once", return_value=battery_qs), \
                patch("sys.stderr", io.StringIO()):
            self.assertEqual(hid.control_worker_main(self.arguments("shutdown")), 1)
        apis[1].HidD_SetOutputReport.assert_called_once()
        self.assertEqual(self.journal.load()["worker_claimed_for"], "shutdown")
        with patch.object(hid, "_apis") as apis, patch("sys.stderr", io.StringIO()):
            self.assertEqual(hid.control_worker_main(self.arguments("shutdown")), 1)
        apis.assert_not_called()

    def test_missing_owner_event_after_restart_denies_before_hid_or_query(self):
        self.continuity.side_effect = UpsPowerBlocked("owner process/event no longer exists")
        with patch.object(hid, "_apis") as apis, patch.object(hid, "_query_status_once") as query, \
                patch("sys.stderr", io.StringIO()):
            self.assertEqual(hid.control_worker_main(self.arguments()), 1)
        apis.assert_not_called()
        query.assert_not_called()

    def test_operation_binding_rejects_foreign_path_pid_state_id_fingerprint_and_claim(self):
        function = hid._production_control_operation
        self.assertEqual(function(DEVICE["path"], self.profile, str(self.path), self.operation_id, "cancel"), self.operation)
        for supplied in (str(self.path.parent / "other.json"), str(self.path.parent.parent / self.path.name), self.path.name):
            with self.subTest(supplied=supplied), self.assertRaises(UpsPowerBlocked):
                function(DEVICE["path"], self.profile, supplied, self.operation_id, "cancel")
        for changes in ({"state": "submitted"}, {"operation_id": str(uuid.uuid4())}, {"owner_pid": -1},
                        {"device_fingerprint": "0" * 64}, {"interrupted": True}, {"worker_claimed_for": "cancel"}):
            self.journal.write({**self.operation, **changes})
            with self.subTest(changes=changes), self.assertRaises((ValueError, UpsPowerBlocked)):
                function(DEVICE["path"], self.profile, str(self.path), self.operation_id, "cancel")
        self.journal.write(self.operation)
        for status in ({"installed": True, "state": "running", "pid": os.getpid() + 1},
                       {"installed": True, "state": "stop_pending", "pid": os.getpid()},
                       {"installed": False, "state": "absent", "pid": 0}):
            self.status.return_value = status
            with self.subTest(status=status), self.assertRaises(UpsPowerBlocked):
                function(DEVICE["path"], self.profile, str(self.path), self.operation_id, "cancel")

    def test_partial_submission_never_reaches_cancel_output_even_if_profile_bit_is_unreliable(self):
        self.operation["submission_complete"] = False
        self.journal.write(self.operation)
        apis = fake_apis()
        with patch.object(hid, "_apis", return_value=apis), patch.object(hid, "_query_status_once", return_value=QS_SAMPLE), \
                patch("sys.stderr", io.StringIO()):
            self.assertEqual(hid.control_worker_main(self.arguments()), 1)
        apis[1].HidD_SetOutputReport.assert_not_called()
        self.assertNotIn("worker_claimed_for", self.journal.load())

    def sequence_operation(self):
        self.profile["action_validation"] = {"hibernate": {
            "completion_evidence": {"trial_count": 2, "observed_range_seconds": [10, 15]},
            "reserve_mode": "observed_sequence", "sequence_validation": {
                "max_seconds_since_mains": 15, "max_load_percent": 11,
                "min_mains_battery_voltage": 27.41, "min_battery_voltage": 25.41,
                "max_countdown_seconds": 10, "source_operation_id": str(uuid.uuid4()),
                "approximate_cut_delay_seconds": 180, "measured_active_battery_seconds": 12.656,
                "measured_since_mains_seconds": 15.016}}}
        baseline = {"timestamp": (self.current - timedelta(seconds=14.5)).isoformat(),
            "on_battery": False, "low_battery": False, "fault": False, "output_voltage": 220,
            "battery_voltage": 27.41, "load_percent": 6, "raw_status": "Line mode",
            "raw_flags": {"status_hex": "09"}}
        self.operation.update(state="arming", action="hibernate", submission_complete=False,
            cancel_attempted=False, started_monotonic=109, reserve_seconds=None, reserve_mode="observed_sequence",
            sequence_evidence={"profile_id": self.profile["profile_id"],
                "device_fingerprint": self.profile["device_fingerprint"],
                "last_mains_monotonic": 95.5, "last_mains_sample": baseline,
                "battery_started_monotonic": 97, "last_sample_monotonic": 110,
                "on_battery_seconds": 0, "countdown_seconds": 10,
                "max_load_percent": 8, "min_battery_voltage": 25.41})
        self.journal.write(self.operation)
        return QS_SAMPLE.replace(b"\x3C\x20\x09\x20", b"\x3C\x20\x89\x20")

    def test_sequence_deadline_expiring_during_qs_prevents_claim_and_any_output(self):
        battery_qs = self.sequence_operation()
        apis, tick = fake_apis(), [110.0]

        def query(path):
            tick[0] = 110.6  # 15.1 seconds from the last mains observation.
            return battery_qs

        with patch.object(hid.time, "monotonic", side_effect=lambda: tick[0]), \
             patch.object(hid, "_apis", return_value=apis), patch.object(hid, "_query_status_once", side_effect=query), \
             patch("sys.stderr", io.StringIO()):
            self.assertEqual(hid.control_worker_main(self.arguments("shutdown")), 1)
        apis[1].HidD_SetOutputReport.assert_not_called()
        self.assertNotIn("worker_claimed_for", self.journal.load())

    def test_sequence_deadline_expiring_during_claim_flush_prevents_first_report(self):
        battery_qs = self.sequence_operation()
        apis, tick = fake_apis(), [110.0]
        real_write = OperationJournal.write

        def write(journal, value):
            real_write(journal, value)
            if value.get("worker_claimed_for") == "shutdown":
                tick[0] = 110.6

        with patch.object(hid.time, "monotonic", side_effect=lambda: tick[0]), \
             patch.object(hid, "_apis", return_value=apis), patch.object(hid, "_query_status_once", return_value=battery_qs), \
             patch.object(OperationJournal, "write", autospec=True, side_effect=write), patch("sys.stderr", io.StringIO()):
            self.assertEqual(hid.control_worker_main(self.arguments("shutdown")), 1)
        apis[1].HidD_SetOutputReport.assert_not_called()
        self.assertEqual(self.journal.load()["worker_claimed_for"], "shutdown")

    def test_sequence_deadline_between_start_fragments_prevents_cr_without_retry(self):
        battery_qs = self.sequence_operation()
        apis, tick = fake_apis(), [110.0]

        def output(handle, report, length):
            self.assertEqual(bytes(report), b"\x00S03R0000")
            tick[0] = 110.6
            return True

        apis[1].HidD_SetOutputReport.side_effect = output
        with patch.object(hid.time, "monotonic", side_effect=lambda: tick[0]), \
             patch.object(hid, "_apis", return_value=apis), patch.object(hid, "_query_status_once", return_value=battery_qs) as query, \
             patch("sys.stderr", io.StringIO()):
            self.assertEqual(hid.control_worker_main(self.arguments("shutdown")), 1)
        apis[1].HidD_SetOutputReport.assert_called_once()
        query.assert_called_once_with(DEVICE["path"])
        self.assertEqual(self.journal.load()["worker_claimed_for"], "shutdown")

    def configurable_operation(self, selected):
        raw = self.sequence_operation()
        self.profile["operation_configuration"] = {"source": "user_configurable", "min_delay_seconds": 60,
            "max_delay_seconds": 540, "max_countdown_seconds": 120, "poll_margin_seconds": 10,
            "requires_on_battery_seconds": 0}
        self.operation["delay_seconds"] = selected
        self.journal.write(self.operation)
        return raw

    def test_selected_1_and_9_minute_commands_are_exactly_owned_not_replaced_with_3(self):
        for selected, first_report in ((60, b"\x00S01R0000"), (540, b"\x00S09R0000")):
            raw = self.configurable_operation(selected)
            apis = fake_apis()
            arguments = self.arguments("shutdown")
            arguments[3] = str(selected)
            with self.subTest(selected=selected), patch.object(hid, "_apis", return_value=apis), \
                 patch.object(hid, "_query_status_once", return_value=raw), patch("sys.stdout", Mock(buffer=io.BytesIO())):
                self.assertEqual(hid.control_worker_main(arguments), 0)
            reports = [bytes(call.args[1]) for call in apis[1].HidD_SetOutputReport.call_args_list]
            self.assertEqual(reports, [first_report, b"\x00\r\x00\x00\x00\x00\x00\x00\x00"])

    def test_ten_minutes_and_a_different_owned_delay_are_rejected_before_usb(self):
        self.configurable_operation(60)
        with patch.object(hid, "_apis") as apis, patch("sys.stderr", io.StringIO()):
            for selected in (600, 540, 180):
                arguments = self.arguments("shutdown")
                arguments[3] = str(selected)
                with self.subTest(selected=selected):
                    self.assertEqual(hid.control_worker_main(arguments), 1)
        apis.assert_not_called()


if __name__ == "__main__":
    unittest.main()
