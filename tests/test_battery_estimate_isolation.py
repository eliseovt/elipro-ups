"""Derived display percentages cannot become real battery alarms or actions."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ups_monitor.alerts import AlertManager
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
        self.monitor.set_policy({"enabled": True, "on_battery_seconds": 86400,
                                 "countdown_seconds": 10})

    @staticmethod
    def sample(percent, **changes):
        return {"timestamp": iso_utc(), "on_battery": True, "low_battery": False,
                "battery_percent": None, "runtime_seconds": None, "battery_voltage": 26.1,
                "load_percent": 10, "fault": False, "battery_percent_estimate": percent,
                "battery_estimate_method": "pmv-v1", **changes}

    def test_estimated_low_charge_never_starts_countdown_emits_low_alarm_or_changes_real_flag(self):
        with patch("ups_monitor.server.windows_action") as windows_action:
            for percent in (80, 20, 1, 0, 100):
                self.clock.return_value += 10
                original = self.sample(percent)
                self.monitor._process_sample(original)
                status = self.monitor.snapshot()
                self.assertEqual(self.monitor.engine.status()["state"], "on_battery")
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
        self.assertNotIn("policy_countdown", kinds)
        self.assertNotIn("policy_action_started", kinds)
        self.adapter.read.assert_not_called()
        # An actual UPS flag remains authoritative despite a high estimate.
        self.monitor._process_sample(self.sample(100, low_battery=True))
        self.assertEqual(self.monitor.engine.status()["state"], "countdown")
        self.assertIn("battery_low", [event["kind"] for event in self.monitor.events()])
        self.assertIn("battery_low", self.alerts._active)

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
