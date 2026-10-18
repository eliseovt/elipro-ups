"""Battery test HTTP/Windows sequencing with fake USB and fake power requests."""
import io
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from ups_monitor.core import Store, iso_utc
from ups_monitor.server import Handler, Monitor, WindowsActionUnconfirmed
from ups_monitor.ups_battery_test import UpsBatteryTest, AUTH_FILENAME, JOURNAL_FILENAME
from ups_monitor.ups_cancel_test import candidate_profile
from ups_monitor.ups_power import NT2402PRO_CANDIDATE_PROFILE, UpsPowerBlocked


class BatteryTestApiTests(unittest.TestCase):
    def setUp(self):
        # A recently booted Windows runner can have less than 211 s of uptime.
        # Advance a controlled monotonic clock instead of making journal times negative.
        self.clock = Mock(return_value=100.0)
        clock_patch = patch("time.monotonic", self.clock)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.device = {**NT2402PRO_CANDIDATE_PROFILE, "path": r"\\?\hid#vid_0665&pid_5161#battery-api-fake"}
        profile = candidate_profile(self.device)
        now = datetime.now(timezone.utc)
        auth = {"schema_version": 1, "owner": "elipro-ups-battery-test", "authorization_id": uuid.uuid4().hex,
                "issued_at": (now - timedelta(seconds=1)).isoformat(),
                "expires_at": (now + timedelta(minutes=14)).isoformat()}
        (self.root / AUTH_FILENAME).write_text(json.dumps(auth), encoding="utf-8")
        cancellation = {"schema_version": 1, "owner": "elipro-ups-cancel-test-operation", "state": "finished",
            "operation_id": str(uuid.uuid4()), "authorization_id": uuid.uuid4().hex, "profile": profile,
            "created_at": iso_utc(), "service_pid": os.getpid(), "started_monotonic": time.monotonic(),
            "delay_seconds": 180, "submission_complete": True, "cancel_attempted": True,
            "result": "cancel_observed_by_user", "physical_observation": {
                "source": "user_independent_load", "outputs_stayed_on": True, "recorded_at": iso_utc()},
            "compatibility_evidence": {"output_cut_observed": True, "pending_signal_not_reported": True,
                "manual_start_observed": True, "device_fingerprint": profile["device_fingerprint"],
                "source_operation_id": str(uuid.uuid4())}}
        (self.root / "ups-cancel-test-operation.json").write_text(json.dumps(cancellation), encoding="utf-8")
        self.usb = Mock()
        self.usb.submit_test.return_value = {"state": "submitted"}
        self.usb.cancel_after_windows_failure.return_value = {"state": "submitted"}
        self.test = UpsBatteryTest(self.root / JOURNAL_FILENAME, transport_factory=Mock(return_value=self.usb))
        self.on_battery = False
        self.adapter = SimpleNamespace(source="fake-native", bridge_required=False, device_identity=self.device,
                                       read=Mock(side_effect=self.sample))
        self.action = Mock()
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.monitor = Monitor(self.store, self.adapter, action_callback=self.action, ups_battery_test=self.test)
        self.monitor.tick()  # Observe actual online baseline before transition.

    def sample(self):
        return {"timestamp": iso_utc(), "on_battery": self.on_battery, "low_battery": False,
            "fault": False, "output_voltage": 220, "load_percent": 8,
            "raw_status": "Battery mode" if self.on_battery else "Line mode",
            "raw_flags": {"status_hex": "89" if self.on_battery else "09"}}

    def battery(self):
        self.on_battery = True
        self.monitor.tick()

    def handler(self, suffix="start", body=None, token=None):
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(server_port=8765, monitor=self.monitor)
        handler.path = "/api/ups/battery-test/" + suffix
        handler.headers = Message()
        content = json.dumps({} if body is None else body).encode()
        for key, value in {"Host": "127.0.0.1:8765", "Content-Type": "application/json", "Content-Length": str(len(content)),
            "X-UPS-Token": self.monitor.csrf_token if token is None else token}.items():
            handler.headers[key] = value
        handler.rfile, handler._json = io.BytesIO(content), Mock()
        return handler

    def test_status_loss_of_mains_and_simulation_never_start_real_test(self):
        self.monitor.snapshot()
        self.battery()
        self.monitor.snapshot()
        self.monitor.test(include_ups=True)
        self.usb.submit_test.assert_not_called()
        self.action.assert_not_called()

    def test_start_needs_exact_consent_and_csrf_and_disabled_policy(self):
        self.battery()
        for data, token in (({}, None), ({"acknowledge_risk": False}, None),
                ({"acknowledge_risk": True, "command": "anything"}, None), ({"acknowledge_risk": True}, "bad")):
            handler = self.handler(body=data, token=token)
            handler.do_POST()
            self.assertIn(handler._json.call_args.args[1], (400, 403))
        self.monitor.set_policy({"enabled": True, "action": "hibernate"})
        handler = self.handler(body={"acknowledge_risk": True})
        handler.do_POST()
        self.assertEqual(handler._json.call_args.args[1], 400)
        self.usb.submit_test.assert_not_called()
        self.action.assert_not_called()

    def test_hibernation_follows_full_or_uncertain_submission_once(self):
        self.battery()
        order = []
        self.usb.submit_test.side_effect = lambda: order.append("ups") or {"state": "submitted"}
        self.action.side_effect = lambda action: order.append(action)
        handler = self.handler(body={"acknowledge_risk": True})
        handler.do_POST()
        snapshot = handler._json.call_args.args[0]
        self.assertEqual(order, ["ups", "hibernate"])
        self.assertEqual(snapshot["ups_battery_test"]["windows_request"], "requested")
        self.assertFalse(snapshot["policy"]["ups_poweroff"])
        with self.assertRaises(UpsPowerBlocked):
            self.monitor.run_ups_battery_test(acknowledge_risk=True)
        self.usb.submit_test.assert_called_once()

    def test_partial_submission_still_requests_hibernation(self):
        self.battery()
        self.usb.submit_test.side_effect = TimeoutError("partial report")
        result = self.monitor.run_ups_battery_test(acknowledge_risk=True)
        self.assertEqual(result["ups_battery_test"]["windows_request"], "requested")
        self.action.assert_called_once_with("hibernate")
        self.usb.cancel_after_windows_failure.assert_not_called()

    def test_post_send_disk_io_and_service_stop_do_not_precede_hibernation(self):
        self.battery()
        order = []
        original = self.test._journal.write
        def write(value):
            order.append("journal:" + value["state"])
            original(value)
        def send():
            order.append("usb")
            self.monitor.stop_event.set()
            return {"state": "submitted"}
        self.test._journal.write = write
        self.usb.submit_test.side_effect = send
        self.action.side_effect = lambda action: order.append(action)
        self.monitor.run_ups_battery_test(acknowledge_risk=True)
        self.assertEqual(order[:4], ["journal:arming", "usb", "hibernate", "journal:submitted"])
        self.action.assert_called_once_with("hibernate")
        self.usb.cancel_after_windows_failure.assert_not_called()

    def test_rejected_windows_request_attempts_only_owned_cancel(self):
        self.battery()
        self.action.side_effect = RuntimeError("Windows rejected")
        result = self.monitor.run_ups_battery_test(acknowledge_risk=True)
        self.assertEqual(result["ups_battery_test"]["windows_request"], "failed")
        self.usb.cancel_after_windows_failure.assert_called_once()
        self.usb.submit_test.assert_called_once()
        with self.assertRaises(UpsPowerBlocked):
            self.monitor.run_ups_battery_test(acknowledge_risk=True)

    def test_windows_timeout_without_transition_is_unconfirmed_and_attempts_one_owned_emergency_cancel(self):
        self.battery()
        self.action.side_effect = WindowsActionUnconfirmed("No se confirmó la solicitud a Windows")
        result = self.monitor.run_ups_battery_test(acknowledge_risk=True)
        self.assertEqual(result["ups_battery_test"]["windows_request"], "unconfirmed")
        self.usb.cancel_after_windows_failure.assert_called_once_with()
        self.usb.submit_test.assert_called_once_with()
        self.action.assert_called_once_with("hibernate")
        events = self.store.events()
        self.assertIn("ups_battery_test_windows_unconfirmed", [event["kind"] for event in events])

    def test_timeout_after_suspend_and_resume_does_not_record_rejection_or_cancel_or_repeat(self):
        self.battery()

        def crossed_transition(action):
            self.monitor.handle_power_event("suspend")
            self.monitor.handle_power_event("resume")
            raise subprocess.TimeoutExpired("shutdown.exe", -302)

        self.action.side_effect = crossed_transition
        result = self.monitor.run_ups_battery_test(acknowledge_risk=True)
        self.assertEqual(result["ups_battery_test"]["windows_request"], "unconfirmed")
        self.assertEqual(self.monitor.system_power_status()["generation"], 2)
        self.usb.cancel_after_windows_failure.assert_not_called()
        self.action.assert_called_once_with("hibernate")
        with self.assertRaises(UpsPowerBlocked):
            self.monitor.run_ups_battery_test(acknowledge_risk=True)
        self.usb.submit_test.assert_called_once_with()
        self.assertNotIn("rechaz", result["ups_battery_test"].get("message", "").lower())
        messages = [event["message"] for event in self.store.events() if event["kind"] == "ups_battery_test_windows_unconfirmed"]
        self.assertTrue(messages)
        self.assertNotIn("hiberna manualmente", " ".join(messages))

    def _late_transition(self, failure):
        self.battery()

        def crossed_transition(action):
            self.monitor.handle_power_event("suspend")
            if failure:
                raise RuntimeError("Windows rejected")

        self.action.side_effect = crossed_transition
        result = self.monitor.run_ups_battery_test(acknowledge_risk=True)
        self.assertEqual(result["ups_battery_test"]["windows_request"], "unconfirmed")
        self.usb.cancel_after_windows_failure.assert_not_called()
        self.action.assert_called_once_with("hibernate")

    def test_late_process_success_after_suspend_is_unconfirmed(self):
        self._late_transition(False)

    def test_late_process_error_after_suspend_is_unconfirmed(self):
        self._late_transition(True)

    def test_suspend_during_read_prevents_usb_and_suspend_during_os_request_prevents_cancel(self):
        self.battery()
        def suspend_read():
            self.monitor.handle_power_event("suspend")
            return self.sample()
        self.adapter.read.side_effect = suspend_read
        with self.assertRaises(UpsPowerBlocked):
            self.monitor.run_ups_battery_test(acknowledge_risk=True)
        self.usb.submit_test.assert_not_called()
        self.action.assert_not_called()

    def test_resume_never_replays_and_observations_only_record_metadata(self):
        self.battery()
        result = self.monitor.run_ups_battery_test(acknowledge_risk=True)
        self.clock.return_value += 211
        self.monitor.handle_power_event("suspend")
        self.monitor.snapshot()
        self.monitor.handle_power_event("resume")
        self.on_battery = False
        self.monitor.tick()
        body = {"operation_id": result["ups_battery_test"]["operation_id"], "cut_observed": True,
                "ac_restored": True, "off_after_ac_seconds": 60, "manual_restart": True,
                "hibernate_resumed": True, "automatic_return_observed": False, "acknowledge_observation": True}
        handler = self.handler("confirm-observation", body)
        handler.do_POST()
        self.assertEqual(handler._json.call_args.args[1:], (), handler._json.call_args)
        status = handler._json.call_args.args[0]["ups_battery_test"]
        self.assertEqual(status["result"], "battery_cut_manual_restart_observed")
        self.assertFalse(status["production_validated"])
        self.usb.submit_test.assert_called_once()
        self.usb.cancel_after_windows_failure.assert_not_called()
        self.action.assert_called_once_with("hibernate")


if __name__ == "__main__":
    unittest.main()
