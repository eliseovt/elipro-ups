"""Explicit battery observations; never starts monitoring or computes capacity.

The ordinary native reader remains QS-only. BT?/BL? are issued only through
this separately invoked diagnostic, while holding the monitor ownership lock.
No store, notification channel, protection engine or power callback is created.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import re
import time

from .hid_windows import StatusTransport, enumerate_ups_bounded
from .instance_guard import InstanceGuardError, MonitorInstanceGuard
from .native_adapter import forza_monitor_running
from .qs_protocol import parse_qs


CAPTURE_LABELS = ("network", "battery", "recovery", "manual")


def _validate_options(samples, interval, label):
    if type(samples) is not int or not 1 <= samples <= 120:
        raise ValueError("--samples debe ser un entero entre 1 y 120")
    if type(interval) not in (int, float) or not math.isfinite(interval) or interval < 1:
        raise ValueError("--interval debe ser un número finito de al menos 1 segundo")
    if label not in CAPTURE_LABELS:
        raise ValueError("Etiqueta de captura no admitida")


def _require_oem_stopped():
    if forza_monitor_running():
        raise OSError("ForzaTracker está activo o no se pudo descartar su lector; no se consultará el UPS")


def _diagnostic_transport():
    _require_oem_stopped()
    devices = enumerate_ups_bounded()
    if len(devices) != 1:
        raise OSError("El diagnóstico requiere exactamente un UPS USB 0665:5161")
    device = devices[0]
    if (device.get("metadata_open") is not True
            or type(device.get("vendor_id")) is not int or device["vendor_id"] != 0x0665
            or type(device.get("product_id")) is not int or device["product_id"] != 0x5161):
        raise OSError("No se pudo verificar el acceso y la identidad del UPS")
    sizes = tuple(device.get(key) for key in ("input_report_bytes", "output_report_bytes", "feature_report_bytes"))
    if any(type(size) is not int for size in sizes) or sizes != (9, 9, 0):
        raise OSError("El descriptor HID no coincide con los reportes validados 9/9/0")
    return StatusTransport(device["path"])


def _decode_bt(raw):
    if raw == b"N\r":
        return None, "unsupported"
    # The vendor drops one leading character, but no real capture confirms its
    # prefix or units. Preserve the entire reply instead of guessing a number.
    return None, "unverified_format"


def _decode_bl(raw):
    if raw == b"N\r":
        return None, "unsupported"
    if raw == b"---\r":
        return None, "unavailable"
    if not re.fullmatch(rb"[0-9]{1,3}\r", raw):
        raise ValueError("Formato BL? no verificado: se esperaba un entero ASCII entre 0 y 100 o ---")
    value = int(raw[:-1])
    if not 0 <= value <= 100:
        raise ValueError("El valor BL? está fuera de 0..100; se conserva sin corregirlo")
    return {"value": value, "unit": "percent", "validation": "format_only"}, "decoded"


def _decode_qs(raw):
    return parse_qs(raw), "decoded"


def _empty_query(status="not_captured", error=None):
    return {"raw_hex": None, "raw_text": None, "decoded": None,
            "format_status": status, "error": error}


def _capture_query(query, decode):
    record = _empty_query()
    try:
        _require_oem_stopped()
        raw = query()
        if type(raw) is not bytes:
            raise ValueError("La respuesta de diagnóstico debe ser bytes")
        record["raw_hex"] = raw.hex(" ")
        record["raw_text"] = raw.decode("ascii", "backslashreplace")
        if not 0 < len(raw) <= 256 or not raw.endswith(b"\r") or b"\r" in raw[:-1]:
            raise ValueError("Respuesta de diagnóstico incompleta, concatenada o demasiado larga")
        record["decoded"], record["format_status"] = decode(raw)
    except (OSError, ValueError) as exc:
        record["error"] = str(exc)[:500]
        record["format_status"] = "error"
    return record


def capture_battery_samples(*, samples=1, interval=1, label="manual"):
    """Return observations under exclusive ownership; never persist telemetry."""
    _validate_options(samples, interval, label)
    report = {"schema_version": 1, "diagnostic": "battery", "physical_validation": "pending",
              "estimation": "not_computed", "samples": []}
    # Acquire before even discovering USB or checking an OEM collector. This is
    # the same computer-wide ownership used by the monitor and Session 0 service.
    with MonitorInstanceGuard():
        transport = _diagnostic_transport()
        for index in range(samples):
            if index:
                time.sleep(interval)
            capture = {"timestamp": datetime.now(timezone.utc).isoformat(), "capture_label": label}
            capture["qs"] = _capture_query(transport.query_status, _decode_qs)
            if capture["qs"]["error"]:
                for name in ("bt", "bl"):
                    capture[name] = _empty_query("skipped", "No se consultó tras fallar la lectura QS")
            else:
                capture["bt"] = _capture_query(lambda: transport.query_battery_diagnostic(b"BT?\r"), _decode_bt)
                capture["bl"] = _capture_query(lambda: transport.query_battery_diagnostic(b"BL?\r"), _decode_bl)
            report["samples"].append(capture)
    return report


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Captura explícita de batería: QS, BT? y BL?; sin estimación ni acciones de energía")
    parser.add_argument("--samples", type=int, default=1, help="Número de capturas, entre 1 y 120 (predeterminado: 1)")
    parser.add_argument("--interval", type=float, default=1, help="Espera entre capturas en segundos, mínimo 1 (predeterminado: 1)")
    parser.add_argument("--label", choices=CAPTURE_LABELS, default="manual", help="Etiqueta descriptiva; no cambia la fuente ni simula estados")
    options = parser.parse_args(argv)
    try:
        _validate_options(options.samples, options.interval, options.label)
    except ValueError as exc:
        parser.error(str(exc))
    return options


def main(argv=None):
    options = parse_args(argv)
    try:
        report = capture_battery_samples(samples=options.samples, interval=options.interval, label=options.label)
    except (InstanceGuardError, OSError, ValueError) as exc:
        print(json.dumps({"schema_version": 1, "diagnostic": "battery", "error": str(exc)[:500], "samples": []}, ensure_ascii=True))
        return 1
    # ASCII JSON survives Windows console encodings and direct file redirection.
    print(json.dumps(report, ensure_ascii=True, allow_nan=False))
    return int(any(query["error"] for sample in report["samples"] for query in (sample["qs"], sample["bt"], sample["bl"])))


if __name__ == "__main__":
    raise SystemExit(main())
