"""Accelerated observation tests; no UPS, SQLite or Windows actions are used."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ups_monitor.stability import StabilityObservation
from ups_monitor.core import Store
from ups_monitor.server import Monitor


class Clock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        return self.value


class StabilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.clock = Clock()
        self.health = {"state": "healthy", "reader_alive": True, "supervisor_alive": True,
                       "read_age_seconds": 1, "read_failures": 0,
                       "process": {"memory_bytes": 25_000_000, "cpu_percent": 0.2},
                       "csrf_token": "secret-token-not-for-report", "local_path": "C:\\private\\file"}
        self.history = {"available": True, "dropped": 0}
        self.monitor = SimpleNamespace(engine=SimpleNamespace(policy={"enabled": False}), dry_run=True,
                                       health_status=Mock(side_effect=lambda: dict(self.health)),
                                       history_status=Mock(side_effect=lambda: dict(self.history)),
                                       snapshot=Mock(side_effect=AssertionError("Panel snapshot is forbidden")),
                                       adapter=SimpleNamespace(read=Mock()), action_callback=Mock())
        self.observer = StabilityObservation(self.monitor, self.directory.name, clock=self.clock)

    def tearDown(self):
        self.observer.close()
        self.directory.cleanup()

    def wait_finished(self, observer):
        observer.thread.join(3)
        self.assertFalse(observer.thread.is_alive())

    def test_accelerated_completion_requires_review_and_persists_filtered_report(self):
        def read_health():
            self.clock.value += 1
            self.health["read_failures"] += 1
            return dict(self.health)

        self.monitor.health_status.side_effect = read_health
        self.observer = StabilityObservation(self.monitor, self.directory.name, clock=self.clock, interval_seconds=0.001)
        self.observer.start(hours=0.01)
        self.wait_finished(self.observer)
        status = self.observer.status()
        self.assertEqual(status["state"], "completed_pending_review")
        self.assertGreaterEqual(status["elapsed_seconds"], 36)
        self.assertEqual(status["read_failures"], status["samples"] - 1)
        report = self.observer.report()
        self.assertTrue(report["review_required"])
        self.assertTrue(report["coverage_complete"])
        self.assertEqual(report["validation"], "pending_review")
        files = list(Path(self.directory.name).glob("*.json"))
        self.assertEqual(len(files), 1)
        saved = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(saved["summary"]["state"], "completed_pending_review")
        payload = json.dumps(saved)
        for forbidden in ("secret-token", "private", self.directory.name):
            self.assertNotIn(forbidden, payload)
        self.monitor.snapshot.assert_not_called()
        self.monitor.adapter.read.assert_not_called()
        self.monitor.action_callback.assert_not_called()
        self.assertFalse(self.monitor.engine.policy["enabled"])

    def test_status_is_read_only_even_when_target_time_elapsed(self):
        self.observer.start(0.01)
        deadline = time.monotonic() + 1
        while self.observer.status()["samples"] < 1 and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertGreater(self.observer.status()["samples"], 0)
        self.clock.value = 100
        with patch.object(self.observer, "_persist") as persist:
            for _ in range(10):
                self.assertEqual(self.observer.status()["state"], "running")
            persist.assert_not_called()
        stopped = self.observer.stop()
        self.assertEqual(stopped["state"], "interrupted")
        self.assertFalse(self.observer.report()["coverage_complete"])

    def test_initial_boot_wait_is_recorded_without_a_false_stale_read(self):
        self.health.update(state="starting", read_successes=0, read_age_seconds=None)
        self.observer._started_monotonic = self.clock()
        self.observer._collect()
        issues = self.observer.report()["issues"]
        self.assertEqual(issues["awaiting_first_read"], 1)
        self.assertNotIn("stale_read", issues)
        self.assertNotIn("monitor_degraded", issues)
        self.clock.value = 16
        self.observer._collect()
        self.assertIn("stale_read", self.observer.report()["issues"])

    def test_stopped_monitor_cannot_start_a_new_observation(self):
        self.monitor.stop_event = threading.Event()
        self.monitor.stop_event.set()
        with self.assertRaisesRegex(ValueError, "deteniendo"):
            self.observer.start()
        self.assertEqual(self.observer.status()["state"], "idle")

    def test_disk_failure_is_isolated_and_exception_payload_is_not_exported(self):
        def read_health():
            self.clock.value += 20
            return dict(self.health)

        self.monitor.health_status.side_effect = read_health
        self.observer = StabilityObservation(self.monitor, self.directory.name, clock=self.clock, interval_seconds=0.001)
        with patch("ups_monitor.stability.Path.mkdir", side_effect=OSError("C:\\private\\secret")):
            self.observer.start(0.01)
            self.wait_finished(self.observer)
        self.assertIn("OSError", self.observer.status()["last_error"])
        self.assertNotIn("private", json.dumps(self.observer.report()))
        self.assertEqual(self.observer.status()["state"], "completed_pending_review")
        self.monitor.action_callback.assert_not_called()

    def test_report_io_cannot_hold_status_lock_and_close_has_a_bounded_wait(self):
        entered, release = threading.Event(), threading.Event()
        original = Path.open

        def blocked_open(path, *args, **kwargs):
            entered.set()
            release.wait(3)
            return original(path, *args, **kwargs)

        with patch("ups_monitor.stability.Path.open", blocked_open):
            self.observer.start(48)
            try:
                self.assertTrue(entered.wait(1))
                started = time.monotonic()
                self.assertEqual(self.observer.status()["state"], "running")
                self.assertFalse(self.observer.close(timeout=0.01))
                self.assertLess(time.monotonic() - started, 0.2)
                self.assertEqual(self.observer.status()["state"], "interrupted")
            finally:
                release.set()
                self.wait_finished(self.observer)

    def prepare_manual(self):
        self.observer._state = "running"
        self.observer._started_monotonic = self.clock()
        self.observer._started_at = "2026-10-17T00:00:00+00:00"
        self.observer._target_seconds = 36

    def test_counters_use_deltas_and_gaps_and_policy_changes_remain_visible(self):
        self.prepare_manual()
        self.health["read_failures"], self.history["dropped"] = 4, 2
        self.observer._collect()
        self.clock.value = 60
        self.health["read_failures"], self.history["dropped"] = 6, 5
        self.monitor.engine.policy["enabled"], self.monitor.dry_run = True, False
        self.observer._collect()
        self.clock.value = 260
        self.health["read_failures"], self.history["dropped"] = 1, 0
        self.observer._collect()
        self.observer._finish("completed_pending_review")
        report = self.observer.report()
        self.assertEqual(report["summary"]["read_failures"], 3)
        self.assertEqual(report["summary"]["history_dropped"], 3)
        self.assertEqual(report["sample_gap_count"], 1)
        self.assertIn("counter_reset", report["issues"])
        self.assertIn("real_actions_enabled", report["unprotected_conditions"])
        self.assertFalse(report["safe_test_conditions"])
        self.assertFalse(report["coverage_complete"])
        self.assertTrue(self.monitor.engine.policy["enabled"], "Observation never changes policy")
        self.monitor.action_callback.assert_not_called()

    def test_bounded_records_preserve_totals_and_first_and_final_observations(self):
        self.prepare_manual()
        for index in range(6001):
            self.clock.value = index * 60
            self.observer._collect()
        report = self.observer.report()
        rows = report["observations"]["rows"]
        self.assertLessEqual(len(rows), 5000)
        self.assertEqual(report["summary"]["samples"], 6001)
        self.assertEqual(rows[0][0], 0)
        self.assertEqual(rows[-1][0], 6000 * 60)
        self.assertGreater(report["observations"]["record_stride"], 1)

    def test_unavailable_health_is_explicit_and_missing_resource_metrics_stay_null(self):
        self.prepare_manual()
        self.monitor.health_status.side_effect = RuntimeError("secret-token")
        self.observer._collect()
        status = self.observer.status()
        self.assertIsNone(status["memory_peak_bytes"])
        self.assertIsNone(status["cpu_peak_percent"])
        self.assertIsNone(status["max_read_age_seconds"])
        self.assertIn("health_unavailable", self.observer.report()["issues"])
        self.assertNotIn("secret-token", json.dumps(self.observer.report()))

    def test_new_observations_keep_old_files_and_reject_duplicate_workers(self):
        self.observer.start(48)
        with self.assertRaisesRegex(ValueError, "ejecución"):
            self.observer.start(48)
        self.observer.stop()
        old_id = self.observer.report()["report_id"]
        self.observer.start(48)
        self.observer.stop()
        self.assertNotEqual(self.observer.report()["report_id"], old_id)
        self.assertEqual(len(list(Path(self.directory.name).glob("*.json"))), 2)

    def test_invalid_duration_does_not_start_thread_or_write_files(self):
        for hours in (True, False, 0, 0.009, 169, "48", float("nan"), float("inf")):
            with self.subTest(hours=hours), self.assertRaises(ValueError):
                self.observer.start(hours)
        self.assertEqual(self.observer.status()["state"], "idle")
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_policy_activation_between_samples_is_latched_without_io(self):
        self.prepare_manual()
        self.observer._collect()
        with patch.object(self.observer, "_persist") as persist:
            self.observer.record_policy_change(True, False)
            self.observer.record_policy_change(False, True)
            persist.assert_not_called()
        self.clock.value = 60
        self.observer._collect()
        report = self.observer.report()
        self.assertFalse(report["safe_test_conditions"])
        self.assertIn("policy_enabled", report["unprotected_conditions"])
        self.assertIn("real_actions_enabled", report["unprotected_conditions"])
        self.assertFalse(self.monitor.engine.policy["enabled"])
        self.assertEqual(report["snapshot_semantics"], "last_recorded_state")
        self.assertIn("running", report["limits"]["crash_reporting"])

    def test_policy_hook_ignores_changes_outside_running_observation(self):
        self.observer.record_policy_change(True, False)
        self.assertTrue(self.observer.report()["safe_test_conditions"])
        self.prepare_manual()
        self.observer._finish("interrupted")
        self.observer.record_policy_change(True, False)
        self.assertTrue(self.observer.report()["safe_test_conditions"])

    def test_monitor_setting_changes_latch_activation_before_next_observation(self):
        store = Store(":memory:")
        action = Mock()
        monitor = Monitor(store, SimpleNamespace(source="test", read=Mock()), action_callback=action)
        self.observer = StabilityObservation(monitor, self.directory.name, clock=self.clock)
        monitor.observation = self.observer
        self.prepare_manual()
        try:
            monitor.set_policy({"enabled": True})
            monitor.set_policy({"enabled": False})
            self.assertEqual(self.observer.status()["samples"], 0)
            self.assertFalse(self.observer.report()["safe_test_conditions"])
            self.assertIn("real_actions_enabled", self.observer.report()["unprotected_conditions"])
            self.assertFalse(monitor.engine.policy["enabled"])
            self.assertFalse(store.get_setting("policy")["enabled"])
            action.assert_not_called()
        finally:
            monitor.stop()
            store.close()


if __name__ == "__main__":
    unittest.main()
