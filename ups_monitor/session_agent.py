"""Desktop tray/sound client. This process never imports or opens a USB reader.

Only a Windows service owns the monitor in an installed build. One agent per
interactive session polls the bounded broker; closing a browser or this agent
does not stop the service. All HTTP requests stay on the fixed loopback host,
disable proxy discovery and redirects, and mutations use the existing CSRF token.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import secrets
import threading
import time
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request
import webbrowser

from .instance_guard import AlreadyRunningError, _WindowsOwnership
from .session_broker import delivery_diagnostics
from .windows_notifications import WindowsNotifier

AGENT_MUTEX = "Local\\EliproUPS.SessionAgent"
STOP_EVENT = "Local\\EliproUPS.SessionAgent.Stop"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class LocalClient:
    def __init__(self, port=8765):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Puerto local inválido")
        self.url = f"http://127.0.0.1:{port}"
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())
        self.token = None

    def _request(self, path, payload=None):
        if path not in {"/api/status", "/api/session/poll", "/api/protection/cancel"}:
            raise ValueError("Operación de agente no admitida")
        headers = {"Accept": "application/json"}
        body = None
        if payload is not None:
            if not self.token:
                raise ValueError("Monitor no autenticado")
            body = json.dumps(payload, allow_nan=False).encode("utf-8")
            headers.update({"Content-Type": "application/json", "X-UPS-Token": self.token,
                            "Origin": self.url})
        request = Request(self.url + path, data=body, headers=headers,
                          method="GET" if body is None else "POST")
        with self.opener.open(request, timeout=3) as response:
            content = response.read(1_000_001)
        if len(content) > 1_000_000:
            raise ValueError("Respuesta local demasiado grande")
        return json.loads(content)

    def connect(self):
        status = self._request("/api/status")
        token = status.get("csrf_token")
        if not isinstance(token, str) or not 16 <= len(token) <= 128:
            raise ValueError("El monitor no devolvió una credencial local válida")
        if status.get("meta", {}).get("runtime_mode") != "service":
            raise ValueError("El monitor actual ya entrega sus avisos directamente; no es un servicio")
        self.token = token

    def poll(self, client_id, after, delivery):
        return self._request("/api/session/poll", {"client_id": client_id, "after": after,
                             "delivery": delivery_diagnostics(delivery)})

    def cancel(self):
        if not self.token:
            self.connect()
        return self._request("/api/protection/cancel", {})


class Agent:
    def __init__(self, client, notifier, *, stop_event=None, clock=time.monotonic):
        self.client, self.notifier = client, notifier
        self.stop_event = stop_event or threading.Event()
        self.client_id = secrets.token_hex(16)
        self.cursor = None
        self.connected = False
        self.had_connection = False
        self.loss_reported = False
        self._clock, self._first_failure = clock, None
        self.notifier.cancel_callback = self.cancel
        self.notifier.exit_callback = self.stop_event.set

    def cancel(self):
        try:
            self.client.cancel()
        except (OSError, ValueError):
            # Do not claim a cancellation reached an unavailable monitor.
            self.notifier.notify("Elipro UPS", "No se pudo confirmar la cancelación. Abre el panel y verifica el estado.",
                                 severity="warning", sound=True)

    def step(self):
        if not self.connected:
            self.client.connect()
            self.cursor = None
            self.connected = True
        response = self.client.poll(self.client_id, self.cursor, self.notifier.status())
        self.notifier.sync_persistent(response.get("persistent", {}))
        for item in response.get("messages", [])[:32]:
            self.notifier.notify(item["title"], item["message"], severity=item.get("severity", "info"),
                                 sound=item.get("sound", True), persistent=item.get("persistent", False),
                                 key=item.get("key"))
        self.cursor = response["cursor"]
        if self.loss_reported:
            self.notifier.notify("Elipro UPS", "El servicio vuelve a estar disponible. Verifica el estado del UPS en el panel.", sound=False)
        self.had_connection, self.loss_reported = True, False
        self._first_failure = None
        if not response.get("running", False):
            self.connected = False

    def run(self, stopped=lambda: False):
        self.notifier.start()
        try:
            while not self.stop_event.is_set() and not stopped():
                try:
                    self.step()
                except (OSError, ValueError, KeyError, TypeError):
                    self.connected = False
                    self.cursor = None
                    # Clear stale persistent warnings after service loss. The
                    # panel reports availability; a tray must not invent power state.
                    self.notifier.sync_persistent({})
                    if self._first_failure is None:
                        self._first_failure = self._clock()
                    if (self.had_connection or self._clock() - self._first_failure >= 10) and not self.loss_reported:
                        self.notifier.notify("Elipro UPS", "No se puede contactar con el servicio. No podemos confirmar que la protección siga activa.",
                                             severity="warning", sound=True)
                        self.loss_reported = True
                self.stop_event.wait(1)
        finally:
            self.notifier.stop()


def _kernel():
    if os.name != "nt":
        raise OSError("El agente de escritorio requiere Windows")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    bindings = {
        "CreateEventW": ([ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p], ctypes.c_void_p),
        "OpenEventW": ([ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p], ctypes.c_void_p),
        "SetEvent": ([ctypes.c_void_p], ctypes.c_int),
        "ResetEvent": ([ctypes.c_void_p], ctypes.c_int),
        "WaitForSingleObject": ([ctypes.c_void_p, ctypes.c_uint32], ctypes.c_uint32),
        "CloseHandle": ([ctypes.c_void_p], ctypes.c_int),
    }
    for name, (arguments, result) in bindings.items():
        method = getattr(kernel, name)
        method.argtypes, method.restype = arguments, result
    return kernel


def stop_current_session():
    kernel = _kernel()
    event = kernel.OpenEventW(2, False, STOP_EVENT)  # EVENT_MODIFY_STATE
    if not event:
        if ctypes.get_last_error() == 2:
            return 0
        raise OSError("No se pudo abrir la señal de cierre del agente")
    try:
        if not kernel.SetEvent(event):
            raise OSError("No se pudo solicitar el cierre del agente")
    finally:
        kernel.CloseHandle(event)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            ownership = _WindowsOwnership(AGENT_MUTEX)
        except AlreadyRunningError:
            time.sleep(0.1)
            continue
        ownership.close()
        return 0
    raise TimeoutError("El agente no confirmó su cierre dentro de ocho segundos")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Avisos de Elipro en la sesión de Windows; no inicia otro monitor")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open-panel", action="store_true")
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args(argv)
    if args.stop:
        return stop_current_session()
    client = LocalClient(args.port)
    if args.open_panel:
        webbrowser.open(client.url)
    try:
        ownership = _WindowsOwnership(AGENT_MUTEX)
    except AlreadyRunningError:
        return 0
    kernel = _kernel()
    event = kernel.CreateEventW(None, True, False, STOP_EVENT)
    if not event:
        ownership.close()
        raise OSError("No se pudo crear la señal de cierre del agente")
    try:
        if not kernel.ResetEvent(event):
            raise OSError("No se pudo reiniciar la señal de cierre del agente")
        notifier = WindowsNotifier(client.url, exit_label="Cerrar avisos de esta sesión…",
                                   exit_message="Se cerrarán los avisos de esta sesión. El servicio seguirá vigilando el UPS y Windows seguirá encendido. ¿Quieres cerrar los avisos?",
                                   tray_tip="Elipro UPS · avisos de esta sesión")
        Agent(client, notifier).run(lambda: kernel.WaitForSingleObject(event, 0) == 0)
        return 0
    finally:
        kernel.CloseHandle(event)
        ownership.close()


if __name__ == "__main__":
    raise SystemExit(main())
