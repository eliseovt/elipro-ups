"""Original Windows HID status collector. It sends only the observed QS query."""
from __future__ import annotations

import socket
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import threading

from .adapters import TelemetryError
from .battery_estimation import Nt2402ProBatteryEstimator
from .hid_windows import StatusTransport, enumerate_ups_bounded as enumerate_ups
from .qs_protocol import parse_qs
from .windows_udp import UdpPortReason, udp_port_occupancy


class ForzaProbeReason(str, Enum):
    RECOGNIZED_REPLY = "recognized_reply"
    PORT_UNREACHABLE = "port_unreachable"
    EMPTY_REPLY = "empty_reply"
    UNEXPECTED_REPLY = "unexpected_reply"
    TIMEOUT = "timeout"
    PERMISSION_DENIED = "permission_denied"
    SOCKET_ERROR = "socket_error"
    ENDPOINT_TABLE_EMPTY = "endpoint_table_empty"
    ENDPOINT_OCCUPIED = "endpoint_occupied"
    ENDPOINT_API_UNAVAILABLE = "endpoint_api_unavailable"
    ENDPOINT_API_ERROR = "endpoint_api_error"
    ENDPOINT_INVALID_TABLE = "endpoint_invalid_table"
    ENDPOINT_TABLE_LIMIT = "endpoint_table_limit"
    ENDPOINT_TABLE_UNSTABLE = "endpoint_table_unstable"


@dataclass(frozen=True)
class ForzaProbeResult:
    state: str
    reason: ForzaProbeReason
    winerror: int | None = None
    errno: int | None = None
    pids: tuple[int, ...] = ()
    reply_reason: ForzaProbeReason | None = None
    family: int | None = None


@dataclass(frozen=True)
class OwnershipDiagnostic:
    checked_at: str
    duration_ms: int
    attempts: tuple[ForzaProbeResult, ...]

    @property
    def result(self):
        return self.attempts[-1]


def _probe_error_result(exc, *, query_error=False):
    winerror, error_number = getattr(exc, "winerror", None), exc.errno
    if query_error and (winerror in (10054, 10061) or error_number in (111, 10054, 10061)):
        return ForzaProbeResult("absent", ForzaProbeReason.PORT_UNREACHABLE, winerror, error_number)
    reason = (ForzaProbeReason.TIMEOUT if isinstance(exc, TimeoutError) else
              ForzaProbeReason.PERMISSION_DENIED if isinstance(exc, PermissionError) else
              ForzaProbeReason.SOCKET_ERROR)
    return ForzaProbeResult("unknown", reason, winerror, error_number)


def _probe_forza_reply():
    """Attempt positive OEM recognition after Windows found an occupied port.

    Negative UDP responses are diagnostic details only; none authorizes USB or
    overrides the ownership table. Socket setup and close errors remain unknown.
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


def probe_forza_monitor():
    """Authorize absence from Windows tables; UDP can only recognize presence."""
    ownership = udp_port_occupancy()
    if ownership.state == "absent":
        return ForzaProbeResult("absent", ForzaProbeReason.ENDPOINT_TABLE_EMPTY)
    if ownership.state != "occupied":
        reasons = {
            UdpPortReason.API_UNAVAILABLE: ForzaProbeReason.ENDPOINT_API_UNAVAILABLE,
            UdpPortReason.API_ERROR: ForzaProbeReason.ENDPOINT_API_ERROR,
            UdpPortReason.INVALID_TABLE: ForzaProbeReason.ENDPOINT_INVALID_TABLE,
            UdpPortReason.TABLE_LIMIT: ForzaProbeReason.ENDPOINT_TABLE_LIMIT,
            UdpPortReason.TABLE_UNSTABLE: ForzaProbeReason.ENDPOINT_TABLE_UNSTABLE,
        }
        return ForzaProbeResult("unknown", reasons.get(ownership.reason, ForzaProbeReason.ENDPOINT_API_ERROR),
                                 winerror=ownership.error_code, family=ownership.family)
    reply = _probe_forza_reply()
    if reply.state == "active":
        return replace(reply, pids=ownership.pids)
    # A timeout/closed port/error can never override a bound Windows endpoint.
    return ForzaProbeResult("unknown", ForzaProbeReason.ENDPOINT_OCCUPIED,
                             winerror=reply.winerror, errno=reply.errno,
                             pids=ownership.pids, reply_reason=reply.reason)


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
        self.last_ownership_result = None
        self._ownership_lock = threading.Lock()
        self._ownership_current = None
        self._ownership_recent = deque(maxlen=16)

    def _record_ownership(self, attempts, started):
        diagnostic = OwnershipDiagnostic(
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            max(0, round((time.monotonic() - started) * 1000)), tuple(attempts))
        with self._ownership_lock:
            previous = self.last_ownership_result
            self.last_ownership_result = diagnostic.result
            self._ownership_current = diagnostic
            if diagnostic.result != previous or len(diagnostic.attempts) > 1:
                self._ownership_recent.append(diagnostic)

    @staticmethod
    def _ownership_json(diagnostic):
        if diagnostic is None:
            return None
        def result_json(result):
            return {"state": result.state, "reason": result.reason.value,
                    "winerror": result.winerror, "errno": result.errno,
                    "pids": list(result.pids), "family": result.family,
                    "reply_reason": result.reply_reason.value if result.reply_reason is not None else None}
        return {"checked_at": diagnostic.checked_at, "duration_ms": diagnostic.duration_ms,
                "attempts": [result_json(result) for result in diagnostic.attempts],
                "result": result_json(diagnostic.result)}

    def ownership_status(self):
        """Copy bounded immutable evidence without any disk or notification I/O."""
        with self._ownership_lock:
            current, recent = self._ownership_current, tuple(self._ownership_recent)
        return {"current": self._ownership_json(current),
                "recent": [self._ownership_json(diagnostic) for diagnostic in recent]}

    @property
    def retry_after_seconds(self):
        return max(0, self._retry_at - self._clock())

    def read(self):
        if self.retry_after_seconds:
            raise TelemetryError("USB en reconexión automática; próximo intento tras el plazo de espera.")
        started = time.monotonic()
        result = probe_forza_monitor()
        attempts = [result]
        for _ in range(2):
            if result.state != "unknown":
                break
            time.sleep(0.1)
            result = probe_forza_monitor()
            attempts.append(result)
        self._record_ownership(attempts, started)
        if result.state != "absent":
            self._battery_estimator.reset()
            self.transport = None
            if result.state == "active":
                raise TelemetryError("El colector ForzaTracker está activo. Detén ese lector antes de usar USB directo para evitar consultas simultáneas.")
            code = (f", winerror={result.winerror}" if result.winerror is not None else "")
            code += (f", errno={result.errno}" if result.errno is not None else "")
            code += (f", pids={result.pids}" if result.pids else "")
            code += (f", reply={result.reply_reason.value}" if result.reply_reason is not None else "")
            code += (f", family={result.family}" if result.family is not None else "")
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
