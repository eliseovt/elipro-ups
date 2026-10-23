"""Session-zero delivery without a real desktop, service, USB or OS action."""
import io
import json
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
import unittest
from unittest.mock import MagicMock, Mock, patch

from ups_monitor.core import Store
from ups_monitor import session_agent
from ups_monitor.server import Handler, Monitor
from ups_monitor.session_agent import Agent, LocalClient
from ups_monitor.session_broker import SessionBroker
from ups_monitor.windows_notifications import DEFAULT_EXIT_LABEL, DEFAULT_EXIT_MESSAGE, WindowsNotifier


CLIENT = "a" * 32


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Mock(return_value=10)
        self.broker = SessionBroker(clock=self.clock)
        self.broker.start()

    def poll(self, client=CLIENT, cursor=None, ready=True):
        return self.broker.poll({"client_id": client, "after": cursor,
                                 "delivery": {"ready": ready, "last_error": None}})

    def test_no_session_does_not_pretend_windows_received_notice(self):
        self.assertFalse(self.broker.status()["ready"])
        self.assertFalse(self.broker.notify("Corte", "Batería"))
        self.assertIn("servicio sigue", self.broker.status()["last_error"])

    def test_each_session_receives_notice_once_without_stealing_other_session_delivery(self):
        initial = self.poll()
        self.poll("b" * 32)
        self.assertTrue(self.broker.notify("Corte", "En batería", severity="warning"))
        first = self.poll(cursor=initial["cursor"])
        second = self.poll("b" * 32, cursor=initial["cursor"])
        self.assertEqual(first["messages"], second["messages"])
        self.assertEqual(first["messages"][0]["title"], "Corte")
        self.assertEqual(self.poll(cursor=first["cursor"])["messages"], [])

    def test_new_client_never_replays_history_but_receives_current_critical_condition(self):
        self.poll()
        self.broker.sync_persistent({"battery_low": {"title": "Batería baja", "message": "Activa"}})
        self.broker.notify("Corte", "Antes de iniciar sesión")
        result = self.poll("b" * 32)
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["persistent"]["battery_low"]["message"], "Activa")

    def test_stale_notice_and_stale_heartbeat_expire_independently_of_new_reads(self):
        initial = self.poll()
        self.broker.notify("Corte", "Ya antiguo")
        self.clock.return_value = 21
        self.assertFalse(self.broker.status()["ready"])
        self.assertEqual(self.poll(cursor=initial["cursor"])["messages"], [])

    def test_recovered_persistent_condition_is_removed_before_queued_delivery(self):
        first = self.poll()
        self.broker.sync_persistent({"battery_low": {"title": "Baja", "message": "Activa"}})
        self.broker.notify("Baja", "Activa", persistent=True, key="battery_low")
        self.broker.sync_persistent({})
        result = self.poll(cursor=first["cursor"])
        self.assertEqual(result["persistent"], {})
        self.assertEqual(result["messages"], [])

    def test_broker_storage_and_agent_count_are_bounded(self):
        self.poll()
        for number in range(100):
            self.broker.notify("Corte", str(number))
        self.assertEqual(len(self.poll(cursor=0)["messages"]), 32)
        for number in range(31):
            self.poll(f"{number:032x}")
        with self.assertRaises(ValueError):
            self.poll("f" * 32)

    def test_malformed_or_command_shaped_inputs_are_rejected(self):
        for value in ({"command": "shutdown"}, {"client_id": CLIENT, "after": True, "delivery": {"ready": True}},
                      {"client_id": "../run", "after": 0, "delivery": {"ready": True}},
                      {"client_id": CLIENT, "after": 0, "delivery": {"ready": "yes"}}):
            with self.assertRaises(ValueError):
                self.broker.poll(value)

    def test_stop_discards_all_desktop_state(self):
        self.poll()
        self.broker.notify("X", "Y")
        self.broker.stop()
        self.assertFalse(self.poll()["running"])
        self.assertFalse(self.broker.status()["ready"])

    def test_ready_agent_preserves_sound_popup_errors_and_delivery_timestamps(self):
        details = {"ready": True, "last_error": "Falló la ventana", "last_sound_error": "Sin sonido",
                   "popup_error": "Ventana de respaldo rechazada", "last_delivered_at": "2026-10-17T22:00:00Z",
                   "last_popup_shown_at": "2026-10-17T22:00:01Z"}
        self.broker.poll({"client_id": CLIENT, "after": None, "delivery": details})
        status = self.broker.status()
        self.assertTrue(status["ready"])
        for key, value in details.items():
            self.assertEqual(status[key], value)
        self.assertEqual(status["diagnostic_scope"], "most_recent_active_agent")
        self.clock.return_value = 21
        expired = self.broker.status()
        self.assertFalse(expired["ready"])
        self.assertIsNone(expired["last_sound_error"])
        self.assertIsNone(expired["last_popup_shown_at"])

    def test_multi_session_diagnostics_describe_latest_heartbeat_without_hiding_its_error(self):
        self.poll()
        self.clock.return_value = 11
        self.broker.poll({"client_id": "b" * 32, "after": None,
                          "delivery": {"ready": False, "last_error": "Sesión más reciente sin escritorio"}})
        status = self.broker.status()
        self.assertTrue(status["ready"])
        self.assertEqual(status["session_agents"], 2)
        self.assertEqual(status["last_error"], "Sesión más reciente sin escritorio")
        self.broker.poll({"client_id": CLIENT, "after": 0,
                          "delivery": {"ready": True, "last_sound_error": "Último heartbeat incluso con reloj igual"}})
        self.assertEqual(self.broker.status()["last_sound_error"], "Último heartbeat incluso con reloj igual")

    def test_diagnostic_fields_are_bounded_and_arbitrary_metadata_is_discarded(self):
        self.broker.poll({"client_id": CLIENT, "after": None,
                          "delivery": {"ready": True, "last_sound_error": "X" * 1000,
                                       "last_popup_shown_at": "Z" * 1000, "environment": {"PATH": "private"}}})
        status = self.broker.status()
        self.assertEqual(len(status["last_sound_error"]), 200)
        self.assertEqual(len(status["last_popup_shown_at"]), 80)
        self.assertNotIn("environment", status)
        self.assertNotIn("environment", self.broker._clients[CLIENT])
        with self.assertRaises(ValueError):
            self.broker.poll({"client_id": CLIENT, "after": 0,
                              "delivery": {"ready": True, "popup_error": {"command": "run"}}})


class AgentTests(unittest.TestCase):
    def test_agent_connects_and_delivers_only_broker_notices(self):
        client, notifier = Mock(), Mock()
        notifier.status.return_value = {"ready": True}
        client.poll.return_value = {"cursor": 7, "persistent": {"battery_low": {"title": "Baja", "message": "Activa"}},
                                    "messages": [{"title": "Corte", "message": "Batería", "severity": "warning", "sound": True}],
                                    "running": True}
        agent = Agent(client, notifier, session_state=lambda: "active")
        agent.step()
        client.connect.assert_called_once()
        self.assertEqual(agent.cursor, 7)
        notifier.sync_persistent.assert_called_once()
        notifier.notify.assert_called_once_with("Corte", "Batería", severity="warning", sound=True, persistent=False, key=None)
        agent.step()
        client.connect.assert_called_once()
        self.assertEqual(client.poll.call_args.args[1], 7)

    def test_cancel_failure_is_reported_without_claiming_success(self):
        client, notifier = Mock(), Mock()
        client.cancel.side_effect = OSError("unavailable")
        Agent(client, notifier).cancel()
        self.assertIn("No se pudo confirmar", notifier.notify.call_args.args[1])
        self.assertEqual(notifier.notify.call_args.kwargs["severity"], "warning")

    def test_agent_exit_waits_for_monitor_stop_before_closing_tray(self):
        client, notifier = Mock(), Mock()
        agent = Agent(client, notifier)
        def confirm_stop():
            self.assertFalse(agent.stop_event.is_set())
            return {"stopped": True}
        client.stop_monitor.side_effect = confirm_stop
        notifier.exit_callback()
        self.assertTrue(agent.stop_event.is_set())
        client.stop_monitor.assert_called_once_with()
        client.cancel.assert_not_called()
        notifier.notify.assert_not_called()
        notifier.exit_callback()
        client.stop_monitor.assert_called_once_with()

    def test_agent_exit_failure_preserves_tray_and_reports_reason(self):
        for error in (OSError("No hay conexión local"), ValueError("La prueba del UPS sigue pendiente")):
            with self.subTest(error=error):
                client, notifier = Mock(), Mock()
                client.stop_monitor.side_effect = error
                agent = Agent(client, notifier)
                notifier.exit_callback()
                self.assertFalse(agent.stop_event.is_set())
                self.assertIn(str(error), notifier.notify.call_args.args[1])
                self.assertEqual(notifier.notify.call_args.kwargs, {"severity": "warning", "sound": True})
                client.stop_monitor.side_effect = None
                client.stop_monitor.return_value = {"stopping": True}
                notifier.exit_callback()
                self.assertTrue(agent.stop_event.is_set())

    def test_installed_agent_uses_shared_exit_label_and_confirmation(self):
        notifier = WindowsNotifier("http://127.0.0.1:8765")
        kernel, ownership = Mock(), Mock()
        with patch.object(session_agent, "_WindowsOwnership", return_value=ownership), \
                patch.object(session_agent, "_kernel", return_value=kernel), \
                patch.object(session_agent, "WindowsNotifier", return_value=notifier) as constructor, \
                patch.object(session_agent, "Agent") as agent:
            self.assertEqual(session_agent.main([]), 0)
        constructor.assert_called_once_with("http://127.0.0.1:8765")
        self.assertEqual(notifier.exit_label, DEFAULT_EXIT_LABEL)
        self.assertEqual(notifier.exit_message, DEFAULT_EXIT_MESSAGE)
        agent.assert_called_once()
        ownership.close.assert_called_once()

    def test_installer_stop_remains_local_agent_only(self):
        with patch.object(session_agent, "stop_current_session", return_value=0) as stop, \
                patch.object(session_agent, "LocalClient") as client, \
                patch.object(session_agent, "WindowsNotifier") as notifier:
            self.assertEqual(session_agent.main(["--stop"]), 0)
        stop.assert_called_once_with()
        client.assert_not_called()
        notifier.assert_not_called()

    def test_client_stop_authenticates_and_posts_only_to_fixed_service_endpoint(self):
        client = LocalClient(8765)
        response = Mock()
        response.read.side_effect = [
            json.dumps({"csrf_token": "a" * 40, "meta": {"runtime_mode": "service"}}).encode(),
            b'{"stopped":true}',
        ]
        client.opener = MagicMock()
        client.opener.open.return_value.__enter__.return_value = response
        self.assertEqual(client.stop_monitor(), {"stopped": True})
        requests = [call.args[0] for call in client.opener.open.call_args_list]
        self.assertEqual(requests[0].full_url, "http://127.0.0.1:8765/api/status")
        self.assertEqual(requests[1].full_url, "http://127.0.0.1:8765/api/service/stop")
        self.assertEqual(requests[1].get_method(), "POST")
        self.assertEqual(requests[1].headers["X-ups-token"], "a" * 40)
        self.assertEqual(requests[1].headers["Origin"], "http://127.0.0.1:8765")
        self.assertEqual(json.loads(requests[1].data), {})

    def test_client_stop_requires_explicit_boolean_service_confirmation(self):
        client = LocalClient()
        client.token = "a" * 40
        for response in ({}, None, [], {"stopped": False}, {"stopping": False},
                         {"stopped": 1}, {"stopping": "true"}):
            with self.subTest(response=response), patch.object(client, "_request", return_value=response):
                with self.assertRaisesRegex(ValueError, "no confirmó"):
                    client.stop_monitor()
        for response in ({"stopped": True}, {"stopping": True}):
            with self.subTest(response=response), patch.object(client, "_request", return_value=response):
                self.assertEqual(client.stop_monitor(), response)

    def test_client_stop_reports_bounded_server_reason_or_generic_refusal(self):
        client = LocalClient()
        client.token = "a" * 40
        for body, expected in ((b'{"error":"Prueba pendiente"}', "Prueba pendiente"),
                               (b"not json", "rechazó"), (b"[]", "rechazó"),
                               (b'x' * 1_000_001, "rechazó")):
            with self.subTest(body=body[:80]):
                stream = io.BytesIO(body)
                error = HTTPError(client.url, 400, "Bad Request", {}, stream)
                with patch.object(client, "_request", side_effect=error):
                    with self.assertRaisesRegex(ValueError, expected):
                        client.stop_monitor()
                self.assertTrue(stream.closed)
        with patch.object(client, "_request", return_value={"error": "X" * 1000}):
            with self.assertRaises(ValueError) as result:
                client.stop_monitor()
            self.assertEqual(len(str(result.exception)), 300)

    def test_client_refuses_source_monitor_to_avoid_duplicate_desktop_delivery(self):
        client = LocalClient()
        with patch.object(client, "_request", return_value={"csrf_token": "a" * 40, "meta": {"runtime_mode": "session"}}):
            with self.assertRaisesRegex(ValueError, "directamente"):
                client.connect()
        self.assertIsNone(client.token)

    def test_client_operations_are_fixed_loopback_without_proxy_redirect_or_arbitrary_paths(self):
        client = LocalClient(8765)
        client.token = "a" * 40
        response = Mock()
        response.read.return_value = b'{"cursor":1}'
        client.opener = MagicMock()
        client.opener.open.return_value.__enter__.return_value = response
        self.assertEqual(client.poll(CLIENT, 0, {"ready": True})["cursor"], 1)
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8765/api/session/poll")
        self.assertEqual(request.headers["X-ups-token"], client.token)
        self.assertEqual(request.headers["Origin"], "http://127.0.0.1:8765")
        self.assertEqual(client.opener.open.call_args.kwargs["timeout"], 3)
        with self.assertRaises(ValueError):
            client._request("http://evil.test/run", {})
        with self.assertRaises(ValueError):
            LocalClient(0)

    def test_client_forwards_only_bounded_diagnostic_whitelist(self):
        client = LocalClient()
        with patch.object(client, "_request", return_value={}) as request:
            client.poll(CLIENT, 0, {"ready": True, "last_sound_error": "X" * 300,
                                  "last_popup_shown_at": "2026-10-17T22:00:01Z", "env": {"PATH": "private"}})
        delivery = request.call_args.args[1]["delivery"]
        self.assertEqual(len(delivery["last_sound_error"]), 200)
        self.assertEqual(delivery["last_popup_shown_at"], "2026-10-17T22:00:01Z")
        self.assertNotIn("env", delivery)


class BrokerRouteTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.monitor = Monitor(self.store, SimpleNamespace(source="test"), dry_run=True)
        self.broker = SessionBroker()
        self.broker.start()

    def tearDown(self):
        self.store.close()

    def handler(self, **headers):
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(server_port=8765, monitor=self.monitor, session_broker=self.broker)
        handler.path = "/api/session/poll"
        body = json.dumps({"client_id": CLIENT, "after": None, "delivery": {"ready": True}}).encode()
        handler.headers = Message()
        values = {"Host": "127.0.0.1:8765", "Content-Type": "application/json", "Content-Length": str(len(body)),
                  "Origin": "http://127.0.0.1:8765", "X-UPS-Token": self.monitor.csrf_token}
        values.update(headers)
        for key, value in values.items():
            handler.headers[key] = value
        handler.rfile = io.BytesIO(body)
        handler._json, handler._send = Mock(), Mock()
        return handler

    def test_broker_requires_same_origin_host_and_csrf_before_accepting_heartbeat(self):
        for bad in ({"Host": "other.test"}, {"Origin": "http://other.test"}, {"X-UPS-Token": "wrong"}):
            handler = self.handler(**bad)
            handler.do_POST()
            self.assertEqual(handler._json.call_args.args[1], 403)
            self.assertFalse(self.broker.status()["ready"])
        handler = self.handler()
        handler.do_POST()
        self.assertTrue(handler._json.call_args.args[0]["running"])
        self.assertTrue(self.broker.status()["ready"])
        self.assertFalse(self.monitor.engine.policy["enabled"])

    def test_source_monitor_rejects_agent_poll_without_changing_monitor(self):
        handler = self.handler()
        del handler.server.session_broker
        handler.do_POST()
        self.assertEqual(handler._json.call_args.args[1], 409)
        self.assertFalse(self.monitor.stop_event.is_set())

    def test_favicon_is_a_fixed_binary_resource_without_utf8_suffix_or_traversal(self):
        handler = self.handler()
        handler.path = "/favicon.ico"
        handler.do_GET()
        self.assertEqual(handler._send.call_args.args[1], "image/vnd.microsoft.icon")
        self.assertEqual(handler._send.call_args.args[0][:4], b"\x00\x00\x01\x00")
        handler.path = "/favicon.ico/../../data/monitor.sqlite3"
        handler.do_GET()
        self.assertEqual(handler._json.call_args.args[1], 404)


if __name__ == "__main__":
    unittest.main()
