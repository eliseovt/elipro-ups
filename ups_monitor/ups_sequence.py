"""Track live bounds and configured countdowns without inventing UPS autonomy."""
from __future__ import annotations

import copy
import math
import re
import time
from datetime import timezone

from . import ups_power
from .ups_power import _fresh_sample


def _number(value, *, positive=False):
    return type(value) in (int, float) and math.isfinite(value) and (value > 0 if positive else value >= 0)


def _configuration(capability):
    if (not isinstance(capability, dict) or capability.get("reserve_mode") != "observed_sequence"
            or capability.get("action") != "hibernate"):
        return None
    if "operational_limits" in capability and (not ups_power.operational_limits_valid(capability["operational_limits"])
            or _operating(capability) is None):
        return None
    envelope = capability.get("sequence_validation")
    fingerprint = capability.get("device_fingerprint")
    if (not isinstance(envelope, dict) or not isinstance(capability.get("profile_id"), str)
            or not capability["profile_id"] or not isinstance(fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)):
        return None
    if "operational_limits" in capability:
        # The trial envelope is retained as evidence, not used as a live gate.
        return envelope
    fields = ("max_seconds_since_mains", "max_load_percent", "min_mains_battery_voltage",
              "min_battery_voltage", "max_countdown_seconds")
    if any(not _number(envelope.get(field), positive=True) for field in fields):
        return None
    # This profile represents only the actual trial, or a stricter envelope.
    if (envelope["max_seconds_since_mains"] > 15 or envelope["max_load_percent"] > 11
            or envelope["min_mains_battery_voltage"] < 27.41 or envelope["min_battery_voltage"] < 25.41
            or envelope["max_countdown_seconds"] > 10):
        return None
    return envelope


def _safe(sample, envelope, limits=None):
    if limits is not None:
        return ups_power.operational_sample_admission(sample, limits)["available"]
    if (not _fresh_sample(sample) or type(sample.get("on_battery")) is not bool
            or sample.get("low_battery") is not False or sample.get("fault") is not False):
        return False
    output, load, voltage = sample.get("output_voltage"), sample.get("load_percent"), sample.get("battery_voltage")
    if (not _number(output, positive=True) or not _number(load) or load > envelope["max_load_percent"]
            or not _number(voltage, positive=True)):
        return False
    if sample["on_battery"]:
        return sample.get("raw_status") == "Battery mode" and voltage >= envelope["min_battery_voltage"]
    return sample.get("raw_status") in ("Line mode", "AVR mode") and voltage >= envelope["min_mains_battery_voltage"]


def _key(capability):
    return (capability.get("profile_id"), capability.get("device_fingerprint"), capability.get("action"),
            copy.deepcopy(capability.get("sequence_validation")), copy.deepcopy(capability.get("operation_configuration")),
            copy.deepcopy(capability.get("operational_limits")))


def _operating(capability):
    try:
        return ups_power._operation_configuration(capability, capability.get("action")) if isinstance(capability, dict) else None
    except ups_power.UpsPowerBlocked:
        return None


def _limits(capability):
    value = capability.get("operational_limits") if isinstance(capability, dict) else None
    return value if ups_power.operational_limits_valid(value) and _operating(capability) else None


def _informational_voltage(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def _captured_monotonic(sample, current):
    """Conservatively remove delivery/consumer delay from the observation time."""
    try:
        stamp = ups_power.datetime.fromisoformat(sample["timestamp"].replace("Z", "+00:00"))
        age = (ups_power.datetime.now(timezone.utc) - stamp).total_seconds()
        return current - age if stamp.tzinfo is not None and 0 <= age <= 15 and current >= age else None
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return None


class UpsSequenceTracker:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.invalidate()

    def invalidate(self):
        self._key = None
        self._last_at = self._last_sample = None
        self._last_mains_at = self._last_mains_sample = self._battery_started = None
        self._maximum_load = self._minimum_voltage = None
        self._reason = "Falta una lectura reciente de red previa al corte para la secuencia validada."
        self._reason_code = "mains_baseline"
        self._blocked_checks = []

    def observe(self, sample, capability):
        if isinstance(capability, dict) and "operational_limits" in capability:
            return self._observe_operational(sample, capability)
        current = self._clock()
        envelope = _configuration(capability)
        limits = _limits(capability)
        if not _number(current) or envelope is None or not _safe(sample, envelope, limits):
            self.invalidate()
            self._reason = "La lectura, carga o tensión quedó fuera de la secuencia observada; requiere una nueva lectura válida de red."
            if limits is not None or isinstance(capability, dict) and "operational_limits" in capability:
                admission = ups_power.operational_sample_admission(sample, capability["operational_limits"])
                self._reason = admission["reason"] or self._reason
                self._reason_code = admission["reason_code"] or "continuity"
                self._blocked_checks = admission["checks"]
                self._key = _key(capability)
                if admission["available"] and _operating(capability) is None:
                    self._reason = "La configuración de tiempos operativos del UPS no corresponde al perfil permitido."
                    self._reason_code = "operation_configuration"
            return
        captured = _captured_monotonic(sample, current)
        if captured is None:
            self.invalidate()
            self._reason = "La fecha de la lectura no permite acreditar una secuencia de energía reciente."
            self._reason_code = "sample_timestamp"
            return
        key = _key(capability)
        gap = self._last_at is not None and not 0 <= captured - self._last_at <= 15
        if self._key != key or gap:
            self.invalidate()
            self._key = key
            if gap:
                self._reason = "Se perdió la continuidad de las lecturas USB; se requiere una nueva lectura válida de red."
                self._reason_code = "continuity"
        self._last_at, self._last_sample = captured, copy.deepcopy(sample)
        if sample["on_battery"] is False:
            self._last_mains_at, self._last_mains_sample = captured, copy.deepcopy(sample)
            self._battery_started = None
            self._maximum_load = sample["load_percent"]
            self._minimum_voltage = _informational_voltage(sample.get("battery_voltage"))
            self._reason = None
            self._reason_code = None
            self._blocked_checks = []
            return
        operating = _operating(capability)
        horizon = operating["max_countdown_seconds"] + operating["poll_margin_seconds"] if operating else envelope["max_seconds_since_mains"]
        if self._last_mains_at is None or not 0 <= current - self._last_mains_at <= horizon:
            self._last_mains_at = self._last_mains_sample = self._battery_started = None
            if self._reason is None:
                self._reason = "La ventana operativa desde la última lectura de red venció; sólo se mantiene la protección de Windows."
                self._reason_code = "countdown_window"
            return
        if self._battery_started is None:
            self._battery_started = captured
        self._maximum_load = max(self._maximum_load, sample["load_percent"])
        voltages = [value for value in (self._minimum_voltage, _informational_voltage(sample.get("battery_voltage"))) if value is not None]
        self._minimum_voltage = min(voltages) if voltages else None
        self._reason = None
        self._reason_code = None

    def status(self, capability, sample=None, policy=None):
        if isinstance(capability, dict) and "operational_limits" in capability:
            return self._status_operational(capability, sample, policy)
        envelope = _configuration(capability)
        limits = _limits(capability)
        sample = self._last_sample if sample is None else sample
        current = self._clock()
        operating = _operating(capability)
        maximum_countdown = operating["max_countdown_seconds"] if operating else envelope["max_countdown_seconds"] if envelope else 10
        policy_valid = (isinstance(policy, dict) and type(policy.get("countdown_seconds")) is int
            and 10 <= policy["countdown_seconds"] <= maximum_countdown)
        horizon = (policy["countdown_seconds"] + operating["poll_margin_seconds"] if operating and policy_valid
            else envelope["max_seconds_since_mains"] if envelope else 15)
        evidence = None
        reason = self._reason
        reason_code = self._reason_code
        admission = ups_power.operational_sample_admission(sample, limits) if limits is not None else None
        checks = copy.deepcopy(self._blocked_checks) if self._last_mains_at is None and self._blocked_checks else admission["checks"] if admission else []
        if envelope is None:
            reason = "El perfil no contiene una secuencia física observada válida."
            reason_code = "sequence_profile"
            if isinstance(capability, dict) and "operational_limits" in capability and (not ups_power.operational_limits_valid(capability["operational_limits"])
                    or _operating(capability) is None):
                admission = ups_power.operational_sample_admission(sample, capability["operational_limits"])
                checks = admission["checks"]
                reason, reason_code = admission["reason"], admission["reason_code"]
                if reason is None:
                    reason = "La configuración de tiempos operativos del UPS no corresponde al perfil permitido."
                    reason_code = "operation_configuration"
                    checks.append({"key": reason_code, "label": "Configuración de tiempos operativos", "passed": False,
                        "value": ups_power._diagnostic_value(capability.get("operation_configuration")),
                        "expected": "temporizadores y margen del perfil permitido", "reason": reason})
        elif self._key != _key(capability) or self._last_mains_at is None or self._last_at is None:
            reason = reason or "Falta una lectura reciente de red vinculada a esta unidad."
            reason_code = reason_code or "mains_baseline"
        elif not _number(current) or not 0 <= current - self._last_at <= 15 or not _safe(sample, envelope, limits):
            reason = "Se perdió la continuidad o la lectura salió de los límites observados; requiere volver a detectar red."
            reason_code = "continuity"
            if admission is not None and not admission["available"]:
                reason, reason_code = admission["reason"], admission["reason_code"]
            self.invalidate()
            self._reason = reason
            self._reason_code = reason_code
            self._blocked_checks = copy.deepcopy(checks)
        elif sample["on_battery"] is False and not 0 <= current - self._last_mains_at <= 15:
            reason = "Falta actualizar la lectura de red vinculada a la secuencia observada."
            reason_code = "mains_baseline"
        elif sample["on_battery"] and (self._battery_started is None
                or not 0 <= current - self._last_mains_at <= horizon):
            reason = "Venció la ventana operativa de la cuenta atrás y su margen de lectura; no se programará el corte del UPS."
            reason_code = "countdown_window"
        elif not policy_valid:
            reason = f"La operación del UPS requiere un tiempo antes de actuar de 10 a {maximum_countdown} segundos; Windows conserva su protección."
            reason_code = "countdown_configuration"
        else:
            reason = None
            reason_code = None
            evidence = {"profile_id": capability["profile_id"], "device_fingerprint": capability["device_fingerprint"],
                "last_mains_monotonic": self._last_mains_at, "last_mains_sample": ups_power._diagnostic_value(self._last_mains_sample),
                "battery_started_monotonic": self._battery_started, "last_sample_monotonic": self._last_at,
                "max_load_percent": self._maximum_load, "min_battery_voltage": self._minimum_voltage,
                # Journal/preflight invariant retained for old operations.
                # The policy itself has only one immediate countdown.
                "on_battery_seconds": 0,
                "countdown_seconds": policy.get("countdown_seconds") if isinstance(policy, dict) else None}
        if limits is not None:
            checks.append({"key": "sequence_continuity", "label": "Continuidad y tiempos configurados",
                "passed": reason is None, "value": None if evidence is None else {
                    "countdown_seconds": policy["countdown_seconds"], "last_sample_age_seconds": current - self._last_at},
                "expected": "red previa y lecturas continuas durante la cuenta atrás",
                "reason": reason})
        return {"available": reason is None, "reason": reason, "reason_code": reason_code, "checks": checks, "evidence": evidence}

    def _observe_operational(self, sample, capability):
        """Follow current battery state without requiring an earlier AC sample."""
        current = self._clock()
        admission = ups_power.operational_sample_admission(sample, capability.get("operational_limits"))
        connection_failed = any(not item["passed"] and item["category"] == "connection" for item in admission["checks"])
        captured = _captured_monotonic(sample, current) if _number(current) and not connection_failed else None
        if _configuration(capability) is None or captured is None:
            self.invalidate()
            self._reason = admission["reason"] or "La configuración o lectura USB actual no permite controlar esta unidad."
            self._reason_code = admission["reason_code"] or "operation_configuration"
            self._blocked_checks = admission["checks"]
            return
        key = _key(capability)
        gap = self._last_at is not None and not 0 <= captured - self._last_at <= 15
        if self._key != key or gap:
            self.invalidate()
            self._key = key
        self._last_at, self._last_sample = captured, copy.deepcopy(sample)
        # Faults and other operations are checked now and again in the worker.
        # They do not rewrite the user's elapsed countdown after they clear.
        if sample["on_battery"] is False:
            self._battery_started = None
            self._maximum_load = self._minimum_voltage = None
        elif sample["on_battery"] is True and self._battery_started is None:
            self._battery_started = captured
        loads = [value for value in (self._maximum_load, _informational_voltage(sample.get("load_percent"))) if value is not None]
        voltages = [value for value in (self._minimum_voltage, _informational_voltage(sample.get("battery_voltage"))) if value is not None]
        self._maximum_load = max(loads) if loads else None
        self._minimum_voltage = min(voltages) if voltages else None
        self._reason, self._reason_code = admission["reason"], admission["reason_code"]
        self._blocked_checks = []

    def _status_operational(self, capability, sample, policy):
        sample = self._last_sample if sample is None else sample
        current = self._clock()
        admission = ups_power.operational_sample_admission(sample, capability.get("operational_limits"))
        checks = admission["checks"]
        operating = _operating(capability)
        configured = _configuration(capability) is not None
        policy_valid = (operating is not None and isinstance(policy, dict)
            and type(policy.get("countdown_seconds")) is int
            and 10 <= policy["countdown_seconds"] <= operating["max_countdown_seconds"])
        reason, code = admission["reason"], admission["reason_code"]
        if reason is None and not configured:
            reason, code = "La configuración del control no corresponde a esta unidad compatible.", "operation_configuration"
        elif reason is None and not policy_valid:
            reason, code = "Configura un tiempo antes de actuar entre 10 y 120 segundos.", "countdown_configuration"
        elif reason is None and (self._key != _key(capability) or self._last_at is None
                or not _number(current) or not 0 <= current - self._last_at <= 15
                or sample.get("on_battery") is True and self._battery_started is None):
            reason, code = "Se requiere una nueva lectura USB válida de esta unidad; la cuenta se recupera también en batería.", "sample_fresh"
        connection_failed = any(not item["passed"] and item["category"] == "connection" for item in checks)
        if connection_failed or code == "sample_fresh":
            self.invalidate()
        evidence = None
        if reason is None:
            evidence = {"kind": "battery_countdown", "version": 2,
                "profile_id": capability["profile_id"], "device_fingerprint": capability["device_fingerprint"],
                "battery_started_monotonic": self._battery_started, "last_sample_monotonic": self._last_at,
                "countdown_seconds": policy["countdown_seconds"], "on_battery_seconds": 0,
                "max_load_percent": self._maximum_load, "min_battery_voltage": self._minimum_voltage}
        if code is not None and not any(item["key"] == code and not item["passed"] for item in checks):
            checks.append({"key": code, "category": "connection", "label": "Control disponible",
                "passed": False, "value": None, "expected": "configuración y lectura USB válidas", "reason": reason})
        return {"available": reason is None, "reason": reason, "reason_code": code, "checks": checks, "evidence": evidence}
