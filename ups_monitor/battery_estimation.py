"""Original, voltage-derived display estimate for the validated NT-2402PRO.

The observed 24 V QS/T profile follows the PMV voltage/load curve, with the
vendor's one-decimal voltage and float32 normalization boundaries. The ten-point
integer filter reports its previous window before inserting the current curve
value. It is seeded from the first valid observation, never from an assumed full
battery. A minimum three-second update interval approximates the observed OEM
cadence at the native reader's two-second poll rate; exact display times differ.

This is not a calibrated state of charge measurement and never populates
battery_percent or low_battery. The separately labelled low-percentage policy
may use this estimate to expedite an already enabled Windows protection.
No USB I/O, runtime calculation, battery control, or proprietary runtime is used here.
"""
from __future__ import annotations

from collections import deque
from decimal import Decimal, ROUND_HALF_UP
import math
import struct
import time


BATTERY_ESTIMATE_METHOD = "nt2402pro-pmv-qs-v1"


def _float32(value):
    return struct.unpack("!f", struct.pack("!f", value))[0]


class Nt2402ProBatteryEstimator:
    """Per-reader estimate state; update() returns only optional display fields."""

    def __init__(self, *, clock=None):
        self._clock = clock or time.monotonic
        self.reset()

    def reset(self):
        self._window = deque(maxlen=10)
        self._last_observed_at = self._updated_at = None
        self._last_mode = None
        self._charge_capacity = 90
        self._charge_since = None
        self._result = None

    @staticmethod
    def _valid_number(value, minimum, maximum):
        return type(value) in (int, float) and minimum <= value <= maximum and math.isfinite(value)

    def _base_percent(self, voltage, load, on_battery, now):
        if on_battery:
            self._charge_capacity, self._charge_since = 90, None
            span = 3.0 if load < 20 else 2.5
            value = int(100 * (voltage - 10.2) / span)
        elif voltage < 11.6:
            value = 0
        elif voltage >= 13.5:
            value = 100
        elif voltage < 13.3:
            value = int(90 * (voltage - 11.6) / 1.7)
        else:
            # Outside this narrow charging band the vendor curve leaves its
            # charge state untouched. Each qualifying interval adds one point.
            if self._charge_capacity < 100:
                if self._charge_since is None:
                    self._charge_since = now
                elif now - self._charge_since >= 720:
                    self._charge_capacity += 1
                    self._charge_since = now
            value = self._charge_capacity
        return min(100, max(0, value))

    def _fields(self):
        return {"battery_percent_estimate": self._result,
                "battery_estimate_method": BATTERY_ESTIMATE_METHOD if self._result is not None else None}

    def update(self, sample):
        voltage, load = sample.get("battery_voltage"), sample.get("load_percent")
        mode = sample.get("raw_status")
        on_battery = sample.get("on_battery")
        valid = (self._valid_number(voltage, 15, 30) and self._valid_number(load, 0, 200)
                 and type(on_battery) is bool and sample.get("fault") is not True
                 and ((on_battery and mode == "Battery mode")
                      or (not on_battery and mode in ("Line mode", "AVR mode"))))
        if not valid:
            self.reset()
            return self._fields()
        now = self._clock()
        if (self._last_observed_at is not None
                and (now - self._last_observed_at > 15 or now < self._last_observed_at)):
            self.reset()
        self._last_observed_at = now
        if (self._updated_at is not None and on_battery is self._last_mode
                and now - self._updated_at < 3):
            return self._fields()
        # The observed curve formats a Float, so its float32 boundary precedes
        # decimal formatting. Never first round the raw measurement to two
        # decimals: that can cross the one-decimal curve's midpoint.
        rounded = float(Decimal(str(_float32(voltage))).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
        normalized = _float32(_float32(rounded) / 2)
        base = self._base_percent(normalized, load, on_battery, now)
        if not self._window:
            self._window.extend([base] * 10)
        self._result = sum(self._window) // len(self._window)
        self._window.append(base)
        self._updated_at, self._last_mode = now, on_battery
        return self._fields()
