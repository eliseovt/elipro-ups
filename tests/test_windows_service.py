"""SCM unit/lifecycle tests with fake APIs. Never install a local service/USB."""
import ctypes
import io
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import tempfile
import unittest
from unittest.mock import Mock, patch

from ups_monitor import entrypoint, runtime, windows_service as service
from ups_monitor.server import _run_owned_monitor


class FakeHostAPI:
    def __init__(self):
        self.states = []
        self.handler = None

    def RegisterServiceCtrlHandlerExW(self, name, handler, context):
        self.handler = handler
        return 17

    def SetServiceStatus(self, handle, pointer):
        status = ctypes.cast(pointer, ctypes.POINTER(service.SERVICE_STATUS)).contents
        self.states.append({"state": status.state, "accepted": status.accepted,
                            "exit_code": status.exit_code, "wait_hint": status.wait_hint})
        return 1


class ServiceTests(unittest.TestCase):
    def args(self, **values):
        return SimpleNamespace(data_dir=Path("test-service-data"), port=8765, self_test=False, **values)

    def test_registration_command_is_absolute_and_safely_quoted_without_power_flags(self):
        with patch.object(runtime.sys, "frozen", True, create=True), \
                patch.object(runtime.sys, "executable", r"C:\Program Files\Elipro UPS\Elipro.exe"):
            vector = service.service_command(Path("data with spaces"), self_test=True)
        self.assertEqual(vector[:3], [r"C:\Program Files\Elipro UPS\Elipro.exe", "service", "run"])
        self.assertTrue(Path(vector[4]).is_absolute())
        self.assertIn("--self-test", vector)
        self.assertNotIn("--allow-power-actions", vector)
        with self.assertRaises(ValueError):
            service.service_command("data", port=0)

    def test_service_target_is_headless_native_and_always_dry_run(self):
        api = FakeHostAPI()
        target = Mock()
        target.side_effect = lambda argv, **kw: kw["ready_callback"]()
        host = service.ServiceHost(self.args(), api=api, target=target)
        host._main(0, None)
        vector = target.call_args.args[0]
        self.assertIn("--headless", vector)
        self.assertIn("--dry-run", vector)
        self.assertEqual(vector[vector.index("--source") + 1], "native")
        self.assertEqual([state["state"] for state in api.states], [2, 4, 1])
        self.assertEqual(api.states[1]["accepted"], 5)
        self.assertIs(target.call_args.kwargs["stop_event"], host.stop_event)

    def test_scm_stop_notifies_target_and_reports_clean_stop_without_windows_action(self):
        api = FakeHostAPI()
        def target(argv, *, stop_event, ready_callback):
            ready_callback()
            api.handler(1, 0, None, None)
            self.assertTrue(stop_event.is_set())
        host = service.ServiceHost(self.args(), api=api, target=target)
        with patch("ups_monitor.server.windows_action") as power:
            host._main(0, None)
        power.assert_not_called()
        self.assertEqual([state["state"] for state in api.states], [2, 4, 3, 1])
        self.assertEqual(api.states[-1]["exit_code"], 0)

    def test_self_test_lifecycle_has_no_monitor_or_usb(self):
        api = FakeHostAPI()
        arguments = self.args()
        arguments.self_test = True
        host = service.ServiceHost(arguments, api=api)
        thread = threading.Thread(target=host._main, args=(0, None))
        with patch("ups_monitor.server.main") as monitor, patch("ups_monitor.hid_windows._apis") as usb:
            thread.start()
            self.assertTrue(self._until(lambda: bool(api.states and api.states[-1]["state"] == 4)))
            api.handler(1, 0, None, None)
            thread.join(2)
        self.assertFalse(thread.is_alive())
        monitor.assert_not_called()
        usb.assert_not_called()
        self.assertEqual(api.states[-1]["state"], 1)

    @staticmethod
    def _until(condition):
        import time
        deadline = time.monotonic() + 2
        while not condition() and time.monotonic() < deadline:
            time.sleep(0.005)
        return condition()

    def test_failure_reports_nonzero_to_scm_for_recovery(self):
        api = FakeHostAPI()
        host = service.ServiceHost(self.args(), api=api, target=Mock(side_effect=RuntimeError("secret")))
        with patch.object(host, "_log_failure") as log:
            host._main(0, None)
        log.assert_called_once()
        self.assertEqual(api.states[-1]["state"], service.STOPPED)
        self.assertEqual(api.states[-1]["exit_code"], 1066)

    def test_stop_during_startup_cannot_report_running_after_stop(self):
        api = FakeHostAPI()
        def target(argv, *, stop_event, ready_callback):
            api.handler(1, 0, None, None)
            ready_callback()
        host = service.ServiceHost(self.args(), api=api, target=target)
        host._main(0, None)
        self.assertNotIn(service.RUNNING, [state["state"] for state in api.states])

    def test_missing_service_stop_and_remove_succeed_without_creating_anything(self):
        api = Mock()
        api.OpenSCManagerW.return_value = 1
        api.OpenServiceW.return_value = None
        with patch.object(ctypes, "get_last_error", return_value=1060, create=True):
            for command in ("stop", "remove", "status"):
                self.assertFalse(service.manage(command, api=api)["installed"])
            with self.assertRaisesRegex(RuntimeError, "no está instalado"):
                service.manage("start", api=api)
        api.CreateServiceW.assert_not_called()

    def test_registration_uses_local_system_autostart_and_only_restart_recovery_actions(self):
        api = Mock()
        api.OpenSCManagerW.return_value = 1
        api.OpenServiceW.return_value = None
        api.CreateServiceW.return_value = 2
        with patch.object(ctypes, "get_last_error", return_value=1060, create=True), \
                patch.object(service, "_query", return_value={"state": "stopped"}):
            service.manage("install", api=api, self_test=True)
        arguments = api.CreateServiceW.call_args.args
        self.assertEqual(arguments[4:7], (0x10, 2, 1))
        self.assertTrue(arguments[3] & service.SERVICE_START)
        self.assertIsNone(arguments[-2])  # NULL account = LocalSystem
        self.assertIn("--self-test", arguments[7])
        failure = ctypes.cast(api.ChangeServiceConfig2W.call_args_list[0].args[2],
                              ctypes.POINTER(service.FAILURE_ACTIONS)).contents
        self.assertEqual([failure.actions[index].type for index in range(failure.count)], [1, 1, 1])
        self.assertIsNone(failure.command)
        self.assertIsNone(failure.reboot)
        self.assertEqual(api.CloseServiceHandle.call_count, 2)

    def test_upgrade_rejects_another_program_even_if_it_uses_same_service_name(self):
        api = Mock()
        api.OpenSCManagerW.return_value, api.OpenServiceW.return_value = 1, 2
        with patch.object(service, "_configuration", return_value={"binary_path": '"C:\\Other App\\Other.exe" service run'}):
            with self.assertRaisesRegex(RuntimeError, "otro ejecutable"):
                service.manage("install", api=api)
        api.ChangeServiceConfigW.assert_not_called()
        api.ControlService.assert_not_called()

    def test_service_and_agent_dispatch_never_fall_through_to_monitor(self):
        with patch("ups_monitor.windows_service.main", return_value=0) as scm, \
                patch("ups_monitor.session_agent.main", return_value=0) as agent, \
                patch("ups_monitor.server.main") as monitor:
            self.assertEqual(entrypoint.main(["service", "status"]), 0)
            scm.assert_called_once_with(["status"])
            self.assertEqual(entrypoint.main(["agent"]), 0)
            agent.assert_called_once_with([])
            with patch.object(entrypoint, "is_frozen", return_value=True):
                self.assertEqual(entrypoint.main([]), 0)
            self.assertEqual(agent.call_args.args[0], ["--open-panel"])
            monitor.assert_not_called()

    def test_headless_monitor_uses_broker_and_service_stop_closes_reader_without_desktop(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            args = SimpleNamespace(source="native", forza_url="unused", diagnose=False,
                                   data_dir=Path(directory), headless=True, port=8765,
                                   dry_run=True, observe_hours=0)
            monitor = Mock()
            monitor.engine.policy = {"enabled": False}
            monitor.thread = monitor.supervisor_thread = monitor.processor_thread = None
            monitor.stop_event = threading.Event()
            stopped, ready = threading.Event(), Mock()
            stopped.set()
            http, store, ownership, broker = Mock(), Mock(), Mock(), Mock()
            store.close.return_value = True
            with patch("ups_monitor.server.make_adapter"), \
                    patch("ups_monitor.server.ResilientStore.open", return_value=store), \
                    patch("ups_monitor.server.Monitor", return_value=monitor), \
                    patch("ups_monitor.server.WindowsNotifier") as desktop, \
                    patch("ups_monitor.session_broker.SessionBroker", return_value=broker), \
                    patch("ups_monitor.server.AlertManager") as alerts, \
                    patch("ups_monitor.server.StabilityObservation"), \
                    patch("ups_monitor.server.ThreadingHTTPServer", return_value=http), patch("builtins.print"):
                _run_owned_monitor(args, ownership, stop_event=stopped, ready_callback=ready)
            desktop.assert_not_called()
            self.assertIs(alerts.call_args.args[1], broker)
            self.assertIs(http.session_broker, broker)
            self.assertEqual(monitor.runtime_mode, "service")
            ready.assert_called_once()
            monitor.stop.assert_called_once()
            self.assertTrue(self._until(lambda: http.shutdown.called))
            ownership.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
