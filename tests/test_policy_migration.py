"""Upgrade old timers with durable review and no USB or Windows actions."""
from datetime import datetime, timedelta, timezone
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from ups_monitor.alerts import AlertManager
from ups_monitor.core import DEFAULT_POLICY, Store, iso_utc
from ups_monitor.persistence import HistoryUnavailable, ResilientStore
from ups_monitor.server import Monitor


class PolicyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.backend = Store(":memory:")
        self.addCleanup(self.backend.close)
        self.backend.set_setting("policy", {**DEFAULT_POLICY, "enabled": True,
            "on_battery_seconds": 300, "countdown_seconds": 30})
        self.adapter = SimpleNamespace(source="migration fixture", bridge_required=False,
            read=Mock(side_effect=AssertionError("No hardware access allowed")))
        self.action = Mock(side_effect=AssertionError("No Windows action allowed"))

    def alerts(self, store):
        notifier = SimpleNamespace(start=Mock(), stop=Mock(), notify=Mock(return_value=True),
            sync_persistent=Mock(), status=Mock(return_value={"state": "running"}))
        alerts = AlertManager(store, notifier)
        self.addCleanup(alerts.stop)
        return alerts

    def test_review_notice_survives_recovery_restart_and_understood_without_being_recreated(self):
        alerts = self.alerts(self.backend)
        monitor = Monitor(self.backend, self.adapter, action_callback=self.action, alerts=alerts)
        self.assertFalse(monitor.engine.policy["enabled"])
        notice = alerts.status()["pending"][0]
        self.assertEqual(notice["kind"], "policy_migration_review")
        alerts.update({"state": "disabled"}, "connected", {"timestamp": iso_utc(), "on_battery": False})
        self.assertEqual(alerts.status()["pending"][0]["id"], notice["id"])
        reloaded = self.alerts(self.backend)
        Monitor(self.backend, self.adapter, action_callback=self.action, alerts=reloaded)
        self.assertEqual([item["id"] for item in reloaded.status()["pending"]], [notice["id"]])
        reloaded.acknowledge([notice["id"]])
        acknowledged = self.alerts(self.backend)
        reviewed = Monitor(self.backend, self.adapter, action_callback=self.action, alerts=acknowledged)
        self.assertEqual(acknowledged.status()["pending"], [])
        self.assertFalse(reviewed.engine.policy["enabled"])
        reviewed.set_policy({"enabled": True, "countdown_seconds": 60})
        self.assertIsNone(self.backend.get_setting("policy_migration_review"))
        self.assertEqual(self.backend.notification_incidents(), [])
        self.adapter.read.assert_not_called()
        self.action.assert_not_called()

    def test_policy_and_review_are_atomic_on_disk_failure_for_store_and_facade(self):
        for resilient in (False, True):
            with self.subTest(resilient=resilient):
                backend = Store(":memory:")
                self.addCleanup(backend.close)
                backend.set_setting("policy", {**DEFAULT_POLICY, "enabled": True,
                    "on_battery_seconds": 300, "countdown_seconds": 30})
                store = ResilientStore.open(":memory:", store_factory=lambda path: backend) if resilient else backend
                if resilient:
                    self.addCleanup(store.close)
                monitor = Monitor(store, self.adapter, action_callback=self.action)
                previous, review = store.get_setting("policy"), store.get_setting("policy_migration_review")
                backend._db.execute("CREATE TRIGGER fail_review BEFORE UPDATE ON settings "
                    "WHEN NEW.key='policy_migration_review' BEGIN SELECT RAISE(ABORT,'disk failure'); END")
                with self.assertRaises(HistoryUnavailable):
                    monitor.set_policy({"enabled": True, "countdown_seconds": 60})
                self.assertEqual(backend.get_setting("policy"), previous)
                self.assertEqual(backend.get_setting("policy_migration_review"), review)
                self.assertEqual(store.get_setting("policy"), previous)
                self.assertFalse(monitor.engine.policy["enabled"])
                backend._db.execute("DROP TRIGGER fail_review")
                monitor.set_policy({"enabled": True, "countdown_seconds": 60})
                restarted = Monitor(store, self.adapter, action_callback=self.action)
                self.assertTrue(restarted.engine.policy["enabled"])
                self.assertFalse(restarted._policy_review_required)
                self.assertIsNone(backend.get_setting("policy_migration_review"))

    def test_acknowledged_unresolved_review_survives_history_retention(self):
        stamp = iso_utc(datetime.now(timezone.utc) - timedelta(days=60))
        self.backend.put_notification({"id": 1, "kind": "policy_migration_review", "title": "Revisa protección",
            "message": "Revisa el tiempo", "severity": "warning", "timestamp": stamp,
            "acknowledged_at": stamp, "resolved_at": None})
        self.backend._last_prune = 0
        self.backend._prune()
        self.assertEqual([item["id"] for item in self.backend.notification_incidents()], [1])


if __name__ == "__main__":
    unittest.main()
