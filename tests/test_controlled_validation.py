"""The dashboard's rule check must remain isolated from the real monitor."""
import copy
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ups_monitor.core import Store
from ups_monitor.server import Monitor
from ups_monitor import validation


class ControlledValidationTests(unittest.TestCase):
    def test_all_six_scenarios_pass_without_device_threads_or_windows_executor(self):
        stores = []

        def isolated_store(path):
            self.assertEqual(path, ":memory:")
            store = Store(path)
            stores.append(store)
            return store

        with patch("ups_monitor.validation.Store", side_effect=isolated_store), \
             patch("ups_monitor.server.windows_action", side_effect=AssertionError("Windows must remain untouched")) as action, \
             patch("ups_monitor.server.make_adapter", side_effect=AssertionError("No live adapter")) as adapter, \
             patch("ups_monitor.server.WindowsNotifier", side_effect=AssertionError("No desktop backend")) as notifier, \
             patch("ups_monitor.server.Monitor.start", side_effect=AssertionError("No monitor worker")) as start, \
             patch("ups_monitor.server.MonitorInstanceGuard.acquire", side_effect=AssertionError("No ownership mutation")) as owner, \
             patch("ups_monitor.server.threading.Thread", side_effect=AssertionError("No background thread")) as thread:
            result = validation.run_controlled()
        self.assertTrue(result["passed"], result["scenarios"])
        self.assertEqual(len(result["scenarios"]), 6)
        self.assertEqual({row["name"] for row in result["scenarios"]}, {
            "cancel_countdown", "restored_mains", "stale_blocks_action", "communication_recovery",
            "dry_run_blocks_action", "disabled_blocks_action"})
        self.assertEqual(result["os_actions_executed"], 0)
        self.assertFalse(result["live_settings_changed"])
        self.assertEqual(result["physical_usb_test"], "pending_user_test")
        self.assertEqual(result["physical_shutdown_test"], "not_tested_disabled")
        for unused in (action, adapter, notifier, start, owner, thread):
            unused.assert_not_called()
        self.assertEqual(len(stores), 6)
        for store in stores:
            with self.assertRaises(sqlite3.ProgrammingError):
                store._db.execute("SELECT 1")

    def test_existing_monitor_state_and_saved_settings_are_unchanged(self):
        store = Store(":memory:")
        adapter = SimpleNamespace(source="existing monitor", read=Mock())
        action = Mock()
        existing = Monitor(store, adapter, dry_run=True, action_callback=action)
        try:
            existing.set_policy({"enabled": False, "countdown_seconds": 42})
            before_policy = copy.deepcopy(existing.engine.policy)
            before_saved = copy.deepcopy(store.get_setting("policy"))
            before_events = copy.deepcopy(store.events())
            result = validation.run_controlled()
            self.assertTrue(result["passed"])
            self.assertEqual(existing.engine.policy, before_policy)
            self.assertEqual(store.get_setting("policy"), before_saved)
            self.assertEqual(store.events(), before_events)
            self.assertIsNone(existing.thread)
            self.assertIsNone(existing.supervisor_thread)
            self.assertFalse(existing.stop_event.is_set())
            adapter.read.assert_not_called()
            action.assert_not_called()
        finally:
            existing.stop()
            store.close()

    def test_action_barrier_regression_is_reported_without_executing_windows(self):
        def bypass_barrier(monitor, action):
            monitor.action_callback(action)

        with patch("ups_monitor.server.Monitor._act", bypass_barrier), \
             patch("ups_monitor.server.windows_action", side_effect=AssertionError("Forbidden real action")) as action:
            report = validation.run_controlled()
        self.assertFalse(report["passed"])
        scenario = next(row for row in report["scenarios"] if row["name"] == "dry_run_blocks_action")
        self.assertFalse(scenario["passed"])
        self.assertTrue(scenario["detail"])
        action.assert_not_called()
        self.assertEqual(report["os_actions_executed"], 0)

    def test_failure_marker_is_kept_and_other_scenarios_continue(self):
        def fail(*_args):
            raise AssertionError("Controlled scenario failure marker")

        scenarios = (("failed_case", "Fallo simulado", fail),
                     ("healthy_case", "Caso correcto", lambda *_args: "Still executed"))
        with patch.object(validation, "_SCENARIOS", scenarios):
            report = validation.run_controlled()
        self.assertFalse(report["passed"])
        self.assertEqual(len(report["scenarios"]), 2)
        self.assertIn("failure marker", report["scenarios"][0]["detail"])
        self.assertFalse(report["scenarios"][0]["passed"])
        self.assertTrue(report["scenarios"][1]["passed"])

    def test_concurrent_check_is_rejected_and_gate_is_released_after_unexpected_error(self):
        self.assertTrue(validation._gate.acquire(blocking=False))
        try:
            with patch.object(validation, "_scenario") as scenario, self.assertRaisesRegex(ValueError, "curso"):
                validation.run_controlled()
            scenario.assert_not_called()
        finally:
            validation._gate.release()
        with patch.object(validation, "iso_utc", side_effect=RuntimeError("Unexpected report failure")), \
             self.assertRaises(RuntimeError):
            validation.run_controlled()
        self.assertTrue(validation.run_controlled()["passed"])


if __name__ == "__main__":
    unittest.main()
