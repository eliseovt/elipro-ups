"""Independent, read-only compatibility capture. Never arms or cancels a UPS.

USB descriptors and QS can establish communication compatibility, not physical
cutoff, cancellation, battery reserve, or manual restart behavior.
"""
from __future__ import annotations

import argparse
import hashlib
import json

from .core import iso_utc
from .hid_windows import StatusTransport, enumerate_ups_bounded
from .instance_guard import InstanceGuardError, MonitorInstanceGuard
from .native_adapter import forza_monitor_running
from .qs_protocol import parse_qs
from .ups_power import UpsPowerCoordinator


SOURCES = {
    "model_manual": "https://forza-ups-frontend.s3.amazonaws.com/media/docs/NT-2402PRO_MANUAL.pdf",
    "protocol": "https://networkupstools.org/protocols/voltronic-qs.html",
    "known_limitations": "https://networkupstools.org/docs/man/nutdrv_qx.html#_known_problems",
}


def capture_compatibility(*, action="shutdown"):
    if action not in ("shutdown", "hibernate"):
        raise ValueError("La acción debe ser shutdown o hibernate.")
    # Hold the same process-lifetime ownership as a monitor, including on errors.
    # This command does not stop or replace another reader to obtain that lock.
    with MonitorInstanceGuard():
        if forza_monitor_running():
            raise OSError("ForzaTracker está activo o no se pudo descartar su lector. No se consultará el UPS.")
        devices = enumerate_ups_bounded()
        if len(devices) != 1:
            raise OSError("El diagnóstico requiere exactamente un UPS USB 0665:5161.")
        device = devices[0]
        report = {"schema_version": 1, "diagnostic": "ups_power", "captured_at": iso_utc(),
                  "device": {key: device[key] for key in (
                      "vendor_id", "product_id", "version_number", "manufacturer", "product",
                      "input_report_bytes", "output_report_bytes", "feature_report_bytes", "metadata_open") if key in device},
                  "communication": "incompatible", "validation": "pending", "available": False,
                  "manual_restart": "unvalidated", "restart_behavior": "unvalidated", "physical_validation": "pending", "raw_responses": [],
                  "sources": SOURCES,
                  "control_candidates": {"delayed_manual_poweroff": "S<01..09>R0000\\r", "cancel_pending": "C\\r"},
                  "reason": "Control pendiente: falta completar la validación de corte, temporización, cancelación, reserva y retorno de las salidas.",
                  "cautions": ["El VID/PID y las cadenas USB genéricas no identifican por sí solos el modelo y firmware.",
                               "Los controles aceptados pueden no responder. El ACK del programa original no prueba recepción.",
                               "La batería baja puede adelantar el corte. Algunas variantes vuelven a encender al regresar la red."]}
        sizes = tuple(device.get(key) for key in ("input_report_bytes", "output_report_bytes", "feature_report_bytes"))
        if (device.get("metadata_open") is not True or
                any(type(device.get(key)) is not int for key in ("vendor_id", "product_id", "version_number")) or
                (device["vendor_id"], device["product_id"], device["version_number"]) != (0x0665, 0x5161, 3) or
                any(type(size) is not int for size in sizes) or sizes != (9, 9, 0)):
            report.update(validation="incompatible", reason="La identidad USB no coincide con la unidad candidata registrada.")
            return report
        report["device"]["connection_fingerprint"] = hashlib.sha256(device["path"].casefold().encode()).hexdigest()
        raw = StatusTransport(device["path"]).query_status()
        report["raw_responses"].append({"query": "QS\\r", "raw_hex": raw.hex(), "raw_text": repr(raw)})
        try:
            sample = parse_qs(raw)
        except ValueError as exc:
            report.update(validation="incompatible", reason="Respuesta QS no compatible: " + str(exc))
            return report
        capability = UpsPowerCoordinator(None).capabilities(device={**device, "protocol": "T"}, action=action)
        report.update(communication="compatible", protocol="T", sample=sample, action=action,
                      delay_seconds=capability.get("delay_seconds"))
        if capability.get("validation") == "validated":
            report.update(validation="validated", physical_validation="validated",
                          restart_behavior=capability.get("restart_behavior", "unvalidated"),
                          manual_restart="verified" if capability.get("manual_restart") is True else "not_required",
                          reason="Perfil físico validado para esta acción. El diagnóstico no habilita controles.")
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Identificación y QS del UPS; no corta energía ni modifica parámetros.")
    parser.add_argument("--action", choices=("shutdown", "hibernate"), default="shutdown",
                        help="Acción cuya validación se consulta; no se ejecuta (predeterminado: shutdown)")
    options = parser.parse_args(argv)
    try:
        report = capture_compatibility(action=options.action)
    except (InstanceGuardError, OSError, ValueError) as exc:
        print(json.dumps({"schema_version": 1, "diagnostic": "ups_power", "available": False,
                          "validation": "pending", "error": str(exc)[:500]}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 0 if report["communication"] == "compatible" else 1


if __name__ == "__main__":
    raise SystemExit(main())
