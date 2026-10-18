import io
from email.message import Message
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from ups_monitor.alerts import AlertManager
from ups_monitor.core import Store, iso_utc
from ups_monitor.server import Handler, Monitor


class FakeNotifier:
    def __init__(self):
        self.messages = []
        self.sent = threading.Event()

    def start(self):
        pass

    def stop(self):
        pass

    def notify(self, title, message, **options):
        self.messages.append({"title": title, "message": message, **options})
        self.sent.set()
        return True

    def status(self):
        return {"available": True, "ready": True, "channel": "test"}


class NotificationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.notifier = FakeNotifier()
        self.alerts = AlertManager(self.store, self.notifier)
        self.action = Mock()
        self.sample = {"timestamp": iso_utc(), "on_battery": False,
                       "load_percent": 7, "battery_voltage": 27.4,
                       "low_battery": False, "fault": False}
        self.adapter = SimpleNamespace(source="test", read=lambda: {**self.sample, "timestamp": iso_utc()})
        self.monitor = Monitor(self.store, self.adapter, action_callback=self.action, alerts=self.alerts)

    def tearDown(self):
        self.monitor.stop()
        self.alerts.stop()
        self.store.close()

    def handler(self, path, data=b"{}", token=True):
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(server_port=8765, monitor=self.monitor)
        handler.path = path
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8765"
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Content-Length"] = str(len(data))
        if token:
            handler.headers["X-UPS-Token"] = self.monitor.csrf_token
        handler.rfile = io.BytesIO(data)
        handler._json = Mock()
        return handler

    def test_worker_sends_cut_notification_without_panel_or_http_requests(self):
        self.sample["on_battery"] = True
        self.alerts.start()
        self.monitor.start()
        self.assertTrue(self.notifier.sent.wait(1), "The monitor must notify without a browser request")
        self.assertEqual(self.notifier.messages[0]["title"], "Corte de energía")
        self.action.assert_not_called()

    def test_snapshot_never_replays_history_or_sends_desktop_notifications(self):
        self.store.add_event("power_lost", "Old saved event")
        self.monitor.tick()
        for _ in range(20):
            self.monitor.snapshot()
        self.assertEqual(self.notifier.messages, [])

    def test_notification_test_is_independent_of_enabled_protection(self):
        self.monitor.set_policy({"enabled": True})
        handler = self.handler("/api/notifications/test")
        handler.do_POST()
        result = handler._json.call_args.args[0]
        self.assertTrue(result["policy"]["enabled"])
        self.assertTrue(result["notifications"]["last_notification"]["accepted"])
        self.assertIn("No se apagará", self.notifier.messages[-1]["message"])
        self.assertIsNone(self.monitor.test_deadline)
        self.action.assert_not_called()

    def test_notification_preferences_persist_without_changing_power_policy(self):
        handler = self.handler("/api/notifications", b'{"sound_enabled":false}')
        handler.do_POST()
        self.assertFalse(self.store.get_setting("notification_settings")["sound_enabled"])
        self.assertFalse(handler._json.call_args.args[0]["policy"]["enabled"])
        self.action.assert_not_called()

    def test_notification_mutations_require_token_and_strict_validation(self):
        handler = self.handler("/api/notifications/test", token=False)
        handler.do_POST()
        self.assertEqual(handler._json.call_args.args[1], 403)
        self.assertEqual(self.notifier.messages, [])
        handler = self.handler("/api/notifications", b'{"sound_enabled":"false"}')
        handler.do_POST()
        self.assertEqual(handler._json.call_args.args[1], 400)
        self.assertEqual(self.notifier.messages, [])

    def test_backend_failure_cannot_interrupt_monitoring(self):
        self.alerts.on_event = Mock(side_effect=RuntimeError("Native failure"))
        self.sample["on_battery"] = True
        with self.assertLogs("elipro.monitor", level="ERROR"):
            self.monitor.tick()
        self.assertEqual(self.monitor.connection, "connected")
        self.assertTrue(self.monitor.sample["on_battery"])
        self.action.assert_not_called()


if __name__ == "__main__":
    unittest.main()
