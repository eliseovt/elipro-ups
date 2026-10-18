"""Internal diagnostics with an explicit public allowlist.

Reports contain an explicit allowlist, never session tokens, environment values,
USB device paths, or command lines. Nothing is uploaded or restored here.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import platform
from datetime import datetime, timezone

from .core import iso_utc


def diagnostics(monitor):
    snapshot = monitor.snapshot()
    connection = snapshot.get("connection", {})
    report = {
        "schema_version": 1,
        "generated_at": iso_utc(),
        "application": copy.deepcopy(snapshot.get("meta", {})),
        "environment": {"system": platform.system(), "release": platform.release(),
                        "python_version": platform.python_version()},
        "connection": {key: connection.get(key) for key in
                       ("state", "source", "last_success", "error_kind")},
        "health": copy.deepcopy(snapshot.get("health", {})),
        "history": copy.deepcopy(snapshot.get("history", {})),
        "policy": copy.deepcopy(snapshot.get("policy", {})),
        "protection": copy.deepcopy(snapshot.get("protection", {})),
        "ups_poweroff": copy.deepcopy(snapshot.get("ups_poweroff", {})),
        "ups_cancel_test": copy.deepcopy(snapshot.get("ups_cancel_test", {})),
        "ups_battery_test": copy.deepcopy(snapshot.get("ups_battery_test", {})),
        "notifications": copy.deepcopy(snapshot.get("notifications", {})),
        "current_sample": copy.deepcopy(snapshot.get("sample")),
        "recent_events": [{key: event.get(key) for key in
                           ("id", "timestamp", "kind", "message", "severity")}
                          for event in snapshot.get("events", [])[:100]],
        "process_supervision": guardian_status(monitor),
    }
    return _public_diagnostic(report)


def guardian_status(monitor):
    """Read guardian metadata only in diagnostics, outside the monitor lock."""
    path = Path(getattr(monitor.store, "path", "data/monitor.sqlite3")).parent / "guardian.json"
    try:
        if not path.is_file() or path.stat().st_size > 8192:
            return {"available": False}
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("child_pid") != os.getpid():
            return {"available": False}
        stamp = datetime.fromisoformat(value["heartbeat_at"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - stamp).total_seconds()
        # PID and session nonce are used for local stop control; the nonce is
        # deliberately never included in a public diagnostic.
        result = {key: value.get(key) for key in ("pid", "child_pid", "port", "state", "started_at",
                  "heartbeat_at", "restarts", "last_exit_code", "next_retry_seconds")}
        result.update(available=True, heartbeat_age_seconds=round(max(0, age), 1),
                      recent_heartbeat=-5 <= age <= 12)
        return result
    except (OSError, ValueError, TypeError, KeyError):
        return {"available": False}


def _public_diagnostic(value):
    if isinstance(value, dict):
        return {key: _public_diagnostic(item) for key, item in value.items()
                if not any(part in key.casefold() for part in
                           ("token", "password", "secret", "authorization", "command_line", "device_path", "usb_path"))
                and key.casefold() not in {"path", "username", "computer_name", "environment_variables"}}
    if isinstance(value, list):
        return [_public_diagnostic(item) for item in value]
    return copy.deepcopy(value)


def csv_cell(value):
    """Keep exported event text inert when opened in a spreadsheet."""
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value
