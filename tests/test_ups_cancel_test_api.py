"""Real-test HTTP endpoints with fake transports; never controls hardware."""
import io
import json
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from ups_monitor.core import Store
from ups_monitor.server import Handler, Monitor
from ups_monitor.ups_cancel_test import UpsCancellationTest, AUTH_FILENAME, JOURNAL_FILENAME
from ups_monitor.ups_power import NT2402PRO_CANDIDATE_PROFILE, UpsPowerBlocked


class CancellationApiTests(unittest.TestCase):
    def setUp(self):
        # A recently booted Windows runner can have less than 211 s of uptime.
        # Advance a controlled monotonic clock instead of making journal times negative.
        self.clock = Mock(return_value=100.0)
        clock_patch = patch("time.monotonic", self.clock)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        test_root = Path(__file__).resolve().parents[1] / ".test-data"
        self.directory = tempfile.TemporaryDirectory(dir=test_root)
        self.addCleanup(self.directory.cleanup)
        directory = Path(self.directory.name)
        auth = {"schema_version": 1, "owner": "elipro-ups-cancel-test", "authorization_id": uuid.uuid4().hex,
                "issued_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=14)).isoformat()}
        (directory / AUTH_FILENAME).write_text(json.dumps(auth), encoding="utf-8")
        self.usb = Mock()
        self.usb.submit_test.return_value = {"state": "submitted"}
        self.usb.cancel_test.return_value = {"state": "submitted"}
        self.test = UpsCancellationTest(directory / JOURNAL_FILENAME, transport_factory=Mock(return_value=self.usb))
        self.adapter = SimpleNamespace(source="native-test", bridge_required=False,
                                       device_identity={**NT2402PRO_CANDIDATE_PROFILE, "path": r"\\?\hid#vid_0665&pid_5161#fake"},
                                       read=Mock(side_effect=self.sample))
        self.action = Mock()
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.monitor = Monitor(self.store, self.adapter, action_callback=self.action, ups_cancel_test=self.test)
        self.monitor.tick()

    @staticmethod
    def sample(timestamp=None):
        return {"timestamp": timestamp or datetime.now(timezone.utc).isoformat(), "on_battery": False, "low_battery": False,
                "fault": False, "output_voltage": 220, "raw_status": "Line mode", "raw_flags": {"status_hex": "09"}}

    def handler(self, suffix="start", body=None, token=None):
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(server_port=8765, monitor=self.monitor)
        handler.path = "/api/ups/cancel-test/" + suffix
        handler.headers = Message()
        content = json.dumps({} if body is None else body).encode()
        for key, value in {"Host": "127.0.0.1:8765", "Content-Type": "application/json", "Content-Length": str(len(content)),
                           "X-UPS-Token": self.monitor.csrf_token if token is None else token}.items():
            handler.headers[key] = value
        handler.rfile = io.BytesIO(content)
        handler._json = Mock()
        return handler

    def test_start_needs_exact_explicit_ack_and_normal_csrf_gate(self):
        for body, token in (({}, None), ({"acknowledge_risk": False}, None),
                            ({"acknowledge_risk": True, "command": "arbitrary"}, None),
                            ({"acknowledge_risk": True}, "wrong")):
            handler = self.handler(body=body, token=token)
            handler.do_POST()
            self.assertIn(handler._json.call_args.args[1], (400, 403))
        self.usb.submit_test.assert_not_called()

    def test_status_and_simulation_never_arm_and_disabled_policy_is_required(self):
        self.monitor.snapshot()
        self.monitor.test(include_ups=True)
        self.usb.submit_test.assert_not_called()
        self.monitor.set_policy({"enabled": True, "action": "hibernate"})
        handler = self.handler(body={"acknowledge_risk": True})
        handler.do_POST()
        self.assertEqual(handler._json.call_args.args[1], 400)
        self.usb.submit_test.assert_not_called()
        self.action.assert_not_called()

    def test_start_returns_snapshot_and_blocks_policy_simulation_stop_and_os_actions(self):
        handler = self.handler(body={"acknowledge_risk": True})
        handler.do_POST()
        result = handler._json.call_args.args[0]
        self.assertEqual(result["ups_cancel_test"]["state"], "submitted")
        self.assertFalse(result["policy"]["enabled"])
        self.assertFalse(result["policy"]["ups_poweroff"])
        self.usb.submit_test.assert_called_once()
        with self.assertRaises(ValueError):
            self.monitor.set_policy({"enabled": True})
        with self.assertRaises(ValueError):
            self.monitor.test()
        with self.assertRaises(RuntimeError):
            self.monitor._act("hibernate")
        stop = self.handler()
        stop.path = "/api/service/stop"
        stop.do_POST()
        self.assertEqual(stop._json.call_args.args[1], 400)
        self.assertFalse(self.monitor.stop_event.is_set())
        self.action.assert_not_called()

    def test_cancel_fetches_fresh_pending_qs_and_is_once_only(self):
        self.monitor.run_ups_cancel_test(acknowledge_risk=True)
        submitted = datetime.fromisoformat(self.test._operation["submitted_at"])
        # The same wall-clock tick cannot prove a read happened after the write.
        self.adapter.read.side_effect = lambda: {**self.sample(submitted.isoformat()), "on_battery": None,
                                                "raw_status": "Shutdown mode", "raw_flags": {"status_hex": "0B"}}
        at_submission = self.handler("cancel")
        at_submission.do_POST()
        self.assertEqual(at_submission._json.call_args.args[1], 400)
        self.usb.cancel_test.assert_not_called()
        after_submission = (submitted + timedelta(milliseconds=1)).isoformat()
        self.adapter.read.side_effect = lambda: {**self.sample(after_submission), "on_battery": None,
                                                "raw_status": "Shutdown mode", "raw_flags": {"status_hex": "0B"}}
        handler = self.handler("cancel")
        handler.do_POST()
        self.assertEqual(handler._json.call_args.args[1:], (), handler._json.call_args)
        self.assertEqual(handler._json.call_args.args[0]["ups_cancel_test"]["state"], "cancel_submitted")
        self.usb.cancel_test.assert_called_once()
        handler.do_POST()
        self.assertEqual(handler._json.call_args.args[1], 400)
        self.usb.cancel_test.assert_called_once()
        self.action.assert_not_called()

    def test_suspend_during_preflight_does_not_send_control(self):
        def sleep_during_query():
            self.monitor.handle_power_event("suspend")
            return self.sample()
        self.adapter.read.side_effect = sleep_during_query
        with self.assertRaises(UpsPowerBlocked):
            self.monitor.run_ups_cancel_test(acknowledge_risk=True)
        self.usb.submit_test.assert_not_called()

    def test_reading_deferred_during_manual_control_is_not_a_reader_failure(self):
        self.monitor._cancel_test_io.set()
        self.monitor._read_lock.acquire()
        try:
            self.monitor.tick()
            self.assertEqual(self.monitor.health.snapshot()["read_failures"], 0)
            self.assertEqual(self.monitor.connection, "connected")
        finally:
            self.monitor._read_lock.release()
            self.monitor._cancel_test_io.clear()

    def test_missing_signal_creates_one_critical_notice_and_no_automatic_cancel(self):
        self.monitor.run_ups_cancel_test(acknowledge_risk=True)
        self.clock.return_value += 11
        self.monitor.tick()
        self.monitor.tick()
        notices = [event for event in self.monitor.events() if event["kind"] == "ups_cancel_test_unconfirmed"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["severity"], "critical")
        self.usb.cancel_test.assert_not_called()
        self.action.assert_not_called()

    def test_review_close_requires_exact_physical_assertions_and_never_controls_ups(self):
        result = self.monitor.run_ups_cancel_test(acknowledge_risk=True)
        operation_id = result["ups_cancel_test"]["operation_id"]
        self.clock.return_value += 211
        self.test.interrupted()
        payload = {"operation_id": operation_id, "output_cut_observed": True,
                   "pending_signal_not_reported": True, "manual_start_observed": True}
        invalid = self.handler("close", {**payload, "manual_start_observed": False})
        invalid.do_POST()
        self.assertEqual(invalid._json.call_args.args[1], 400)
        handler = self.handler("close", payload)
        handler.do_POST()
        self.assertEqual(handler._json.call_args.args[1:], (), handler._json.call_args)
        closed = handler._json.call_args.args[0]
        self.assertEqual(closed["ups_cancel_test"]["result"], "cut_observed_cancel_not_tested")
        self.assertFalse(closed["ups_cancel_test"]["production_validated"])
        self.assertFalse(self.test.pending())
        self.usb.submit_test.assert_called_once()
        self.usb.cancel_test.assert_not_called()
        self.action.assert_not_called()

    def test_independent_load_report_is_metadata_after_the_complete_original_deadline(self):
        started = self.monitor.run_ups_cancel_test(acknowledge_risk=True)
        submitted = datetime.fromisoformat(self.test._operation["submitted_at"])
        after_submission = (submitted + timedelta(milliseconds=1)).isoformat()
        self.adapter.read.side_effect = lambda: {**self.sample(after_submission), "on_battery": None,
                                                "raw_status": "Shutdown mode", "raw_flags": {"status_hex": "0B"}}
        self.monitor.run_ups_cancel_test(cancel=True)
        self.clock.return_value += 211
        self.test.interrupted()
        self.adapter.read.side_effect = self.sample
        self.monitor.tick()
        body = {"operation_id": started["ups_cancel_test"]["operation_id"],
                "outputs_stayed_on": True, "independent_load_observed": True}
        handler = self.handler("confirm-observation", body)
        handler.do_POST()
        self.assertEqual(handler._json.call_args.args[1:], (), handler._json.call_args)
        result = handler._json.call_args.args[0]
        self.assertEqual(result["ups_cancel_test"]["result"], "cancel_observed_by_user")
        self.assertFalse(result["ups_cancel_test"]["production_validated"])
        self.usb.submit_test.assert_called_once()
        self.usb.cancel_test.assert_called_once()
        self.action.assert_not_called()


if __name__ == "__main__":
    unittest.main()
