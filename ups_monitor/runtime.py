"""Paths and fixed child commands shared by source and packaged builds.

A frozen executable is the application itself, not a Python interpreter. Passing
``-m`` to it would recursively launch the application instead of the isolated
reader. Keep internal roles explicit so packaging cannot weaken the
existing process boundary around USB queries.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys


def is_frozen():
    return bool(getattr(sys, "frozen", False))


def resource_root():
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent.parent


def default_data_dir():
    """Source retains its local data folder; installed data is never bundled."""
    if not is_frozen():
        return resource_root() / "data"
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "Elipro UPS" / "data"


def service_data_dir():
    """Machine data shared by the service, independent of the signed-in user."""
    return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Elipro UPS" / "data"


def child_command(role, *arguments):
    modules = {
        "monitor": ("ups_monitor",),
        "guardian": ("ups_monitor.guardian",),
        "qs-worker": ("ups_monitor.hid_windows", "--qs-worker"),
        "battery-worker": ("ups_monitor.hid_windows", "--battery-worker"),
        "battery-diagnostic": ("ups_monitor.battery_diagnostics",),
        "ups-power-diagnostic": ("ups_monitor.ups_power_diagnostics",),
        "control-worker": ("ups_monitor.hid_windows", "--control-worker"),
        "cancel-test-worker": ("ups_monitor.hid_windows", "--cancel-test-worker"),
        "authorize-ups-cancel-test": ("ups_monitor.ups_test_authorization",),
        "battery-test-worker": ("ups_monitor.hid_windows", "--battery-test-worker"),
        "authorize-ups-battery-test": ("ups_monitor.ups_test_authorization", "--battery-test"),
        "service": ("ups_monitor.windows_service",),
        "agent": ("ups_monitor.session_agent",),
    }
    if role not in modules:
        raise ValueError("Rol de Elipro no admitido")
    arguments = [str(value) for value in arguments]
    if is_frozen():
        return [sys.executable, role, *arguments]
    return [sys.executable, "-m", *modules[role], *arguments]
