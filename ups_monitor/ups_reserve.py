"""Conservative battery reserve, independent of protection-policy timers.

QS battery percentages are estimates, not runtime. A measured reserve requires
an explicit recharge baseline and load envelope from a validated unit profile.
Short returns of mains preserve already spent battery time rather than crediting
the complete reserve again.
"""
from __future__ import annotations

import copy
import math
import re
import time

from .ups_power import _fresh_sample


def _number(value, *, positive=False):
    return (type(value) in (int, float) and math.isfinite(value)
            and (value > 0 if positive else value >= 0))


def _safe(sample):
    return (isinstance(sample, dict) and _fresh_sample(sample) and type(sample.get("on_battery")) is bool
            and sample.get("low_battery") is False and sample.get("fault") is False
            and ((sample["on_battery"] and sample.get("raw_status") == "Battery mode")
                 or not sample["on_battery"] and sample.get("raw_status") in ("Line mode", "AVR mode")))


def _configuration(capability):
    if not isinstance(capability, dict) or not _number(capability.get("reserve_seconds"), positive=True):
        return None
    fingerprint = capability.get("device_fingerprint")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        return None
    baseline = capability.get("reserve_baseline")
    limit = capability.get("validated_max_load_percent")
    if (not isinstance(baseline, dict) or set(baseline) != {"recharge_seconds", "min_battery_voltage"}
            or not _number(baseline.get("recharge_seconds"), positive=True)
            or not _number(baseline.get("min_battery_voltage"), positive=True)
            or not _number(limit, positive=True) or limit > 100):
        return None
    return baseline, limit


class UpsReserveTracker:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._last_at = None
        self._last_sample = None
        self._key = None
        self.invalidate()

    def invalidate(self):
        """Lose the measured baseline; a fresh runtime remains independent."""
        self._charging_since = None
        self._budget = None
        self._last_at = None
        self._last_sample = None

    def observe(self, sample, capability):
        current = self._clock()
        if not _number(current) or not _safe(sample):
            self.invalidate()
            return
        configuration = _configuration(capability)
        key = (capability.get("profile_id"), capability.get("device_fingerprint"), capability.get("action"), capability.get("reserve_seconds"),
               copy.deepcopy(capability.get("reserve_baseline")), capability.get("validated_max_load_percent")) if isinstance(capability, dict) else None
        if self._key != key:
            self.invalidate()
            self._key = key
        previous_at, previous_sample = self._last_at, self._last_sample
        if previous_at is not None and not 0 <= current - previous_at <= 15:
            self.invalidate()
            previous_at = previous_sample = None
        self._last_at, self._last_sample = current, copy.deepcopy(sample)
        if configuration is None:
            self._budget = self._charging_since = None
            return
        baseline, limit = configuration
        load = sample.get("load_percent")
        if not _number(load) or load > limit:
            self._budget = self._charging_since = None
            return
        if self._budget is not None and previous_at is not None and (
                sample["on_battery"] or previous_sample["on_battery"]):
            # Include the whole transition interval: mains may have failed just
            # after the preceding online sample, not at this polling instant.
            self._budget = max(0.0, self._budget - (current - previous_at))
        if sample["on_battery"]:
            self._charging_since = None
            return
        voltage = sample.get("battery_voltage")
        if not _number(voltage, positive=True) or voltage < baseline["min_battery_voltage"]:
            self._budget = self._charging_since = None
            return
        if self._charging_since is None:
            self._charging_since = current
        if current - self._charging_since >= baseline["recharge_seconds"]:
            self._budget = float(capability["reserve_seconds"])

    def remaining(self, capability, sample=None):
        sample = self._last_sample if sample is None else sample
        if (not isinstance(capability, dict) or not _number(capability.get("reserve_seconds"), positive=True)
                or not _safe(sample)):
            self.invalidate()
            return None
        measured = float(capability["reserve_seconds"])
        limit = capability.get("validated_max_load_percent")
        load = sample.get("load_percent")
        if limit is not None and (not _number(limit, positive=True) or limit > 100 or not _number(load) or load > limit):
            self._budget = self._charging_since = None
            return None
        runtime = sample.get("runtime_seconds")
        if sample["on_battery"] is True and _number(runtime):
            # This field must come from the hardware reader, never percent or
            # estimated autonomy. Current QS readers deliberately return None.
            return min(float(runtime), measured)
        configuration = _configuration(capability)
        current = self._clock()
        key = (capability.get("profile_id"), capability.get("device_fingerprint"), capability.get("action"), capability.get("reserve_seconds"),
               capability.get("reserve_baseline"), capability.get("validated_max_load_percent"))
        if (configuration is None or key != self._key or self._budget is None or self._last_at is None
                or not _number(current) or not 0 <= current - self._last_at <= 15):
            return None
        if sample["on_battery"] is False:
            voltage = sample.get("battery_voltage")
            if not _number(voltage, positive=True) or voltage < configuration[0]["min_battery_voltage"]:
                self._budget = self._charging_since = None
                return None
        elapsed = current - self._last_at if sample["on_battery"] or self._last_sample["on_battery"] else 0
        return max(0.0, min(measured, self._budget - elapsed))
