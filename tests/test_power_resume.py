"""Windows sleep boundaries with fake clocks, USB and power actions only."""
import threading
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from ups_monitor.adapters import TelemetryError
from ups_monitor.core import Store, iso_utc
from ups_monitor.server import Monitor


class PowerResumeTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.clock = Mock(return_value=100.0)
        self.action = Mock()
        self.alerts = Mock()
        self.alerts.status.return_value = {}
        self.adapter = SimpleNamespace(source="test", read=Mock(side_effect=self.sample))
        self.monitor = Monitor(self.store, self.adapter, alerts=self.alerts,
                               action_callback=self.action, health_clock=self.clock)
        self.monitor.engine._clock = self.clock
        self.monitor.tick()
        self.alerts.reset_mock()

    def tearDown(self):
        self.monitor.stop()
        self.store.close()

    @staticmethod
    def sample():
        return {"timestamp": iso_utc(), "on_battery": False, "low_battery": False}

    def notices(self):
        return [call.args[0] for call in self.alerts.on_event.call_args_list]

    def sleep_and_resume(self):
        self.monitor.handle_power_event("suspend")
        self.clock.return_value = 200.0
        self.monitor.handle_power_event("resume")

    def test_expected_pause_and_quick_recovery_are_not_communication_incidents(self):
        self.monitor.handle_power_event("suspend")
        self.clock.return_value = 190.0
        self.monitor._supervise_once()
        paused = self.monitor.snapshot()
        self.assertEqual(paused["system_power"]["state"], "suspended")
        self.assertEqual(paused["connection"]["state"], "suspended")
        self.assertIsNone(paused["sample"])
        self.assertEqual(paused["health"]["state"], "suspended")
        self.monitor.handle_power_event("resume")
        self.monitor._supervise_once()
        self.clock.return_value = 190.3
        self.monitor.tick()
        self.assertEqual(self.monitor.snapshot()["system_power"]["state"], "running")
        self.assertEqual(self.monitor.connection, "connected")
        self.assertEqual(self.notices(), [])
        self.assertFalse(any(event["kind"] in ("connection_lost", "monitor_error")
                             for event in self.store.events()))
        self.action.assert_not_called()

    def test_inflight_read_from_before_sleep_cannot_report_a_timeout_on_resume(self):
        entered, release = threading.Event(), threading.Event()

        def old_read():
            entered.set()
            release.wait(2)
            raise TelemetryError("HID diagnóstico excedió 3 segundos")

        self.adapter.read.side_effect = old_read
        worker = threading.Thread(target=self.monitor.tick)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.sleep_and_resume()
            self.monitor._supervise_once()
            release.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.adapter.read.side_effect = self.sample
            self.clock.return_value = 207.0
            self.monitor.tick()
            self.assertEqual(self.monitor.connection, "connected")
            self.assertEqual(self.monitor.health.snapshot()["read_failures"], 0)
            self.assertEqual(self.notices(), [])
        finally:
            release.set()
            worker.join(2)

    def test_old_queued_fresh_sample_cannot_finish_a_pre_sleep_countdown(self):
        self.monitor.set_policy({"enabled": True, "countdown_seconds": 10})
        battery = {**self.sample(), "on_battery": True, "low_battery": True}
        self.monitor._process_sample(battery)
        self.assertEqual(self.monitor.engine.status()["countdown_remaining"], 10)
        self.monitor.telemetry.publish(battery, captured_at=100, power_generation=0)
        self.sleep_and_resume()
        self.monitor._consume_once()
        self.assertIsNone(self.monitor.sample)
        self.assertIsNone(self.monitor.engine.status()["countdown_remaining"])
        self.monitor._process_sample(battery, power_generation=0)
        self.assertIsNone(self.monitor.sample, "A still-fresh timestamp does not cross a sleep boundary")
        self.adapter.read.side_effect = lambda: {**self.sample(), "on_battery": True, "low_battery": True}
        self.monitor.tick()
        self.assertEqual(self.monitor.engine.status()["countdown_remaining"], 10)
        self.action.assert_not_called()

    def test_duplicate_resume_does_not_extend_reconnection_grace(self):
        self.sleep_and_resume()
        self.clock.return_value = 210.0
        self.monitor.handle_power_event("resume")
        self.assertEqual(self.monitor.system_power_status()["generation"], 2)
        self.assertEqual(self.monitor.system_power_status()["resume_remaining"], 5)
        self.monitor._supervise_once()
        self.assertNotIn("monitor_error", self.notices())
        self.clock.return_value = 215.0
        self.monitor._supervise_once()
        self.assertEqual(self.notices().count("connection_lost"), 1)
        self.assertEqual(self.monitor.error_kind, "resume")
        self.assertEqual(self.monitor.connection, "disconnected")
        with self.assertRaisesRegex(RuntimeError, "lecturas nuevas"):
            self.monitor._act("shutdown")
        self.action.assert_not_called()

    def test_transient_usb_error_inside_grace_is_quiet_but_persistent_error_is_not(self):
        self.sleep_and_resume()
        self.adapter.read.side_effect = TelemetryError("USB aún reconectando")
        self.clock.return_value = 207.0
        self.monitor.tick()
        self.assertEqual(self.monitor.connection, "resuming")
        self.assertEqual(self.notices(), [])
        self.clock.return_value = 216.0
        self.monitor._supervise_once()
        self.assertIn("connection_lost", self.notices())
        self.adapter.read.side_effect = self.sample
        self.monitor.tick()
        self.assertIn("connection_restored", self.notices())
        self.assertEqual(self.monitor.connection, "connected")
        self.assertFalse(self.monitor._power_gate.is_set())

    def test_genuine_failure_before_sleep_retains_its_recovery_notification(self):
        self.adapter.read.side_effect = TelemetryError("USB desconectado antes de hibernar")
        self.monitor.tick()
        self.assertIn("connection_lost", self.notices())
        self.sleep_and_resume()
        self.adapter.read.side_effect = self.sample
        self.monitor.tick()
        self.assertEqual(self.notices().count("connection_lost"), 1)
        self.assertEqual(self.notices().count("connection_restored"), 1)

    def test_manual_cancellation_stays_suppressed_after_resume_on_battery(self):
        self.monitor.set_policy({"enabled": True, "countdown_seconds": 10})
        self.adapter.read.side_effect = lambda: {**self.sample(), "on_battery": True, "low_battery": True}
        self.monitor.tick()
        self.monitor.cancel()
        self.sleep_and_resume()
        self.monitor.tick()
        self.assertEqual(self.monitor.engine.status()["state"], "cancelled")
        self.assertIsNone(self.monitor.engine.status()["countdown_remaining"])
        self.action.assert_not_called()

    def test_power_callback_never_waits_for_decisions_and_blocks_actions_immediately(self):
        held, release, handled = threading.Event(), threading.Event(), threading.Event()

        def blocked_decision():
            with self.monitor.lock:
                held.set()
                release.wait(2)

        worker = threading.Thread(target=blocked_decision)
        worker.start()
        callback = threading.Thread(target=lambda: (self.monitor.handle_power_event("suspend"), handled.set()))
        try:
            self.assertTrue(held.wait(1))
            callback.start()
            self.assertTrue(handled.wait(0.5), "The Windows callback must not wait for USB/decisions")
            with self.assertRaisesRegex(RuntimeError, "suspendido"):
                self.monitor._act("hibernate")
            self.action.assert_not_called()
        finally:
            release.set()
            callback.join(1)
            worker.join(2)

    def test_no_power_signal_means_stale_telemetry_still_generates_a_real_alarm(self):
        self.monitor.sample["timestamp"] = iso_utc(datetime.now(timezone.utc) - timedelta(seconds=30))
        self.monitor._supervise_once()
        self.assertIn("connection_lost", self.notices())
        self.assertEqual(self.monitor.connection, "stale")

    def test_duplicate_resume_after_recovery_does_not_invalidate_a_new_sample(self):
        self.sleep_and_resume()
        self.monitor.tick()
        self.monitor.handle_power_event("resume")
        self.assertEqual(self.monitor.system_power_status()["state"], "running")
        self.assertIsNotNone(self.monitor.snapshot()["sample"])
        self.assertFalse(self.monitor._power_gate.is_set())

    def test_stale_sample_during_grace_never_reenables_actions(self):
        self.sleep_and_resume()
        self.adapter.read.side_effect = lambda: {**self.sample(), "timestamp": iso_utc(
            datetime.now(timezone.utc) - timedelta(seconds=30))}
        self.monitor.tick()
        self.assertEqual(self.monitor.system_power_status()["state"], "resuming")
        self.assertIsNone(self.monitor.sample)
        self.assertTrue(self.monitor._power_gate.is_set())
        self.assertEqual(self.notices(), [])

    def test_power_transition_during_action_preparation_blocks_the_windows_request(self):
        original = self.monitor._fresh_sample

        def suspend_during_freshness_check(sample):
            result = original(sample)
            self.monitor.handle_power_event("suspend")
            return result

        self.monitor._fresh_sample = suspend_during_freshness_check
        with self.assertRaisesRegex(RuntimeError, "cambió de estado"):
            self.monitor._act("shutdown")
        self.action.assert_not_called()

    def test_health_result_computed_before_sleep_is_not_reported_after_resume(self):
        self.monitor.sample["timestamp"] = iso_utc(datetime.now(timezone.utc) - timedelta(seconds=30))
        original = self.monitor._health_problem

        def sleep_after_health_check():
            problem = original()
            self.sleep_and_resume()
            return problem

        self.monitor._health_problem = sleep_after_health_check
        self.monitor._supervise_once()
        self.assertEqual(self.monitor.system_power_status()["state"], "resuming")
        self.assertEqual(self.notices(), [])
        self.assertFalse(any(event["kind"] == "connection_lost" for event in self.store.events()))


if __name__ == "__main__":
    unittest.main()
