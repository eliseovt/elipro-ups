"""Original Windows HID status collector. It sends only the observed QS query."""
from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from enum import Enum

from .adapters import TelemetryError
from .battery_estimation import Nt2402ProBatteryEstimator
from .hid_windows import StatusTransport, enumerate_ups_bounded as enumerate_ups
from .qs_protocol import parse_qs


class ForzaProbeReason(str, Enum):
    RECOGNIZED_REPLY = "recognized_reply"
    PORT_UNREACHABLE = "port_unreachable"
    EMPTY_REPLY = "empty_reply"
    UNEXPECTED_REPLY = "unexpected_reply"
    TIMEOUT = "timeout"
    PERMISSION_DENIED = "permission_denied"
    SOCKET_ERROR = "socket_error"


@dataclass(frozen=True)
class ForzaProbeResult:
    state: str
    reason: ForzaProbeReason
    winerror: int | None = None
    errno: int | None = None


def _probe_error_result(exc, *, query_error=False):
    winerror, error_number = getattr(exc, "winerror", None), exc.errno
    if query_error and (winerror in (10054, 10061) or error_number in (111, 10054, 10061)):
        return ForzaProbeResult("absent", ForzaProbeReason.PORT_UNREACHABLE, winerror, error_number)
    reason = (ForzaProbeReason.TIMEOUT if isinstance(exc, TimeoutError) else
              ForzaProbeReason.PERMISSION_DENIED if isinstance(exc, PermissionError) else
              ForzaProbeReason.SOCKET_ERROR)
    return ForzaProbeResult("unknown", reason, winerror, error_number)


def probe_forza_monitor():
    """Distinguish a recognized OEM reply from confirmed absence or uncertainty.

    A timeout/permission error is not proof that it is safe to open a second
    collector. Only an explicit local UDP port-unreachable during the query
    means no listener. Socket construction/configuration/close failures remain
    uncertain even if their error number resembles a port-unreachable error.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.3)
            try:
                probe.connect(("127.0.0.1", 51222))
                probe.send(b"VP_LINK")
                reply = probe.recv(256)
            except OSError as exc:
                result = _probe_error_result(exc, query_error=True)
            else:
                if reply == b"monitorOK":
                    result = ForzaProbeResult("active", ForzaProbeReason.RECOGNIZED_REPLY)
                else:
                    reason = ForzaProbeReason.EMPTY_REPLY if reply == b"" else ForzaProbeReason.UNEXPECTED_REPLY
                    result = ForzaProbeResult("unknown", reason)
    except OSError as exc:
        # A close failure overrides even a candidate "absent" from the query:
        # uncertainty about any resource operation must keep native USB blocked.
        return _probe_error_result(exc)
    return result


def forza_monitor_running():
    """Compatibility guard: uncertainty still blocks native USB or selects bridge."""
    return probe_forza_monitor().state != "absent"


class NativeHidAdapter:
    source = "USB directo · lector propio QS-Hex"
    bridge_required = False

    def __init__(self, *, clock=None):
        self.transport = None
        self._clock = clock or time.monotonic
        self._battery_estimator = Nt2402ProBatteryEstimator(clock=self._clock)
        self._failures = 0
        self._retry_at = 0

    @property
    def retry_after_seconds(self):
        return max(0, self._retry_at - self._clock())

    def read(self):
        if self.retry_after_seconds:
            raise TelemetryError("USB en reconexión automática; próximo intento tras el plazo de espera.")
        result = probe_forza_monitor()
        for _ in range(2):
            if result.state != "unknown":
                break
            time.sleep(0.1)
            result = probe_forza_monitor()
        if result.state != "absent":
            self._battery_estimator.reset()
            self.transport = None
            if result.state == "active":
                raise TelemetryError("El colector ForzaTracker está activo. Detén ese lector antes de usar USB directo para evitar consultas simultáneas.")
            code = (f", winerror={result.winerror}" if result.winerror is not None else "")
            code += (f", errno={result.errno}" if result.errno is not None else "")
            raise TelemetryError("No se pudo confirmar que el USB esté libre. Elipro reintentará automáticamente. "
                                 f"Motivo: {result.reason.value}{code}.")
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
            sample = parse_qs(self.transport.query_status(), preserve_battery_precision=True)
            sample.update(self._battery_estimator.update(sample))
            sample["battery_voltage"] = round(sample["battery_voltage"], 2)
            self._failures = 0
            self._retry_at = 0
            return sample
        except (OSError, ValueError, TelemetryError) as exc:
            self._battery_estimator.reset()
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
