"""Reader supervision without HTTP, real hardware or operating-system actions."""
import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ups_monitor.adapters import TelemetryError
from ups_monitor.core import Store, iso_utc
from ups_monitor.server import Monitor
from ups_monitor import health as health_module


class MonitorHealthTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.action = Mock()
        self.clock = Mock(return_value=100.0)
        self.alerts = Mock()
        self.alerts.status.return_value = {}
        self.adapter = SimpleNamespace(source="test", read=lambda: {
            "timestamp": iso_utc(), "on_battery": True, "low_battery": False})
        self.monitor = Monitor(self.store, self.adapter, action_callback=self.action,
                               alerts=self.alerts, health_clock=self.clock)
        self.engine_clock = Mock(return_value=100.0)
        self.monitor.engine._clock = self.engine_clock

    def tearDown(self):
        self.monitor.stop()
        self.store.close()

    def test_read_metrics_include_failures_recovery_and_serializable_resources(self):
        self.monitor.tick()
        self.clock.return_value = 102
        self.adapter.read = Mock(side_effect=TelemetryError("USB desconectado"))
        self.monitor.tick()
        failed = self.monitor.health_status()
        self.assertEqual((failed["read_successes"], failed["read_failures"], failed["consecutive_failures"]), (1, 1, 1))
        self.clock.return_value = 104
        self.adapter.read = lambda: {"timestamp": iso_utc(), "on_battery": False}
        self.monitor.tick()
        self.clock.return_value = 107
        status = self.monitor.health_status()
        self.assertEqual(status["recoveries"], 1)
        self.assertEqual(status["consecutive_failures"], 0)
        self.assertEqual(status["read_age_seconds"], 3)
        self.assertEqual(status["uptime_seconds"], 7)
        self.assertEqual(status["state"], "healthy")
        self.assertGreater(status["process"]["pid"], 0)
        json.dumps(status, allow_nan=False)
        self.action.assert_not_called()

    def test_independent_supervisor_cancels_stalled_reader_and_notifies_without_panel(self):
        self.monitor.set_policy({"enabled": True, "countdown_seconds": 10})
        self.monitor.tick()
        self.assertEqual(self.monitor.engine.status()["state"], "countdown")
        entered, release, notified, updated = (threading.Event() for _ in range(4))
        self.alerts.update.side_effect = lambda *_args: updated.set()

        def stalled_read():
            entered.set()
            release.wait(5)
            return {"timestamp": iso_utc(), "on_battery": True, "low_battery": False}

        def on_event(kind, *_args):
            if kind == "monitor_error":
                notified.set()

        self.alerts.on_event.side_effect = on_event
        self.adapter.read = stalled_read
        self.monitor.thread = threading.Thread(target=self.monitor.tick)
        self.monitor.thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.clock.return_value = 116
            self.monitor.supervisor_thread = threading.Thread(target=self.monitor.supervise)
            self.monitor.supervisor_thread.start()
            self.assertTrue(notified.wait(2), "No browser request may be required to discover a blocked read")
            self.assertTrue(updated.wait(1))
            self.assertEqual(self.monitor.engine.status()["state"], "unknown")
            self.assertIsNone(self.monitor.sample)
            self.assertEqual(self.monitor.connection, "stale")
            self.assertTrue(self.monitor._health_fault.is_set())
            self.engine_clock.return_value = 120
            release.set()
            self.monitor.thread.join(1)
            self.assertEqual(self.monitor.connection, "connected")
            self.assertEqual(self.monitor.engine.status()["countdown_remaining"], 10,
                             "Recovery must begin a new countdown, never finish the old one")
            self.assertEqual(self.monitor.health_status()["recoveries"], 1)
            self.action.assert_not_called()
        finally:
            self.monitor.stop_event.set()
            release.set()

    def test_live_stalled_reader_is_never_replaced(self):
        self.monitor.thread = Mock(is_alive=Mock(return_value=True))
        self.monitor.health.begin_read()
        self.clock.return_value = 116
        with patch("ups_monitor.server.threading.Thread") as thread_factory:
            self.monitor._supervise_once()
            self.clock.return_value = 180
            self.monitor._supervise_once()
        thread_factory.assert_not_called()
        self.assertEqual(self.monitor.health_status()["reader_restarts"], 0)
        self.assertEqual(self.monitor.error_kind, "backend")
        self.monitor.thread = None
        self.action.assert_not_called()

    def test_dead_reader_is_detected_and_restarted_after_backoff_only(self):
        self.monitor.thread = Mock(is_alive=Mock(return_value=False))
        replacement = Mock(is_alive=Mock(return_value=True))
        with patch("ups_monitor.server.threading.Thread", return_value=replacement) as thread_factory:
            self.monitor._supervise_once()
            self.assertEqual(self.monitor.error_kind, "backend")
            thread_factory.assert_not_called()
            self.clock.return_value = 101
            self.monitor._supervise_once()
            thread_factory.assert_not_called()
            self.clock.return_value = 103
            self.monitor._supervise_once()
            thread_factory.assert_called_once()
            replacement.start.assert_called_once()
            self.monitor._supervise_once()
            thread_factory.assert_called_once()
        self.assertEqual(self.monitor.health_status()["reader_restarts"], 1)
        self.monitor.thread = None
        self.action.assert_not_called()

    def test_supervisor_invalidates_stale_sample_without_a_new_read(self):
        self.monitor.set_policy({"enabled": True, "countdown_seconds": 10})
        self.monitor.tick()
        self.monitor.sample["timestamp"] = iso_utc(datetime.now(timezone.utc) - timedelta(seconds=16))
        self.monitor._supervise_once()
        self.assertEqual(self.monitor.connection, "stale")
        self.assertIsNone(self.monitor.sample)
        self.assertIsNone(self.monitor.engine.status()["countdown_remaining"])
        self.assertIn("connection_lost", [call.args[0] for call in self.alerts.on_event.call_args_list])
        self.action.assert_not_called()

    def test_action_barrier_precedes_busy_decision_lock(self):
        self.monitor.tick()
        held, release = threading.Event(), threading.Event()

        def hold_lock():
            with self.monitor.lock:
                held.set()
                release.wait(2)

        worker = threading.Thread(target=hold_lock)
        worker.start()
        try:
            self.assertTrue(held.wait(1))
            self.monitor.health.begin_read()
            self.clock.return_value = 116
            self.monitor._supervise_once()
            self.assertTrue(self.monitor._health_fault.is_set())
            self.assertIn("monitor_error", [call.args[0] for call in self.alerts.on_event.call_args_list],
                          "Desktop delivery must not wait for the busy decision lock")
            with self.assertRaisesRegex(RuntimeError, "lector"):
                self.monitor._act("shutdown")
            self.action.assert_not_called()
        finally:
            release.set()
            worker.join(2)

    def test_start_owns_polling_consumer_supervisor_and_stop_finishes_all(self):
        self.adapter.read = lambda: {"timestamp": iso_utc(), "on_battery": False}
        self.monitor.start()
        self.assertTrue(self.monitor.thread.is_alive())
        self.assertTrue(self.monitor.processor_thread.is_alive())
        self.assertTrue(self.monitor.supervisor_thread.is_alive())
        self.monitor.stop()
        self.assertFalse(self.monitor.thread.is_alive())
        self.assertFalse(self.monitor.processor_thread.is_alive())
        self.assertFalse(self.monitor.supervisor_thread.is_alive())
        self.assertEqual(self.monitor.health_status()["state"], "stopped")
        self.assertFalse(self.monitor.engine.policy["enabled"])
        self.action.assert_not_called()

    def test_dead_supervisor_is_degraded_and_blocks_action_even_with_fresh_telemetry(self):
        self.monitor.tick()
        self.monitor.supervisor_thread = Mock(is_alive=Mock(return_value=False))
        status = self.monitor.health_status()
        self.assertEqual(status["state"], "degraded")
        self.assertIn("supervisor", status["message"])
        with self.assertRaisesRegex(RuntimeError, "supervisión"):
            self.monitor._act("shutdown")
        self.action.assert_not_called()

    def test_failure_cause_precedes_policy_suspension_in_incident_timeline(self):
        self.monitor.set_policy({"enabled": True, "countdown_seconds": 10})
        self.monitor.tick()
        self.adapter.read = Mock(side_effect=TelemetryError("USB desconectado"))
        self.monitor.tick()
        kinds = [event["kind"] for event in reversed(self.store.events())]
        self.assertLess(max(index for index, kind in enumerate(kinds) if kind == "connection_lost"),
                        max(index for index, kind in enumerate(kinds) if kind == "policy_unknown"))
        self.assertIsNone(self.monitor.engine.status()["countdown_remaining"])
        self.action.assert_not_called()

    def test_windows_memory_bindings_are_reused_across_status_queries(self):
        with patch.object(health_module.os, "name", "nt"), \
             patch.object(health_module, "_memory_reader", health_module._MEMORY_UNINITIALIZED), \
             patch.object(health_module, "_make_memory_reader", return_value=lambda: 10_000) as bind:
            for _ in range(50):
                self.assertEqual(health_module.working_set_bytes(), 10_000)
            bind.assert_called_once()


if __name__ == "__main__":
    unittest.main()
