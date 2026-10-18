"""Local diagnostics and consistent, downloadable SQLite backups.

Reports contain an explicit allowlist, never session tokens, environment values,
USB device paths, or command lines. Nothing is uploaded or restored here.
"""
from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import platform
import sqlite3
import tempfile
import threading
import zipfile
from datetime import datetime, timezone

from .core import DEFAULT_POLICY, iso_utc
from .alerts import DEFAULT_NOTIFICATION_SETTINGS
from .persistence import HistoryUnavailable

_backup_gate = threading.Lock()
_validation_gate = threading.Lock()


def validation_record(monitor):
    path = Path(getattr(monitor.store, "path", "data/monitor.sqlite3")).parent / "validation.json"
    try:
        if not path.is_file() or path.stat().st_size > 65536:
            return {"available": False, "message": "No hay un registro local de validación."}
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("Formato de validación inválido")
        return {"available": True, "record": value}
    except (OSError, ValueError, TypeError):
        return {"available": False, "message": "No se pudo leer el registro local de validación."}


def save_validation(monitor, result):
    """Save simulated results separately from physical user confirmations."""
    with _validation_gate:
        current = validation_record(monitor)
        record = current.get("record", {"schema_version": 1})
        record["automated_scenarios"] = copy.deepcopy(result)
        path = Path(monitor.store.path).parent / "validation.json"
        temporary = path.with_suffix(".json.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
            temporary.replace(path)
            return True
        except OSError:
            return False


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
        "notifications": copy.deepcopy(snapshot.get("notifications", {})),
        "current_sample": copy.deepcopy(snapshot.get("sample")),
        "recent_events": [{key: event.get(key) for key in
                           ("id", "timestamp", "kind", "message", "severity")}
                          for event in snapshot.get("events", [])[:100]],
        "validation": validation_record(monitor),
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
        # deliberately never included in a downloadable diagnostic.
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


def backup_archive(monitor):
    """Return a ZIP with one consistent database and its saved configuration.

    The facade serializes SQLite backup independently from the decision thread.
    Pending in-memory writes are disclosed rather than claimed to be backed up.
    No caller-supplied paths or restore operations are accepted.
    """
    if not _backup_gate.acquire(blocking=False):
        raise HistoryUnavailable("Ya se está preparando un respaldo. Inténtalo cuando termine.")
    try:
        state = monitor.history_status()
        data_path = Path(getattr(monitor.store, "path", Path(__file__).parents[1] / "data" / "monitor.sqlite3"))
        temporary_parent = data_path.parent if data_path.parent.is_dir() else None
        with tempfile.TemporaryDirectory(prefix="elipro-backup-", dir=temporary_parent) as directory:
            database_path = Path(directory) / "history.sqlite3"
            metadata = monitor.store.backup(database_path)
            saved = {}
            try:
                connection = sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
                try:
                    for key, payload in connection.execute(
                            "SELECT key,value FROM settings WHERE key IN ('policy','notification_settings')"):
                        saved[key] = json.loads(payload)
                finally:
                    connection.close()
            except (sqlite3.Error, ValueError, TypeError) as exc:
                raise HistoryUnavailable("No se pudo preparar el respaldo: la configuración guardada no se pudo leer.") from exc
            saved.setdefault("policy", dict(DEFAULT_POLICY))
            saved.setdefault("notification_settings", dict(DEFAULT_NOTIFICATION_SETTINGS))
            manifest = {"schema_version": 1, "application": "Elipro UPS", "created_at": iso_utc(),
                        "database": "history.sqlite3", "database_snapshot": metadata,
                        "pending_in_memory_at_request": state.get("pending", 0),
                        "dropped_during_session": state.get("dropped", 0),
                        "restored_automatically": False}
            content = io.BytesIO()
            with zipfile.ZipFile(content, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(database_path, "history.sqlite3")
                archive.writestr("configuration.json", json.dumps(saved, ensure_ascii=False, indent=2, allow_nan=False))
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False))
                archive.writestr("validation.json", json.dumps(validation_record(monitor), ensure_ascii=False, indent=2, allow_nan=False))
                observation = getattr(monitor, "observation", None)
                if observation is not None:
                    archive.writestr("stability.json", json.dumps(observation.report(), ensure_ascii=False, indent=2, allow_nan=False))
                archive.writestr("README.txt", "Respaldo local de Elipro UPS.\n"
                    "history.sqlite3 es una copia consistente creada con la API de respaldo de SQLite.\n"
                    "configuration.json contiene los ajustes de esa misma copia.\n"
                    "Los registros aún pendientes en memoria pueden no estar incluidos; consulta manifest.json.\n"
                    "Conserva el ZIP original. Esta versión no restaura ni activa políticas automáticamente.\n"
                    "No reemplaces la base de datos mientras Elipro esté ejecutándose.\n")
            return content.getvalue()
    finally:
        _backup_gate.release()


def csv_cell(value):
    """Keep exported event text inert when opened in a spreadsheet."""
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value
