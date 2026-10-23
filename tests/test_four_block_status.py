"""Four live UPS blockers, exercised without any real energy/USB operation."""
import unittest
from unittest.mock import Mock

from ups_monitor.server import Monitor


class FourBlockStatusTests(unittest.TestCase):
    def setUp(self):
        from tests.test_ups_sequence_integration import ObservedSequenceIntegrationTests
        self.fixture = ObservedSequenceIntegrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.monitor = self.fixture.monitor
        self.fixture.monitor.set_policy({**self.fixture.policy, "ups_poweroff_delay_seconds": 60})

    def with_probe(self, probe):
        self.monitor = Monitor(self.fixture.store, self.fixture.monitor.adapter,
            ups_power=self.fixture.coordinator, allow_ups_poweroff=True,
            health_clock=lambda: self.fixture.tick, action_callback=self.fixture.action,
            windows_readiness_probe=probe)
        self.monitor.engine._clock = lambda: self.fixture.tick
        self.monitor._process_sample(self.fixture.sample(False))
        self.fixture.monitor = self.monitor
        return self.monitor

    def blockers(self, status):
        entries = status["blockers"]
        self.assertEqual(len(entries), 4)
        self.assertEqual([item["key"] for item in entries],
            ["connection", "windows", "operation", "controlled_shutdown"])
        return {item["key"]: item for item in entries}

    def test_status_always_exposes_exactly_four_required_categories(self):
        status = self.monitor.ups_power_status()
        self.assertTrue(status["available"])
        self.assertFalse(any(item["blocked"] for item in self.blockers(status).values()))
        self.monitor.connection = "disconnected"
        status = self.monitor.ups_power_status()
        self.assertFalse(status["available"])
        self.assertTrue(self.blockers(status)["connection"]["blocked"])

    def test_windows_unprepared_blocks_ups_but_still_requests_windows_protection(self):
        self.with_probe(lambda action: {"available": False, "reason": "Hibernación no disponible para esta prueba."})
        status = self.monitor.ups_power_status(refresh_windows=True)
        groups = self.blockers(status)
        self.assertFalse(status["available"])
        self.assertTrue(groups["windows"]["blocked"])
        self.assertIn("Hibernación no disponible", str(groups["windows"]))
        self.assertFalse(groups["connection"]["blocked"])
        for _ in range(6):
            self.fixture.advance()
        self.fixture.usb.submit_shutdown.assert_not_called()
        self.fixture.action.assert_called_once_with("hibernate")
        blocked = [event for event in self.fixture.store.events() if event["kind"] == "ups_poweroff_blocked"]
        self.assertTrue(blocked)
        self.assertIn("Hibernación no disponible", blocked[0]["message"])

    def test_explicit_refresh_discards_cached_windows_readiness(self):
        probe = Mock(return_value={"available": True, "reason": None})
        self.with_probe(probe)
        self.assertTrue(self.monitor.ups_power_status(refresh_windows=True)["available"])
        calls = probe.call_count
        probe.return_value = {"available": False, "reason": "Hibernación deshabilitada tras la consulta."}
        self.assertTrue(self.monitor.ups_power_status()["available"])
        self.assertEqual(probe.call_count, calls)
        status = self.monitor.ups_power_status(refresh_windows=True)
        self.assertFalse(status["available"])
        self.assertTrue(self.blockers(status)["windows"]["blocked"])
        self.assertGreater(probe.call_count, calls)

    def test_action_rechecks_windows_even_if_cached_status_was_available(self):
        probe = Mock(return_value={"available": True, "reason": None})
        self.with_probe(probe)
        for _ in range(5):
            self.fixture.advance()
        self.assertTrue(self.monitor.ups_power_status(refresh_windows=True)["available"])
        calls = probe.call_count
        probe.return_value = {"available": False, "reason": "Windows dejó de admitir la hibernación."}
        self.assertTrue(self.monitor.ups_power_status()["available"])
        self.fixture.advance()
        self.assertGreater(probe.call_count, calls)
        self.fixture.usb.submit_shutdown.assert_not_called()
        self.fixture.action.assert_called_once_with("hibernate")
        block = self.monitor.ups_power_status()["last_block"]
        self.assertIn("Windows dejó de admitir", block["reason"])

    def test_high_load_and_absent_battery_voltage_do_not_create_a_fifth_gate(self):
        self.monitor._process_sample(self.fixture.sample(False, load_percent=100, battery_voltage=None))
        initial = self.monitor.ups_power_status()
        self.assertTrue(initial["available"])
        self.assertFalse(any(item["blocked"] for item in self.blockers(initial).values()))
        for _ in range(6):
            self.fixture.advance(load_percent=100, battery_voltage=None)
        self.fixture.usb.submit_shutdown.assert_called_once_with(60)
        self.fixture.action.assert_called_once_with("hibernate")
        self.assertFalse(any(event["kind"] == "ups_poweroff_blocked" for event in self.fixture.store.events()))

    def test_device_fault_is_only_the_controlled_shutdown_group(self):
        self.monitor.sample = self.fixture.sample(None, fault=True, raw_status="Fault mode", raw_flags={"status_hex": "99"})
        status = self.monitor.ups_power_status()
        groups = self.blockers(status)
        self.assertFalse(status["available"])
        self.assertEqual([key for key, group in groups.items() if group["blocked"]], ["controlled_shutdown"])
        self.assertIn("falla", str(groups["controlled_shutdown"]))

    def test_device_test_is_only_the_pending_operation_group(self):
        self.monitor.sample = self.fixture.sample(None, raw_status="Battery test mode", raw_flags={"status_hex": "8D"})
        status = self.monitor.ups_power_status()
        groups = self.blockers(status)
        self.assertFalse(status["available"])
        self.assertEqual([key for key, group in groups.items() if group["blocked"]], ["operation"])

    def test_stale_critical_sample_does_not_invent_a_current_ups_fault(self):
        self.monitor.sample = self.fixture.sample(True, low_battery=True, raw_flags={"status_hex": "C9"},
                                                  timestamp="2020-01-01T00:00:00Z")
        status = self.monitor.ups_power_status()
        groups = self.blockers(status)
        self.assertFalse(status["available"])
        self.assertEqual([key for key, group in groups.items() if group["blocked"]], ["connection"])


if __name__ == "__main__":
    unittest.main()
