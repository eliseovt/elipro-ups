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


class ControlWorkerAuthorizationTests(unittest.TestCase):
    def token_apis(self, *, system=True, size=None, read_ok=True, sid=123):
        kernel, security = Mock(), Mock()
        kernel.GetCurrentProcess.return_value = 42

        def open_token(process, access, pointer):
            ctypes.cast(pointer, ctypes.POINTER(service.HANDLE)).contents.value = 17
            return 1

        def information(token, kind, buffer, length, needed):
            ctypes.cast(needed, ctypes.POINTER(service.DWORD)).contents.value = (
                ctypes.sizeof(service.TOKEN_USER) if size is None else size)
            if buffer is None:
                return 0
            if not read_ok:
                return 0
            user = service.TOKEN_USER(sid, 0)
            ctypes.memmove(buffer, ctypes.byref(user), ctypes.sizeof(user))
            return 1

        security.OpenProcessToken.side_effect = open_token
        security.GetTokenInformation.side_effect = information
        security.IsWellKnownSid.return_value = int(system)
        return kernel, security

    def test_token_user_sid_is_checked_against_local_system_and_handle_is_closed(self):
        kernel, security = self.token_apis()
        with patch.object(service.os, "name", "nt"), patch.object(service, "_token_apis", return_value=(kernel, security)):
            self.assertTrue(service._is_local_system())
        self.assertEqual(security.OpenProcessToken.call_args.args[:2], (42, 0x0008))
        security.IsWellKnownSid.assert_called_once_with(123, 22)
        self.assertEqual(kernel.CloseHandle.call_args.args[0].value, 17)
        self.assertTrue(all(call.args[1] == 1 for call in security.GetTokenInformation.call_args_list))

    def test_forged_account_environment_does_not_make_another_sid_local_system(self):
        kernel, security = self.token_apis(system=False)
        with patch.dict(service.os.environ, {"USERNAME": "SYSTEM", "USERDOMAIN": "NT AUTHORITY"}), \
                patch.object(service.os, "name", "nt"), \
                patch.object(service, "_token_apis", return_value=(kernel, security)):
            self.assertFalse(service._is_local_system())
        security.IsWellKnownSid.assert_called_once_with(123, 22)

    def test_token_query_errors_and_invalid_sizes_or_sid_fail_closed(self):
        for options in ({"size": 0}, {"size": 65537}, {"read_ok": False}, {"sid": None}):
            with self.subTest(options=options):
                kernel, security = self.token_apis(**options)
                with patch.object(service.os, "name", "nt"), \
                        patch.object(service, "_token_apis", return_value=(kernel, security)):
                    self.assertFalse(service._is_local_system())
                security.IsWellKnownSid.assert_not_called()
                kernel.CloseHandle.assert_called_once()
        with patch.object(service.os, "name", "nt"), \
                patch.object(service, "_token_apis", side_effect=OSError("fake API error")):
            self.assertFalse(service._is_local_system())
        with patch.object(service.os, "name", "posix"), patch.object(service, "_token_apis") as apis:
            self.assertFalse(service._is_local_system())
        apis.assert_not_called()

    def authorized(self, *, status=None, binary=None, identity=True, manager=1, handle=2, query_error=None):
        api = Mock()
        api.OpenSCManagerW.return_value, api.OpenServiceW.return_value = manager, handle
        if binary is None:
            binary = service.subprocess.list2cmdline(service.service_command(
                "data", allow_power_actions=True, allow_ups_poweroff=True))
        if status is None:
            status = {"installed": True, "state": "running"}
        with patch.object(service, "_is_local_system", return_value=identity), \
                patch.object(service, "_apis", return_value=api) as apis, \
                patch.object(service, "_query", return_value=status, side_effect=query_error), \
                patch.object(service, "_configuration", return_value={"binary_path": binary}):
            result = service.control_worker_authorized()
        for name in ("CreateServiceW", "ChangeServiceConfigW", "ChangeServiceConfig2W", "StartServiceW", "ControlService", "DeleteService"):
            getattr(api, name).assert_not_called()
        return result, api, apis

    def test_control_requires_running_owned_service_with_both_registered_gates(self):
        allowed, api, _ = self.authorized()
        self.assertTrue(allowed)
        api.OpenSCManagerW.assert_called_once_with(None, None, service.SC_MANAGER_CONNECT)
        api.OpenServiceW.assert_called_once_with(1, service.NAME, service.SERVICE_QUERY_STATUS | service.SERVICE_QUERY_CONFIG)
        self.assertEqual([call.args[0] for call in api.CloseServiceHandle.call_args_list], [2, 1])
        prefix = service.subprocess.list2cmdline(service.child_command("service", "run"))
        for suffix in ("", " --allow-power-actions", " --allow-ups-poweroff",
                       " --allow-power-actions --allow-ups-poweroff --self-test",
                       " --allow-power-actions --allow-ups-poweroff --simulate"):
            with self.subTest(suffix=suffix):
                self.assertFalse(self.authorized(binary=prefix + suffix)[0])
        self.assertFalse(self.authorized(binary='"C:\\Other App\\Other.exe" service run --allow-power-actions --allow-ups-poweroff')[0])

    def test_absent_stopping_or_unknown_service_and_non_system_process_are_rejected(self):
        for status in ({"installed": False, "state": "absent"}, {"installed": 1, "state": "running"},
                       {"installed": True, "state": "stop_pending"}, {"installed": True, "state": "stopped"},
                       {"installed": True, "state": "start_pending"}, {"installed": True, "state": "unknown"}):
            with self.subTest(status=status):
                self.assertFalse(self.authorized(status=status)[0])
        allowed, api, apis = self.authorized(identity=False)
        self.assertFalse(allowed)
        apis.assert_not_called()
        api.OpenSCManagerW.assert_not_called()

    def test_scm_access_failures_are_closed_without_leaking_open_handles(self):
        allowed, api, _ = self.authorized(manager=0)
        self.assertFalse(allowed)
        api.CloseServiceHandle.assert_not_called()
        allowed, api, _ = self.authorized(handle=0)
        self.assertFalse(allowed)
        api.CloseServiceHandle.assert_called_once_with(1)
        allowed, api, _ = self.authorized(query_error=OSError("fake query denied"))
        self.assertFalse(allowed)
        self.assertEqual([call.args[0] for call in api.CloseServiceHandle.call_args_list], [2, 1])
        with patch.object(service, "_is_local_system", side_effect=OSError("fake token error")), \
                patch.object(service, "_apis") as apis:
            self.assertFalse(service.control_worker_authorized())
        apis.assert_not_called()


class ServiceTests(unittest.TestCase):
    def args(self, **values):
        return SimpleNamespace(**{"data_dir": Path("test-service-data"), "port": 8765, "self_test": False, **values})

    def test_registration_command_is_absolute_and_safely_quoted_without_power_flags(self):
        with patch.object(runtime.sys, "frozen", True, create=True), \
                patch.object(runtime.sys, "executable", r"C:\Program Files\Elipro UPS\Elipro.exe"):
            vector = service.service_command(Path("data with spaces"), self_test=True)
        self.assertEqual(vector[:3], [r"C:\Program Files\Elipro UPS\Elipro.exe", "service", "run"])
        self.assertTrue(Path(vector[4]).is_absolute())
        self.assertIn("--self-test", vector)
        self.assertNotIn("--allow-power-actions", vector)
        self.assertNotIn("--allow-ups-poweroff", vector)
        with self.assertRaises(ValueError):
            service.service_command("data", port=0)

    def test_service_target_is_headless_native_and_dry_run_by_default(self):
        api = FakeHostAPI()
        target = Mock()
        target.side_effect = lambda argv, **kw: kw["ready_callback"]()
        host = service.ServiceHost(self.args(), api=api, target=target)
        host._main(0, None)
        vector = target.call_args.args[0]
        self.assertIn("--headless", vector)
        self.assertIn("--dry-run", vector)
        self.assertNotIn("--allow-ups-poweroff", vector)
        self.assertEqual(vector[vector.index("--source") + 1], "native")
        self.assertEqual([state["state"] for state in api.states], [2, 4, 1])
        self.assertEqual(api.states[1]["accepted"], 5 | service.SERVICE_ACCEPT_POWEREVENT)
        self.assertIs(target.call_args.kwargs["stop_event"], host.stop_event)

    def test_explicit_capability_is_registered_and_passed_to_service_target_only(self):
        vector = service.service_command("data", allow_power_actions=True)
        self.assertIn("--allow-power-actions", vector)
        api, target = FakeHostAPI(), Mock()
        target.side_effect = lambda argv, **kw: kw["ready_callback"]()
        host = service.ServiceHost(self.args(allow_power_actions=True), api=api, target=target)
        host._main(0, None)
        launched = target.call_args.args[0]
        self.assertIn("--headless", launched)
        self.assertNotIn("--dry-run", launched)
        self.assertNotIn("--allow-power-actions", launched)  # consumed by SCM host
        self.assertNotIn("--enabled", launched)  # saved policy is an independent gate
        self.assertNotIn("--allow-ups-poweroff", launched)
        self.assertEqual(api.states[-1]["exit_code"], 0)

    def test_ups_capability_is_explicit_and_passed_to_native_monitor_only(self):
        vector = service.service_command("data", allow_power_actions=True, allow_ups_poweroff=True)
        self.assertIn("--allow-power-actions", vector)
        self.assertIn("--allow-ups-poweroff", vector)
        api, target = FakeHostAPI(), Mock()
        target.side_effect = lambda argv, **kw: kw["ready_callback"]()
        host = service.ServiceHost(self.args(allow_power_actions=True, allow_ups_poweroff=True), api=api, target=target)
        host._main(0, None)
        launched = target.call_args.args[0]
        self.assertEqual(launched[launched.index("--source") + 1], "native")
        self.assertIn("--allow-ups-poweroff", launched)
        self.assertNotIn("--dry-run", launched)
        self.assertNotIn("--enabled", launched)
        self.assertEqual(api.states[-1]["exit_code"], 0)

    def test_invalid_ups_capabilities_fail_before_accessing_scm(self):
        cases = ({"allow_ups_poweroff": True},
                 {"allow_ups_poweroff": True, "self_test": True},
                 {"allow_ups_poweroff": True, "simulate": True},
                 {"allow_ups_poweroff": True, "allow_power_actions": True, "self_test": True},
                 {"allow_ups_poweroff": True, "allow_power_actions": True, "simulate": True},
                 {"allow_ups_poweroff": 1, "allow_power_actions": True})
        with patch.object(service, "_apis") as apis:
            for options in cases:
                with self.subTest(options=options):
                    with self.assertRaises(ValueError):
                        service.service_command("data", **options)
                    with self.assertRaises(ValueError):
                        service.manage("install", **options)
                    with self.assertRaises(ValueError):
                        service.ServiceHost(self.args(**options))
            apis.assert_not_called()

    def test_self_test_rejects_power_capability_before_touching_scm(self):
        with self.assertRaisesRegex(ValueError, "no admite"):
            service.service_command("data", self_test=True, allow_power_actions=True)
        with patch.object(service, "_apis") as apis:
            with self.assertRaisesRegex(ValueError, "no admite"):
                service.manage("install", self_test=True, allow_power_actions=True)
            args = self.args(allow_power_actions=True)
            args.self_test = True
            with self.assertRaisesRegex(ValueError, "no admite"):
                service.ServiceHost(args)
            apis.assert_not_called()

    def test_power_capability_cannot_be_changed_by_start_or_status(self):
        with patch.object(service, "_apis") as apis:
            for command in ("start", "status", "stop", "remove"):
                with self.assertRaisesRegex(ValueError, "registrar"):
                    service.manage(command, allow_power_actions=True)
                with self.assertRaisesRegex(ValueError, "registrar"):
                    service.manage(command, allow_power_actions=True, allow_ups_poweroff=True)
            apis.assert_not_called()

    def test_cli_rejects_self_test_power_capability_combination(self):
        with patch.object(service, "ServiceHost") as host, patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as result:
                service.main(["run", "--self-test", "--allow-power-actions"])
        self.assertEqual(result.exception.code, 2)
        host.assert_not_called()

    def test_cli_rejects_invalid_ups_gate_combinations_without_changing_registration(self):
        cases = (["run", "--allow-ups-poweroff"],
                 ["run", "--self-test", "--allow-ups-poweroff"],
                 ["run", "--simulate", "--allow-ups-poweroff"],
                 ["run", "--simulate", "--allow-power-actions", "--allow-ups-poweroff"],
                 ["start", "--allow-power-actions", "--allow-ups-poweroff"],
                 ["status", "--allow-ups-poweroff"])
        with patch.object(service, "ServiceHost") as host, patch.object(service, "manage") as manage, \
                patch("sys.stderr", new_callable=io.StringIO):
            for argv in cases:
                with self.subTest(argv=argv), self.assertRaises(SystemExit) as result:
                    service.main(argv)
                self.assertEqual(result.exception.code, 2)
        host.assert_not_called()
        manage.assert_not_called()

    def test_cli_install_passes_both_admin_gates_to_registration(self):
        with patch.object(service, "manage", return_value={}) as manage, patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(service.main(["install", "--allow-power-actions", "--allow-ups-poweroff"]), 0)
        self.assertEqual(manage.call_args.args, ("install",))
        self.assertTrue(manage.call_args.kwargs["allow_power_actions"])
        self.assertTrue(manage.call_args.kwargs["allow_ups_poweroff"])

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

    def test_scm_power_notifications_only_forward_to_ready_monitor_without_io_or_restart(self):
        api, monitor = FakeHostAPI(), Mock()
        received = []
        monitor.handle_power_event.side_effect = received.append

        def target(argv, *, stop_event, ready_callback):
            # Before the monitor is attached, startup notifications are ignored.
            self.assertEqual(api.handler(service.SERVICE_CONTROL_POWEREVENT, service.PBT_APMSUSPEND, None, None), 0)
            monitor.handle_power_event.assert_not_called()
            ready_callback(monitor)
            count = len(api.states)
            for event in (service.PBT_APMSUSPEND, service.PBT_APMRESUMEAUTOMATIC, service.PBT_APMRESUMESUSPEND):
                self.assertEqual(api.handler(service.SERVICE_CONTROL_POWEREVENT, event, None, None), 0)
            # Unknown power events do not dereference their optional payload.
            self.assertEqual(api.handler(service.SERVICE_CONTROL_POWEREVENT, 0x8013, 123, None), 0)
            self.assertEqual(len(api.states), count)
            self.assertFalse(stop_event.is_set())

        host = service.ServiceHost(self.args(), api=api, target=target)
        with patch("ups_monitor.server.windows_action") as power, \
                patch("ups_monitor.hid_windows._apis") as usb, \
                patch.object(host, "_log_failure") as log, patch.object(service.time, "sleep") as wait:
            host._main(0, None)
        self.assertEqual(received, ["suspend", "resume", "resume"])
        self.assertEqual(monitor.method_calls, [unittest.mock.call.handle_power_event(value) for value in received])
        power.assert_not_called()
        usb.assert_not_called()
        log.assert_not_called()
        wait.assert_not_called()
        self.assertEqual([state["state"] for state in api.states], [2, 4, 1])
        self.assertIsNone(host.monitor)

    def test_scm_power_events_are_ignored_after_stop_and_callback_errors_do_not_escape(self):
        api, monitor = FakeHostAPI(), Mock()
        monitor.handle_power_event.side_effect = RuntimeError("fake nonblocking callback failure")

        def target(argv, *, stop_event, ready_callback):
            ready_callback(monitor)
            self.assertEqual(api.handler(service.SERVICE_CONTROL_POWEREVENT, service.PBT_APMSUSPEND, None, None), 0)
            self.assertFalse(stop_event.is_set())
            self.assertEqual(api.handler(1, 0, None, None), 0)
            for event in (service.PBT_APMSUSPEND, service.PBT_APMRESUMEAUTOMATIC, service.PBT_APMRESUMESUSPEND):
                self.assertEqual(api.handler(service.SERVICE_CONTROL_POWEREVENT, event, None, None), 0)

        host = service.ServiceHost(self.args(), api=api, target=target)
        with patch.object(host, "_log_failure") as log:
            host._main(0, None)
        monitor.handle_power_event.assert_called_once_with("suspend")
        log.assert_not_called()
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
                result = service.manage(command, api=api)
                self.assertFalse(result["installed"])
                self.assertIs(result["allow_power_actions"], False)
                self.assertIs(result["allow_ups_poweroff"], False)
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

    def test_registration_power_capability_requires_explicit_opt_in(self):
        for enabled, ups_enabled in ((False, False), (True, False), (True, True)):
            with self.subTest(allow_power_actions=enabled, allow_ups_poweroff=ups_enabled):
                api = Mock()
                api.OpenSCManagerW.return_value = 1
                api.OpenServiceW.return_value = None
                api.CreateServiceW.return_value = 2
                with patch.object(ctypes, "get_last_error", return_value=1060, create=True), \
                        patch.object(service, "_query", return_value={"state": "stopped"}):
                    result = service.manage("install", api=api, allow_power_actions=enabled,
                                            allow_ups_poweroff=ups_enabled)
                self.assertEqual("--allow-power-actions" in api.CreateServiceW.call_args.args[7], enabled)
                self.assertEqual("--allow-ups-poweroff" in api.CreateServiceW.call_args.args[7], ups_enabled)
                self.assertEqual(result["allow_power_actions"], enabled)
                self.assertEqual(result["allow_ups_poweroff"], ups_enabled)

    def test_status_reports_power_capability_without_mutating_registration(self):
        api = Mock()
        api.OpenSCManagerW.return_value, api.OpenServiceW.return_value = 1, 2
        with patch.object(service, "_configuration", return_value={
                "binary_path": "Elipro.exe service run --allow-power-actions"}), \
                patch.object(service, "_query", return_value={"state": "running"}):
            result = service.manage("status", api=api)
        self.assertTrue(result["allow_power_actions"])
        self.assertFalse(result["allow_ups_poweroff"])
        api.ChangeServiceConfigW.assert_not_called()

    def test_explicit_install_without_gates_restores_blocking_on_an_existing_service(self):
        api = Mock()
        api.OpenSCManagerW.return_value, api.OpenServiceW.return_value = 1, 2
        current = service.subprocess.list2cmdline(service.service_command(
            "data", allow_power_actions=True, allow_ups_poweroff=True))
        with patch.object(service, "_configuration", return_value={"binary_path": current}), \
                patch.object(service, "_query", return_value={"state": "stopped"}), \
                patch.object(service, "_stop") as stop:
            result = service.manage("install", api=api)
        stop.assert_called_once_with(api, 2)
        registration = api.ChangeServiceConfigW.call_args.args[4]
        self.assertNotIn("--allow-power-actions", registration)
        self.assertNotIn("--allow-ups-poweroff", registration)
        self.assertIs(result["allow_power_actions"], False)
        self.assertIs(result["allow_ups_poweroff"], False)

    def test_status_reports_both_registered_gates_without_mutation(self):
        api = Mock()
        api.OpenSCManagerW.return_value, api.OpenServiceW.return_value = 1, 2
        with patch.object(service, "_configuration", return_value={
                "binary_path": "Elipro.exe service run --allow-power-actions --allow-ups-poweroff"}), \
                patch.object(service, "_query", return_value={"state": "running"}):
            result = service.manage("status", api=api)
        self.assertTrue(result["allow_power_actions"])
        self.assertTrue(result["allow_ups_poweroff"])
        api.ChangeServiceConfigW.assert_not_called()

    def test_status_cannot_mistake_capability_text_in_quoted_data_path_for_flags(self):
        api = Mock()
        api.OpenSCManagerW.return_value, api.OpenServiceW.return_value = 1, 2
        path = ('"C:\\Program Files\\Elipro UPS\\Elipro.exe" service run --data-dir '
                '"C:\\data --allow-power-actions --allow-ups-poweroff --simulate --self-test files" --port 8765')
        with patch.object(service, "_configuration", return_value={"binary_path": path}), \
                patch.object(service, "_query", return_value={"state": "running"}):
            result = service.manage("status", api=api)
        self.assertFalse(any(result[key] for key in ("allow_power_actions", "allow_ups_poweroff", "simulation", "self_test")))

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
                                   dry_run=True)
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
                    patch("ups_monitor.server.ThreadingHTTPServer", return_value=http), patch("builtins.print"):
                _run_owned_monitor(args, ownership, stop_event=stopped, ready_callback=ready)
            desktop.assert_not_called()
            self.assertIs(alerts.call_args.args[1], broker)
            self.assertIs(http.session_broker, broker)
            self.assertEqual(monitor.runtime_mode, "service")
            ready.assert_called_once_with(monitor)
            monitor.stop.assert_called_once()
            self.assertTrue(self._until(lambda: http.shutdown.called))
            ownership.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
