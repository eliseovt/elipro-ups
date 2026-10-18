"""Native Windows SCM host. Never displays UI or installs itself on import.

The service owns USB, policy and history. Desktop delivery is delegated to the
per-session agent. Installation deliberately keeps --dry-run unless an
administrator explicitly registers --allow-power-actions. That capability never
enables the saved policy; both gates must allow a real action independently.

SCM contracts: https://learn.microsoft.com/windows/win32/services/service-programs
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback

from .runtime import child_command, is_frozen, service_data_dir

NAME = "EliproUPS"
DISPLAY_NAME = "Elipro UPS"
DWORD = ctypes.c_uint32
HANDLE = ctypes.c_void_p
CALLBACK = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
SERVICE_MAIN = CALLBACK(None, DWORD, ctypes.POINTER(ctypes.c_wchar_p))
HANDLER = CALLBACK(DWORD, DWORD, DWORD, HANDLE, HANDLE)
STOPPED, START_PENDING, STOP_PENDING, RUNNING = 1, 2, 3, 4
STATE_NAMES = {1: "stopped", 2: "start_pending", 3: "stop_pending", 4: "running",
               5: "continue_pending", 6: "pause_pending", 7: "paused"}
SC_MANAGER_CONNECT, SC_MANAGER_CREATE_SERVICE = 1, 2
SERVICE_QUERY_CONFIG, SERVICE_CHANGE_CONFIG, SERVICE_QUERY_STATUS = 1, 2, 4
SERVICE_START, SERVICE_STOP, DELETE = 0x10, 0x20, 0x10000
ERROR_SERVICE_DOES_NOT_EXIST, ERROR_SERVICE_NOT_ACTIVE, ERROR_SERVICE_ALREADY_RUNNING = 1060, 1062, 1056


class SERVICE_STATUS(ctypes.Structure):
    _fields_ = [(name, DWORD) for name in ("service_type", "state", "accepted", "exit_code",
                                         "specific_exit_code", "checkpoint", "wait_hint")]


class SERVICE_STATUS_PROCESS(ctypes.Structure):
    _fields_ = SERVICE_STATUS._fields_ + [("pid", DWORD), ("flags", DWORD)]


class SERVICE_TABLE_ENTRY(ctypes.Structure):
    _fields_ = [("name", ctypes.c_wchar_p), ("main", SERVICE_MAIN)]


class QUERY_SERVICE_CONFIG(ctypes.Structure):
    _fields_ = [("service_type", DWORD), ("start_type", DWORD), ("error_control", DWORD),
                ("binary_path", ctypes.c_wchar_p), ("group", ctypes.c_wchar_p), ("tag", DWORD),
                ("dependencies", ctypes.c_wchar_p), ("account", ctypes.c_wchar_p),
                ("display_name", ctypes.c_wchar_p)]


class SC_ACTION(ctypes.Structure):
    _fields_ = [("type", DWORD), ("delay", DWORD)]


class FAILURE_ACTIONS(ctypes.Structure):
    _fields_ = [("reset", DWORD), ("reboot", ctypes.c_wchar_p), ("command", ctypes.c_wchar_p),
                ("count", DWORD), ("actions", ctypes.POINTER(SC_ACTION))]


def _error(operation):
    code = ctypes.get_last_error()
    return OSError(code, f"No se pudo {operation} el servicio Elipro (Windows {code}).")


def _apis():
    if os.name != "nt":
        raise OSError("El servicio requiere Windows.")
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    bindings = {
        "OpenSCManagerW": ([ctypes.c_wchar_p, ctypes.c_wchar_p, DWORD], HANDLE),
        "OpenServiceW": ([HANDLE, ctypes.c_wchar_p, DWORD], HANDLE),
        "CloseServiceHandle": ([HANDLE], ctypes.c_int),
        "CreateServiceW": ([HANDLE, ctypes.c_wchar_p, ctypes.c_wchar_p, DWORD, DWORD, DWORD, DWORD,
                             ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.POINTER(DWORD),
                             ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p], HANDLE),
        "ChangeServiceConfigW": ([HANDLE, DWORD, DWORD, DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                   ctypes.POINTER(DWORD), ctypes.c_wchar_p, ctypes.c_wchar_p,
                                   ctypes.c_wchar_p, ctypes.c_wchar_p], ctypes.c_int),
        "ChangeServiceConfig2W": ([HANDLE, DWORD, HANDLE], ctypes.c_int),
        "QueryServiceConfigW": ([HANDLE, HANDLE, DWORD, ctypes.POINTER(DWORD)], ctypes.c_int),
        "QueryServiceStatusEx": ([HANDLE, DWORD, HANDLE, DWORD, ctypes.POINTER(DWORD)], ctypes.c_int),
        "StartServiceW": ([HANDLE, DWORD, ctypes.POINTER(ctypes.c_wchar_p)], ctypes.c_int),
        "ControlService": ([HANDLE, DWORD, ctypes.POINTER(SERVICE_STATUS)], ctypes.c_int),
        "DeleteService": ([HANDLE], ctypes.c_int),
        "StartServiceCtrlDispatcherW": ([ctypes.POINTER(SERVICE_TABLE_ENTRY)], ctypes.c_int),
        "RegisterServiceCtrlHandlerExW": ([ctypes.c_wchar_p, HANDLER, HANDLE], HANDLE),
        "SetServiceStatus": ([HANDLE, ctypes.POINTER(SERVICE_STATUS)], ctypes.c_int),
    }
    for name, (arguments, result) in bindings.items():
        function = getattr(api, name)
        function.argtypes, function.restype = arguments, result
    return api


def _validate_modes(self_test, allow_power_actions, simulate=False):
    if any(type(value) is not bool for value in (self_test, allow_power_actions, simulate)):
        raise ValueError("Las opciones de servicio deben ser booleanas.")
    if self_test and allow_power_actions:
        raise ValueError("--self-test no admite --allow-power-actions.")
    if simulate and allow_power_actions:
        raise ValueError("--simulate no admite --allow-power-actions.")
    if simulate and self_test:
        raise ValueError("--simulate no admite --self-test.")


def service_command(data_dir, port=8765, *, self_test=False, allow_power_actions=False, simulate=False):
    _validate_modes(self_test, allow_power_actions, simulate)
    if not 1 <= port <= 65535:
        raise ValueError("Puerto inválido.")
    command = child_command("service", "run", "--data-dir", Path(data_dir).resolve(), "--port", port)
    if self_test:
        command.append("--self-test")
    if allow_power_actions:
        command.append("--allow-power-actions")
    if simulate:
        command.append("--simulate")
    return command


def _configuration(api, handle):
    needed = DWORD()
    api.QueryServiceConfigW(handle, None, 0, ctypes.byref(needed))
    if not needed.value or needed.value > 65536:
        raise _error("consultar la configuración de")
    buffer = ctypes.create_string_buffer(needed.value)
    if not api.QueryServiceConfigW(handle, buffer, needed.value, ctypes.byref(needed)):
        raise _error("consultar la configuración de")
    config = ctypes.cast(buffer, ctypes.POINTER(QUERY_SERVICE_CONFIG)).contents
    return {"binary_path": config.binary_path or "", "start_type": config.start_type}


def _query(api, handle):
    status, needed = SERVICE_STATUS_PROCESS(), DWORD()
    if not api.QueryServiceStatusEx(handle, 0, ctypes.byref(status), ctypes.sizeof(status), ctypes.byref(needed)):
        raise _error("consultar")
    return {"name": NAME, "installed": True, "state": STATE_NAMES.get(status.state, "unknown"),
            "pid": status.pid, "exit_code": status.exit_code}


def _wait_for(api, handle, state, *, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        result = _query(api, handle)
        if result["state"] == state:
            return result
        if state == "running" and result["state"] == "stopped":
            raise RuntimeError("El servicio no arrancó. Revisa el registro de eventos de Windows y el registro de Elipro.")
        if time.monotonic() >= deadline:
            raise TimeoutError("Windows no confirmó el cambio de estado del servicio dentro de 30 segundos.")
        time.sleep(0.2)


def _stop(api, handle):
    if _query(api, handle)["state"] == "stopped":
        return
    status = SERVICE_STATUS()
    if not api.ControlService(handle, 1, ctypes.byref(status)):
        if ctypes.get_last_error() != ERROR_SERVICE_NOT_ACTIVE:
            raise _error("detener")
    _wait_for(api, handle, "stopped")


def _owned_binary(binary_path):
    # Do not replace/delete a same-name service belonging to another program.
    # service_command always quotes an absolute interpreter/executable path.
    expected = subprocess.list2cmdline(child_command("service", "run"))
    return binary_path == expected or binary_path.startswith(expected + " ")


def manage(command, *, data_dir=None, port=8765, self_test=False, allow_power_actions=False, simulate=False, api=None):
    _validate_modes(self_test, allow_power_actions, simulate)
    if allow_power_actions and command != "install":
        raise ValueError("--allow-power-actions requiere registrar el servicio con install.")
    if simulate and command != "install":
        raise ValueError("--simulate requiere registrar el servicio con install.")
    api = api or _apis()
    access = SC_MANAGER_CONNECT | (SC_MANAGER_CREATE_SERVICE if command == "install" else 0)
    manager = api.OpenSCManagerW(None, None, access)
    if not manager:
        raise _error("abrir el administrador de")
    handle = None
    try:
        desired = SERVICE_QUERY_STATUS | SERVICE_QUERY_CONFIG
        if command == "install":
            # SC_ACTION_RESTART additionally requires SERVICE_START on this
            # handle, even though installation does not start the service.
            desired |= SERVICE_CHANGE_CONFIG | SERVICE_STOP | SERVICE_START
        elif command == "start":
            desired |= SERVICE_START
        elif command in ("stop", "remove"):
            desired |= SERVICE_STOP | (DELETE if command == "remove" else 0)
        handle = api.OpenServiceW(manager, NAME, desired)
        if not handle and ctypes.get_last_error() != ERROR_SERVICE_DOES_NOT_EXIST:
            raise _error("abrir")
        if command == "install":
            vector = service_command(data_dir or service_data_dir(), port, self_test=self_test,
                                     allow_power_actions=allow_power_actions, simulate=simulate)
            binary = subprocess.list2cmdline(vector)
            if handle:
                if not _owned_binary(_configuration(api, handle)["binary_path"]):
                    raise RuntimeError("El servicio EliproUPS existente usa otro ejecutable; no se modificará.")
                _stop(api, handle)
                if not api.ChangeServiceConfigW(handle, 0x10, 2, 1, binary, None, None, None, "LocalSystem", None, DISPLAY_NAME):
                    raise _error("actualizar")
            else:
                handle = api.CreateServiceW(manager, NAME, DISPLAY_NAME, desired, 0x10, 2, 1,
                                            binary, None, None, None, None, None)
                if not handle:
                    raise _error("crear")
            actions = (SC_ACTION * 3)(SC_ACTION(1, 5000), SC_ACTION(1, 15000), SC_ACTION(1, 30000))
            failure = FAILURE_ACTIONS(86400, None, None, 3, actions)
            if not api.ChangeServiceConfig2W(handle, 2, ctypes.byref(failure)):
                raise _error("configurar recuperación de")
            on_failure = ctypes.c_int(1)
            if not api.ChangeServiceConfig2W(handle, 4, ctypes.byref(on_failure)):
                raise _error("configurar errores de")
            return {**_query(api, handle), "self_test": self_test,
                    "allow_power_actions": allow_power_actions, "simulation": simulate}
        if not handle:
            if command == "start":
                raise RuntimeError("El servicio Elipro no está instalado.")
            return {"name": NAME, "installed": False, "state": "absent", "pid": 0,
                    "self_test": False, "allow_power_actions": False, "simulation": False}
        configuration = _configuration(api, handle)
        if command != "status" and not _owned_binary(configuration["binary_path"]):
            raise RuntimeError("El servicio EliproUPS existente usa otro ejecutable; no se modificará.")
        if command == "start":
            if not api.StartServiceW(handle, 0, None) and ctypes.get_last_error() != ERROR_SERVICE_ALREADY_RUNNING:
                raise _error("iniciar")
            result = _wait_for(api, handle, "running")
        elif command in ("stop", "remove"):
            _stop(api, handle)
            if command == "remove":
                if not api.DeleteService(handle):
                    raise _error("eliminar")
                return {"name": NAME, "installed": False, "state": "absent", "pid": 0,
                        "self_test": False, "allow_power_actions": False, "simulation": False}
            result = _query(api, handle)
        else:
            result = _query(api, handle)
        return {**result, "self_test": "--self-test" in configuration["binary_path"],
                "allow_power_actions": "--allow-power-actions" in configuration["binary_path"],
                "simulation": "--simulate" in configuration["binary_path"]}
    finally:
        if handle:
            api.CloseServiceHandle(handle)
        api.CloseServiceHandle(manager)


class ServiceHost:
    def __init__(self, args, *, api=None, target=None):
        _validate_modes(args.self_test, getattr(args, "allow_power_actions", False), getattr(args, "simulate", False))
        self.args, self.api, self.target = args, api or _apis(), target
        self.stop_event = threading.Event()
        self.handle = None
        self._state_lock = threading.RLock()
        self._last_status = None
        self._main_callback = SERVICE_MAIN(self._main)
        self._control_callback = HANDLER(self._control)

    def _report(self, state, *, exit_code=0):
        with self._state_lock:
            accepted = 1 | 4 if state == RUNNING else 0  # STOP | SHUTDOWN
            pending = state in (START_PENDING, STOP_PENDING)
            status = SERVICE_STATUS(0x10, state, accepted, exit_code, 1 if exit_code else 0,
                                    1 if pending else 0, 30000 if pending else 0)
            if not self.api.SetServiceStatus(self.handle, ctypes.byref(status)):
                raise _error("informar estado de")
            self._last_status = status

    def _control(self, code, _event_type, _event_data, _context):
        if code in (1, 5):  # STOP or Windows shutdown: stop reader, never initiate power action.
            try:
                self._report(STOP_PENDING)
            except OSError:
                pass
            finally:
                self.stop_event.set()
        return 0

    def _ready(self):
        self._report(STOP_PENDING if self.stop_event.is_set() else RUNNING)

    def _run_target(self):
        if self.args.self_test:
            self._ready()
            self.stop_event.wait()
            return
        if self.target is None:
            from .server import main
            target = main
        else:
            target = self.target
        simulated = getattr(self.args, "simulate", False)
        vector = ["--headless", "--source", "simulation" if simulated else "native", "--data-dir", str(self.args.data_dir),
                  "--port", str(self.args.port)]
        if simulated or not getattr(self.args, "allow_power_actions", False):
            vector.append("--dry-run")
        target(vector, stop_event=self.stop_event,
               ready_callback=self._ready)

    def _main(self, _argc, _argv):
        self.handle = self.api.RegisterServiceCtrlHandlerExW(NAME, self._control_callback, None)
        if not self.handle:
            return
        failure = 0
        try:
            self._report(START_PENDING)
            self._run_target()
        except BaseException as exc:
            # Nonzero stop is visible to SCM recovery; no exception payload can
            # leak local identifiers through a public CI log.
            failure = 1066
            self._log_failure(exc)
        finally:
            self.stop_event.set()
            self._report(STOPPED, exit_code=failure)

    def _log_failure(self, exc):
        handler = None
        try:
            self.args.data_dir.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(self.args.data_dir / "service-bootstrap.log", maxBytes=500_000,
                                          backupCount=2, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            record = logging.LogRecord("elipro.service", logging.ERROR, __file__, 0,
                                       "Service bootstrap failed (%s)\n%s",
                                       (type(exc).__name__, "".join(traceback.format_tb(exc.__traceback__))), None)
            handler.emit(record)
        except OSError:
            pass  # SCM still receives the failure when disk/logging is unavailable.
        finally:
            if handler is not None:
                handler.close()

    def run(self):
        table = (SERVICE_TABLE_ENTRY * 2)(SERVICE_TABLE_ENTRY(NAME, self._main_callback),
                                          SERVICE_TABLE_ENTRY(None, SERVICE_MAIN()))
        if not self.api.StartServiceCtrlDispatcherW(table):
            raise _error("conectar con SCM; usa service start para iniciar")
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Servicio Windows Elipro UPS (requiere administrador para modificarlo)")
    parser.add_argument("command", choices=("install", "start", "stop", "remove", "status", "run"))
    parser.add_argument("--data-dir", type=Path, default=service_data_dir())
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--self-test", action="store_true", help="Prueba SCM sin USB, monitor ni acciones de energía")
    parser.add_argument("--simulate", action="store_true", help="Monitor y política reales con telemetría simulada; bloquea acciones Windows")
    parser.add_argument("--allow-power-actions", action="store_true",
                        help="Opt-in administrativo al registrar/ejecutar el servicio; no activa la política guardada")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("Puerto fuera de rango")
    if args.self_test and args.allow_power_actions:
        parser.error("--self-test no admite --allow-power-actions")
    if args.simulate and (args.allow_power_actions or args.self_test):
        parser.error("--simulate no admite --allow-power-actions ni --self-test")
    if args.simulate and args.command not in ("install", "run"):
        parser.error("--simulate solo se admite con install o run")
    if args.allow_power_actions and args.command not in ("install", "run"):
        parser.error("--allow-power-actions solo se admite con install o run")
    try:
        if args.command == "run":
            return ServiceHost(args).run()
        print(json.dumps(manage(args.command, data_dir=args.data_dir, port=args.port, self_test=args.self_test,
                                allow_power_actions=args.allow_power_actions, simulate=args.simulate), ensure_ascii=False))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
