"""Activate the observed sequence using fake USB and fake Windows requests."""
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from ups_monitor.alerts import AlertManager
from ups_monitor.core import DEFAULT_POLICY, Store, iso_utc
from ups_monitor.server import Monitor
from ups_monitor.ups_power import (NT2402PRO_CANDIDATE_PROFILE, VALIDATED_PROFILES,
                                   UpsPowerCoordinator, device_fingerprint)


class ObservedSequenceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.root.cleanup)
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.tick = 100.0
        self.device = {**NT2402PRO_CANDIDATE_PROFILE, "path": r"\\?\hid#vid_0665&pid_5161#sequence-integration-fake"}
        profile = copy.deepcopy(VALIDATED_PROFILES["nt2402pro-qs-t-rev3"])
        profile["device_fingerprint"] = device_fingerprint(self.device)
        profile["pending_signal_evidence"]["device_fingerprint"] = profile["device_fingerprint"]
        self.usb = Mock()
        self.usb.submit_shutdown.return_value = {"state": "submitted"}
        self.usb.cancel_shutdown.return_value = {"state": "submitted"}
        self.coordinator = UpsPowerCoordinator(Path(self.root.name) / "operation.json",
            validated_profiles={profile["profile_id"]: profile}, transport_factory=Mock(return_value=self.usb),
            clock=lambda: self.tick)
        self.action = Mock()
        adapter = SimpleNamespace(source="fake-native", bridge_required=False, device_identity=self.device)
        self.monitor = Monitor(self.store, adapter, ups_power=self.coordinator,
            allow_ups_poweroff=True, health_clock=lambda: self.tick, action_callback=self.action)
        self.monitor.engine._clock = lambda: self.tick
        self.monitor._process_sample(self.sample(False))
        self.policy = {**DEFAULT_POLICY, "enabled": True, "action": "hibernate",
                       "countdown_seconds": 10, "ups_poweroff": True}
        self.monitor.set_policy(self.policy)
        self.monitor._process_sample(self.sample(False))

    @staticmethod
    def sample(battery, **changes):
        return {"timestamp": iso_utc(), "on_battery": battery, "low_battery": False, "fault": False,
            "output_voltage": 220, "load_percent": 11 if battery else 6,
            "battery_voltage": 25.41 if battery else 27.41, "runtime_seconds": None,
            "raw_status": "Battery mode" if battery else "Line mode",
            "raw_flags": {"status_hex": "89" if battery else "09"}, **changes}

    def advance(self, seconds=2, **changes):
        self.tick += seconds
        self.monitor._process_sample(self.sample(True, **changes))

    def test_ten_second_sequence_submits_ups_then_windows_without_runtime_forecast(self):
        order = []
        self.usb.submit_shutdown.side_effect = lambda delay: order.append(("ups", delay)) or {"state": "submitted"}
        self.action.side_effect = lambda action: order.append(("windows", action))
        self.advance()
        for _ in range(5):
            self.advance()
        self.assertEqual(order, [("ups", 180), ("windows", "hibernate")])
        operation = self.coordinator.status()
        self.assertEqual(operation["reserve_mode"], "observed_sequence")
        self.assertIsNone(operation["reserve_seconds"])
        self.assertEqual(operation["sequence_evidence"]["countdown_seconds"], 10)
        self.assertTrue(operation["submission_complete"])

    def test_cancelling_the_ten_second_countdown_never_sends_a_hardware_command(self):
        self.advance()
        self.monitor.cancel()
        for _ in range(6):
            self.advance()
        self.usb.submit_shutdown.assert_not_called()
        self.usb.cancel_shutdown.assert_not_called()
        self.action.assert_not_called()

    def test_one_hundred_twenty_seconds_executes_selected_ups_after_cancel_window(self):
        result = self.monitor.set_policy({**self.policy, "countdown_seconds": 120})
        self.assertEqual(result["policy"]["countdown_seconds"], 120)
        self.assertTrue(result["policy"]["ups_poweroff"])
        self.assertTrue(result["ups_poweroff"]["can_configure"])
        self.advance()
        for _ in range(59):
            self.advance()
        self.usb.submit_shutdown.assert_not_called()
        self.action.assert_not_called()
        self.advance()
        self.action.assert_called_once_with("hibernate")
        self.usb.submit_shutdown.assert_called_once_with(180)

    def test_default_fifteen_seconds_and_three_minutes_both_execute_after_full_countdown(self):
        self.assertEqual(DEFAULT_POLICY["countdown_seconds"], 15)
        self.assertEqual(DEFAULT_POLICY["ups_poweroff_delay_seconds"], 180)
        self.monitor.set_policy({**self.policy, "countdown_seconds": 15})
        self.advance()
        for _ in range(7):
            self.advance()
        self.usb.submit_shutdown.assert_not_called()
        self.action.assert_not_called()
        self.advance()
        self.usb.submit_shutdown.assert_called_once_with(180)
        self.action.assert_called_once_with("hibernate")
        self.assertEqual(self.coordinator.status()["sequence_evidence"]["countdown_seconds"], 15)

    def test_configured_one_minute_reaches_hardware_and_durable_operation_unchanged(self):
        self.monitor.set_policy({**self.policy, "ups_poweroff_delay_seconds": 60})
        self.advance()
        for _ in range(5):
            self.advance()
        self.usb.submit_shutdown.assert_called_once_with(60)
        self.assertEqual(self.coordinator.status()["delay_seconds"], 60)
        self.action.assert_called_once_with("hibernate")

    def test_configured_nine_minutes_reaches_hardware_without_an_autonomy_claim(self):
        self.monitor.set_policy({**self.policy, "ups_poweroff_delay_seconds": 540})
        self.advance()
        for _ in range(5):
            self.advance()
        self.usb.submit_shutdown.assert_called_once_with(540)
        self.assertEqual(self.coordinator.status()["delay_seconds"], 540)
        self.assertIsNone(self.coordinator.status()["reserve_seconds"])

    def test_unsupported_ten_minutes_cannot_send_a_command_or_be_saved(self):
        with self.assertRaises(ValueError):
            self.monitor.set_policy({**self.policy, "ups_poweroff_delay_seconds": 600})
        self.usb.submit_shutdown.assert_not_called()
        self.action.assert_not_called()

    def test_high_load_alone_does_not_block_windows_or_ups(self):
        self.advance(load_percent=100)
        for _ in range(5):
            self.advance()
        self.action.assert_called_once_with("hibernate")
        self.usb.submit_shutdown.assert_called_once_with(180)

    def test_reported_voltage_with_user_ten_second_and_one_minute_timers_submits_ups_then_hibernates(self):
        self.monitor.set_policy({**self.policy, "ups_poweroff_delay_seconds": 60})
        order = []
        self.usb.submit_shutdown.side_effect = lambda delay: order.append(("ups_command", delay)) or {"state": "submitted"}
        self.action.side_effect = lambda action: order.append(("windows", action))
        self.advance(battery_voltage=25.18, load_percent=11)
        for _ in range(4):
            self.advance(battery_voltage=25.18, load_percent=11)
        self.usb.submit_shutdown.assert_not_called()
        self.action.assert_not_called()
        self.advance(battery_voltage=25.18, load_percent=11)
        self.assertEqual(order, [("ups_command", 60), ("windows", "hibernate")])
        operation = self.coordinator.status()
        self.assertEqual(operation["delay_seconds"], 60)
        self.assertEqual(operation["sequence_evidence"]["countdown_seconds"], 10)
        self.assertEqual(operation["sequence_evidence"]["min_battery_voltage"], 25.18)
        self.assertIsNone(operation["reserve_seconds"])
        self.assertFalse(operation.get("physical_confirmation", False))

    def test_load_above_trial_does_not_block_ups_without_overload_or_device_alarm(self):
        self.advance(load_percent=23)
        for _ in range(5):
            self.advance(load_percent=23)
        self.action.assert_called_once_with("hibernate")
        self.usb.submit_shutdown.assert_called_once_with(180)

    def capture_submission_order(self):
        order = []
        self.usb.submit_shutdown.side_effect = lambda delay: order.append(("ups_command", delay)) or {"state": "submitted"}
        self.action.side_effect = lambda action: order.append(("windows", action))
        return order

    def test_known_estimated_twenty_percent_shortens_first_countdown_to_ten_seconds_then_submits_one_minute(self):
        self.monitor.set_policy({**self.policy, "countdown_seconds": 120, "ups_poweroff_delay_seconds": 60})
        order = self.capture_submission_order()
        low = {"battery_percent_estimate": 20, "battery_estimate_method": "nt2402pro-pmv-qs-v1",
               "battery_voltage": None}
        self.advance(**low)
        status = self.monitor.engine.status()
        self.assertEqual(status["effective_countdown_seconds"], 10)
        self.assertEqual(status["countdown_remaining"], 10)
        self.assertEqual(status["emergency"]["source"], "estimated_percentage")
        self.assertEqual(status["emergency"]["percent"], 20)
        self.assertFalse(status["emergency"]["immediate"])
        for _ in range(4):
            self.advance(**low)
        self.assertEqual(order, [])
        self.advance(**low)
        self.assertEqual(order, [("ups_command", 60), ("windows", "hibernate")])
        operation = self.coordinator.status()
        self.assertEqual(operation["sequence_evidence"]["countdown_seconds"], 10)
        self.assertEqual(operation["delay_seconds"], 60)
        self.assertIsNone(operation["reserve_seconds"])
        messages = [event["message"] for event in self.store.events() if event["kind"] == "battery_low_percentage"]
        self.assertTrue(any("estimada" in message and "20" in message for message in messages))

    def test_reported_percentage_crossing_after_thirty_seconds_shortens_total_to_forty_seconds(self):
        self.monitor.set_policy({**self.policy, "countdown_seconds": 120, "ups_poweroff_delay_seconds": 60})
        order = self.capture_submission_order()
        self.advance(battery_percent=80)
        for _ in range(14):
            self.advance(battery_percent=80)
        self.assertEqual(self.monitor.engine.status()["on_battery_seconds"], 28)
        self.advance(battery_percent=20)
        status = self.monitor.engine.status()
        self.assertEqual(status["on_battery_seconds"], 30)
        self.assertEqual(status["effective_countdown_seconds"], 40)
        self.assertEqual(status["countdown_remaining"], 10)
        self.assertEqual(status["emergency"]["source"], "reported_percentage")
        for _ in range(4):
            self.advance(battery_percent=20)
        self.assertEqual(order, [])
        self.advance(battery_percent=20)
        self.assertEqual(order, [("ups_command", 60), ("windows", "hibernate")])
        self.assertEqual(self.coordinator.status()["sequence_evidence"]["countdown_seconds"], 40)
        self.assertEqual(self.monitor.engine.policy["countdown_seconds"], 120)

    def test_low_percentage_never_extends_a_deadline_with_five_seconds_remaining(self):
        self.monitor.set_policy({**self.policy, "countdown_seconds": 30, "ups_poweroff_delay_seconds": 60})
        order = self.capture_submission_order()
        self.advance(battery_percent=80)
        for _ in range(12):
            self.advance(battery_percent=80)
        self.advance(seconds=1, battery_percent=20)
        status = self.monitor.engine.status()
        self.assertEqual(status["on_battery_seconds"], 25)
        self.assertEqual(status["countdown_remaining"], 5)
        self.assertEqual(status["effective_countdown_seconds"], 30)
        for _ in range(4):
            self.advance(seconds=1, battery_percent=20)
        self.assertEqual(order, [])
        self.advance(seconds=1, battery_percent=20)
        self.assertEqual(order, [("ups_command", 60), ("windows", "hibernate")])
        self.assertEqual(self.coordinator.status()["sequence_evidence"]["countdown_seconds"], 30)

    def test_high_percentage_keeps_user_timer_even_with_missing_or_zero_voltage(self):
        self.monitor.set_policy({**self.policy, "countdown_seconds": 30, "ups_poweroff_delay_seconds": 60})
        self.monitor._process_sample(self.sample(False, battery_voltage=None, battery_percent=90))
        order = self.capture_submission_order()
        self.advance(battery_percent=90, battery_voltage=None)
        for index in range(14):
            self.advance(battery_percent=90, battery_voltage=0 if index % 2 else None)
        status = self.monitor.engine.status()
        self.assertFalse(status["emergency"]["active"])
        self.assertEqual(status["effective_countdown_seconds"], 30)
        self.assertEqual(status["countdown_remaining"], 2)
        self.assertTrue(self.monitor.ups_power_status()["available"])
        self.assertEqual(order, [])
        self.advance(battery_percent=90, battery_voltage=None)
        self.assertEqual(order, [("ups_command", 60), ("windows", "hibernate")])
        self.assertEqual(self.coordinator.status()["sequence_evidence"]["countdown_seconds"], 30)

    def test_hardware_critical_alarm_requests_windows_immediately_without_a_risky_usb_command(self):
        notifier = Mock()
        notifier.status.return_value = {"available": True, "ready": True}
        self.monitor.alerts = AlertManager(self.store, notifier)
        self.addCleanup(self.monitor.alerts.stop)
        self.monitor.set_policy({**self.policy, "countdown_seconds": 120, "ups_poweroff_delay_seconds": 60})
        order = self.capture_submission_order()
        self.advance(battery_percent=5, low_battery=True, raw_flags={"status_hex": "C9"})
        self.assertEqual(order, [("windows", "hibernate")])
        self.usb.submit_shutdown.assert_not_called()
        self.usb.cancel_shutdown.assert_not_called()
        self.assertEqual(self.coordinator.status()["state"], "idle")
        status = self.monitor.engine.status()
        self.assertEqual(status["state"], "action_started")
        self.assertTrue(status["emergency"]["immediate"])
        self.assertEqual(status["emergency"]["source"], "ups_critical_alarm")
        capability = self.monitor.ups_power_status()
        self.assertFalse(capability["available"])
        self.assertIn("cortar inmediatamente", capability["reason"])
        events = [event for event in self.store.events() if event["kind"] == "ups_poweroff_blocked"]
        self.assertTrue(events)
        self.assertIn("cortar inmediatamente", events[0]["message"])
        self.monitor._process_sample(self.sample(False))
        self.assertTrue(any(item["kind"] == "ups_poweroff_blocked"
                            for item in self.monitor.notification_status()["pending"]))

    def test_gap_in_telemetry_recovers_in_battery_after_a_new_full_countdown(self):
        self.advance()
        self.advance(seconds=16)
        for _ in range(5):
            self.advance()
        self.action.assert_called_once_with("hibernate")
        self.usb.submit_shutdown.assert_called_once_with(180)

    def test_legacy_countdown_outside_range_is_disabled_until_explicitly_reviewed(self):
        saved = Store(":memory:")
        self.addCleanup(saved.close)
        saved.set_setting("policy", {**self.policy, "countdown_seconds": 3600})
        monitor = Monitor(saved, self.monitor.adapter, action_callback=Mock())
        self.assertEqual(monitor.engine.policy["countdown_seconds"], 120)
        self.assertFalse(monitor.engine.policy["enabled"])
        self.assertEqual(saved.get_setting("policy")["countdown_seconds"], 120)
        self.assertEqual(saved.get_setting("policy_migration_backup")["policy"]["countdown_seconds"], 3600)
        self.assertTrue(saved.get_setting("policy_migration_review")["requires_review"])
        self.assertIn("policy_migration_review", {item["kind"] for item in saved.events()})
        with self.assertRaises(ValueError):
            self.monitor.set_policy({**self.policy, "countdown_seconds": 121})

    def test_completed_timer_can_be_reviewed_without_any_repeated_power_command(self):
        self.advance()
        for _ in range(5):
            self.advance()
        operation_id = self.coordinator.status()["operation_id"]
        self.monitor.handle_power_event("suspend")
        self.monitor.snapshot()
        self.tick += 211
        self.monitor.handle_power_event("resume")
        self.monitor._process_sample(self.sample(False))
        self.assertTrue(self.monitor.ups_power_status()["can_review"])
        reviewed = self.monitor.complete_ups_review({"operation_id": operation_id, "acknowledge_review": True})
        self.assertEqual(reviewed["ups_poweroff"]["operation"]["state"], "completed")
        self.assertFalse(reviewed["ups_poweroff"]["operation"]["physical_confirmation"])
        self.assertEqual(reviewed["ups_poweroff"]["operation"]["confirmation"], "unconfirmed")
        self.assertTrue(reviewed["ups_poweroff"]["available"])
        self.usb.submit_shutdown.assert_called_once()
        self.usb.cancel_shutdown.assert_not_called()
        self.action.assert_called_once()

    def test_review_cannot_bypass_the_original_timer(self):
        self.advance()
        for _ in range(5):
            self.advance()
        operation_id = self.coordinator.status()["operation_id"]
        self.monitor._process_sample(self.sample(False))
        with self.assertRaises(ValueError):
            self.monitor.complete_ups_review({"operation_id": operation_id, "acknowledge_review": True})
        self.usb.submit_shutdown.assert_called_once()
        self.usb.cancel_shutdown.assert_not_called()

    def test_early_resume_warns_about_pending_cut_without_repeating_any_action(self):
        self.advance()
        for _ in range(5):
            self.advance()
        self.monitor.handle_power_event("suspend")
        self.monitor.snapshot()
        self.tick += 20
        self.monitor.handle_power_event("resume")
        self.monitor.snapshot()
        messages = [event["message"] for event in self.store.events() if event["kind"] == "ups_poweroff_unconfirmed"]
        self.assertTrue(any("antes de terminar" in message for message in messages))
        self.usb.submit_shutdown.assert_called_once()
        self.usb.cancel_shutdown.assert_not_called()
        self.action.assert_called_once()


if __name__ == "__main__":
    unittest.main()
