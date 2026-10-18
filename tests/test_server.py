import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ups_monitor.core import Store, iso_utc
from ups_monitor.adapters import TelemetryError
from ups_monitor.server import Handler, Monitor, main, windows_action


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.adapter = SimpleNamespace(source="test")
        self.action = Mock()
        self.monitor = Monitor(self.store, self.adapter, action_callback=self.action)

    def tearDown(self):
        self.store.close()

    def handler(self, path="/api/policy", body=None, **headers):
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(server_port=8765, monitor=self.monitor)
        handler.path = path
        handler.headers = Message()
        content = json.dumps(body or {}).encode()
        values = {"Host": "127.0.0.1:8765", "Content-Type": "application/json",
                  "Content-Length": str(len(content)), "X-UPS-Token": self.monitor.csrf_token}
        values.update(headers)
        for key, value in values.items():
            handler.headers[key] = value
        handler.rfile = io.BytesIO(content)
        handler._json = Mock()
        handler._send = Mock()
        return handler

    def test_host_and_origin_gate_read_and_write(self):
        for header in [{"Host": "attacker.example:8765"}, {"Host": "127.0.0.1.evil:8765"},
                       {"Host": "localhost"}, {"Origin": "http://attacker.example"},
                       {"Origin": "null"}, {"Origin": "http://127.0.0.1:9999"}]:
            handler = self.handler(body={"enabled": True}, **header)
            handler.do_POST()
            self.assertEqual(handler._json.call_args.args[1], 403)
            self.assertFalse(self.monitor.engine.policy["enabled"])
            handler.do_GET()
            self.assertEqual(handler._json.call_args.args[1], 403)

    def test_csrf_token_is_required_for_mutations(self):
        for token in ["", "incorrect"]:
            handler = self.handler(body={"enabled": True}, **{"X-UPS-Token": token})
            handler.do_POST()
            self.assertEqual(handler._json.call_args.args[1], 403)
            self.assertFalse(self.monitor.engine.policy["enabled"])

    def test_same_origin_policy_request_is_strictly_validated(self):
        handler = self.handler(body={"enabled": "false"})
        handler.do_POST()
        self.assertEqual(handler._json.call_args.args[1], 400)
        self.assertFalse(self.monitor.engine.policy["enabled"])
        handler = self.handler(body={"enabled": True}, Origin="http://127.0.0.1:8765")
        handler.do_POST()
        self.assertTrue(self.monitor.engine.policy["enabled"])
        self.action.assert_not_called()

    def test_non_json_and_large_bodies_are_rejected_before_reading(self):
        for headers in [{"Content-Type": "text/plain"}, {"Content-Length": "999999"},
                        {"Content-Length": "-1"}]:
            handler = self.handler(**headers)
            handler.rfile = Mock()
            handler.do_POST()
            self.assertEqual(handler._json.call_args.args[1], 400)
            handler.rfile.read.assert_not_called()

    def test_export_and_invalid_history_never_mutate_policy(self):
        handler = self.handler(path="/api/history?hours=nan")
        handler.do_GET()
        self.assertEqual(handler._json.call_args.args[1], 400)
        self.action.assert_not_called()

    def test_simulation_is_rejected_while_real_policy_is_enabled(self):
        self.monitor.set_policy({"enabled": True, "countdown_seconds": 10})
        with self.assertRaises(ValueError):
            self.monitor.test()
        self.assertIsNone(self.monitor.test_deadline)
        self.action.assert_not_called()

    def test_enabling_policy_cancels_simulation_before_real_countdown(self):
        self.monitor.test()
        self.monitor.set_policy({"enabled": True, "countdown_seconds": 10})
        self.adapter.read = lambda: {"timestamp": iso_utc(), "on_battery": True,
                                     "battery_percent": 5, "low_battery": None}
        self.monitor.tick()
        snapshot = self.monitor.snapshot()
        self.assertFalse(snapshot["protection"]["simulation"],
                         "A simulated notice must not hide a real shutdown countdown")
        self.assertEqual(snapshot["protection"]["countdown_remaining"], 10)
        self.action.assert_not_called()

    def test_test_countdown_never_calls_operating_system(self):
        with patch("ups_monitor.server.time.monotonic", return_value=100):
            self.monitor.test()
        with patch("ups_monitor.server.time.monotonic", return_value=116):
            self.monitor.snapshot()
        self.action.assert_not_called()
        self.assertTrue(any("finalizada" in event["message"] for event in self.store.events()))

    def test_windows_shutdown_never_forces_application_termination(self):
        with patch("ups_monitor.server.os.name", "nt"), patch("ups_monitor.server.subprocess.run") as run:
            run.return_value.returncode = 0
            windows_action("shutdown")
            args = run.call_args.args[0]
        self.assertEqual(args[-3:], ["/s", "/t", "0"])
        self.assertNotIn("/f", args)

    def test_dry_run_blocks_real_action(self):
        monitor = Monitor(self.store, self.adapter, dry_run=True, action_callback=self.action)
        monitor._act("shutdown")
        self.action.assert_not_called()
        self.assertEqual(self.store.events()[0]["kind"], "simulation")

    def test_dry_run_completed_policy_is_explicitly_simulated(self):
        monitor = Monitor(self.store, self.adapter, dry_run=True, action_callback=self.action)
        monitor.set_policy({"enabled": True, "countdown_seconds": 10})
        self.adapter.read = lambda: {"timestamp": iso_utc(), "on_battery": True, "low_battery": True}
        clock = Mock(return_value=100)
        monitor.engine._clock = clock
        monitor.tick()
        clock.return_value = 111
        monitor.tick()
        protection = monitor.snapshot()["protection"]
        self.assertTrue(protection["simulation"])
        self.assertIn("simulada", protection["reason"])
        self.assertFalse(any(event["kind"] == "policy_action_started" for event in self.store.events()))
        self.action.assert_not_called()

    def test_stale_snapshot_cancels_countdown_and_hides_old_measurements(self):
        self.monitor.set_policy({"enabled": True, "countdown_seconds": 10})
        self.adapter.read = lambda: {"timestamp": iso_utc(), "on_battery": True, "low_battery": True}
        self.monitor.tick()
        self.monitor.sample["timestamp"] = iso_utc(datetime.now(timezone.utc) - timedelta(seconds=16))
        snapshot = self.monitor.snapshot()
        self.assertEqual(snapshot["connection"]["state"], "stale")
        self.assertIsNone(snapshot["sample"])
        self.assertIsNone(snapshot["protection"]["countdown_remaining"])
        self.assertEqual(self.monitor.engine.status()["state"], "unknown")
        self.action.assert_not_called()

    def test_stale_adapter_result_and_dead_reader_never_claim_connected(self):
        self.adapter.read = lambda: {"timestamp": iso_utc(datetime.now(timezone.utc) - timedelta(seconds=30)), "on_battery": False}
        self.monitor.tick()
        self.assertIsNone(self.monitor.snapshot()["sample"])
        self.monitor.thread = Mock(is_alive=Mock(return_value=False))
        snapshot = self.monitor.snapshot()
        self.assertEqual(snapshot["connection"]["error_kind"], "backend")
        self.assertEqual(snapshot["connection"]["state"], "disconnected")

    def test_stop_during_read_discards_result_and_never_calls_windows(self):
        def read():
            self.monitor.stop_event.set()
            return {"timestamp": iso_utc(), "on_battery": True, "low_battery": True}
        self.adapter.read = read
        self.monitor.tick()
        self.assertIsNone(self.monitor.sample)
        self.assertEqual(self.store.history(), [])
        with self.assertRaises(RuntimeError):
            self.monitor._act("shutdown")
        self.action.assert_not_called()

    def test_repeated_telemetry_loss_is_deduplicated(self):
        self.adapter.read = Mock(side_effect=TelemetryError("USB desconectado"))
        self.monitor.tick()
        self.monitor.tick()
        self.assertEqual(len([event for event in self.store.events() if event["kind"] == "connection_lost"]), 1)

    def test_api_failure_returns_500_without_fabricating_telemetry_or_logging_secrets(self):
        handler = self.handler(path="/api/status")
        with patch.object(self.monitor, "snapshot", side_effect=RuntimeError("secret-token")):
            with self.assertLogs("elipro.monitor", level="ERROR") as logs:
                handler.do_GET()
        self.assertEqual(handler._json.call_args.args[1], 500)
        self.assertNotIn("secret-token", "".join(logs.output))
        self.assertIsNone(self.monitor.sample)

    def test_stop_fails_explicitly_if_reader_does_not_exit(self):
        self.monitor.thread = Mock(is_alive=Mock(return_value=True))
        with self.assertLogs("elipro.monitor", level="ERROR"), self.assertRaises(RuntimeError):
            self.monitor.stop()
        self.monitor.thread.join.assert_called_once_with(timeout=12)
        self.assertIsNone(self.monitor.sample)

    def test_cleanup_closes_socket_but_keeps_database_open_for_unfinished_reader(self):
        monitor, server, store = Mock(), Mock(), Mock()
        ownership = Mock()
        monitor.engine.policy = {"enabled": False}
        monitor.thread.is_alive.return_value = True
        monitor.stop.side_effect = RuntimeError("Lector aún activo")
        server.server_close.side_effect = lambda: self.assertTrue(monitor.stop.called)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            with patch("sys.argv", ["elipro", "--data-dir", directory]), \
                 patch("ups_monitor.server.make_adapter"), \
                 patch("ups_monitor.server.ResilientStore.open", return_value=store), \
                 patch("ups_monitor.server.MonitorInstanceGuard", return_value=ownership), \
                 patch("ups_monitor.server.Monitor", return_value=monitor), \
                 patch("ups_monitor.server.WindowsNotifier"), \
                 patch("ups_monitor.server.AlertManager"), \
                 patch("ups_monitor.server.ThreadingHTTPServer", return_value=server), \
                 patch("builtins.print"), self.assertRaises(RuntimeError):
                main()
        server.server_close.assert_called_once()
        store.close.assert_not_called()
        ownership.close.assert_not_called()

    def test_stop_request_still_shuts_server_down_if_client_disconnects(self):
        handler = self.handler(path="/api/service/stop")
        handler.server.shutdown = Mock()
        handler._json.side_effect = BrokenPipeError()
        with patch("ups_monitor.server.threading.Thread") as thread:
            handler.do_POST()
        thread.return_value.start.assert_called_once()
        self.assertTrue(self.monitor.stop_event.is_set())

    def test_explicit_stop_does_not_become_a_restart_after_cleanup_failure(self):
        monitor, server, store, ownership = Mock(), Mock(), Mock(), Mock()
        monitor.engine.policy = {"enabled": False}
        monitor.thread.is_alive.return_value = True
        monitor.stop.side_effect = RuntimeError("Simulated cleanup failure")
        server.serve_forever.side_effect = lambda **kwargs: server.mark_exit_requested()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            with patch("sys.argv", ["elipro", "--data-dir", directory]), \
                 patch("ups_monitor.server.make_adapter"), \
                 patch("ups_monitor.server.ResilientStore.open", return_value=store), \
                 patch("ups_monitor.server.MonitorInstanceGuard", return_value=ownership), \
                 patch("ups_monitor.server.Monitor", return_value=monitor), \
                 patch("ups_monitor.server.WindowsNotifier"), \
                 patch("ups_monitor.server.AlertManager"), \
                 patch("ups_monitor.server.ThreadingHTTPServer", return_value=server), \
                 patch("builtins.print"), self.assertLogs("elipro.monitor", level="ERROR"):
                self.assertIsNone(main())
        self.assertIs(ownership.user_exit_requested, True)
        store.close.assert_not_called()
        ownership.close.assert_not_called()

    def test_stop_marks_user_intent_before_scheduling_exit_callback(self):
        handler = self.handler(path="/api/service/stop")
        handler.server.shutdown = Mock()
        handler.server.mark_exit_requested = Mock()
        handler.server.request_exit = Mock()
        with patch("ups_monitor.server.threading.Thread") as thread:
            handler.do_POST()
        handler.server.mark_exit_requested.assert_called_once_with()
        self.assertIs(thread.call_args.kwargs["target"], handler.server.request_exit)
        self.assertTrue(self.monitor.stop_event.is_set())

    def test_guardian_restart_is_audited_without_enabling_protection(self):
        monitor, server, store, ownership = Mock(), Mock(), Mock(), Mock()
        monitor.engine.policy = {"enabled": False}
        monitor.thread.is_alive.return_value = False
        monitor.supervisor_thread.is_alive.return_value = False
        monitor.processor_thread = None
        store.close.return_value = True
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            with patch("sys.argv", ["elipro", "--data-dir", directory, "--dry-run"]), \
                 patch.dict("os.environ", {"ELIPRO_GUARDIAN_RESTARTS": "2"}), \
                 patch("ups_monitor.server.make_adapter"), \
                 patch("ups_monitor.server.ResilientStore.open", return_value=store), \
                 patch("ups_monitor.server.MonitorInstanceGuard", return_value=ownership), \
                 patch("ups_monitor.server.Monitor", return_value=monitor), \
                 patch("ups_monitor.server.WindowsNotifier"), \
                 patch("ups_monitor.server.AlertManager"), \
                 patch("ups_monitor.server.ThreadingHTTPServer", return_value=server), \
                 patch("builtins.print"):
                main()
        event = monitor._event.call_args
        self.assertEqual(event.args[0], "monitor_restarted")
        self.assertEqual(event.args[2], {"restart_count": 2})
        self.assertFalse(monitor.engine.policy["enabled"])
        ownership.close.assert_called_once_with()

    def test_cleanup_keeps_database_and_ownership_for_unfinished_consumer(self):
        monitor, server, store, ownership = Mock(), Mock(), Mock(), Mock()
        monitor.engine.policy = {"enabled": False}
        monitor.thread.is_alive.return_value = False
        monitor.supervisor_thread.is_alive.return_value = False
        monitor.processor_thread.is_alive.return_value = True
        monitor.stop.side_effect = RuntimeError("Consumer still active")
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            with patch("sys.argv", ["elipro", "--data-dir", directory]), \
                 patch("ups_monitor.server.make_adapter"), \
                 patch("ups_monitor.server.ResilientStore.open", return_value=store), \
                 patch("ups_monitor.server.MonitorInstanceGuard", return_value=ownership), \
                 patch("ups_monitor.server.Monitor", return_value=monitor), \
                 patch("ups_monitor.server.WindowsNotifier"), \
                 patch("ups_monitor.server.AlertManager"), \
                 patch("ups_monitor.server.ThreadingHTTPServer", return_value=server), \
                 patch("builtins.print"), self.assertRaisesRegex(RuntimeError, "Consumer"):
                main()
        store.close.assert_not_called()
        ownership.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
