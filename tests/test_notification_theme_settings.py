"""Notification appearance is persisted and delivered independently of power rules."""
from contextlib import closing
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from ups_monitor.alerts import AlertManager, validate_notification_settings
from ups_monitor.core import Store
from ups_monitor.session_agent import Agent
from ups_monitor.session_broker import SessionBroker


class ThemeNotifier:
    def __init__(self):
        self.events = []

    def set_theme(self, value):
        self.events.append(("theme", value))

    def start(self):
        self.events.append(("start",))

    def stop(self):
        pass

    def status(self):
        return {"ready": True}

    def sync_persistent(self, value):
        self.events.append(("persistent", value))

    def notify(self, title, message, **options):
        self.events.append(("notice", title))
        return True


class ThemeSettingsTests(unittest.TestCase):
    def test_old_preferences_default_to_automatic_and_theme_is_strict(self):
        self.assertEqual(validate_notification_settings({"sound_enabled": False})["theme"], "auto")
        for value in (None, True, 1, [], {}, "Light", "system", "", "light "):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_notification_settings({"theme": value})
        for value in ("auto", "light", "dark"):
            self.assertEqual(validate_notification_settings({"theme": value})["theme"], value)

    def test_saved_theme_applies_before_native_start_and_partial_saves_preserve_it(self):
        with closing(Store(":memory:")) as store:
            store.set_setting("notification_settings", {"theme": "dark"})
            store.set_setting("policy", {"enabled": False})
            notifier = ThemeNotifier()
            manager = AlertManager(store, notifier)
            manager.start()
            self.assertEqual(notifier.events[:2], [("theme", "dark"), ("start",)])
            manager.update_settings({"theme": "light"})
            manager.update_settings({"sound_enabled": False})
            self.assertEqual(store.get_setting("notification_settings")["theme"], "light")
            self.assertEqual(manager.status()["settings"]["theme"], "light")
            self.assertEqual([event for event in notifier.events if event[0] == "theme"][-1], ("theme", "light"))
            self.assertEqual(store.get_setting("policy"), {"enabled": False})
            manager.stop()
            restarted = ThemeNotifier()
            restored = AlertManager(store, restarted)
            restored.start()
            self.assertEqual(restarted.events[:2], [("theme", "light"), ("start",)])
            restored.stop()

    def test_failed_setting_persistence_does_not_apply_unsaved_theme(self):
        store = SimpleNamespace(get_setting=lambda *_: {}, pending_notifications=lambda: [],
                                notification_incidents=lambda: [], set_setting=Mock(side_effect=OSError("disk full")))
        notifier = ThemeNotifier()
        manager = AlertManager(store, notifier)
        manager.start()
        with self.assertRaises(OSError):
            manager.update_settings({"theme": "dark"})
        self.assertEqual(manager.status()["settings"]["theme"], "auto")
        self.assertNotIn(("theme", "dark"), notifier.events)
        manager.stop()

    def test_broker_theme_reaches_every_poll_including_locked_and_stopped_sessions(self):
        broker = SessionBroker()
        broker.start()
        legacy = {"client_id": "a" * 32, "after": None, "delivery": {"ready": True}}
        self.assertEqual(broker.poll(legacy)["notification_theme"], "auto")
        broker.set_theme("light")
        self.assertEqual(broker.poll(legacy)["notification_theme"], "light")
        modern = {**legacy, "protocol": 2, "acks": [], "epoch": None, "session_state": "locked"}
        self.assertEqual(broker.poll(modern)["notification_theme"], "light")
        broker.stop()
        self.assertEqual(broker.poll(modern)["notification_theme"], "light")
        for value in (None, [], "system"):
            with self.assertRaises(ValueError):
                broker.set_theme(value)

    def test_agent_applies_theme_before_persistent_and_queued_notices(self):
        client = Mock()
        client.poll.return_value = {"cursor": 1, "running": True, "notification_theme": "dark",
                                    "persistent": {}, "messages": [{"title": "Corte", "message": "En batería"}]}
        notifier = ThemeNotifier()
        agent = Agent(client, notifier, session_state=lambda: "active")
        agent.step()
        self.assertEqual(notifier.events, [("theme", "dark"), ("persistent", {}), ("notice", "Corte")])
        client.poll.return_value["notification_theme"] = "light"
        client.poll.return_value["messages"] = []
        agent.step()
        self.assertEqual(notifier.events[-2:], [("theme", "light"), ("persistent", {})])

    def test_agent_rejects_malformed_theme_before_displaying_and_supports_older_services(self):
        client = Mock()
        response = {"cursor": 1, "running": True, "notification_theme": [],
                    "persistent": {}, "messages": [{"title": "Corte", "message": "En batería"}]}
        client.poll.return_value = response
        notifier = ThemeNotifier()
        agent = Agent(client, notifier, session_state=lambda: "active")
        with self.assertRaises(ValueError):
            agent.step()
        self.assertEqual(notifier.events, [])
        response.pop("notification_theme")
        agent.step()
        self.assertEqual(notifier.events[0], ("theme", "auto"))


if __name__ == "__main__":
    unittest.main()
