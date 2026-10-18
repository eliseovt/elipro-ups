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
    envelope = capability.get("sequence_validation")
    fingerprint = capability.get("device_fingerprint")
    if (not isinstance(envelope, dict) or not isinstance(capability.get("profile_id"), str)
            or not capability["profile_id"] or not isinstance(fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)):
        return None
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


def _safe(sample, envelope):
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
            copy.deepcopy(capability.get("sequence_validation")), copy.deepcopy(capability.get("operation_configuration")))


def _operating(capability):
    value = capability.get("operation_configuration") if isinstance(capability, dict) else None
    expected = {"source": "user_configurable", "min_delay_seconds": 60, "max_delay_seconds": 540,
        "max_countdown_seconds": 120, "poll_margin_seconds": 10, "requires_on_battery_seconds": 0}
    return value if isinstance(value, dict) and value == expected and all(
        type(value.get(key)) is int for key in expected if key != "source") else None


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

    def observe(self, sample, capability):
        current = self._clock()
        envelope = _configuration(capability)
        if not _number(current) or envelope is None or not _safe(sample, envelope):
            self.invalidate()
            self._reason = "La lectura, carga o tensión quedó fuera de la secuencia observada; requiere una nueva lectura válida de red."
            return
        captured = _captured_monotonic(sample, current)
        if captured is None:
            self.invalidate()
            self._reason = "La fecha de la lectura no permite acreditar una secuencia de energía reciente."
            return
        key = _key(capability)
        if self._key != key or self._last_at is not None and not 0 <= captured - self._last_at <= 15:
            self.invalidate()
            self._key = key
        self._last_at, self._last_sample = captured, copy.deepcopy(sample)
        if sample["on_battery"] is False:
            self._last_mains_at, self._last_mains_sample = captured, copy.deepcopy(sample)
            self._battery_started = None
            self._maximum_load, self._minimum_voltage = sample["load_percent"], sample["battery_voltage"]
            self._reason = None
            return
        operating = _operating(capability)
        horizon = operating["max_countdown_seconds"] + operating["poll_margin_seconds"] if operating else envelope["max_seconds_since_mains"]
        if self._last_mains_at is None or not 0 <= current - self._last_mains_at <= horizon:
            self._last_mains_at = self._last_mains_sample = self._battery_started = None
            self._reason = "La ventana observada desde la última lectura de red venció; sólo se mantiene la protección de Windows."
            return
        if self._battery_started is None:
            self._battery_started = captured
        self._maximum_load = max(self._maximum_load, sample["load_percent"])
        self._minimum_voltage = min(self._minimum_voltage, sample["battery_voltage"])
        self._reason = None

    def status(self, capability, sample=None, policy=None):
        envelope = _configuration(capability)
        sample = self._last_sample if sample is None else sample
        current = self._clock()
        operating = _operating(capability)
        maximum_countdown = operating["max_countdown_seconds"] if operating else envelope["max_countdown_seconds"] if envelope else 10
        policy_valid = (isinstance(policy, dict) and type(policy.get("on_battery_seconds")) is int
            and policy["on_battery_seconds"] == 0 and type(policy.get("countdown_seconds")) is int
            and 10 <= policy["countdown_seconds"] <= maximum_countdown)
        horizon = (policy["countdown_seconds"] + operating["poll_margin_seconds"] if operating and policy_valid
            else envelope["max_seconds_since_mains"] if envelope else 15)
        evidence = None
        reason = self._reason
        if envelope is None:
            reason = "El perfil no contiene una secuencia física observada válida."
        elif self._key != _key(capability) or self._last_mains_at is None or self._last_at is None:
            reason = reason or "Falta una lectura reciente de red vinculada a esta unidad."
        elif not _number(current) or not 0 <= current - self._last_at <= 15 or not _safe(sample, envelope):
            reason = "Se perdió la continuidad o la lectura salió de los límites observados; requiere volver a detectar red."
            self.invalidate()
            self._reason = reason
        elif sample["on_battery"] is False and not 0 <= current - self._last_mains_at <= 15:
            reason = "Falta actualizar la lectura de red vinculada a la secuencia observada."
        elif sample["on_battery"] and (self._battery_started is None
                or not 0 <= current - self._last_mains_at <= horizon):
            reason = "Venció la ventana operativa de la cuenta atrás y su margen de lectura; no se programará el corte del UPS."
        elif not policy_valid:
            reason = f"La operación del UPS requiere espera con batería de 0 segundos y cuenta atrás de 10 a {maximum_countdown} segundos; Windows conserva su protección."
        else:
            reason = None
            evidence = {"profile_id": capability["profile_id"], "device_fingerprint": capability["device_fingerprint"],
                "last_mains_monotonic": self._last_mains_at, "last_mains_sample": copy.deepcopy(self._last_mains_sample),
                "battery_started_monotonic": self._battery_started, "last_sample_monotonic": self._last_at,
                "max_load_percent": self._maximum_load, "min_battery_voltage": self._minimum_voltage,
                "on_battery_seconds": policy.get("on_battery_seconds") if isinstance(policy, dict) else None,
                "countdown_seconds": policy.get("countdown_seconds") if isinstance(policy, dict) else None}
        return {"available": reason is None, "reason": reason, "evidence": evidence}
