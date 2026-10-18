import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ups_monitor.alerts import DEFAULT_NOTIFICATION_SETTINGS
from ups_monitor.core import DEFAULT_POLICY, Store, iso_utc
from ups_monitor.persistence import HistoryUnavailable, ResilientStore


def eventually(predicate, timeout=1.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("La operación asíncrona no terminó dentro del plazo")


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.backends = []

    def tearDown(self):
        for value in self.backends:
            value.close(timeout=0.2)

    def memory(self, **options):
        backend = Store(":memory:", busy_timeout_ms=100)
        result = ResilientStore.open(":memory:", store_factory=lambda path: backend,
                                     retry_seconds=0.02, max_retry_seconds=0.05, **options)
        self.backends.append(result)
        return result, backend

    def test_async_sample_event_preserve_timestamp_and_flush(self):
        store, backend = self.memory()
        stamp = "2026-10-17T12:30:00Z"
        value = store.add_event("power_lost", "Corte", {"on_battery": True}, timestamp=stamp)
        self.assertLess(value["id"], 0)
        self.assertEqual(store.events()[0]["timestamp"], stamp)
        store.add_sample({"timestamp": iso_utc(), "on_battery": True, "battery_voltage": 26.1})
        store.start()
        eventually(lambda: store.status()["pending"] == 0)
        self.assertGreater(store.events()[0]["id"], 0)
        self.assertEqual(backend.events()[0]["timestamp"], stamp)
        self.assertEqual(len(backend.history()), 1)
        self.assertTrue(store.close())

    def test_slow_worker_never_blocks_memory_events_or_telemetry(self):
        store, backend = self.memory()
        entered, release = threading.Event(), threading.Event()
        original = backend.add_sample

        def stalled(sample):
            entered.set()
            release.wait(2)
            return original(sample)

        backend.add_sample = stalled
        store.start()
        store.add_sample({"timestamp": iso_utc(), "on_battery": True})
        self.assertTrue(entered.wait(1))
        try:
            started = time.monotonic()
            for number in range(20):
                store.add_event("power_lost", str(number))
                store.add_sample({"timestamp": iso_utc(), "on_battery": True})
                self.assertTrue(store.events())
                self.assertIsNotNone(store.status())
                self.assertFalse(store.get_setting("policy")["enabled"])
            self.assertLess(time.monotonic() - started, 0.2)
            with self.assertRaises(HistoryUnavailable):
                store.set_setting("policy", {**DEFAULT_POLICY, "enabled": True})
            self.assertFalse(store.get_setting("policy")["enabled"])
        finally:
            release.set()

    def test_disk_full_keeps_recent_event_and_retries_in_original_order(self):
        store, backend = self.memory()
        original = backend.add_event
        backend.add_event = Mock(side_effect=sqlite3.OperationalError("database or disk is full"))
        store.start()
        store.add_event("power_lost", "Corte")
        store.add_event("battery_low", "Batería baja")
        eventually(lambda: store.status()["last_error"] is not None)
        self.assertEqual(store.status()["state"], "degraded")
        self.assertEqual(store.status()["pending"], 2)
        self.assertEqual([row["kind"] for row in store.events()], ["battery_low", "power_lost"])
        backend.add_event = original
        eventually(lambda: store.status()["pending"] == 0)
        self.assertTrue(store.status()["available"])
        self.assertIsNone(store.status()["last_error"])
        self.assertEqual([row["kind"] for row in backend.events()], ["battery_low", "power_lost"])

    def test_actual_sqlite_lock_does_not_block_queue_and_recovers(self):
        test_root = Path(__file__).resolve().parents[1] / ".test-data"
        test_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_root) as directory:
            path = Path(directory) / "monitor.sqlite3"
            store = ResilientStore.open(path, retry_seconds=0.02, max_retry_seconds=0.05)
            self.backends.append(store)
            locker = sqlite3.connect(path)
            locker.execute("BEGIN IMMEDIATE")
            store.start()
            try:
                started = time.monotonic()
                store.add_event("power_lost", "Corte")
                self.assertLess(time.monotonic() - started, 0.1)
                eventually(lambda: "locked" in (store.status()["last_error"] or ""))
                self.assertEqual(store.status()["pending"], 1)
                self.assertEqual(store.events()[0]["kind"], "power_lost")
            finally:
                locker.rollback()
                locker.close()
            eventually(lambda: store.status()["pending"] == 0)
            self.assertTrue(store.close())

    def test_startup_failure_uses_safe_cached_defaults_and_recovery_does_not_enable(self):
        backend = Store(":memory:")
        backend.set_setting("policy", {**DEFAULT_POLICY, "enabled": True})
        ready = threading.Event()

        def factory(path):
            if not ready.is_set():
                raise sqlite3.OperationalError("unable to open database file")
            return backend

        store = ResilientStore.open("unavailable", store_factory=factory,
                                    retry_seconds=0.02, max_retry_seconds=0.05)
        self.backends.append(store)
        self.assertFalse(store.get_setting("policy")["enabled"])
        self.assertEqual(store.get_setting("notification_settings"), DEFAULT_NOTIFICATION_SETTINGS)
        store.start()
        ready.set()
        store.add_event("power_lost", "Corte")
        eventually(lambda: store.status()["available"])
        self.assertFalse(store.get_setting("policy")["enabled"])
        self.assertTrue(backend.get_setting("policy")["enabled"])

    def test_unreadable_or_invalid_initial_policy_is_disabled(self):
        for value in (sqlite3.DatabaseError("settings damaged"), {"enabled": "true"}):
            store, backend = self.memory()
            store.close()
            backend = Mock()
            backend.events.return_value = []

            def read(key, default):
                if key == "policy":
                    if isinstance(value, Exception):
                        raise value
                    return value
                return default

            backend.get_setting.side_effect = read
            result = ResilientStore.open("fake", store_factory=lambda path: backend)
            self.backends.append(result)
            self.assertEqual(result.get_setting("policy"), DEFAULT_POLICY)
            self.assertFalse(result.status()["available"])

    def test_failed_settings_change_does_not_mutate_cache(self):
        store, backend = self.memory()
        with patch.object(backend, "set_setting", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(HistoryUnavailable):
                store.set_setting("policy", {**DEFAULT_POLICY, "enabled": True})
        self.assertFalse(store.get_setting("policy")["enabled"])
        self.assertFalse(backend.get_setting("policy", DEFAULT_POLICY)["enabled"])

    def test_settings_and_event_reads_are_copied(self):
        store, backend = self.memory()
        settings = store.get_setting("policy")
        settings["enabled"] = True
        self.assertFalse(store.get_setting("policy")["enabled"])
        store.add_event("power_lost", "Corte", {"source": "real"})
        store.events()[0]["data"]["source"] = "changed"
        self.assertEqual(store.events()[0]["data"]["source"], "real")

    def test_queue_is_bounded_and_prioritizes_events(self):
        store, backend = self.memory(capacity=2)
        store.add_sample({"timestamp": iso_utc(), "on_battery": True})
        store.add_event("power_lost", "Corte")
        store.add_event("battery_low", "Batería baja")
        store.add_sample({"timestamp": iso_utc(), "on_battery": True})
        self.assertEqual(store.status()["pending"], 2)
        self.assertEqual(store.status()["dropped"], 2)
        store.start()
        eventually(lambda: store.status()["pending"] == 0)
        self.assertEqual([row["kind"] for row in backend.events()], ["battery_low", "power_lost"])
        self.assertIn("algunas entradas", store.status()["message"])

    def test_history_failure_is_explicit_but_status_events_keep_working(self):
        store, backend = self.memory()
        store.add_event("power_lost", "Corte")
        backend.history = Mock(side_effect=sqlite3.DatabaseError("malformed"))
        backend.iter_samples = Mock(side_effect=sqlite3.DatabaseError("malformed"))
        with self.assertRaises(HistoryUnavailable):
            store.history()
        with self.assertRaises(HistoryUnavailable):
            list(store.iter_samples())
        self.assertEqual(store.events()[0]["kind"], "power_lost")
        self.assertEqual(store.status()["state"], "degraded")

    def test_corrupt_sample_payload_is_history_failure_not_invalid_request(self):
        store, backend = self.memory()
        backend._db.execute("INSERT INTO samples(ts,timestamp,payload) VALUES(?,?,?)",
                            (time.time(), iso_utc(), "{broken"))
        backend._db.commit()
        with self.assertRaises(HistoryUnavailable) as failure:
            store.history()
        self.assertIsInstance(failure.exception.__cause__, json.JSONDecodeError)
        self.assertFalse(store.status()["available"])
        with self.assertRaises(HistoryUnavailable) as failure:
            list(store.iter_samples())
        self.assertIsInstance(failure.exception.__cause__, json.JSONDecodeError)

    def test_export_releases_storage_between_pages(self):
        store, backend = self.memory()
        for number in range(501):
            backend.add_sample({"timestamp": iso_utc(), "on_battery": False,
                                "battery_voltage": number})
        iterator = store.iter_samples()
        self.assertEqual(next(iterator)["battery_voltage"], 0)
        store.set_setting("new", {"saved": True})
        self.assertEqual(len(list(iterator)), 500)

    def test_successful_history_reads_cannot_hide_a_write_failure(self):
        store, backend = self.memory()
        backend.add_event = Mock(side_effect=sqlite3.OperationalError("disk full"))
        store.start()
        store.add_event("power_lost", "Corte")
        eventually(lambda: store.status()["last_error"] is not None)
        self.assertEqual(store.history(), [])
        store.set_setting("notification_settings", DEFAULT_NOTIFICATION_SETTINGS)
        self.assertFalse(store.status()["available"])
        self.assertEqual(store.status()["last_error"], "disk full")
        self.assertEqual(store.status()["pending"], 1)

    def test_recovery_keeps_startup_configuration_warning_visible(self):
        backend = Store(":memory:")
        original = backend.get_setting
        backend.get_setting = Mock(side_effect=sqlite3.DatabaseError("cannot read settings"))
        store = ResilientStore.open(":memory:", store_factory=lambda path: backend,
                                    retry_seconds=0.02, max_retry_seconds=0.05)
        self.backends.append(store)
        backend.get_setting = original
        store.start()
        eventually(lambda: store.status()["available"])
        self.assertIn("predeterminados", store.status()["configuration_warning"])
        self.assertFalse(store.get_setting("policy")["enabled"])

    def test_events_never_enter_database_after_initial_load(self):
        store, backend = self.memory()
        with patch.object(backend, "events", side_effect=AssertionError("no SQL here")), \
                patch.object(backend, "get_setting", side_effect=AssertionError("no SQL here")):
            store.start()
            self.assertEqual(store.events(), [])
            self.assertFalse(store.get_setting("policy")["enabled"])
            self.assertEqual(store.get_setting("future", 123), 123)

    def test_close_timeout_never_closes_connection_while_worker_is_using_it(self):
        store, backend = self.memory()
        entered, release = threading.Event(), threading.Event()
        original_write, original_close = backend.add_sample, backend.close
        closed = threading.Event()

        def stalled(sample):
            entered.set()
            release.wait(2)
            return original_write(sample)

        def close():
            closed.set()
            original_close()

        backend.add_sample, backend.close = stalled, close
        store.start()
        store.add_sample({"timestamp": iso_utc(), "on_battery": True})
        self.assertTrue(entered.wait(1))
        try:
            started = time.monotonic()
            self.assertFalse(store.close(timeout=0.03))
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertFalse(closed.is_set())
        finally:
            release.set()
        eventually(closed.is_set)
        self.assertEqual(store.status()["state"], "stopped")


if __name__ == "__main__":
    unittest.main()
