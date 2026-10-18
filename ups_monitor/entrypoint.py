"""Command dispatcher for the standalone Elipro executable.

``smoke-test`` validates the packaged resources without accessing USB, starting a
monitor, creating data, or issuing a Windows power command. CI uses this command
on the produced executable before constructing the installer.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from . import __version__
from .runtime import child_command, default_data_dir, is_frozen, resource_root


def smoke_report():
    root = resource_root()
    required = ("index.html", "app.js", "style.css")
    resources = {name: (root / "static" / name).is_file() for name in required}
    # Generating these vectors is safe: no subprocess is started.
    commands = {role: child_command(role)[1:] for role in ("monitor", "guardian", "qs-worker")}
    outside = not default_data_dir().is_relative_to(root)
    return {"application": "Elipro UPS", "version": __version__,
            "frozen": is_frozen(), "architecture": "x64" if sys.maxsize > 2**32 else "x86",
            "resources": resources, "commands": commands,
            "data_outside_bundle": outside,
            "ok": all(resources.values()) and sys.maxsize > 2**32 and (not is_frozen() or outside)}


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        arguments = ["agent", "--open-panel"] if is_frozen() else ["guardian"]
    command, remaining = arguments[0], arguments[1:]
    if command in ("--help", "-h"):
        parser = argparse.ArgumentParser(description="Elipro UPS para Windows x64")
        parser.add_argument("command", nargs="?", choices=("monitor", "guardian", "qs-worker", "smoke-test", "service", "agent"),
                            help="Instalado: abre panel/agente; service gestiona el servicio; smoke-test no accede al UPS")
        parser.print_help()
        return 0
    if command == "--version":
        print(__version__)
        return 0
    if command == "smoke-test":
        if remaining:
            raise SystemExit("smoke-test no admite argumentos")
        report = smoke_report()
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["ok"] else 1
    if command == "monitor":
        from .server import main as monitor_main
        monitor_main(remaining)
        return 0
    if command == "guardian":
        from .guardian import main as guardian_main
        guardian_main(remaining)
        return 0
    if command == "qs-worker":
        from .hid_windows import worker_main
        return worker_main(remaining)
    if command == "service":
        from .windows_service import main as service_main
        return service_main(remaining)
    if command == "agent":
        from .session_agent import main as agent_main
        return agent_main(remaining)
    raise SystemExit("Comando de Elipro no reconocido. Usa --help.")
