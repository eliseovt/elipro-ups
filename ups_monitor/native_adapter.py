"""Original Windows HID status collector. It sends only the observed QS query."""
from __future__ import annotations

import socket
import time

from .adapters import TelemetryError
from .hid_windows import StatusTransport, enumerate_ups_bounded as enumerate_ups
from .qs_protocol import parse_qs


def forza_monitor_running():
    """Return true if an OEM listener exists or its absence cannot be confirmed.

    A timeout/permission error is not proof that it is safe to open a second
    collector. Only an explicit local UDP port-unreachable means no listener.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.settimeout(0.3)
        try:
            probe.connect(("127.0.0.1", 51222))
            probe.send(b"VP_LINK")
            probe.recv(256)
            return True
        except OSError as exc:
            return getattr(exc, "winerror", None) not in (10054, 10061) and exc.errno not in (111, 10054, 10061)


class NativeHidAdapter:
    source = "USB directo · lector propio QS-Hex"
    bridge_required = False

    def __init__(self, *, clock=None):
        self.transport = None
        self._clock = clock or time.monotonic
        self._failures = 0
        self._retry_at = 0

    @property
    def retry_after_seconds(self):
        return max(0, self._retry_at - self._clock())

    def read(self):
        if self.retry_after_seconds:
            raise TelemetryError("USB en reconexión automática; próximo intento tras el plazo de espera.")
        if forza_monitor_running():
            raise TelemetryError("El colector ForzaTracker está activo. Detén ese lector antes de usar USB directo para evitar consultas simultáneas.")
        try:
            if self.transport is None:
                devices = enumerate_ups()
                if len(devices) != 1:
                    raise TelemetryError("Se requiere exactamente un UPS USB 0665:5161 conectado.")
                device = devices[0]
                if device.get("metadata_open") is not True:
                    raise TelemetryError("Windows no permite abrir el UPS. Revisa permisos y el cable USB.")
                if (device.get("vendor_id"), device.get("product_id")) != (0x0665, 0x5161):
                    raise TelemetryError("El identificador USB no coincide con el dispositivo validado.")
                report_sizes = tuple(device.get(key) for key in ("input_report_bytes", "output_report_bytes", "feature_report_bytes"))
                if any(type(value) is not int for value in report_sizes) or report_sizes != (9, 9, 0):
                    raise TelemetryError("El descriptor HID no coincide con el validado para este UPS.")
                self.transport = StatusTransport(device["path"])
            sample = parse_qs(self.transport.query_status())
            self._failures = 0
            self._retry_at = 0
            return sample
        except (OSError, ValueError, TelemetryError) as exc:
            self.transport = None
            self._failures += 1
            self._retry_at = self._clock() + min(30, 2 ** min(self._failures, 5))
            raise TelemetryError("No se recibió una respuesta USB válida: " + str(exc)) from exc


def make_adapter(source, forza_url):
    from .adapters import ForzaBridge, SimulationAdapter
    if source == "simulation":
        return SimulationAdapter()
    if source not in ("auto", "native", "bridge"):
        raise ValueError("Fuente de datos no válida.")
    if source == "auto":
        # Select once. Do not silently change measurement source during an outage.
        source = "bridge" if forza_monitor_running() else "native"
    return NativeHidAdapter() if source == "native" else ForzaBridge(forza_url)
