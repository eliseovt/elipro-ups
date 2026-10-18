"""Read-only telemetry bridges. No UPS control commands are implemented here."""
from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


class TelemetryError(RuntimeError):
    pass


def number(value, minimum=None, maximum=None):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    if minimum is not None and result < minimum:
        return None
    if maximum is not None and result > maximum:
        return None
    return result


def normalize(payload):
    """Normalize the observed ViewPower PMV schema without inventing measurements."""
    if payload.get("protocolType") != "PMV":
        raise TelemetryError("El protocolo detectado no es PMV; requiere validar otro adaptador.")
    info = payload.get("workInfo")
    if not isinstance(info, dict) or not info:
        raise TelemetryError("ForzaTracker no está entregando datos del UPS.")
    observed = info.get("currentTime")
    if isinstance(observed, dict):
        observed = observed.get("time")
    epoch_ms = number(observed, 1)
    if epoch_ms is None:
        raise TelemetryError("La telemetría no incluye una fecha verificable.")
    try:
        stamp = datetime.fromtimestamp(epoch_ms / 1000, timezone.utc)
    except (ValueError, OSError, OverflowError) as exc:
        raise TelemetryError("Fecha de telemetría inválida.") from exc
    age = (datetime.now(timezone.utc) - stamp).total_seconds()
    if age > 15 or age < -5:
        raise TelemetryError("ForzaTracker entrega datos obsoletos; protección suspendida.")
    mode = str(info.get("workMode", "")).strip()
    on_battery = {"Line mode": False, "AVR mode": False, "Battery mode": True}.get(mode)
    # PMV returns 0 for unsupported runtime/temperature. Keep these unavailable.
    # Load is a percentage; its relation to real watts is not confirmed on this model.
    warnings = info.get("warnings")
    warning_codes = None
    if isinstance(warnings, list) and all(isinstance(item, str) for item in warnings):
        warning_codes = set()
        for item in warnings:
            match = re.fullmatch(r"(a\d+)(?::\[warning\.\1\])?", item.strip())
            if match:
                warning_codes.add(match.group(1))
    sample = {
        "timestamp": stamp.isoformat(),
        "input_voltage": number(info.get("inputVoltage"), 0, 400),
        "output_voltage": number(info.get("outputVoltage"), 0, 400),
        "frequency_hz": number(info.get("outputFrequency"), 40, 70),
        "load_percent": number(info.get("outputLoadPercent"), 0, 200),
        "battery_percent": number(info.get("batteryCapacity"), 0, 100),
        "battery_voltage": number(info.get("batteryVoltage"), 0, 100),
        "runtime_seconds": None,
        "temperature_c": None,
        "on_battery": on_battery,
        "low_battery": "a7" in warning_codes if warning_codes is not None else None,
        "fault": True if mode == "Fault mode" else None,
        "estimated_watts": None,
        "watts_is_estimate": False,
        "raw_status": mode,
    }
    # a7 is explicitly Low battery in both vendor language dictionaries. The
    # original UI also has an inconsistent a6 check; deliberately do not use it.
    return sample


class ForzaBridge:
    source = "ForzaTracker local · puente de solo lectura"

    def __init__(self, base_url="http://127.0.0.1:15179/Forza"):
        parts = urllib.parse.urlsplit(base_url)
        if (parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost")
                or parts.username or parts.password or parts.query or parts.fragment):
            raise ValueError("El puente debe apuntar a HTTP local sin credenciales.")
        self.base_url = base_url.rstrip("/")
        self.port_name = None
        # Ignore environment proxies; telemetry must stay on loopback.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def _read(self, path, values=None):
        if path not in ("monitoringWorkStatus", "workstatus/reqMonitorData"):
            raise ValueError("Ruta de lectura no permitida.")
        request = urllib.request.Request(
            self.base_url + "/" + path,
            data=urllib.parse.urlencode(values or {}).encode("ascii"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",  # Vendor read endpoints use POST.
        )
        try:
            with self.opener.open(request, timeout=4) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise TelemetryError("Respuesta de telemetría demasiado grande.")
                data = json.loads(raw.decode("utf-8-sig"))
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise TelemetryError("No se pudo leer ForzaTracker en 127.0.0.1:15179. Comprueba que esté ejecutándose.") from exc
        if not isinstance(data, dict):
            raise TelemetryError("Respuesta de ForzaTracker no válida.")
        return data

    def read(self):
        try:
            if not self.port_name:
                discovery = self._read("monitoringWorkStatus")
                port = discovery.get("portName")
                if not isinstance(port, str) or not port.startswith("USB"):
                    raise TelemetryError("ForzaTracker no detecta un UPS USB local.")
                self.port_name = port
            payload = self._read("workstatus/reqMonitorData", {"portName": self.port_name})
            return normalize(payload)
        except TelemetryError:
            self.port_name = None
            raise


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TelemetryError("El puente local intentó redirigir la conexión.")
