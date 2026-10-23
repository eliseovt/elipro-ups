"""Isolated, fail-closed UPS output control and durable operation ownership.

The production registry contains one unit-bound observed hibernation sequence.
User-configured timers are separate operational choices, not new measurements
of battery autonomy, hibernation duration or hardware timing.
Tests inject profiles and fake transports explicitly; no test opens real USB.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
import uuid


NT2402PRO_CANDIDATE_PROFILE = {
    "profile_id": "nt2402pro-qs-t-rev3", "model": "NT-2402PRO", "protocol": "T",
    "vendor_id": 0x0665, "product_id": 0x5161, "version_number": 3,
    "input_report_bytes": 9, "output_report_bytes": 9, "feature_report_bytes": 0,
}
OBSERVED_UNIT_FINGERPRINT = "7df095c878742cf146a631909fd5f39946926b787720d9932263d2805e59399b"
# These are operational telemetry/status checks, not measurements of autonomy.
# Keep the physical trial and its point readings below unchanged for traceability.
OPERATIONAL_LIMITS = {"source": "device_status", "version": 2,
    "load_percent": "informational", "battery_voltage": "informational",
    "low_battery_alarm": "windows_only_immediate_cut_risk"}
VALIDATED_PROFILES = {"nt2402pro-qs-t-rev3": {
    **NT2402PRO_CANDIDATE_PROFILE, "device_fingerprint": OBSERVED_UNIT_FINGERPRINT,
    "restart_behavior": "automatic_on_mains", "restart_behavior_verified": True, "cancel_verified": True,
    "cancel_source_operation_id": "d211088e-9e60-4a93-91fa-987e0c960b68",
    "low_battery_cutoff_behavior": "immediate", "low_battery_evidence": "protocol_worst_case_not_physically_tested",
    "pending_signal_unreliable": True,
    "pending_signal_evidence": {"device_fingerprint": OBSERVED_UNIT_FINGERPRINT,
        "output_cut_observed": True, "pending_signal_not_reported": True,
        "cancel_observed_without_cut": True, "automatic_return_observed": True,
        "cut_source_operation_id": "de13ebcf-4428-447b-9493-4802686d3a0c",
        "cancel_source_operation_id": "d211088e-9e60-4a93-91fa-987e0c960b68",
        "return_source_operation_id": "a8b574b3-8bff-4615-80c2-7647c0baced6"},
    "operation_configuration": {"source": "user_configurable", "min_delay_seconds": 60,
        "max_delay_seconds": 540, "max_countdown_seconds": 120, "poll_margin_seconds": 10,
        "requires_on_battery_seconds": 0},
    "operational_limits": OPERATIONAL_LIMITS.copy(),
    "action_validation": {"hibernate": {
        "completion_evidence": {"trial_count": 2, "observed_range_seconds": [10, 15]},
        "reserve_mode": "observed_sequence",
        "sequence_validation": {"max_seconds_since_mains": 15, "max_load_percent": 11,
            "min_mains_battery_voltage": 27.41, "min_battery_voltage": 25.41, "max_countdown_seconds": 10,
            "source_operation_id": "a8b574b3-8bff-4615-80c2-7647c0baced6", "approximate_cut_delay_seconds": 180,
            "measured_active_battery_seconds": 12.656, "measured_since_mains_seconds": 15.016,
            "measured_full_submission_since_mains_seconds": 15.353}}}}}
_DEVICE_FIELDS = ("vendor_id", "product_id", "version_number", "input_report_bytes",
                  "output_report_bytes", "feature_report_bytes")
_PENDING = {"arming", "submitted", "armed", "uncertain", "cancelling", "cancel_submitted"}


class UpsPowerBlocked(ValueError):
    """Output control lacks complete evidence or a safe, owned operation."""


def _operation_configuration(profile, action):
    value = profile.get("operation_configuration")
    if value is None:
        return None
    expected = {"source": "user_configurable", "min_delay_seconds": 60, "max_delay_seconds": 540,
        "max_countdown_seconds": 120, "poll_margin_seconds": 10, "requires_on_battery_seconds": 0}
    if action != "hibernate" or value != expected or any(type(value.get(key)) is not int for key in expected if key != "source"):
        raise UpsPowerBlocked("La configuración operativa del UPS no corresponde al perfil permitido.")
    return copy.deepcopy(value)


def _operational_limits(profile, action):
    value = profile.get("operational_limits")
    if value is None:
        return None
    if (action != "hibernate" or _operation_configuration(profile, action) is None
            or not operational_limits_valid(value)):
        raise UpsPowerBlocked("Los criterios operativos del UPS no corresponden al perfil permitido.")
    return copy.deepcopy(value)


def operational_limits_valid(value):
    """Reject type-coerced or partial criteria in both UI admission and USB."""
    return (isinstance(value, dict) and value == OPERATIONAL_LIMITS
        and all(type(value.get(key)) is type(expected) for key, expected in OPERATIONAL_LIMITS.items()))


def configured_delay_seconds(profile, action, selected=None):
    """Separate the user's timer from the unchanged physical observation."""
    observed, reserve = _profile_evidence(profile, action)
    if selected is None:
        return observed
    if type(selected) is int and selected == 600:
        raise UpsPowerBlocked("10 minutos no tiene soporte documentado para QS-Hex: el protocolo permite de 1 a 9. Se conserva la protección de Windows.")
    if type(selected) is not int or not 60 <= selected <= 540 or selected % 60:
        raise UpsPowerBlocked("El retardo configurado debe ser de 1 a 9 minutos enteros.")
    operating = _operation_configuration(profile, action)
    if reserve is None and operating is not None:
        validation = profile["action_validation"][action]
        completion = validation.get("completion_evidence", validation.get("completion_seconds"))
        values = completion["observed_range_seconds"] if isinstance(completion, dict) else completion
        if selected < max(values) + 45:
            raise UpsPowerBlocked("El retardo configurado no conserva al menos 45 segundos sobre la hibernación observada.")
        return selected
    if selected != observed:
        raise UpsPowerBlocked("Este perfil sólo permite el retardo de su acción físicamente validada.")
    return observed


def shutdown_command(delay_seconds):
    """Only whole-minute, bounded stay-off commands; no restart fallback."""
    if type(delay_seconds) is not int or not 60 <= delay_seconds <= 540 or delay_seconds % 60:
        raise ValueError("El retardo del UPS debe ser de 1 a 9 minutos enteros.")
    return f"S{delay_seconds // 60:02d}R0000\r".encode("ascii")


def control_reports(command):
    """Pure codec: ID zero plus eight payload bytes, CR only in the last report."""
    if type(command) is not bytes or (command != b"C\r" and not re.fullmatch(rb"S0[1-9]R0000\r", command)):
        raise ValueError("Solo se admite apagado permanente acotado o cancelación del UPS.")
    return tuple(b"\x00" + command[index:index + 8].ljust(8, b"\x00")
                 for index in range(0, len(command), 8))


def minimum_delay_seconds(completion_seconds):
    """Use the upper bound of at least two trials, preserving reported ranges.

    A range is not expanded into invented individual measurements. Its explicit
    shape is {"trial_count": 2, "observed_range_seconds": [10, 15]}.
    """
    if isinstance(completion_seconds, dict):
        count = completion_seconds.get("trial_count")
        values = completion_seconds.get("observed_range_seconds")
        valid = (set(completion_seconds) == {"trial_count", "observed_range_seconds"}
                 and type(count) is int and count >= 2 and isinstance(values, (list, tuple)) and len(values) == 2)
    else:
        values = completion_seconds
        valid = isinstance(values, (list, tuple)) and len(values) >= 2
    if (not valid or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0 for value in values)
            or isinstance(completion_seconds, dict) and values[0] > values[1]):
        raise UpsPowerBlocked("Faltan al menos dos ensayos válidos de finalización de Windows o su rango observado.")
    delay = max(180, math.ceil((max(values) + 120) / 60) * 60)
    if delay > 540:
        raise UpsPowerBlocked("La finalización medida de Windows excede el retardo máximo validable del UPS.")
    return delay


def _restart_behavior(profile):
    behavior = profile.get("restart_behavior")
    if behavior is None and profile.get("manual_restart_verified") is True:
        return "manual_only"  # Legacy unit profiles retain their original meaning.
    if behavior == "manual_only" and (profile.get("manual_restart_verified") is True or profile.get("restart_behavior_verified") is True):
        return behavior
    if behavior == "automatic_on_mains" and profile.get("restart_behavior_verified") is True:
        return behavior
    raise UpsPowerBlocked("Falta validar el comportamiento de retorno de la salida de esta unidad.")


def _operation_restart_behavior(operation):
    behavior = operation.get("restart_behavior")
    if behavior is None and operation.get("manual_restart") is True:
        return "manual_only"
    if behavior == "manual_only" and operation.get("manual_restart") is True:
        return behavior
    if (behavior == "automatic_on_mains" and operation.get("manual_restart") is False
            and operation.get("restart_behavior_verified") is True):
        return behavior
    raise ValueError("El registro no documenta un comportamiento de retorno validado.")


def _sequence_configuration(validation, action, delay):
    sequence = validation.get("sequence_validation")
    numeric = ("max_seconds_since_mains", "max_load_percent", "min_mains_battery_voltage", "min_battery_voltage",
               "measured_active_battery_seconds", "measured_since_mains_seconds")
    if (action != "hibernate" or not isinstance(sequence, dict) or "reserve_seconds" in validation
            or any(type(sequence.get(key)) not in (int, float) or not math.isfinite(sequence[key]) or sequence[key] <= 0 for key in numeric)
            or sequence["max_load_percent"] > 100 or sequence["max_seconds_since_mains"] > sequence["measured_since_mains_seconds"]
            or type(sequence.get("max_countdown_seconds")) is not int or sequence["max_countdown_seconds"] != 10
            or sequence.get("approximate_cut_delay_seconds") != delay):
        raise UpsPowerBlocked("La secuencia observada solo permite hibernación rápida dentro de sus condiciones documentadas.")
    try:
        uuid.UUID(sequence["source_operation_id"])
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise UpsPowerBlocked("La secuencia requiere trazabilidad de su observación física.") from exc
    return sequence


def _profile_evidence(profile, action):
    if action not in ("shutdown", "hibernate"):
        raise UpsPowerBlocked("Acción de Windows no admitida para el apagado del UPS.")
    if (not isinstance(profile, dict) or any(profile.get(key) != value for key, value in NT2402PRO_CANDIDATE_PROFILE.items())
            or any(type(profile.get(key)) is not int for key in _DEVICE_FIELDS)):
        raise UpsPowerBlocked("El perfil no corresponde al UPS y protocolo candidatos.")
    fingerprint = profile.get("device_fingerprint")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise UpsPowerBlocked("El perfil no está vinculado a una unidad USB físicamente validada.")
    behavior = _restart_behavior(profile)
    if (profile.get("cancel_verified") is not True
            or profile.get("low_battery_cutoff_behavior") not in ("immediate", "delay_preserved")):
        raise UpsPowerBlocked("Falta validar cancelación o corte por batería baja.")
    unreliable = profile.get("pending_signal_unreliable", False)
    if type(unreliable) is not bool:
        raise UpsPowerBlocked("La fiabilidad del estado pendiente debe estar documentada como booleana.")
    if unreliable:
        evidence = profile.get("pending_signal_evidence")
        return_observed = (isinstance(evidence, dict) and (
            evidence.get("manual_start_observed") is True if behavior == "manual_only" else
            evidence.get("automatic_return_observed") is True))
        if (not isinstance(evidence, dict) or not return_observed or evidence.get("device_fingerprint") != fingerprint
                or any(evidence.get(field) is not True for field in (
                    "output_cut_observed", "pending_signal_not_reported", "cancel_observed_without_cut"))):
            raise UpsPowerBlocked("La excepción del bit pendiente requiere evidencia física de esta misma unidad.")
    actions = profile.get("action_validation")
    validation = actions.get(action) if isinstance(actions, dict) else None
    if not isinstance(validation, dict):
        raise UpsPowerBlocked("Esta acción de Windows no tiene validación física.")
    if "completion_seconds" in validation and "completion_evidence" in validation:
        raise UpsPowerBlocked("La evidencia de finalización de Windows no debe ser ambigua.")
    delay = minimum_delay_seconds(validation.get("completion_evidence", validation.get("completion_seconds")))
    if validation.get("reserve_mode") == "observed_sequence":
        _sequence_configuration(validation, action, delay)
        _operational_limits(profile, action)
        return delay, None
    if validation.get("reserve_mode", "measured_reserve") != "measured_reserve":
        raise UpsPowerBlocked("Modo de evidencia de batería no admitido.")
    reserve = validation.get("reserve_seconds")
    if type(reserve) not in (int, float) or not math.isfinite(reserve) or reserve < delay:
        raise UpsPowerBlocked("La reserva de batería medida no cubre el retardo de corte.")
    if validation.get("reserve_asof", "battery_start") != "battery_start":
        raise UpsPowerBlocked("La reserva validada debe referirse al comienzo del uso de batería.")
    load = validation.get("validated_max_load_percent")
    if load is not None and (type(load) not in (int, float) or not math.isfinite(load) or not 0 < load <= 100):
        raise UpsPowerBlocked("La carga máxima validada de batería no es válida.")
    if "reserve_baseline" in validation:
        baseline = validation["reserve_baseline"]
        if (not isinstance(baseline, dict) or set(baseline) != {"recharge_seconds", "min_battery_voltage"}
                or any(type(baseline.get(key)) not in (int, float) or not math.isfinite(baseline[key]) or baseline[key] <= 0
                       for key in ("recharge_seconds", "min_battery_voltage")) or load is None):
            raise UpsPowerBlocked("La reserva medida requiere una condición de recarga y una carga máxima físicamente validadas.")
    return delay, reserve


def profile_matches_device(profile, device):
    if not isinstance(device, dict):
        return False
    return (all(type(device.get(key)) is int and device[key] == profile.get(key) for key in _DEVICE_FIELDS)
            and device.get("protocol", profile.get("protocol")) == profile.get("protocol")
            and isinstance(device.get("path"), str) and bool(device["path"])
            and device_fingerprint(device) == profile.get("device_fingerprint"))


def validated_control_profile(profile_id, profiles=None):
    """Internal workers consult the production registry before opening HID."""
    registry = VALIDATED_PROFILES if profiles is None else profiles
    profile = registry.get(profile_id) if isinstance(registry, dict) else None
    if not isinstance(profile, dict):
        raise UpsPowerBlocked("No hay un perfil físico validado para controlar este UPS.")
    for action in ("shutdown", "hibernate"):
        try:
            _profile_evidence(profile, action)
            return copy.deepcopy(profile)
        except UpsPowerBlocked:
            pass
    raise UpsPowerBlocked("El perfil no contiene una acción validada para controlar el UPS.")


def device_fingerprint(device):
    identity = {key: device.get(key) for key in _DEVICE_FIELDS}
    identity["path_hash"] = hashlib.sha256(device["path"].casefold().encode("utf-8")).hexdigest()
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()


def _stamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fresh_sample(sample):
    if not isinstance(sample, dict) or sample.get("connected", True) is not True:
        return False
    try:
        stamp = datetime.fromisoformat(sample["timestamp"].replace("Z", "+00:00"))
        return stamp.tzinfo is not None and -5 <= (datetime.now(timezone.utc) - stamp).total_seconds() <= 15
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return False


def _qs_shutdown_bit(sample):
    if not _fresh_sample(sample):
        return None
    raw = sample.get("raw_flags")
    flags = raw.get("status_hex") if isinstance(raw, dict) else None
    if not isinstance(flags, str) or not re.fullmatch(r"[0-9A-Fa-f]{2}", flags):
        return None
    return bool(int(flags, 16) & 2)


def _diagnostic_value(value):
    """Keep failure diagnostics JSON-safe even for malformed telemetry."""
    if type(value) is float and not math.isfinite(value):
        return repr(value)
    if isinstance(value, dict):
        return {str(key): _diagnostic_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_diagnostic_value(item) for item in value]
    if value is None or type(value) in (str, int, float, bool):
        return value
    return repr(value)


def operational_sample_admission(sample, limits, *, expected_battery=None):
    """One status/telemetry criterion for the tracker and final USB preflight.

    Battery voltage is informational. A previous trial's voltage is not a
    low-battery threshold or a claim that today's battery has measured runtime.
    Only the current sample is checked; a previous AC reading is not required.
    """
    sample = sample if isinstance(sample, dict) else {}
    checks = []

    def check(key, label, passed, value, expected, reason):
        checks.append({"key": key, "label": label, "passed": bool(passed),
            "value": _diagnostic_value(value), "expected": expected, "reason": None if passed else reason,
            "category": ("operation" if key in ("battery_test", "shutdown_pending") else
                "controlled_shutdown" if key in ("low_battery", "fault", "output_voltage") else "connection")})

    valid_limits = operational_limits_valid(limits)
    check("operational_profile", "Criterios operativos", valid_limits, limits, "device_status v2",
        "El perfil carece de criterios operativos de estado del UPS válidos.")
    fresh = _fresh_sample(sample)
    check("sample_fresh", "Lectura USB reciente", fresh, sample.get("timestamp"),
        "máximo 15 s",
        "La lectura USB falta, está desconectada o está vencida; se requiere una nueva lectura válida.")
    raw = sample.get("raw_flags")
    flags = raw.get("status_hex") if isinstance(raw, dict) else None
    valid_flags = isinstance(flags, str) and re.fullmatch(r"[0-9A-Fa-f]{2}", flags) is not None
    bits = int(flags, 16) if valid_flags else None
    check("qs_status", "Estado QS confirmado", valid_flags, flags, "dos dígitos hexadecimales",
        "La lectura no incluye un estado QS válido para controlar el UPS.")
    critical = sample.get("low_battery") is True or bits is not None and bool(bits & 0x40)
    check("low_battery", "Sin alarma crítica de batería del UPS", sample.get("low_battery") is False
        and bits is not None and not bits & 0x40, sample.get("low_battery"), False,
        "El UPS reporta batería crítica; una orden podría cortar inmediatamente. Se prioriza hibernar Windows."
        if critical else "El estado de batería crítica no está confirmado o contradice la lectura QS.")
    if critical:
        checks[-1].update(blocking=True, severity="critical")
    if bits is None or not bits & 0x40:
        checks[-1]["category"] = "connection" if not checks[-1]["passed"] else "controlled_shutdown"
    check("fault", "Sin falla del UPS", sample.get("fault") is False and bits is not None
        and not bits & 0x10, sample.get("fault"), False,
        "El UPS indica una falla o su ausencia no está confirmada; solo se protege Windows.")
    if bits is None or not bits & 0x10:
        checks[-1]["category"] = "connection" if not checks[-1]["passed"] else "controlled_shutdown"
    check("battery_test", "Sin prueba de batería activa", bits is not None and not bits & 0x04,
        None if bits is None else bool(bits & 0x04), False,
        "Hay una prueba de batería activa o su estado no se puede confirmar.")
    check("shutdown_pending", "Sin otra orden de apagado activa", bits is not None and not bits & 0x02,
        None if bits is None else bool(bits & 0x02), False,
        "El UPS informa un apagado pendiente o no permite descartarlo; no se repetirá la orden.")
    battery = sample.get("on_battery")
    state_ok = (type(battery) is bool and bits is not None and bool(bits & 0x80) == battery
        and sample.get("raw_status") in (("Battery mode",) if battery else ("Line mode", "AVR mode"))
        and (expected_battery is None or battery is expected_battery))
    check("power_state", "Fuente de energía confirmada", state_ok,
        {"on_battery": battery, "raw_status": sample.get("raw_status")},
        "batería" if expected_battery is True else "red eléctrica" if expected_battery is False else "red o batería confirmadas",
        "El estado de energía no confirma " + ("el uso de batería." if expected_battery is True else
            "la red eléctrica previa." if expected_battery is False else "red o batería sin estados contradictorios."))
    # The QS parser intentionally leaves on_battery unknown in these modes.
    # A coherent device-reported fault/test/pending order is not loss of USB.
    if battery is None and bits is not None:
        for mode, flag, category in (("Fault mode", 0x10, "controlled_shutdown"),
                ("Battery test mode", 0x04, "operation"), ("Shutdown mode", 0x02, "operation")):
            if sample.get("raw_status") == mode and bits & flag:
                checks[-1]["category"] = category
                break
    voltage = sample.get("battery_voltage")
    check("battery_voltage", "Voltaje de batería informativo", True, voltage,
        "solo información; no bloquea el apagado", None)
    checks[-1].update(blocking=False, severity="info", category=None)
    load = sample.get("load_percent")
    check("load_percent", "Carga del UPS informativa", True, load,
        "solo información; una carga elevada por sí sola no bloquea el apagado", None)
    checks[-1].update(blocking=False, severity="info", category=None)
    check("output_voltage", "Salida del UPS activa", _output_active(sample), sample.get("output_voltage"), "finito y mayor que 0 V",
        "La salida del UPS no está activa o su voltaje no es válido.")
    if not fresh or not valid_flags:
        # Missing/stale/undecodable telemetry cannot establish a current device
        # fault or an operation in progress. Report its actual connection cause.
        for item in checks:
            if not item["passed"]:
                item["category"] = "connection"
    failures = [item for item in checks if not item["passed"]]
    return {"available": not failures, "reason_code": failures[0]["key"] if failures else None,
        "reason": " ".join(item["reason"] for item in failures) or None, "checks": checks}


def _shutdown_active(sample):
    return isinstance(sample, dict) and sample.get("raw_status") == "Shutdown mode" and _qs_shutdown_bit(sample) is True


def _observed_after(sample, completed_at):
    """A cached, otherwise fresh QS reading cannot confirm a later write."""
    if not _fresh_sample(sample) or not isinstance(completed_at, str):
        return False
    try:
        observed = datetime.fromisoformat(sample["timestamp"].replace("Z", "+00:00"))
        completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        return completed.tzinfo is not None and observed > completed
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
        return False


def _output_active(sample):
    output = sample.get("output_voltage") if isinstance(sample, dict) else None
    return type(output) in (int, float) and math.isfinite(output) and output > 0


def _cancel_evidence(profile, operation, sample):
    """The unreliable-bit exception requires a complete own submission."""
    submitted_at = operation.get("submitted_at") or operation.get("submission_finished_at")
    if operation.get("submission_complete") is not True:
        return False
    if (not _observed_after(sample, submitted_at) or not _output_active(sample)
            or sample.get("low_battery") is not False or sample.get("fault") is not False):
        return False
    if _shutdown_active(sample):
        return True
    return (profile.get("pending_signal_unreliable") is True and operation.get("submission_complete") is True
            and _qs_shutdown_bit(sample) is False
            and ((sample.get("on_battery") is False and sample.get("raw_status") in ("Line mode", "AVR mode"))
                 or sample.get("on_battery") is True and sample.get("raw_status") == "Battery mode"))


def validate_production_cancel_preflight(profile, operation, sample, *, now_monotonic=None):
    """Final worker check on a direct QS reading while its device mutex is held."""
    if not isinstance(operation, dict):
        raise UpsPowerBlocked("Falta una operación durable propia para cancelar el UPS.")
    delay = configured_delay_seconds(profile, operation.get("action"), operation.get("delay_seconds"))
    started = operation.get("started_monotonic")
    owner_pid = operation.get("owner_pid")
    current = time.monotonic() if now_monotonic is None else now_monotonic
    if (operation.get("owner") != "elipro-ups-power" or operation.get("state") != "cancelling"
            or operation.get("cancel_attempted") is not True or operation.get("interrupted") is True
            or operation.get("profile_id") != profile["profile_id"] or _operation_restart_behavior(operation) != _restart_behavior(profile)
            or operation.get("device_fingerprint") != profile["device_fingerprint"]
            or operation.get("delay_seconds") != delay or operation.get("submission_complete") is not True
            or type(owner_pid) is not int or owner_pid not in (os.getpid(), os.getppid())
            or type(started) not in (int, float) or not math.isfinite(started)
            or type(current) not in (int, float) or not math.isfinite(current)
            or not 0 <= current - started < delay - 10 or not _cancel_evidence(profile, operation, sample)):
        raise UpsPowerBlocked("No hay una cancelación propia, reciente y segura para este UPS.")
    return True


def validate_production_start_preflight(profile, operation, sample, *, now_monotonic=None):
    """Repeat battery/load/reserve checks on the worker's immediate QS sample."""
    if not isinstance(operation, dict):
        raise UpsPowerBlocked("Falta una operación durable propia para apagar el UPS.")
    _, reserve = _profile_evidence(profile, operation.get("action"))
    delay = configured_delay_seconds(profile, operation.get("action"), operation.get("delay_seconds"))
    current = time.monotonic() if now_monotonic is None else now_monotonic
    started = operation.get("started_monotonic")
    remaining = operation.get("reserve_seconds")
    load = sample.get("load_percent") if isinstance(sample, dict) else None
    limits = _operational_limits(profile, operation["action"])
    load_limit = None if limits is not None else profile["action_validation"][operation["action"]].get("validated_max_load_percent")
    if limits is not None:
        admission = operational_sample_admission(sample, limits, expected_battery=True)
        if not admission["available"]:
            raise UpsPowerBlocked(admission["reason"])
    if (operation.get("owner") != "elipro-ups-power" or operation.get("state") != "arming"
            or operation.get("interrupted") is True or operation.get("submission_complete") is True
            or type(operation.get("owner_pid")) is not int or operation["owner_pid"] not in (os.getpid(), os.getppid())
            or operation.get("profile_id") != profile["profile_id"] or _operation_restart_behavior(operation) != _restart_behavior(profile)
            or operation.get("device_fingerprint") != profile["device_fingerprint"]
            or operation.get("delay_seconds") != delay or type(started) not in (int, float) or not math.isfinite(started)
            or type(current) not in (int, float) or not math.isfinite(current) or not 0 <= current - started <= 10
            or not _fresh_sample(sample) or sample.get("on_battery") is not True
            or limits is None and sample.get("low_battery") is not False
            or sample.get("fault") is not False or sample.get("raw_status") != "Battery mode" or not _output_active(sample)
            or load_limit is not None and (type(load) not in (int, float) or not math.isfinite(load) or not 0 <= load <= load_limit)):
        raise UpsPowerBlocked("La lectura inmediata no permite completar Windows y cortar el UPS con reserva validada.")
    if reserve is None:
        if operation.get("reserve_seconds") is not None or operation.get("reserve_mode") != "observed_sequence":
            raise UpsPowerBlocked("La secuencia observada no acredita segundos de autonomía.")
        validate_observed_sequence_preflight(profile, operation["action"], sample, operation.get("sequence_evidence"), now_monotonic=current)
    elif (type(remaining) not in (int, float) or not math.isfinite(remaining)
          or min(reserve, remaining) - (current - started) < delay):
        raise UpsPowerBlocked("La reserva restante no cubre el corte retardado validado.")
    return True


def validate_observed_sequence_preflight(profile, action, sample, evidence, *, now_monotonic=None):
    """Validate continuity and operational status; never infer battery runtime."""
    delay, reserve = _profile_evidence(profile, action)
    if reserve is not None or not isinstance(evidence, dict):
        raise UpsPowerBlocked("Falta evidencia continua de la secuencia de hibernación observada.")
    sequence = _sequence_configuration(profile["action_validation"][action], action, delay)
    operating = _operation_configuration(profile, action)
    limits = _operational_limits(profile, action)
    current = time.monotonic() if now_monotonic is None else now_monotonic
    if limits is not None:
        # Production operates from the current battery countdown, including
        # service startup or USB reconnection while mains is still absent.
        # Historical trial readings remain traceability, never runtime gates.
        times = [evidence.get(key) for key in ("battery_started_monotonic", "last_sample_monotonic")]
        countdown = evidence.get("countdown_seconds")
        if (evidence.get("kind") != "battery_countdown" or type(evidence.get("version")) is not int
                or evidence["version"] != 2
                or evidence.get("profile_id") != profile["profile_id"]
                or evidence.get("device_fingerprint") != profile["device_fingerprint"]
                or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in [current, *times])
                or not times[0] <= times[1] <= current or current - times[1] > 15
                or type(countdown) is not int or not 10 <= countdown <= operating["max_countdown_seconds"]
                or current - times[0] < countdown):
            raise UpsPowerBlocked("Falta una cuenta atrás cumplida con lecturas USB recientes vinculadas a esta unidad.")
        admission = operational_sample_admission(sample, limits, expected_battery=True)
        if not admission["available"]:
            raise UpsPowerBlocked(admission["reason"])
        return True
    times = [evidence.get(key) for key in ("last_mains_monotonic", "battery_started_monotonic", "last_sample_monotonic")]
    if (any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in [current, *times])
            or not times[0] <= times[1] <= times[2] <= current
            or not 0 <= current - times[2] <= 15
            or evidence.get("profile_id") != profile["profile_id"] or evidence.get("device_fingerprint") != profile["device_fingerprint"]
            or type(evidence.get("on_battery_seconds")) is not int or evidence["on_battery_seconds"] != 0
            or type(evidence.get("countdown_seconds")) is not int
            or not 10 <= evidence["countdown_seconds"] <= (operating["max_countdown_seconds"] if operating else sequence["max_countdown_seconds"])
            or current - times[1] < evidence["countdown_seconds"]
            or not 0 <= current - times[0] <= (evidence["countdown_seconds"] + operating["poll_margin_seconds"]
                if operating else sequence["max_seconds_since_mains"])):
        raise UpsPowerBlocked("La operación requiere un tiempo antes de actuar de 10 a 120 segundos y lecturas continuas.")
    mains = evidence.get("last_mains_sample")
    for reading, online in ((mains, True), (sample, False)):
        # The historical CA sample is retained throughout the countdown. Its
        # monotonic origin and uninterrupted fresh QS readings bound that age;
        # it is not presented as a new CA reading at the end of a long countdown.
        historical = online and operating is not None
        valid_age, shutdown_bit = _fresh_sample(reading), _qs_shutdown_bit(reading)
        if historical and isinstance(reading, dict):
            try:
                stamp = datetime.fromisoformat(reading["timestamp"].replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - stamp).total_seconds()
                valid_age = stamp.tzinfo is not None and 0 <= age <= evidence["countdown_seconds"] + operating["poll_margin_seconds"] + 5
                flags = reading.get("raw_flags", {}).get("status_hex")
                shutdown_bit = bool(int(flags, 16) & 2) if isinstance(flags, str) and re.fullmatch(r"[0-9A-Fa-f]{2}", flags) else None
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                valid_age = False
        if (not valid_age or reading.get("on_battery") is not (False if online else True)
                or reading.get("low_battery") is not False or reading.get("fault") is not False
                or reading.get("raw_status") not in (("Line mode", "AVR mode") if online else ("Battery mode",))
                or not _output_active(reading) or shutdown_bit is not False):
            raise UpsPowerBlocked("La secuencia requiere CA previa y batería actual confirmadas, sin falla ni batería baja.")
        voltage, load = reading.get("battery_voltage"), reading.get("load_percent")
        floor = sequence["min_mains_battery_voltage"] if online else sequence["min_battery_voltage"]
        if (type(voltage) not in (int, float) or not math.isfinite(voltage) or voltage < floor
                or type(load) not in (int, float) or not math.isfinite(load) or not 0 <= load <= sequence["max_load_percent"]):
            raise UpsPowerBlocked("Voltaje o carga fuera de la secuencia físicamente observada.")
    maximum, minimum = evidence.get("max_load_percent"), evidence.get("min_battery_voltage")
    if (type(maximum) not in (int, float) or not math.isfinite(maximum) or not 0 <= maximum <= sequence["max_load_percent"]
            or type(minimum) not in (int, float) or not math.isfinite(minimum) or minimum < sequence["min_battery_voltage"]):
        raise UpsPowerBlocked("La continuidad de carga y voltaje no satisface la secuencia observada.")
    return True


class OperationJournal:
    """Atomically replace and flush the journal before any control transaction."""

    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return None
        if self.path.stat().st_size > 16384:
            raise ValueError("El registro de control del UPS excede el límite permitido.")
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("owner") != "elipro-ups-power"
                or value.get("state") not in _PENDING | {"cancelled", "completed"}
                or not isinstance(value.get("device_fingerprint"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", value["device_fingerprint"])):
            raise ValueError("El registro de control del UPS no es propio o válido.")
        uuid.UUID(value["operation_id"])
        created = datetime.fromisoformat(value["created_at"].replace("Z", "+00:00"))
        if (created.tzinfo is None or value.get("action") not in ("shutdown", "hibernate")
                or not isinstance(value.get("profile_id"), str) or not value["profile_id"]
                or type(value.get("delay_seconds")) is not int or not 60 <= value["delay_seconds"] <= 540
                or value["delay_seconds"] % 60):
            raise ValueError("El registro previo no contiene una operación validada completa.")
        _operation_restart_behavior(value)
        return value

    def write(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=self.path.name + ".", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                json.dump(value, output, ensure_ascii=False, allow_nan=False, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            temporary = None
            with self.path.open("r+b") as output:
                os.fsync(output.fileno())
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class UpsPowerCoordinator:
    def __init__(self, journal_path=None, *, validated_profiles=None, transport_factory=None, clock=None):
        self._profiles = VALIDATED_PROFILES if validated_profiles is None else copy.deepcopy(validated_profiles)
        self._transport_factory = transport_factory
        self._clock = time.monotonic if clock is None else clock
        self._journal = OperationJournal(journal_path) if journal_path is not None else None
        self._lock = threading.RLock()
        self._operation = None
        self._journal_error = None
        self._orphaned = False
        self._started_at = None
        self._interrupted = threading.Event()
        self._interruption_count = 0
        self._last_cancel_observation = None
        self._active_transport = None
        try:
            self._operation = self._journal.load() if self._journal is not None else None
            self._orphaned = bool(self._operation and self._operation["state"] in _PENDING)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
            self._journal_error = "El registro previo del UPS necesita revisión: " + str(exc)

    def _profile(self, device=None, action="shutdown"):
        reasons = []
        for profile_id in self._profiles:
            try:
                profile = validated_control_profile(profile_id, self._profiles)
                delay, reserve = _profile_evidence(profile, action)
                if device is not None and not profile_matches_device(profile, device):
                    continue
                return profile, delay, reserve
            except UpsPowerBlocked as exc:
                reasons.append(str(exc))
        raise UpsPowerBlocked(reasons[0] if reasons else "Perfil de control pendiente de validar en este equipo; el corte está bloqueado.")

    def capabilities(self, device=None, action="shutdown", delay_seconds=None):
        result = {"available": False, "validation": "profile_unvalidated", "reason": None,
                  "delay_seconds": delay_seconds, "reserve_seconds": None, "manual_restart": False,
                  "restart_behavior": None, "model": "NT-2402PRO", "action": action}
        try:
            profile, delay, reserve = self._profile(device, action)
            selected = delay if delay_seconds is None else delay_seconds
            result.update(delay_seconds=selected, observed_delay_seconds=delay,
                delay_hardware_validated=type(selected) is int and selected == delay,
                delay_protocol_supported=type(selected) is int and 60 <= selected <= 540 and selected % 60 == 0,
                operation_configuration=_operation_configuration(profile, action))
            result["operational_limits"] = _operational_limits(profile, action)
            configured_delay_seconds(profile, action, selected)
            if device is None:
                raise UpsPowerBlocked("Falta identificar la unidad USB físicamente validada.")
            behavior = _restart_behavior(profile)
            result.update(available=True, validation="validated", profile_id=profile["profile_id"],
                          device_fingerprint=profile["device_fingerprint"],
                          delay_seconds=selected, reserve_seconds=reserve, manual_restart=behavior == "manual_only",
                          restart_behavior=behavior,
                          low_battery_cutoff_behavior=profile["low_battery_cutoff_behavior"], reserve_asof="battery_start",
                          validated_max_load_percent=profile["action_validation"][action].get("validated_max_load_percent"),
                          reserve_baseline=copy.deepcopy(profile["action_validation"][action].get("reserve_baseline")),
                          reserve_mode=profile["action_validation"][action].get("reserve_mode", "measured_reserve"),
                          sequence_validation=copy.deepcopy(profile["action_validation"][action].get("sequence_validation")),
                          pending_signal_unreliable=profile.get("pending_signal_unreliable", False))
        except UpsPowerBlocked as exc:
            result["reason"] = str(exc)
        return result

    def status(self):
        with self._lock:
            if self._journal_error:
                return {"state": "review_required", "reason": self._journal_error, "confirmation": "unconfirmed"}
            if not self._operation:
                return {"state": "idle", "confirmation": None}
            result = copy.deepcopy(self._operation)
            if self._orphaned or self._interrupted.is_set() and self._operation["state"] in _PENDING:
                result.update(state="review_required", previous_state=self._operation["state"],
                              reason="Hay una operación previa del UPS sin cierre confirmado; requiere revisión y no se repetirá.")
            return result

    def can_review(self, device, sample):
        """Acknowledge a finished timer, without certifying its physical result.

        Windows protection is independent; UPS control stays blocked until this
        cycle is reviewed. An earlier process or partial write needs admin review.
        """
        with self._lock:
            operation = self._operation
            if (self._journal_error or not operation or operation["state"] not in _PENDING
                    or operation.get("submission_complete") is not True or self._started_at is None
                    or type(operation.get("owner_pid")) is not int or operation["owner_pid"] != os.getpid()):
                return False
            try:
                elapsed = self._clock() - self._started_at
                profile, _, _ = self._profile(device, operation["action"])
                return (math.isfinite(elapsed) and elapsed >= operation["delay_seconds"] + 30
                        and profile["profile_id"] == operation["profile_id"]
                        and device_fingerprint(device) == operation["device_fingerprint"]
                        and _fresh_sample(sample) and sample.get("on_battery") is False
                        and sample.get("raw_status") in ("Line mode", "AVR mode") and _output_active(sample)
                        and sample.get("low_battery") is False and sample.get("fault") is False
                        and _qs_shutdown_bit(sample) is False)
            except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
                return False

    def complete_review(self, operation_id, acknowledge_review, device, sample):
        """Record user review only; never send C/S or claim physical success."""
        with self._lock:
            if acknowledge_review is not True:
                raise UpsPowerBlocked("Confirma la revisión de este ciclo del UPS para preparar otro.")
            try:
                uuid.UUID(operation_id)
            except (ValueError, TypeError, AttributeError) as exc:
                raise UpsPowerBlocked("La revisión requiere el identificador de la operación propia.") from exc
            if (not self._operation or self._operation["operation_id"] != operation_id
                    or not self.can_review(device=device, sample=sample)):
                raise UpsPowerBlocked("La revisión requiere la operación completa del proceso actual, plazo cumplido y salida activa con CA.")
            self._save(state="completed", confirmation="unconfirmed", physical_confirmation=False,
                       review_source="user", reviewed_at=_stamp())
            self._orphaned = False
            self._interrupted.clear()
            self._last_cancel_observation = None
            return self.status()

    def _save(self, **updates):
        if self._journal is None:
            raise UpsPowerBlocked("No hay un registro durable configurado para controlar el UPS.")
        value = {**self._operation, **updates, "updated_at": _stamp()}
        self._journal.write(value)
        self._operation = value

    def _transport(self, device, profile):
        if self._active_transport is None:
            if self._transport_factory is not None:
                self._active_transport = self._transport_factory(device["path"], profile["profile_id"])
            else:
                from .hid_windows import ControlTransport
                self._active_transport = ControlTransport(device["path"], profile["profile_id"], journal_path=self._journal.path,
                                                         operation_id=self._operation["operation_id"])
            if self._interrupted.is_set():
                signal = getattr(self._active_transport, "signal_interruption", None)
                if callable(signal):
                    signal()
        return self._active_transport

    def signal_interruption(self):
        """SCM callback path: no coordinator lock, disk, wait or USB."""
        self._interruption_count += 1
        self._interrupted.set()
        transport = self._active_transport
        signal = getattr(transport, "signal_interruption", None)
        if callable(signal):
            signal()

    def interrupted(self):
        """Invalidate continuity before waiting for an in-flight coordinator IO."""
        self.signal_interruption()
        with self._lock:
            if self._operation and self._operation["state"] in _PENDING:
                self._orphaned = True
                try:
                    self._save(state="uncertain", confirmation="unconfirmed", interrupted=True,
                               reason="La suspensión o pérdida de continuidad requiere revisión; no se repetirá control.")
                except OSError:
                    self._journal_error = "No se pudo guardar la interrupción del control del UPS; requiere revisión."
            return self.status()

    def arm(self, action, *, device, sample, reserve_seconds=None, sequence_evidence=None, on_submission=None, delay_seconds=None):
        with self._lock:
            interruption_count = self._interruption_count
            if on_submission is not None and not callable(on_submission):
                raise ValueError("La solicitud posterior al envío debe ser una función interna.")
            if self._journal is None:
                raise UpsPowerBlocked("No hay un registro durable configurado para controlar el UPS.")
            if self._journal_error or self._orphaned or self._operation and self._operation["state"] in _PENDING:
                raise UpsPowerBlocked("Ya hay una operación del UPS pendiente de revisión; no se repetirá.")
            profile, delay, measured_reserve = self._profile(device, action)
            delay = configured_delay_seconds(profile, action, delay_seconds)
            if not profile_matches_device(profile, device):
                raise UpsPowerBlocked("La identidad del UPS no coincide con el perfil validado.")
            limits = _operational_limits(profile, action)
            if limits is not None:
                admission = operational_sample_admission(sample, limits, expected_battery=True)
                if not admission["available"]:
                    raise UpsPowerBlocked(admission["reason"])
            if (not _fresh_sample(sample) or sample.get("on_battery") is not True
                    or limits is None and sample.get("low_battery") is not False or sample.get("fault") is not False
                    or sample.get("raw_status") != "Battery mode"):
                raise UpsPowerBlocked("El corte del UPS requiere batería confirmada, sin batería baja ni falla.")
            if measured_reserve is None:
                if reserve_seconds is not None:
                    raise UpsPowerBlocked("La secuencia observada no permite inventar una reserva de batería.")
                validate_observed_sequence_preflight(profile, action, sample, sequence_evidence, now_monotonic=self._clock())
            elif (type(reserve_seconds) not in (int, float) or not math.isfinite(reserve_seconds)
                    or reserve_seconds < delay or measured_reserve < delay):
                raise UpsPowerBlocked("No hay reserva de batería validada para completar Windows antes del corte.")
            load_limit = None if limits is not None else profile["action_validation"][action].get("validated_max_load_percent")
            load = sample.get("load_percent")
            if load_limit is not None and (type(load) not in (int, float) or not math.isfinite(load) or not 0 <= load <= load_limit):
                raise UpsPowerBlocked("La carga actual no está dentro de la reserva de batería físicamente validada.")
            self._interrupted.clear()
            started = self._clock()
            behavior = _restart_behavior(profile)
            operation = {"schema_version": 1, "owner": "elipro-ups-power", "operation_id": str(uuid.uuid4()),
                         "state": "arming", "confirmation": "unconfirmed", "created_at": _stamp(),
                         "profile_id": profile["profile_id"], "device_fingerprint": device_fingerprint(device),
                         "action": action, "delay_seconds": delay, "manual_restart": behavior == "manual_only",
                         "restart_behavior": behavior, "restart_behavior_verified": True,
                         "started_monotonic": started, "owner_pid": os.getpid(), "reserve_seconds": reserve_seconds,
                         "reserve_mode": profile["action_validation"][action].get("reserve_mode", "measured_reserve"),
                         "sequence_evidence": copy.deepcopy(sequence_evidence), "submission_complete": False}
            # Do not adopt the operation until the required pre-write record is durable.
            self._journal.write(operation)
            self._operation = operation
            self._started_at = started
            self._last_cancel_observation = None
            self._active_transport = None
            try:
                if self._interrupted.is_set() or interruption_count != self._interruption_count:
                    raise UpsPowerBlocked("El sistema cambió de estado antes de enviar la orden del UPS.")
                transport = self._transport(device, profile)
                try:
                    result = transport.submit_shutdown(delay)
                    if isinstance(result, dict) and result.get("state") == "submitted":
                        # This fact must survive S4 initiated inside the callback;
                        # do not flush disk between this memory update and Windows.
                        self._operation.update(submission_complete=True, submitted_at=_stamp())
                finally:
                    # A partial USB write may already have armed the hardware.
                    # No journal flush or status query may delay Windows rescue.
                    if on_submission is not None:
                        try:
                            on_submission()
                        except Exception:
                            self._operation["windows_callback_failed"] = True
                if not isinstance(result, dict) or result.get("state") != "submitted":
                    raise OSError("El transporte no confirmó el envío completo; el resultado es incierto.")
                if self._interrupted.is_set() or interruption_count != self._interruption_count:
                    raise UpsPowerBlocked("El sistema cambió de estado durante el control del UPS.")
                self._save(state="submitted", confirmation="unconfirmed", submitted_at=self._operation["submitted_at"], submission_complete=True)
            except Exception as exc:
                self._operation.update(state="uncertain", confirmation="unconfirmed", reason=str(exc)[:300],
                                       submission_finished_at=_stamp())
                try:
                    self._journal.write(self._operation)
                except OSError:
                    self._journal_error = "No se pudo guardar el resultado incierto del UPS; requiere revisión."
                return self.status()
            return self.status()

    def mark_windows_requested(self):
        with self._lock:
            if self._orphaned or self._interrupted.is_set() or not self._operation or self._operation["state"] not in ("submitted", "armed"):
                raise UpsPowerBlocked("No hay un envío propio del UPS que permita solicitar la acción de Windows.")
            self._save(windows_requested=True, windows_requested_at=_stamp())
            return self.status()

    def cancel(self, *, device, sample):
        with self._lock:
            if (self._journal_error or self._orphaned or self._interrupted.is_set()
                    or self._started_at is None or not self._operation or self._operation["state"] not in _PENDING
                    or self._operation.get("cancel_attempted") is True):
                raise UpsPowerBlocked("No hay una operación propia del UPS que se pueda cancelar.")
            profile, _, _ = self._profile(device, self._operation["action"])
            if (device_fingerprint(device) != self._operation["device_fingerprint"]
                    or profile["profile_id"] != self._operation["profile_id"] or not _cancel_evidence(profile, self._operation, sample)):
                raise UpsPowerBlocked("La cancelación requiere el mismo UPS y su apagado pendiente corroborado por QS.")
            elapsed = self._clock() - self._started_at
            if not 0 <= elapsed < self._operation["delay_seconds"] - 10:
                raise UpsPowerBlocked("No hay una ventana segura corroborada para cancelar sin reencender la salida.")
            self._save(state="cancelling", confirmation="unconfirmed", cancel_attempted=True,
                       cancel_started_monotonic=self._clock(), cancel_evidence=copy.deepcopy(sample))
            try:
                if self._interrupted.is_set():
                    raise UpsPowerBlocked("La operación fue interrumpida antes de la cancelación.")
                result = self._transport(device, profile).cancel_shutdown()
                if not isinstance(result, dict) or result.get("state") != "submitted":
                    raise OSError("El transporte no confirmó el envío completo de la cancelación.")
                if self._interrupted.is_set():
                    raise UpsPowerBlocked("La operación fue interrumpida durante la cancelación.")
                self._save(state="cancel_submitted", confirmation="unconfirmed", cancel_submitted_at=_stamp())
                self._last_cancel_observation = self._clock()
            except Exception as exc:
                self._operation.update(state="uncertain", reason=str(exc)[:300])
                try:
                    self._journal.write(self._operation)
                except OSError:
                    self._journal_error = "No se pudo guardar la cancelación incierta del UPS."
            return self.status()

    def observe(self, *, device, sample):
        with self._lock:
            if self._orphaned or self._interrupted.is_set() or self._journal_error or not self._operation:
                return self.status()
            profile, _, _ = self._profile(device, self._operation["action"])
            if (not profile_matches_device(profile, device)
                    or device_fingerprint(device) != self._operation["device_fingerprint"] or not _fresh_sample(sample)):
                return self.status()
            if (self._operation["state"] == "submitted" and _shutdown_active(sample)
                    and _observed_after(sample, self._operation.get("submitted_at"))):
                self._save(state="armed", confirmation="observed_in_qs")
            elif self._operation["state"] == "cancel_submitted" and profile.get("pending_signal_unreliable") is True:
                # A constant zero bit is not a transition. Only uninterrupted
                # powered-output observations beyond the original timer count.
                current = self._clock()
                if (self._last_cancel_observation is None or not 0 <= current - self._last_cancel_observation <= 15
                        or not _output_active(sample) or sample.get("low_battery") is not False or sample.get("fault") is not False):
                    self.interrupted()
                elif _observed_after(sample, self._operation.get("cancel_submitted_at")):
                    self._last_cancel_observation = current
                    if current - self._started_at >= self._operation["delay_seconds"] + 30:
                        self._save(state="cancelled", confirmation="output_active_after_deadline")
            elif (self._operation["state"] == "cancel_submitted" and _qs_shutdown_bit(sample) is False
                  and _observed_after(sample, self._operation.get("cancel_submitted_at"))
                  and sample.get("raw_status") in ("Battery mode", "Line mode", "AVR mode")):
                self._save(state="cancelled", confirmation="observed_in_qs")
            return self.status()
