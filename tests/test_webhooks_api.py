import io
import json
import tempfile
import threading
import time
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from ups_monitor.core import Store, iso_utc
from ups_monitor.server import Handler, Monitor
from ups_monitor.webhooks import WebhookManager


EVENTS = ["power_lost", "power_restored", "battery_low", "overload", "ups_fault"]
URL = "https://n8n.example.test/webhook/elipro-ups"


class WebhookApiTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.sender = Mock(return_value=204)
        self.webhooks = WebhookManager(self.store, sender=self.sender, retry_delay_seconds=0.01)
        self.action = Mock()
        self.alerts = SimpleNamespace(on_event=Mock(), update=Mock(), status=Mock(return_value={
            "settings": {}, "delivery": {"available": True}, "last_notification": None,
        }))
        self.sample = {
            "timestamp": iso_utc(), "on_battery": False, "battery_voltage": 27.4,
            "load_percent": 7, "input_voltage": 220, "low_battery": False, "fault": False,
        }
        self.adapter = SimpleNamespace(source="test", read=lambda: {**self.sample, "timestamp": iso_utc()})
        self.monitor = self.make_monitor(self.store, self.webhooks)

    def tearDown(self):
        self.monitor.stop()
        self.store.close()

    def make_monitor(self, store, webhooks=None, *, dry_run=False):
        return Monitor(store, self.adapter, dry_run=dry_run, action_callback=self.action,
                       alerts=self.alerts, webhooks=webhooks)

    def handler(self, path="/api/webhooks", body=None, **headers):
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(server_port=8765, monitor=self.monitor)
        handler.path = path
        handler.headers = Message()
        content = json.dumps({} if body is None else body).encode("utf-8")
        values = {"Host": "127.0.0.1:8765", "Content-Type": "application/json",
                  "Content-Length": str(len(content)), "X-UPS-Token": self.monitor.csrf_token}
        values.update(headers)
        for key, value in values.items():
            handler.headers[key] = value
        handler.rfile = io.BytesIO(content)
        handler._json = Mock()
        return handler

    def configure(self, **changes):
        settings = {"webhook_enabled": True, "webhook_url": URL,
                    "webhook_events": ["power_lost", "battery_low"], **changes}
        handler = self.handler(body=settings)
        handler.do_POST()
        self.assertEqual(handler._json.call_args.kwargs, {})
        self.assertEqual(len(handler._json.call_args.args), 1, handler._json.call_args)
        return handler._json.call_args.args[0]

    def wait_until(self, predicate, message):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if predicate():
                return
            threading.Event().wait(0.005)
        self.fail(message)

    def wait_for_test(self, state):
        self.wait_until(lambda: (self.webhooks.status().get("test") or {}).get("state") == state,
                        f"Webhook test did not reach {state!r}: {self.webhooks.status()}")
        return self.monitor.snapshot()["webhooks"]["test"]

    def status_from_api(self):
        handler = self.handler("/api/status")
        handler.do_GET()
        self.assertEqual(len(handler._json.call_args.args), 1, handler._json.call_args)
        return handler._json.call_args.args[0]["webhooks"]

    def replace_webhooks(self, *, dry_run=False, retry_delay_seconds=0.01):
        self.monitor.stop()
        self.webhooks = WebhookManager(self.store, dry_run=dry_run, sender=self.sender,
                                       retry_delay_seconds=retry_delay_seconds)
        self.monitor = self.make_monitor(self.store, self.webhooks, dry_run=dry_run)

    def test_default_snapshot_disables_master_and_selects_only_two_events(self):
        status = self.monitor.snapshot()["webhooks"]
        self.assertEqual(status["settings"], {
            "webhook_enabled": False, "webhook_url": "",
            "webhook_events": ["power_lost", "battery_low"],
        })
        self.assertFalse(status["dry_run"])
        self.assertIsNone(status["test"])
        self.assertEqual(status["deliveries"], [])
        self.sender.assert_not_called()

    def test_saved_settings_survive_restart_and_disabling_preserves_url_and_events(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / "webhooks.sqlite3"
            store = Store(path)
            monitor = self.make_monitor(store)
            settings = {"webhook_enabled": True, "webhook_url": URL,
                        "webhook_events": ["power_restored", "ups_fault"]}
            try:
                saved = monitor.set_webhooks(settings)
                self.assertEqual(saved["webhooks"]["settings"], settings)
                paused = monitor.set_webhooks({"webhook_enabled": False})["webhooks"]["settings"]
                self.assertEqual(paused, {**settings, "webhook_enabled": False})
                self.assertEqual(store.get_setting("webhooks"), paused)
            finally:
                monitor.stop()
                store.close()
            reopened = Store(path)
            restarted = self.make_monitor(reopened)
            try:
                self.assertEqual(restarted.snapshot()["webhooks"]["settings"], paused)
                resumed = restarted.set_webhooks({"webhook_enabled": True})
                self.assertEqual(resumed["webhooks"]["settings"], settings)
            finally:
                restarted.stop()
                reopened.close()
        self.action.assert_not_called()

    def test_mutations_reject_invalid_settings_without_changing_saved_configuration(self):
        saved = self.configure()["webhooks"]["settings"]
        for body in [[], "settings", {"webhook_enabled": "false"}, {"webhook_enabled": 1},
                     {"webhook_url": None}, {"webhook_url": [URL]},
                     {"webhook_url": "not-a-url"}, {"webhook_url": "ftp://example.test/ups"},
                     {"webhook_events": "power_lost"}, {"webhook_events": ["webhook_test"]},
                     {"webhook_events": ["connection_lost"]}, {"webhook_events": [True]}]:
            with self.subTest(body=body):
                handler = self.handler(body=body)
                handler.do_POST()
                self.assertEqual(handler._json.call_args.args[1], 400)
                self.assertEqual(self.webhooks.status()["settings"], saved)
                self.assertEqual(self.store.get_setting("webhooks"), saved)
        self.sender.assert_not_called()

    def test_both_webhook_endpoints_require_csrf_and_local_origin(self):
        for path in ("/api/webhooks", "/api/webhooks/test"):
            for headers in [{"X-UPS-Token": ""}, {"X-UPS-Token": "wrong"},
                            {"Host": "attacker.example:8765"},
                            {"Origin": "http://attacker.example"}, {"Origin": "null"}]:
                with self.subTest(path=path, headers=headers):
                    handler = self.handler(path, {"webhook_enabled": True, "webhook_url": URL}, **headers)
                    handler.do_POST()
                    self.assertEqual(handler._json.call_args.args[1], 403)
        self.assertFalse(self.webhooks.status()["settings"]["webhook_enabled"])
        self.sender.assert_not_called()

    def test_empty_url_or_disabled_master_never_sends_automatic_events(self):
        self.webhooks.start()
        self.sample["on_battery"] = True
        self.monitor.tick()
        self.configure(webhook_enabled=False)
        self.sample.update(on_battery=False)
        self.monitor.tick()
        self.sample.update(on_battery=True, low_battery=True)
        self.monitor.tick()
        self.monitor.snapshot()
        self.sender.assert_not_called()

    def test_test_endpoint_rejects_disabled_webhooks_and_unsaved_overrides(self):
        handler = self.handler("/api/webhooks/test")
        handler.do_POST()
        self.assertEqual(handler._json.call_args.args[1], 400)
        self.configure()
        for body in [{"webhook_url": "https://other.example.test/unsaved"}, [], "test"]:
            with self.subTest(body=body):
                handler = self.handler("/api/webhooks/test", body)
                handler.do_POST()
                self.assertEqual(handler._json.call_args.args[1], 400)
        self.sender.assert_not_called()

    def test_test_endpoint_sends_webhook_test_and_reports_success_without_power_actions(self):
        saved = self.configure(webhook_events=[])["webhooks"]["settings"]
        self.monitor.tick()
        handler = self.handler("/api/webhooks/test")
        handler.do_POST()
        self.assertEqual(len(handler._json.call_args.args), 1)
        result = self.wait_for_test("success")
        self.assertTrue(result["message"])
        self.assertEqual(result["http_status"], 204)
        self.assertEqual((result["attempts"], result["max_attempts"]), (1, 2))
        self.sender.assert_called_once()
        args, kwargs = self.sender.call_args
        self.assertEqual(args[0], URL)
        self.assertEqual(args[1]["event"], "webhook_test")
        self.assertEqual(args[1]["battery_voltage"], self.sample["battery_voltage"])
        self.assertEqual(kwargs["timeout"], 5)
        self.assertEqual(self.store.get_setting("webhooks"), saved)
        self.assertFalse(self.monitor.engine.policy["enabled"])
        self.assertIsNone(self.monitor.test_deadline)
        self.action.assert_not_called()

    def test_test_endpoint_reports_http_failure_and_monitor_remains_connected(self):
        self.configure()
        self.monitor.tick()
        self.sender.side_effect = OSError("Connection refused")
        handler = self.handler("/api/webhooks/test")
        with self.assertLogs("elipro.monitor", level="ERROR"):
            handler.do_POST()
            result = self.wait_for_test("error")
        self.assertTrue(result["message"])
        self.assertEqual((result["attempts"], result["max_attempts"]), (2, 2))
        self.assertEqual(self.sender.call_count, 2)
        delivery = self.status_from_api()["deliveries"][-1]
        self.assertEqual(delivery["id"], result["id"])
        self.assertEqual((delivery["state"], delivery["attempts"]), ("error", 2))
        self.assertEqual(self.monitor.connection, "connected")
        self.action.assert_not_called()

    def test_dry_run_simulates_test_and_never_posts_real_transition(self):
        self.replace_webhooks(dry_run=True)
        self.configure()
        self.webhooks.start()
        self.sample.update(on_battery=True, low_battery=True)
        self.monitor.tick()
        handler = self.handler("/api/webhooks/test")
        handler.do_POST()
        result = self.wait_for_test("simulated")
        self.assertTrue(self.monitor.snapshot()["webhooks"]["dry_run"])
        self.assertIn("simulad", result["message"].lower())
        self.assertEqual(result["attempts"], 0)
        self.sender.assert_not_called()
        self.action.assert_not_called()

    def test_api_delivery_history_exposes_retry_then_success_on_second_attempt(self):
        self.replace_webhooks(retry_delay_seconds=1)
        self.configure()
        second_entered, release = threading.Event(), threading.Event()

        def send_with_one_failure(*_args, **_kwargs):
            if self.sender.call_count == 1:
                raise OSError("Temporary receiver failure")
            second_entered.set()
            release.wait(2)
            return 202

        self.sender.side_effect = send_with_one_failure
        handler = self.handler("/api/webhooks/test")
        try:
            with self.assertLogs("elipro.monitor", level="ERROR"):
                handler.do_POST()
                retrying = self.wait_for_test("retrying")
            history = self.status_from_api()["deliveries"]
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["id"], retrying["id"])
            self.assertEqual((history[0]["state"], history[0]["attempts"], history[0]["max_attempts"]),
                             ("retrying", 1, 2))
            self.assertTrue(history[0]["next_retry_at"].endswith("Z"))
            self.assertTrue(second_entered.wait(2), "Second attempt was never started")
            sending = self.status_from_api()["deliveries"][-1]
            self.assertEqual((sending["state"], sending["attempts"]), ("sending", 2))
            release.set()
            final = self.wait_for_test("success")
        finally:
            release.set()
        delivery = self.status_from_api()["deliveries"][-1]
        self.assertEqual(delivery["id"], final["id"])
        self.assertEqual((delivery["state"], delivery["attempts"], delivery["max_attempts"]),
                         ("success", 2, 2))
        self.assertEqual(delivery["http_status"], 202)
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(self.sender.call_args_list[0].args, self.sender.call_args_list[1].args)
        self.assertEqual(self.sender.call_args_list[0].kwargs, {"timeout": 5})
        self.assertEqual(self.sender.call_args_list[1].kwargs, {"timeout": 5})

    def test_disabling_master_cancels_pending_retry_and_preserves_configuration(self):
        self.replace_webhooks(retry_delay_seconds=1)
        saved = self.configure()["webhooks"]["settings"]
        self.sender.side_effect = OSError("Receiver offline")
        handler = self.handler("/api/webhooks/test")
        with self.assertLogs("elipro.monitor", level="ERROR"):
            handler.do_POST()
            retrying = self.wait_for_test("retrying")
        handler = self.handler(body={"webhook_enabled": False})
        handler.do_POST()
        status = self.status_from_api()
        self.assertEqual(status["settings"], {**saved, "webhook_enabled": False})
        self.assertEqual(status["test"]["id"], retrying["id"])
        self.assertEqual(status["test"]["state"], "cancelled")
        self.assertEqual((status["deliveries"][-1]["state"], status["deliveries"][-1]["attempts"]),
                         ("cancelled", 1))
        threading.Event().wait(1.05)
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(self.store.get_setting("webhooks"), {**saved, "webhook_enabled": False})

    def test_automatic_delivery_stops_after_two_failures_and_status_never_retries_it(self):
        self.configure()
        self.sender.side_effect = OSError("Receiver offline")
        self.webhooks.start()
        self.sample["on_battery"] = True
        with self.assertLogs("elipro.monitor", level="ERROR"):
            self.monitor.tick()
            self.wait_until(lambda: (self.webhooks.status()["deliveries"][-1:] or [{}])[0].get("state") == "error",
                            "Automatic delivery never reached final error")
        delivery = self.status_from_api()["deliveries"][-1]
        self.assertEqual((delivery["event"], delivery["attempts"], delivery["max_attempts"]),
                         ("power_lost", 2, 2))
        for _ in range(10):
            self.status_from_api()
            self.monitor.tick()
        threading.Event().wait(0.03)
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(self.status_from_api()["deliveries"][-1]["id"], delivery["id"])
        self.assertEqual(self.monitor.connection, "connected")
        self.action.assert_not_called()

    def test_delivery_history_survives_restart_without_replaying_success_or_error(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / "delivery-history.sqlite3"
            store = Store(path)
            sender = Mock(return_value=204)
            manager = WebhookManager(store, sender=sender, retry_delay_seconds=0.01)
            monitor = self.make_monitor(store, manager)
            try:
                monitor.set_webhooks({"webhook_enabled": True, "webhook_url": URL})
                monitor.test_webhooks()
                self.wait_until(lambda: manager.status()["test"]["state"] == "success",
                                "First delivery did not succeed")
                successful = manager.status()["test"]
                sender.side_effect = OSError("Receiver unavailable")
                with self.assertLogs("elipro.monitor", level="ERROR"):
                    monitor.test_webhooks()
                    self.wait_until(lambda: manager.status()["test"]["state"] == "error",
                                    "Second delivery did not fail after its retry")
                failed = manager.status()["test"]
                self.wait_until(lambda: len(store.webhook_deliveries()) == 2,
                                "Terminal delivery history was not persisted")
            finally:
                monitor.stop()
                store.close()
            reopened = Store(path)
            replay_sender = Mock(return_value=204)
            resumed_manager = WebhookManager(reopened, sender=replay_sender, retry_delay_seconds=0.01)
            resumed = self.make_monitor(reopened, resumed_manager)
            try:
                deliveries = {entry["id"]: entry for entry in resumed.snapshot()["webhooks"]["deliveries"]}
                self.assertEqual(set(deliveries), {successful["id"], failed["id"]})
                self.assertEqual((deliveries[successful["id"]]["state"], deliveries[successful["id"]]["attempts"]),
                                 ("success", 1))
                self.assertEqual((deliveries[failed["id"]]["state"], deliveries[failed["id"]]["attempts"]),
                                 ("error", 2))
                resumed_manager.start()
                resumed.tick()
                for _ in range(10):
                    resumed.snapshot()
                threading.Event().wait(0.03)
                replay_sender.assert_not_called()
                self.action.assert_not_called()
            finally:
                resumed.stop()
                reopened.close()

    def test_five_transitions_use_current_sample_once_and_keep_desktop_delivery(self):
        self.configure(webhook_events=EVENTS)
        self.webhooks.start()
        self.monitor.tick()
        transitions = [
            ("power_lost", {"on_battery": True, "input_voltage": 0, "battery_voltage": 26.1, "load_percent": 35}),
            ("battery_low", {"low_battery": True, "battery_voltage": 24.2}),
            ("overload", {"load_percent": 110}),
            ("ups_fault", {"fault": True}),
            ("power_restored", {"on_battery": False, "input_voltage": 220, "battery_voltage": 27.5,
                                "load_percent": 7, "low_battery": False, "fault": False}),
        ]
        expected = {}
        for event, changes in transitions:
            self.sample.update(changes)
            expected[event] = dict(self.sample)
            self.monitor.tick()
            self.monitor.tick()
        self.wait_until(lambda: self.sender.call_count == len(EVENTS), "Expected exactly five webhook transitions")
        payloads = {entry.args[1]["event"]: entry.args[1] for entry in self.sender.call_args_list}
        self.assertEqual(set(payloads), set(EVENTS))
        for event, payload in payloads.items():
            with self.subTest(event=event):
                for field in ("on_battery", "battery_voltage", "load_percent", "input_voltage"):
                    self.assertEqual(payload[field], expected[event][field])
                self.assertTrue(payload["hostname"])
                self.assertTrue(payload["timestamp"].endswith("Z"))
                self.assertTrue(payload["message"])
                self.assertIn(payload["severity"], ("info", "warning", "critical"))
        desktop_kinds = [entry.args[0] for entry in self.alerts.on_event.call_args_list if entry.args[0] in EVENTS]
        self.assertEqual(desktop_kinds, [event for event, _ in transitions])
        self.assertIn(call("power_lost", "UPS funcionando con batería: suministro de red interrumpido.", None),
                      self.alerts.on_event.call_args_list)
        self.action.assert_not_called()

    def test_event_write_failure_does_not_prevent_webhook_or_desktop_delivery(self):
        self.configure()
        self.webhooks.start()
        self.sample.update(on_battery=True, battery_voltage=26.1)
        with patch.object(self.store, "add_event", side_effect=OSError("Disk unavailable")), \
                self.assertLogs("elipro.monitor", level="ERROR"):
            self.monitor.tick()
        self.wait_until(lambda: self.sender.call_count == 1, "SQLite failure prevented a webhook")
        self.assertEqual(self.sender.call_args.args[1]["event"], "power_lost")
        self.assertIn(call("power_lost", "UPS funcionando con batería: suministro de red interrumpido.", None),
                      self.alerts.on_event.call_args_list)
        self.assertEqual(self.monitor.connection, "connected")

    def test_desktop_backend_failure_does_not_prevent_webhook_delivery(self):
        self.configure()
        self.webhooks.start()
        self.alerts.on_event.side_effect = RuntimeError("Desktop backend unavailable")
        self.sample["on_battery"] = True
        with self.assertLogs("elipro.monitor", level="ERROR"):
            self.monitor.tick()
        self.wait_until(lambda: self.sender.call_count == 1, "Desktop backend failure prevented a webhook")
        self.assertEqual(self.monitor.connection, "connected")
        self.action.assert_not_called()

    def test_reading_status_never_replays_persisted_webhook_events(self):
        self.store.add_event("power_lost", "An old saved outage")
        self.configure()
        self.webhooks.start()
        self.monitor.tick()
        for _ in range(10):
            self.monitor.snapshot()
        self.sender.assert_not_called()

    def test_monitor_stop_returns_while_http_sender_is_in_flight(self):
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()

        def blocked_sender(*_args, **_kwargs):
            entered.set()
            release.wait(5)
            return 204

        self.sender.side_effect = blocked_sender
        self.configure()
        self.webhooks.start()
        self.sample["on_battery"] = True
        self.monitor.tick()
        self.assertTrue(entered.wait(1), "Webhook worker never started HTTP delivery")
        stopper = threading.Thread(target=lambda: (self.monitor.stop(), stopped.set()), daemon=True)
        try:
            stopper.start()
            self.assertTrue(stopped.wait(0.5), "Stopping the monitor waited for an HTTP response")
        finally:
            release.set()
            stopper.join(1)


if __name__ == "__main__":
    unittest.main()
