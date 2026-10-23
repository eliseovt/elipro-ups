"""Report real protection decisions through fake USB and fake Windows requests."""
import json
import unittest
from unittest.mock import Mock

from ups_monitor.alerts import AlertManager
from ups_monitor.server import Monitor
from tests.test_alerts import Notifier


class UpsBlockReportingTests(unittest.TestCase):
    def setUp(self):
        # Composition reuses the genuine profile/tracker/coordinator setup
        # without collecting another copy of that module's tests.
        from tests.test_ups_sequence_integration import ObservedSequenceIntegrationTests
        self.fixture = ObservedSequenceIntegrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.monitor = self.fixture.monitor
        self.store = self.fixture.store
        self.alerts = AlertManager(self.store, Notifier())
        self.alerts.start()
        self.addCleanup(self.alerts.stop)
        self.monitor.alerts = self.alerts

    def fail_cut_on_device_fault(self):
        self.monitor.set_policy({**self.fixture.policy, "ups_poweroff_delay_seconds": 60})
        for _ in range(5):
            self.fixture.advance(load_percent=100, battery_voltage=25.18)
        # A real QS fault detected at the final decision must stop the USB
        # command while the already-due Windows request remains independent.
        self.fixture.tick += 2
        self.monitor.sample = self.fixture.sample(None, fault=True, raw_status="Fault mode",
            raw_flags={"status_hex": "99"}, load_percent=100, battery_voltage=25.18)
        self.monitor._act("hibernate")
        self.fixture.action.assert_called_once_with("hibernate")
        self.fixture.usb.submit_shutdown.assert_not_called()
        self.fixture.usb.cancel_shutdown.assert_not_called()
        return next(event for event in self.store.events() if event["kind"] == "ups_poweroff_blocked")

    def reload_monitor(self):
        monitor = Monitor(self.store, self.monitor.adapter, ups_power=self.fixture.coordinator,
                          allow_ups_poweroff=True, health_clock=lambda: self.fixture.tick,
                          action_callback=Mock())
        monitor.engine._clock = lambda: self.fixture.tick
        monitor._process_sample(self.fixture.sample(False))
        return monitor

    def test_failed_ups_cut_keeps_windows_and_records_the_exact_decision_until_ack(self):
        event = self.fail_cut_on_device_fault()
        detail = event["data"]
        self.assertIn("falla", event["message"])
        self.assertEqual(detail["configured_countdown_seconds"], 10)
        self.assertEqual(detail["configured_delay_seconds"], 60)
        self.assertEqual(detail["sample"]["load_percent"], 100)
        self.assertEqual(detail["sample"]["battery_voltage"], 25.18)
        self.assertIsNone(detail["sample"]["on_battery"])
        self.assertTrue(detail["sample"]["fault"])
        self.assertEqual(detail["sample"]["raw_status"], "Fault mode")
        check = next(item for item in detail["checks"] if item["key"] == "fault")
        self.assertFalse(check["passed"])
        self.assertIs(check["value"], True)
        self.assertIs(check["expected"], False)
        self.assertIn("falla", check["reason"])
        load = next(item for item in detail["checks"] if item["key"] == "load_percent")
        self.assertTrue(load["passed"])
        self.assertIs(load["blocking"], False)
        self.assertTrue(detail["reason_code"])
        notice = next(item for item in self.alerts.status()["pending"] if item["kind"] == "ups_poweroff_blocked")
        self.assertEqual(notice["severity"], "warning")
        self.assertEqual(notice["message"], event["message"])
        self.monitor._process_sample(self.fixture.sample(False))
        status = self.monitor.snapshot()
        self.assertTrue(status["ups_poweroff"]["available"])
        self.assertIn(notice["id"], {item["id"] for item in status["notifications"]["pending"]})
        self.assertEqual(status["ups_poweroff"]["last_block"]["sample"], detail["sample"])
        self.alerts.acknowledge([notice["id"]])
        status = self.monitor.snapshot()
        self.assertNotIn(notice["id"], {item["id"] for item in status["notifications"]["pending"]})
        self.assertEqual(status["ups_poweroff"]["last_block"]["reason"], event["message"])
        rendered = next(item for item in status["events"] if item["id"] == event["id"])
        self.assertEqual(rendered["severity"], "warning")

    def test_last_block_preserves_persisted_timestamp_and_original_evidence_after_restart(self):
        event = self.fail_cut_on_device_fault()
        self.monitor._process_sample(self.fixture.sample(False))
        original = self.monitor.ups_power_status()["last_block"]
        self.assertEqual(original["timestamp"], event["timestamp"])
        restored = self.reload_monitor().ups_power_status()
        self.assertTrue(restored["available"])
        self.assertEqual(restored["last_block"], original)
        restored["last_block"]["checks"][0]["passed"] = "mutated client copy"
        self.assertEqual(self.monitor.ups_power_status()["last_block"], original)

    def test_old_block_has_historical_date_without_inventing_missing_measurements(self):
        stamp = "2026-10-18T19:12:12Z"
        self.store.add_event("ups_poweroff_blocked", "Bloqueo de una versión anterior.", timestamp=stamp)
        result = self.reload_monitor().ups_power_status()
        self.assertTrue(result["available"])
        self.assertEqual(result["last_block"]["timestamp"], stamp)
        self.assertEqual(result["last_block"]["reason"], "Bloqueo de una versión anterior.")
        self.assertNotIn("sample", result["last_block"])
        self.assertNotIn("checks", result["last_block"])

    def test_nonfinite_bad_readings_are_safe_in_block_diagnostics_and_survive_reload(self):
        self.monitor.sample = self.fixture.sample(None, fault=True, raw_status="Fault mode",
            raw_flags={"status_hex": "99"}, battery_voltage=float("nan"), load_percent=float("inf"))
        capability = self.monitor.ups_power_status()
        self.assertFalse(capability["available"])
        self.monitor._record_ups_block(capability)
        block = self.monitor.ups_power_status()["last_block"]
        json.dumps(block, allow_nan=False)
        self.assertEqual(block["sample"]["battery_voltage"], "nan")
        self.assertEqual(block["sample"]["load_percent"], "inf")
        self.assertTrue(self.monitor.history_status()["available"])
        self.monitor.sample = self.fixture.sample(False)
        restored = self.reload_monitor().ups_power_status()["last_block"]
        self.assertEqual(restored["sample"], block["sample"])
        self.assertEqual(restored["checks"], block["checks"])
        json.dumps(restored, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
