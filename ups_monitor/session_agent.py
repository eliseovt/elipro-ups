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
import re
import secrets
import threading
import time
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request
import webbrowser

from .instance_guard import AlreadyRunningError, _WindowsOwnership
from .session_broker import SESSION_STATES, delivery_diagnostics
from .windows_notifications import WindowsNotifier

AGENT_MUTEX = "Local\\EliproUPS.SessionAgent"
STOP_EVENT = "Local\\EliproUPS.SessionAgent.Stop"


class _WTSINFOEX_LEVEL1(ctypes.Structure):
    # Fixed-width WCHAR arrays keep the native Windows layout testable on any
    # host. LARGE_INTEGER aligns the containing union to eight bytes on x64.
    _fields_ = [("SessionId", ctypes.c_uint32), ("SessionState", ctypes.c_int32),
                ("SessionFlags", ctypes.c_int32), ("WinStationName", ctypes.c_uint16 * 33),
                ("UserName", ctypes.c_uint16 * 21), ("DomainName", ctypes.c_uint16 * 18)] + [
                    (name, ctypes.c_int64) for name in
                    ("LogonTime", "ConnectTime", "DisconnectTime", "LastInputTime", "CurrentTime")] + [
                    (name, ctypes.c_uint32) for name in
                    ("IncomingBytes", "OutgoingBytes", "IncomingFrames", "OutgoingFrames",
                     "IncomingCompressedBytes", "OutgoingCompressedBytes")]


class _WTSINFOEX_DATA(ctypes.Union):
    _fields_ = [("Level1", _WTSINFOEX_LEVEL1)]


class _WTSINFOEX(ctypes.Structure):
    _fields_ = [("Level", ctypes.c_uint32), ("Data", _WTSINFOEX_DATA)]


def query_session_state():
    """Return the calling desktop's state; uncertainty never permits delivery.

    WTSInfoEx reports lock state separately from an active RDP connection. Elipro
    supports Windows 10+, so the inverted Windows 7 SessionFlags defect does not
    apply. The allocated WTS buffer is always released, even on malformed data.
    """
    if os.name != "nt":
        return "unknown"
    buffer, size = ctypes.c_void_p(), ctypes.c_uint32()
    api = None
    try:
        api = ctypes.WinDLL("wtsapi32", use_last_error=True)
        query = api.WTSQuerySessionInformationW
        query.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                          ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32)]
        query.restype = ctypes.c_int
        api.WTSFreeMemory.argtypes, api.WTSFreeMemory.restype = [ctypes.c_void_p], None
        if not query(None, 0xFFFFFFFF, 25, ctypes.byref(buffer), ctypes.byref(size)):
            return "unknown"
        if not buffer.value or size.value < ctypes.sizeof(_WTSINFOEX):
            return "unknown"
        value = ctypes.cast(buffer, ctypes.POINTER(_WTSINFOEX)).contents
        if value.Level != 1:
            return "unknown"
        session = value.Data.Level1
        if session.SessionState != 0:  # WTSActive
            return "disconnected"
        return {0: "locked", 1: "active"}.get(session.SessionFlags, "unknown")
    except (AttributeError, OSError, TypeError, ValueError):
        return "unknown"
    finally:
        if api is not None and buffer.value:
            api.WTSFreeMemory(buffer)


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

    def poll(self, client_id, after, delivery, *, acks=(), epoch=None, session_state="unknown"):
        return self._request("/api/session/poll", {"client_id": client_id, "after": after,
                             "delivery": delivery_diagnostics(delivery), "protocol": 2,
                             "acks": list(acks), "epoch": epoch, "session_state": session_state})

    def cancel(self):
        if not self.token:
            self.connect()
        return self._request("/api/protection/cancel", {})


class Agent:
    def __init__(self, client, notifier, *, stop_event=None, clock=time.monotonic, session_state=None):
        self.client, self.notifier = client, notifier
        self.stop_event = stop_event or threading.Event()
        self.client_id = secrets.token_hex(16)
        self.cursor = None
        self.epoch = None
        self._acks = set()
        self._inflight = {}
        self._payloads = {}
        self.connected = False
        self.had_connection = False
        self.loss_reported = False
        self._clock, self._first_failure = clock, None
        self._session_state = session_state or query_session_state
        self.notifier.cancel_callback = self.cancel
        self.notifier.exit_callback = self.stop_event.set
        probe = self._notifier_method("set_session_probe")
        if probe is not None:
            probe(lambda: self._current_session() == "active")

    def _current_session(self):
        try:
            value = self._session_state()
            return value if isinstance(value, str) and value in SESSION_STATES else "unknown"
        except Exception:
            return "unknown"

    def _notifier_method(self, name):
        # Mock/dynamic proxies must not invent support for receipt semantics.
        return getattr(self.notifier, name) if callable(getattr(type(self.notifier), name, None)) else None

    def _availability(self, available):
        setter = self._notifier_method("set_session_available")
        if setter is not None:
            setter(available)

    def _collect_receipts(self, active):
        drain = self._notifier_method("drain_receipts")
        for receipt in (drain()[:256] if drain is not None else []):
            if not isinstance(receipt, dict):
                continue
            delivery_id = receipt.get("delivery_id")
            if delivery_id not in self._inflight:
                continue
            self._inflight.pop(delivery_id, None)
            self._payloads.pop(delivery_id, None)
            if receipt.get("outcome") == "accepted" and active:
                epoch, _, message_id = delivery_id.partition(":")
                if epoch == self.epoch and message_id.isdecimal() and len(self._acks) < 64:
                    self._acks.add(int(message_id))

    def _expire_inflight(self):
        cancel = self._notifier_method("cancel_delivery")
        now = self._clock()
        for delivery_id, deadline in tuple(self._inflight.items()):
            if deadline <= now:
                # A delayed native window thread may still own this payload.
                # Invalidate its ticket before dropping the comparison state.
                if cancel is not None:
                    cancel(delivery_id)
                self._inflight.pop(delivery_id, None)
                self._payloads.pop(delivery_id, None)

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
        session = self._current_session()
        active = session == "active"
        self._availability(active)
        self._collect_receipts(active)
        self._expire_inflight()
        sent_acks = sorted(self._acks) if active else []
        response = self.client.poll(self.client_id, self.cursor, self.notifier.status(),
                                    acks=sent_acks, epoch=self.epoch, session_state=session)
        # Clear ACKs only after a successful round trip. A lost HTTP response
        # causes an idempotent retry, never a second display or false receipt.
        self._acks.difference_update(sent_acks)
        epoch = response.get("epoch")
        v2 = response.get("protocol") == 2 and isinstance(epoch, str) and re.fullmatch(r"[a-f0-9]{32}", epoch)
        if v2 and self.epoch != epoch:
            self._availability(False)
            self._inflight.clear()
            self._payloads.clear()
            self._acks.clear()
            self.epoch = epoch
        active = active and self._current_session() == "active"
        self._availability(active)
        self.notifier.sync_persistent(response.get("persistent", {}) if active else {})
        for item in response.get("messages", [])[:32]:
            if not active:
                break
            options = {"severity": item.get("severity", "info"), "sound": item.get("sound", True),
                       "persistent": item.get("persistent", False), "key": item.get("key")}
            if v2:
                message_id = item.get("id")
                if type(message_id) is not int or not 0 < message_id <= 2**63 - 1:
                    continue
                delivery_id = f"{epoch}:{message_id}"
                signature = (item["title"], item["message"], options["severity"], options["sound"],
                             options["persistent"], options["key"])
                if delivery_id in self._payloads and self._payloads[delivery_id] != signature:
                    cancel = self._notifier_method("cancel_delivery")
                    if cancel is not None:
                        cancel(delivery_id)
                    self._inflight.pop(delivery_id, None)
                    self._payloads.pop(delivery_id, None)
                if self._inflight.get(delivery_id, 0) > self._clock() or message_id in self._acks:
                    continue
                if len(self._inflight) >= 32:
                    continue
                options["delivery_id"] = delivery_id
            accepted = self.notifier.notify(item["title"], item["message"], **options)
            if v2 and accepted is True:
                self._inflight[delivery_id] = self._clock() + 5
                self._payloads[delivery_id] = signature
        self.cursor = response["cursor"]
        self._expire_inflight()
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
                    self._availability(False)
                    if self._first_failure is None:
                        self._first_failure = self._clock()
                    if (self.had_connection or self._clock() - self._first_failure >= 10) and not self.loss_reported:
                        # Cancel stale broker tickets, then permit this local
                        # service-loss warning only on a confirmed live desktop.
                        active = self._current_session() == "active"
                        self._availability(active)
                        if active:
                            accepted = self.notifier.notify("Elipro UPS", "No se puede contactar con el servicio. No podemos confirmar que la protección siga activa.",
                                                            severity="warning", sound=True)
                            self.loss_reported = accepted is True
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
