"""Storage failure injection; all power actions and desktop delivery are fakes."""
import io
import sqlite3
import threading
import time
import unittest
from email.message import Message
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ups_monitor.alerts import AlertManager
from ups_monitor.core import DEFAULT_POLICY, Store, iso_utc
from ups_monitor.instance_guard import AlreadyRunningError
from ups_monitor.persistence import HistoryUnavailable, ResilientStore
from ups_monitor.server import Handler, Monitor, main


class ProtectionIsolationTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.action = Mock()
        self.alerts = Mock()
        self.alerts.status.return_value = {}
        self.adapter = SimpleNamespace(source="test", read=lambda: {
            "timestamp": iso_utc(), "on_battery": True, "low_battery": False})
        self.monitor = Monitor(self.store, self.adapter, action_callback=self.action, alerts=self.alerts)
        self.clock = Mock(return_value=100)
        self.monitor.engine._clock = self.clock

    def tearDown(self):
        self.monitor.stop()
        self.store.close()

    def test_failed_event_and_sample_writes_do_not_interrupt_countdown_or_alerts(self):
        self.monitor.set_policy({"enabled": True, "countdown_seconds": 10})
        for message in ("database is locked", "database or disk is full"):
            with self.subTest(message=message):
                self.monitor.engine.update_policy({**DEFAULT_POLICY, "enabled": False})
                self.monitor.engine.update_policy({**DEFAULT_POLICY, "enabled": True, "countdown_seconds": 10})
                self.action.reset_mock()
                self.clock.return_value += 20
                with patch.object(self.store, "add_event", side_effect=sqlite3.OperationalError(message)), \
                     patch.object(self.store, "add_sample", side_effect=sqlite3.OperationalError(message)), \
                     self.assertLogs("elipro.monitor", level="ERROR"):
                    self.monitor.tick()
                    self.assertEqual(self.monitor.engine.status()["state"], "countdown")
                    self.clock.return_value += 11
                    self.monitor.tick()
                self.action.assert_called_once_with("shutdown")
                self.assertEqual(self.monitor.connection, "connected")
                self.assertIsNotNone(self.monitor.sample)
        kinds = [call.args[0] for call in self.alerts.on_event.call_args_list]
        self.assertIn("power_lost", kinds)
        self.assertIn("policy_countdown", kinds)

    def test_history_read_failure_keeps_status_and_usb_available(self):
        self.monitor.tick()
        with patch.object(self.store, "events", side_effect=sqlite3.DatabaseError("bad history")), \
             self.assertLogs("elipro.monitor", level="ERROR"):
            result = self.monitor.snapshot()
        self.assertEqual(result["connection"]["state"], "connected")
        self.assertFalse(result["history"]["available"])
        self.assertFalse(result["policy"]["enabled"])
        self.assertIsNotNone(result["sample"])
        self.action.assert_not_called()

    def test_storage_failure_does_not_block_manual_cancellation(self):
        self.monitor.set_policy({"enabled": True, "countdown_seconds": 10})
        self.monitor.tick()
        with patch.object(self.store, "add_event", side_effect=sqlite3.OperationalError("full")), \
             self.assertLogs("elipro.monitor", level="ERROR"):
            result = self.monitor.cancel()
        self.assertEqual(result["protection"]["state"], "cancelled")
        self.clock.return_value += 11
        self.monitor.tick()
        self.action.assert_not_called()

    def test_real_monitor_remains_responsive_while_history_worker_is_stalled(self):
        history = ResilientStore.open(":memory:", store_factory=lambda _: self.store)
        monitor = Monitor(history, self.adapter, action_callback=self.action, alerts=self.alerts)
        clock = Mock(return_value=100)
        monitor.engine._clock = clock
        monitor.set_policy({"enabled": True, "countdown_seconds": 10})
        entered, release = threading.Event(), threading.Event()
        original = self.store.add_sample
        def stalled(sample):
            entered.set()
            release.wait(2)
            return original(sample)
        with patch.object(self.store, "add_sample", side_effect=stalled):
            history.start()
            try:
                monitor.tick()
                self.assertTrue(entered.wait(1))
                clock.return_value = 111
                started = time.monotonic()
                monitor.tick()
                result = monitor.snapshot()
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(result["connection"]["state"], "connected")
                self.assertGreater(result["history"]["pending"], 0)
                self.action.assert_called_once_with("shutdown")
            finally:
                monitor.stop()
                release.set()
                history.close()

    def test_failed_settings_do_not_enable_policy_and_disabling_still_applies(self):
        with patch.object(self.store, "set_setting", side_effect=sqlite3.OperationalError("full")), \
             self.assertLogs("elipro.monitor", level="ERROR"):
            with self.assertRaises(HistoryUnavailable):
                self.monitor.set_policy({"enabled": True})
        self.assertFalse(self.monitor.engine.policy["enabled"])
        self.monitor.engine.update_policy({**DEFAULT_POLICY, "enabled": True})
        with patch.object(self.store, "set_setting", side_effect=sqlite3.OperationalError("full")):
            with self.assertRaisesRegex(HistoryUnavailable, "desactivó en esta sesión"):
                self.monitor.set_policy({"enabled": False})
        self.assertFalse(self.monitor.engine.policy["enabled"])
        self.action.assert_not_called()

    def test_slow_settings_write_does_not_hold_telemetry_lock(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def slow_write(*args):
            entered.set()
            release.wait(2)
        def save():
            try:
                self.monitor.set_policy({"enabled": False})
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()
        with patch.object(self.store, "set_setting", side_effect=slow_write):
            worker = threading.Thread(target=save)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(self.monitor.lock.acquire(timeout=0.1))
                self.monitor.lock.release()
                self.monitor.tick()
                self.assertEqual(self.monitor.connection, "connected")
            finally:
                release.set()
                worker.join(2)
        self.assertTrue(finished.is_set())
        self.assertEqual(errors, [])
        self.action.assert_not_called()

    def test_unreadable_startup_settings_use_disabled_policy(self):
        with patch.object(self.store, "get_setting", side_effect=sqlite3.DatabaseError("bad settings")), \
             self.assertLogs("elipro.monitor", level="ERROR"):
            monitor = Monitor(self.store, self.adapter, action_callback=self.action)
        self.assertFalse(monitor.engine.policy["enabled"])
        monitor.tick()
        self.action.assert_not_called()

    def test_history_endpoint_returns_specific_unavailable_error(self):
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(server_port=8765, monitor=self.monitor)
        handler.path = "/api/history?hours=1"
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8765"
        handler._json = Mock()
        with patch.object(self.store, "history", side_effect=HistoryUnavailable("Historial no disponible")):
            handler.do_GET()
        self.assertEqual(handler._json.call_args.args[1], 503)
        self.action.assert_not_called()

    def test_second_monitor_is_rejected_before_any_resource_initialization(self):
        guard = Mock()
        guard.acquire.side_effect = AlreadyRunningError("Ya hay un monitor activo")
        with patch("sys.argv", ["elipro", "--port", "8766", "--data-dir", "unused-other-folder"]), \
             patch("ups_monitor.server.MonitorInstanceGuard", return_value=guard), \
             patch("ups_monitor.server.make_adapter") as adapter, \
             patch("ups_monitor.server.ResilientStore.open") as store, \
             patch("ups_monitor.server.WindowsNotifier") as notifier, \
             patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as exit_context:
            main()
        self.assertEqual(exit_context.exception.code, 2)
        adapter.assert_not_called()
        store.assert_not_called()
        notifier.assert_not_called()


class NotificationSettingsIsolationTests(unittest.TestCase):
    def test_blocked_settings_write_does_not_block_desktop_alert_delivery(self):
        store, notifier = Mock(), Mock()
        store.get_setting.return_value = {"enabled": True, "sound_enabled": False, "repeat_seconds": 30}
        notifier.notify.return_value = True
        alerts = AlertManager(store, notifier)
        entered, release = threading.Event(), threading.Event()
        store.set_setting.side_effect = lambda *args: (entered.set(), release.wait(2))
        worker = threading.Thread(target=lambda: alerts.update_settings({"repeat_seconds": 60}))
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(alerts._lock.acquire(timeout=0.1))
            alerts._lock.release()
            alerts.on_event("power_lost", "Prueba sin Windows")
            notifier.notify.assert_called_once()
        finally:
            release.set()
            worker.join(2)
            alerts.stop()


if __name__ == "__main__":
    unittest.main()
