"""Activate the observed sequence using fake USB and fake Windows requests."""
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

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
        self.policy = {**DEFAULT_POLICY, "enabled": True, "action": "hibernate", "on_battery_seconds": 0,
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

    def test_heavier_load_or_lower_voltage_retains_windows_and_skips_ups(self):
        self.advance(load_percent=12)
        for _ in range(5):
            self.advance()
        self.action.assert_called_once_with("hibernate")
        self.usb.submit_shutdown.assert_not_called()

    def test_gap_in_telemetry_does_not_recreate_a_last_mains_baseline(self):
        self.advance()
        self.advance(seconds=16)
        for _ in range(5):
            self.advance()
        self.action.assert_called_once_with("hibernate")
        self.usb.submit_shutdown.assert_not_called()

    def test_legacy_countdown_is_clamped_only_when_loading_saved_settings(self):
        saved = Store(":memory:")
        self.addCleanup(saved.close)
        saved.set_setting("policy", {**self.policy, "countdown_seconds": 3600})
        monitor = Monitor(saved, self.monitor.adapter, action_callback=Mock())
        self.assertEqual(monitor.engine.policy["countdown_seconds"], 120)
        self.assertEqual(saved.get_setting("policy")["countdown_seconds"], 120)
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
