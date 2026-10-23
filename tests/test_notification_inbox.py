import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ups_monitor.alerts import AlertManager
from ups_monitor.core import Store, iso_utc
from ups_monitor.persistence import ResilientStore
from tests.test_alerts import Clock, Notifier
from tests.test_persistence import eventually


class NotificationInboxTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / ".test-data")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "monitor.sqlite3"
        self.clock = Clock()

    def manager(self, store):
        value = AlertManager(store, Notifier(), clock=self.clock.monotonic, utc_clock=self.clock.utc)
        value.start()
        self.addCleanup(value.stop)
        return value

    def sample(self, **changes):
        return {"timestamp": iso_utc(self.clock.utc()), "on_battery": False,
                "low_battery": False, "load_percent": 10, "fault": False, **changes}

    def test_five_second_outage_survives_recovery_restart_and_selective_ack(self):
        store = Store(self.path)
        first = self.manager(store)
        first.on_event("power_lost", "El UPS usa batería")
        self.clock.advance(5)
        first.on_event("power_restored", "Volvió la red")
        pending = first.status()["pending"]
        self.assertEqual([item["kind"] for item in pending], ["power_lost", "power_restored"])
        self.assertIsNotNone(pending[0]["resolved_at"])
        first.stop()
        store.close()
        reopened = Store(self.path)
        self.addCleanup(reopened.close)
        second = self.manager(reopened)
        self.assertEqual([item["id"] for item in second.status()["pending"]], [item["id"] for item in pending])
        second.acknowledge([pending[0]["id"]])
        self.assertEqual([item["id"] for item in second.status()["pending"]], [pending[1]["id"]])
        second.acknowledge([pending[0]["id"]])  # Lost HTTP replies are idempotent.
        self.assertEqual(len(second.status()["pending"]), 1)

    def test_acknowledged_condition_is_not_rearmed_by_repeat_or_restart(self):
        store = Store(self.path)
        first = self.manager(store)
        first.update({}, "connected", self.sample(low_battery=True))
        notice = first.status()["pending"][0]
        first.acknowledge([notice["id"]])
        self.clock.advance(31)
        first.update({}, "connected", self.sample(low_battery=True))
        self.assertEqual(first.status()["pending"], [])
        first.stop()
        store.close()
        reopened = Store(self.path)
        self.addCleanup(reopened.close)
        second = self.manager(reopened)
        second.update({}, "connected", self.sample(low_battery=True))
        self.assertEqual(second.status()["pending"], [])
        second.update({}, "connected", self.sample())
        self.clock.advance(1)
        second.update({}, "connected", self.sample(low_battery=True))
        self.assertEqual(len(second.status()["pending"]), 1)
        self.assertNotEqual(second.status()["pending"][0]["id"], notice["id"])

    def test_failed_ack_keeps_notice_and_rejects_invalid_ids(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        manager = self.manager(store)
        manager.on_event("power_lost", "Corte")
        notice_id = manager.status()["pending"][0]["id"]
        with patch.object(store, "acknowledge_notifications", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                manager.acknowledge([notice_id])
        self.assertEqual(len(manager.status()["pending"]), 1)
        for ids in ([], [True], [0], [2**53], ["1"], None):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                manager.acknowledge(ids)

    def test_unread_notice_outlives_history_retention_and_desktop_disable(self):
        store = Store(self.path, retention_days=1)
        manager = self.manager(store)
        manager.update_settings({"enabled": False})
        manager.on_event("power_lost", "Corte antiguo")
        self.assertEqual(len(manager.status()["pending"]), 1)
        manager.stop()
        store.close()
        reopened = Store(self.path, retention_days=1)
        self.addCleanup(reopened.close)
        self.assertEqual(len(reopened.pending_notifications()), 1)

    def test_delayed_old_write_cannot_resurrect_acknowledged_notice(self):
        backend = Store(":memory:")
        store = ResilientStore.open(":memory:", store_factory=lambda path: backend)
        self.addCleanup(store.close)
        manager = self.manager(store)
        manager.on_event("power_lost", "Corte")  # Queued, worker not started.
        notice_id = manager.status()["pending"][0]["id"]
        manager.acknowledge([notice_id])  # Commits before the older queued write.
        store.start()
        eventually(lambda: store.status()["pending"] == 0)
        self.assertEqual(backend.pending_notifications(), [])
        self.assertEqual(manager.status()["pending"], [])

    def test_unavailable_startup_recovers_old_unread_without_restart(self):
        original = Store(self.path)
        first = self.manager(original)
        first.on_event("power_lost", "Corte antes del reinicio")
        notice_id = first.status()["pending"][0]["id"]
        first.stop()
        original.close()
        available = threading.Event()
        def factory(path):
            if not available.is_set():
                raise sqlite3.OperationalError("locked")
            return Store(path)
        store = ResilientStore.open(self.path, store_factory=factory,
                                    retry_seconds=.01, max_retry_seconds=.02)
        self.addCleanup(store.close)
        manager = self.manager(store)
        self.assertEqual(manager.status()["pending"], [])
        store.start()
        available.set()
        eventually(lambda: any(item["id"] == notice_id for item in manager.status()["pending"]))

    def test_slow_database_does_not_block_alert_selection(self):
        backend = Store(":memory:")
        store = ResilientStore.open(":memory:", store_factory=lambda path: backend)
        self.addCleanup(store.close)
        entered, release = threading.Event(), threading.Event()
        original = backend.put_notification
        def stalled(record):
            entered.set()
            release.wait(2)
            return original(record)
        backend.put_notification = stalled
        manager = self.manager(store)
        store.start()
        manager.on_event("power_lost", "Corte")
        self.assertTrue(entered.wait(1))
        try:
            start = time.monotonic()
            self.clock.advance(5)
            manager.on_event("power_restored", "Volvió la red")
            manager.update({}, "connected", self.sample())
            self.assertEqual(len(manager.status()["pending"]), 2)
            self.assertLess(time.monotonic() - start, .2)
        finally:
            release.set()

    def test_failed_resolution_of_acknowledged_condition_is_retried(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        manager = self.manager(store)
        manager.update({}, "connected", self.sample(low_battery=True))
        notice_id = manager.status()["pending"][0]["id"]
        manager.acknowledge([notice_id])
        with patch.object(store, "put_notification", side_effect=sqlite3.OperationalError("full")):
            manager.update({}, "connected", self.sample())
        self.assertIn(notice_id, manager._dirty)
        self.clock.advance(31)
        manager.update({}, "connected", self.sample())
        self.assertNotIn(notice_id, manager._dirty)
        self.assertEqual(store.notification_incidents(), [])

    def test_ups_block_is_warning_and_stays_unread_after_recovery_and_restart(self):
        store = Store(self.path)
        first = self.manager(store)
        reason = "El UPS reporta batería crítica; una orden podría cortar inmediatamente. Se prioriza hibernar Windows."
        first.on_event("ups_poweroff_blocked", reason, {"reason_code": "battery_low_or_unknown"})
        block = next(item for item in first.status()["pending"] if item["kind"] == "ups_poweroff_blocked")
        self.assertEqual(block["severity"], "warning")
        self.assertEqual(block["message"], reason)
        self.assertEqual(first.notifier.messages[-1]["severity"], "warning")
        self.clock.advance(5)
        first.on_event("power_restored", "Volvió la electricidad")
        first.update({}, "connected", self.sample())
        self.assertIn(block["id"], {item["id"] for item in first.status()["pending"]})
        first.stop()
        store.close()
        reopened = Store(self.path)
        self.addCleanup(reopened.close)
        second = self.manager(reopened)
        restored = next(item for item in second.status()["pending"] if item["id"] == block["id"])
        self.assertEqual(restored["message"], reason)
        self.assertEqual(restored["severity"], "warning")
        second.acknowledge([block["id"]])
        self.assertNotIn(block["id"], {item["id"] for item in second.status()["pending"]})
        self.assertTrue(any(item["kind"] == "power_restored" for item in second.status()["pending"]))

    def test_new_ups_block_inside_desktop_cooldown_is_durable_after_previous_ack(self):
        store = Store(self.path)
        first = self.manager(store)
        first.on_event("ups_poweroff_blocked", "Primer corte: sobrecarga")
        initial = first.status()["pending"][0]
        first.acknowledge([initial["id"]])
        delivered = len(first.notifier.messages)
        self.clock.advance(5)
        first.on_event("ups_poweroff_blocked", "Segundo corte: se perdió la conexión USB")
        pending = first.status()["pending"]
        self.assertEqual(len(pending), 1)
        self.assertNotEqual(pending[0]["id"], initial["id"])
        self.assertEqual(pending[0]["message"], "Segundo corte: se perdió la conexión USB")
        self.assertEqual(pending[0]["severity"], "warning")
        self.assertEqual(len(first.notifier.messages), delivered, "desktop throttling must not drop a new durable incident")
        second_id = pending[0]["id"]
        first.stop()
        store.close()
        reopened = Store(self.path)
        self.addCleanup(reopened.close)
        second = self.manager(reopened)
        self.assertEqual([item["id"] for item in second.status()["pending"]], [second_id])
        second.acknowledge([second_id])
        self.assertEqual(second.status()["pending"], [])


if __name__ == "__main__":
    unittest.main()
