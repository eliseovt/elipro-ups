"""Shared presentation metadata, never a source of protection decisions."""
from __future__ import annotations

import math
import re


_CRITICAL = frozenset(("battery_low", "battery_low_percentage", "overload", "ups_fault", "monitor_error"))
_INFORMATIONAL = frozenset(("power_restored", "connection_restored", "monitor_restarted",
                           "policy_cancelled", "ups_poweroff_reviewed", "simulation", "notification_test"))


def notification_priority(kind, countdown_active=False):
    if kind == "policy_countdown" and countdown_active:
        return 3
    if kind in _CRITICAL:
        return 2
    return 0 if kind in _INFORMATIONAL else 1


def _number(value, minimum, maximum):
    return type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum


def normalize_notice_metadata(value):
    """Copy supported optional fields; legacy title/message maps stay legacy."""
    result = {"title": str(value["title"])[:160], "message": str(value["message"])[:2000]}
    if type(value.get("priority")) is int and 0 <= value["priority"] <= 3:
        result["priority"] = value["priority"]
    if value.get("severity") in ("info", "warning", "critical"):
        result["severity"] = value["severity"]
    for name in ("sound", "confirmed"):
        if type(value.get(name)) is bool:
            result[name] = value[name]
    if type(value.get("repeat_seconds")) is int and 10 <= value["repeat_seconds"] <= 300:
        result["repeat_seconds"] = value["repeat_seconds"]
    if _number(value.get("confirmed_for_ms"), 0, 3000):
        result["confirmed_for_ms"] = value["confirmed_for_ms"]
    countdown = value.get("countdown")
    if (isinstance(countdown, dict) and isinstance(countdown.get("countdown_id"), str)
            and re.fullmatch(r"[a-f0-9]{32}", countdown["countdown_id"])
            and countdown.get("action") in ("shutdown", "hibernate")
            and _number(countdown.get("remaining_seconds"), 0, 86400)
            and _number(countdown.get("valid_for_ms"), 0, 3000)
            and type(countdown.get("cancelable")) is bool):
        result["countdown"] = {name: countdown[name] for name in (
            "countdown_id", "action", "remaining_seconds", "valid_for_ms", "cancelable")}
    return result


def age_notice_metadata(value, elapsed_seconds):
    """Discount the original observation's lifetime at every transport hop."""
    result = normalize_notice_metadata(value)
    elapsed = max(0, elapsed_seconds) if _number(elapsed_seconds, -86400, 86400) else 86400
    if "confirmed_for_ms" in result:
        result["confirmed_for_ms"] = max(0, result["confirmed_for_ms"] - elapsed * 1000)
        if not result["confirmed_for_ms"]:
            result["confirmed"] = False
    countdown = result.get("countdown")
    if countdown is not None:
        countdown["remaining_seconds"] = max(0, countdown["remaining_seconds"] - elapsed)
        countdown["valid_for_ms"] = max(0, countdown["valid_for_ms"] - elapsed * 1000)
        if countdown["remaining_seconds"] <= 0 or countdown["valid_for_ms"] <= 0:
            countdown["cancelable"] = False
            result["confirmed"] = False
            if result.get("priority") == 3:
                result["priority"] = 1
    return result
