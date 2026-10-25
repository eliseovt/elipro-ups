"""Priority and freshness contracts without Windows UI or power actions."""
import unittest

from ups_monitor.alerts import AlertManager
from ups_monitor.core import Store, iso_utc
from ups_monitor.notification_priority import age_notice_metadata, normalize_notice_metadata
from ups_monitor.session_broker import SessionBroker
from tests.test_alerts import Clock, Notifier


class PriorityNotifier(Notifier):
    supports_notification_priority = True

    def sync_persistent(self, messages):
        self.live = messages

    def notify(self, title, message, **options):
        self.messages.append({"title": title, "message": message, **options})
        return True


def countdown_metadata(**changes):
    return {"title": "Protección", "message": "Cuenta activa", "priority": 3,
            "severity": "critical", "sound": True, "repeat_seconds": 30,
            "confirmed": True, "confirmed_for_ms": 3000,
            "countdown": {"countdown_id": "a" * 32, "action": "hibernate", "remaining_seconds": 30,
                          "valid_for_ms": 3000, "cancelable": True, **changes}}


class NotificationPriorityTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.notifier = PriorityNotifier()
        self.alerts = AlertManager(self.store, self.notifier, clock=self.clock.monotonic, utc_clock=self.clock.utc)
        self.alerts.start()
        self.addCleanup(self.alerts.stop)

    def sample(self, **changes):
        return {"timestamp": iso_utc(self.clock.utc()), "on_battery": True, "low_battery": False,
                "load_percent": 10, "fault": False, **changes}

    def protection(self, **changes):
        return {"state": "countdown", "countdown_remaining": 30, "action": "hibernate",
                "countdown_id": "a" * 32, "countdown_valid_for_ms": 3000,
                "cancelable": True, **changes}

    def test_cascade_prioritizes_current_countdown_and_preserves_unread_history(self):
        self.alerts.on_event("power_lost", "Corte")
        self.clock.advance(1)
        self.alerts.update({}, "connected", self.sample(battery_percent=10))
        self.clock.advance(1)
        self.alerts.update(self.protection(), "connected", self.sample(battery_percent=10))
        pending = self.alerts.status()["pending"]
        self.assertEqual([item["kind"] for item in pending], ["policy_countdown", "battery_low", "power_lost"])
        self.assertEqual([item["priority"] for item in pending], [3, 2, 1])
        self.assertEqual(list(self.notifier.live), ["notice:" + str(item["id"]) for item in pending])
        self.assertTrue(all(item["acknowledged_at"] is None for item in pending))
        for stored in self.store.pending_notifications():
            self.assertNotIn("countdown", stored)
            self.assertNotIn("priority", stored)
            self.assertNotIn("confirmed_for_ms", stored)

    def test_reads_and_preference_sync_do_not_rejuvenate_countdown(self):
        self.alerts.update(self.protection(), "connected", self.sample())
        self.clock.advance(1)
        first = self.alerts.status()["pending"][0]
        self.assertEqual(first["countdown"]["remaining_seconds"], 29)
        self.assertEqual(first["countdown"]["valid_for_ms"], 2000)
        self.clock.advance(1)
        self.alerts.update_settings({"sound_enabled": False})
        second = self.alerts.status()["pending"][0]
        self.assertEqual(second["countdown"]["valid_for_ms"], 1000)
        self.assertFalse(second["sound"])
        self.clock.advance(1.01)
        expired = self.alerts.status()["pending"][0]
        self.assertEqual(expired["priority"], 1)
        self.assertFalse(expired["confirmed"])
        self.assertFalse(expired["countdown"]["cancelable"])
        self.assertIsNone(expired["resolved_at"])

    def test_same_episode_cannot_extend_and_shortening_preserves_identity(self):
        self.alerts.update(self.protection(), "connected", self.sample())
        self.clock.advance(1)
        self.alerts.update(self.protection(countdown_remaining=30), "connected", self.sample())
        self.assertEqual(self.alerts.status()["pending"][0]["countdown"]["remaining_seconds"], 29)
        self.alerts.update(self.protection(countdown_remaining=10), "connected", self.sample())
        pending = self.alerts.status()["pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["countdown"]["remaining_seconds"], 10)
        self.assertEqual(pending[0]["countdown"]["countdown_id"], "a" * 32)

    def test_new_countdown_does_not_inherit_previous_episode_acknowledgement(self):
        self.alerts.update(self.protection(), "connected", self.sample())
        old = self.alerts.status()["pending"][0]
        self.alerts.acknowledge([old["id"]])
        self.clock.advance(1)
        self.alerts.update(self.protection(countdown_id="b" * 32), "connected", self.sample())
        new = self.alerts.status()["pending"][0]
        self.assertNotEqual(new["id"], old["id"])
        self.assertEqual(new["countdown"]["countdown_id"], "b" * 32)
        self.assertEqual(new["priority"], 3)
        self.assertIsNone(new["acknowledged_at"])

    def test_equal_priority_remains_chronological_and_unknown_metrics_preserve_incidents(self):
        self.alerts.on_event("ups_fault", "Falla")
        self.clock.advance(1)
        self.alerts.on_event("overload", "Sobrecarga")
        self.assertEqual([item["kind"] for item in self.alerts.status()["pending"]], ["ups_fault", "overload"])
        self.alerts.update({}, "connected", self.sample(fault=None, load_percent=None))
        self.assertTrue(all(item["priority"] == 2 and item["resolved_at"] is None
                            for item in self.alerts.status()["pending"]))
        self.assertTrue(all(item["confirmed"] is False for item in self.alerts.status()["pending"]))

    def test_fresh_battery_observation_renews_outage_but_reads_do_not(self):
        self.alerts.on_event("power_lost", "Corte")
        self.clock.advance(4)
        self.assertFalse(self.alerts.status()["pending"][0]["confirmed"])
        self.alerts.update({}, "connected", self.sample())
        self.assertTrue(self.alerts.status()["pending"][0]["confirmed"])
        self.clock.advance(4)
        self.assertFalse(self.alerts.status()["pending"][0]["confirmed"])

    def test_restarted_monitor_never_inherits_acknowledgement_of_previous_countdown(self):
        self.alerts.update(self.protection(), "connected", self.sample())
        old = self.alerts.status()["pending"][0]
        self.alerts.acknowledge([old["id"]])
        self.alerts.stop()
        restarted = AlertManager(self.store, PriorityNotifier(), clock=self.clock.monotonic, utc_clock=self.clock.utc)
        restarted.start()
        self.addCleanup(restarted.stop)
        restarted.update(self.protection(countdown_id="b" * 32), "connected", self.sample())
        current = restarted.status()["pending"][0]
        self.assertNotEqual(current["id"], old["id"])
        self.assertEqual(current["countdown"]["countdown_id"], "b" * 32)
        self.assertIsNone(current["acknowledged_at"])


class PriorityMetadataTests(unittest.TestCase):
    def test_legacy_metadata_stays_legacy_and_bad_actions_are_not_forwarded(self):
        legacy = {"title": "Aviso", "message": "Texto"}
        self.assertEqual(normalize_notice_metadata(legacy), legacy)
        for changes in ({"countdown_id": "bad"}, {"action": "run"},
                        {"valid_for_ms": 3001}, {"remaining_seconds": float("nan")}, {"cancelable": "true"}):
            with self.subTest(changes=changes):
                self.assertNotIn("countdown", normalize_notice_metadata(countdown_metadata(**changes)))
        self.assertNotIn("priority", normalize_notice_metadata({**legacy, "priority": True}))

    def test_freshness_and_remaining_are_discounted_without_mutating_source(self):
        source = countdown_metadata()
        aged = age_notice_metadata(source, 1.25)
        self.assertEqual(aged["countdown"]["remaining_seconds"], 28.75)
        self.assertEqual(aged["countdown"]["valid_for_ms"], 1750)
        self.assertEqual(source["countdown"]["valid_for_ms"], 3000)
        expired = age_notice_metadata(aged, 2)
        self.assertFalse(expired["confirmed"])
        self.assertFalse(expired["countdown"]["cancelable"])
        self.assertEqual(expired["priority"], 1)


class BrokerPriorityTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.broker = SessionBroker(clock=self.clock.monotonic, capacity=64)
        self.broker.start()
        self.addCleanup(self.broker.stop)

    def poll(self):
        return self.broker.poll({"client_id": "a" * 32, "after": None, "delivery": {"ready": True},
                                 "protocol": 2, "acks": [], "epoch": None, "session_state": "active"})

    def test_priority_is_applied_before_delivery_page_and_offered_ids_are_exact(self):
        for number in range(32):
            self.broker.publish_event("power_lost", "Corte", str(number), priority=1)
        self.broker.sync_persistent({"notice:99": countdown_metadata()})
        self.broker.publish_event("policy_countdown", "Protección", "Activa", persistent=True, key="notice:99", priority=3)
        response = self.poll()
        self.assertEqual(response["messages"][0]["kind"], "policy_countdown")
        self.assertEqual(len(response["messages"]), 32)
        self.assertEqual(self.broker._receivers["a" * 32]["offered"], {item["id"] for item in response["messages"]})
        self.assertEqual(self.broker.status()["pending_retained"], 33)

    def test_persistent_priority_is_applied_before_capacity(self):
        values = {f"notice:{number}": {"title": "Aviso", "message": "Atención", "priority": 1}
                  for number in range(200)}
        values["notice:999"] = countdown_metadata()
        self.broker.sync_persistent(values)
        self.assertEqual(len(self.broker._persistent), 200)
        self.assertEqual(next(iter(self.poll()["persistent"])), "notice:999")

    def test_poll_does_not_rejuvenate_validity_and_expiry_disables_action(self):
        self.broker.sync_persistent({"notice:99": countdown_metadata()})
        self.clock.advance(1)
        first = self.poll()["persistent"]["notice:99"]
        self.assertEqual(first["countdown"]["valid_for_ms"], 2000)
        self.clock.advance(1)
        second = self.poll()["persistent"]["notice:99"]
        self.assertEqual(second["countdown"]["remaining_seconds"], 28)
        self.assertEqual(second["countdown"]["valid_for_ms"], 1000)
        self.clock.advance(1.01)
        expired = self.poll()["persistent"]["notice:99"]
        self.assertFalse(expired["countdown"]["cancelable"])
        self.assertFalse(expired["confirmed"])
        self.assertEqual(expired["priority"], 1)


if __name__ == "__main__":
    unittest.main()
