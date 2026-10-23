"""Authorized percentage protection preserves the device's original telemetry."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ups_monitor.alerts import AlertManager
from ups_monitor.battery_estimation import BATTERY_ESTIMATE_METHOD
from ups_monitor.core import Store, TelemetryRingBuffer, iso_utc
from ups_monitor.server import Monitor


class BatteryEstimateIsolationTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.action = Mock()
        self.notifier = SimpleNamespace(start=Mock(), stop=Mock(), notify=Mock(return_value=True),
                                        sync_persistent=Mock(), status=Mock(return_value={"state": "running"}))
        self.alerts = AlertManager(self.store, self.notifier)
        self.alerts.start()
        self.addCleanup(self.alerts.stop)
        self.adapter = SimpleNamespace(source="isolated battery fixture", bridge_required=False,
                                       read=Mock(side_effect=AssertionError("No hardware access allowed")))
        self.monitor = Monitor(self.store, self.adapter, action_callback=self.action, alerts=self.alerts)
        self.clock = Mock(return_value=100)
        self.monitor.engine._clock = self.clock
        self.monitor.set_policy({"enabled": True, "countdown_seconds": 120})

    @staticmethod
    def sample(percent, **changes):
        return {"timestamp": iso_utc(), "on_battery": True, "low_battery": False,
                "battery_percent": None, "runtime_seconds": None, "battery_voltage": 26.1,
                "load_percent": 10, "fault": False, "battery_percent_estimate": percent,
                "battery_estimate_method": BATTERY_ESTIMATE_METHOD, **changes}

    def test_unknown_estimate_never_accelerates_or_rewrites_device_telemetry(self):
        with patch("ups_monitor.server.windows_action") as windows_action:
            for percent in (80, 20, 1, 0, 100):
                self.clock.return_value += 10
                original = self.sample(percent, battery_estimate_method="unknown-method")
                self.monitor._process_sample(original)
                status = self.monitor.snapshot()
                self.assertEqual(self.monitor.engine.status()["state"], "countdown")
                self.assertEqual(self.monitor.engine.status()["countdown_remaining"], 120 - (self.clock.return_value - 110))
                self.assertEqual(status["sample"]["battery_percent_estimate"], percent)
                self.assertIsNone(status["sample"]["battery_percent"])
                self.assertIs(status["sample"]["low_battery"], False)
                self.assertIs(original["low_battery"], False)
                self.assertFalse(self.monitor.low)
                self.assertNotIn("battery_low", self.alerts._active)
            self.action.assert_not_called()
            windows_action.assert_not_called()
        kinds = [event["kind"] for event in self.monitor.events()]
        self.assertNotIn("battery_low", kinds)
        self.assertEqual(kinds.count("policy_countdown"), 1)
        self.assertNotIn("battery_low_percentage", kinds)
        self.assertNotIn("policy_action_started", kinds)
        self.adapter.read.assert_not_called()
        # An actual UPS flag remains authoritative despite a high estimate.
        self.monitor._process_sample(self.sample(100, low_battery=True))
        self.assertEqual(self.monitor.engine.status()["state"], "action_started")
        self.action.assert_called_once_with("shutdown")
        self.assertIn("battery_low", [event["kind"] for event in self.monitor.events()])
        self.assertIn("battery_low", self.alerts._active)

    def test_known_estimate_accelerates_with_distinct_persistent_notice_and_native_flags_intact(self):
        self.monitor._process_sample(self.sample(80))
        self.clock.return_value = 110
        original = self.sample(20)
        self.monitor._process_sample(original)
        status = self.monitor.snapshot()
        self.assertEqual(status["protection"]["countdown_remaining"], 10)
        self.assertEqual(status["protection"]["effective_countdown_seconds"], 20)
        self.assertEqual(status["protection"]["emergency"]["source"], "estimated_percentage")
        self.assertEqual(status["protection"]["emergency"]["percent"], 20)
        self.assertIsNone(status["sample"]["battery_percent"])
        self.assertIs(status["sample"]["low_battery"], False)
        self.assertIsNone(original["battery_percent"])
        self.assertIs(original["low_battery"], False)
        self.assertFalse(self.monitor.low)
        kinds = [event["kind"] for event in self.monitor.events()]
        self.assertIn("battery_low_percentage", kinds)
        self.assertNotIn("battery_low", kinds)
        notice = next(item for item in status["notifications"]["pending"]
                      if item["kind"] == "battery_low_percentage")
        self.assertIn("estimada", notice["message"])
        self.assertIsNone(notice["acknowledged_at"])
        self.clock.return_value = 119
        self.monitor._process_sample(self.sample(20))
        self.action.assert_not_called()
        self.clock.return_value = 120
        self.monitor._process_sample(self.sample(20))
        self.action.assert_called_once_with("shutdown")
        self.adapter.read.assert_not_called()

    def test_reported_high_percentage_takes_precedence_over_low_known_estimate(self):
        self.monitor._process_sample(self.sample(1, battery_percent=80))
        status = self.monitor.snapshot()
        self.assertEqual(status["protection"]["countdown_remaining"], 120)
        self.assertFalse(status["protection"]["emergency"]["active"])
        self.assertEqual(status["sample"]["battery_percent"], 80)
        self.assertEqual(status["sample"]["battery_percent_estimate"], 1)
        self.assertNotIn("battery_low_percentage", [event["kind"] for event in self.monitor.events()])
        self.assertFalse(self.monitor.low)
        self.action.assert_not_called()

    def test_manual_cancel_respects_low_known_estimate_until_new_outage(self):
        self.monitor._process_sample(self.sample(20))
        self.monitor.cancel()
        for tick in (110, 120, 130):
            self.clock.return_value = tick
            self.monitor._process_sample(self.sample(1))
        self.action.assert_not_called()
        self.assertTrue(self.monitor.engine.status()["suppressed"])
        self.monitor._process_sample(self.sample(1, on_battery=False))
        self.clock.return_value = 131
        self.monitor._process_sample(self.sample(1))
        self.assertFalse(self.monitor.engine.status()["suppressed"])
        self.assertEqual(self.monitor.engine.status()["countdown_remaining"], 10)
        self.clock.return_value = 141
        self.monitor._process_sample(self.sample(1))
        self.action.assert_called_once_with("shutdown")
        self.adapter.read.assert_not_called()

    def test_estimate_does_not_hide_reported_low_percentage(self):
        self.monitor._process_sample(self.sample(100, battery_percent=5))
        self.assertTrue(self.monitor.low)
        self.assertEqual(self.monitor.engine.status()["state"], "countdown")
        self.assertEqual(self.monitor.sample["battery_percent"], 5)
        self.assertEqual(self.monitor.sample["battery_percent_estimate"], 100)
        self.assertIn("battery_low", self.alerts._active)
        self.action.assert_not_called()

    def test_estimate_is_not_recorded_as_alarm_evidence_in_telemetry_overflow(self):
        ring = TelemetryRingBuffer(2)
        for percent in (80, 20, 0, 1, 50):
            ring.publish(self.sample(percent))
        entries, overflow = ring.drain()
        self.assertNotIn("battery_low", overflow["transitions"])
        self.assertFalse(any("battery_low" in entry["facts"] for entry in entries))
        ring.publish(self.sample(100, low_battery=True))
        entries, _ = ring.drain()
        self.assertIn("battery_low", entries[0]["facts"])


if __name__ == "__main__":
    unittest.main()
