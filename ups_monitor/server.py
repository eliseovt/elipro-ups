"""Loopback dashboard and local protection controller, using only the standard library."""
from __future__ import annotations

import argparse
import copy
import csv
import io
import json
import logging
import math
from logging.handlers import RotatingFileHandler
import os
import secrets
import subprocess
import sys
import threading
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .adapters import TelemetryError
from .native_adapter import make_adapter
from .core import (BATTERY_ESTIMATE_FIELDS, DEFAULT_POLICY, SAMPLE_FIELDS, IncidentCursorExpired, RecordCursorExpired, PolicyEngine,
                   TelemetryRingBuffer, battery_estimate_fields, battery_percentage, iso_utc, load_saved_policy, validate_policy)
from .alerts import AlertManager, DEFAULT_NOTIFICATION_SETTINGS
from .webhooks import WebhookManager
from .instance_guard import MonitorInstanceGuard, InstanceGuardError
from .health import MonitorHealth
from .persistence import ResilientStore, HistoryUnavailable
from .windows_notifications import WindowsNotifier
from .windows_power_readiness import windows_power_readiness
from .reports import csv_cell
from .guardian import request_parent_stop
from .runtime import default_data_dir, resource_root
from .ups_power import UpsPowerCoordinator, UpsPowerBlocked, _diagnostic_value
from .ups_cancel_test import UpsCancellationTest
from .ups_battery_test import UpsBatteryTest
from .ups_reserve import UpsReserveTracker
from .ups_sequence import UpsSequenceTracker

ROOT = resource_root()
LOGGER = logging.getLogger("elipro.monitor")


class _PowerTransition(Exception):
    """A read crossed a known Windows sleep boundary and must be discarded."""


class _ReadDeferred(Exception):
    """An explicit control transaction temporarily owns the USB reader."""


class WindowsActionUnconfirmed(RuntimeError):
    """The request may have acted; lack of process completion is not rejection."""


class CountdownConflictError(ValueError):
    """A guarded cancellation no longer names a current cancelable episode."""


class CountdownBusyError(RuntimeError):
    """The protection lock could not be acquired within the UI request budget."""


def log_failure(message, exc):
    # Keep traceback locations, but never log request bodies, tokens or exception payloads.
    LOGGER.error("%s (%s)\n%s", message, type(exc).__name__, "".join(traceback.format_tb(exc.__traceback__)))


def windows_action(action):
    if os.name != "nt":
        raise RuntimeError("La protección solo puede actuar en Windows.")
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "shutdown.exe"
    # Countdown is handled by us. /t > 0 would imply forced app termination.
    args = [str(executable), "/s", "/t", "0"] if action == "shutdown" else [str(executable), "/h"]
    try:
        result = subprocess.run(args, capture_output=True, timeout=10,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise WindowsActionUnconfirmed(
            "No se confirmó el resultado de la solicitud a Windows dentro del plazo; podría haber suspendido o hibernado el equipo.") from None
    if result.returncode:
        raise RuntimeError("Windows rechazó la acción. Revisa permisos o disponibilidad de hibernación.")


def archive_options(query, *, incidents=False, pagination=True):
    values = parse_qs(query, keep_blank_values=True)
    allowed = {"from", "to"} | ({"sort", "direction"} if incidents else {"kind", "q"})
    if pagination:
        allowed |= {"limit", "cursor"}
    if set(values) - allowed or any(len(value) != 1 for value in values.values()):
        raise ValueError("Parámetros de historial no válidos.")
    result = {"start": values.get("from", [""])[0] or None,
              "end": values.get("to", [""])[0] or None}
    if not incidents:
        result.update(kind=values.get("kind", [""])[0] or None,
                      query=values.get("q", [""])[0] or None)
    else:
        for name in ("sort", "direction"):
            if name in values:
                result[name] = values[name][0]
    if pagination:
        result.update(limit=int(values.get("limit", ["50"])[0]),
                      cursor=values.get("cursor", [""])[0] or None)
    return result


def write_events_csv(store, options, target):
    writer = csv.DictWriter(target, fieldnames=("id", "timestamp", "kind", "message", "data"))
    writer.writeheader()
    for event in store.iter_events(**options):
        row = {key: event.get(key) for key in ("id", "timestamp", "kind", "message")}
        row["data"] = json.dumps(event.get("data", {}), ensure_ascii=False, allow_nan=False)
        writer.writerow({key: csv_cell(value) for key, value in row.items()})


def write_records_csv(store, options, target):
    fields = ("fecha", "tipo", "descripcion", "estado", "duracion_segundos", "recuperacion", "proteccion", "detalles")
    writer = csv.DictWriter(target, fieldnames=fields)
    writer.writeheader()
    categories = {"power": "Energía", "connection": "Conexión", "protection": "Protección",
                  "alerts": "Alertas", "settings": "Configuración", "other": "Otros"}
    states = {"open": "Sin recuperación registrada", "recovered": "Recuperado"}
    actions = {"none": "Sin acciones", "failed": "Fallida", "started": "Iniciada",
               "cancelled": "Cancelada", "simulated": "Simulada"}
    for record in store.iter_records(**options):
        row = {"fecha": record["timestamp"], "tipo": categories[record["category"]],
               "descripcion": record["description"], "estado": states.get(record["status"], ""),
               "duracion_segundos": record["duration_seconds"], "recuperacion": record.get("ended_at"),
               "proteccion": actions.get(record["action_result"], ""),
               "detalles": json.dumps(record["events"], ensure_ascii=False, allow_nan=False)}
        writer.writerow({key: csv_cell(value) for key, value in row.items()})


class Monitor:
    def __init__(self, store, adapter, *, dry_run=False, action_callback=None, alerts=None, health_clock=None,
                 telemetry_capacity=256, poll_interval=2, allow_ups_poweroff=False, ups_power=None, ups_journal_path=None,
                 ups_cancel_test=None, ups_cancel_test_journal=None, ups_battery_test=None, ups_battery_test_journal=None,
                 webhooks=None, windows_readiness_probe=None):
        self.store, self.adapter = store, adapter
        self._recent_records_snapshot_cache = None
        self.simulation_source = getattr(adapter, "simulation", False) is True
        self.dry_run = bool(dry_run) or self.simulation_source
        if type(allow_ups_poweroff) is not bool:
            raise ValueError("allow_ups_poweroff debe ser booleano")
        self.allow_ups_poweroff = allow_ups_poweroff and not self.dry_run
        self.ups_power = ups_power if ups_power is not None else UpsPowerCoordinator(ups_journal_path)
        self.ups_cancel_test = ups_cancel_test if ups_cancel_test is not None else UpsCancellationTest(ups_cancel_test_journal)
        self.ups_battery_test = ups_battery_test if ups_battery_test is not None else UpsBatteryTest(ups_battery_test_journal)
        self._cancel_test_io = threading.Event()
        self._windows_request = None
        # Injected executors provide their own readiness contract in simulations/tests.
        # The installed service always queries Windows using its actual service token.
        self._windows_readiness_probe = windows_readiness_probe or (
            (lambda action: {"available": True, "reason": None}) if action_callback is not None
            else windows_power_readiness)
        self._windows_readiness_cache = None
        self._ups_error = None
        self.alerts = alerts
        self.lock = threading.RLock()
        self.settings_lock = threading.Lock()
        self._history_failures = {}
        self._last_ups_block = None
        try:
            previous_block = next((event for event in store.events(1000)
                                   if event.get("kind") == "ups_poweroff_blocked"), None)
            if previous_block is not None:
                self._remember_ups_block(previous_block)
        except Exception:
            # History availability cannot prevent the monitor from starting.
            pass
        self.stop_event = threading.Event()
        self.thread = None
        self.processor_thread = None
        self.supervisor_thread = None
        if type(poll_interval) not in (int, float) or not math.isfinite(poll_interval) or not 0.01 <= poll_interval <= 10:
            raise ValueError("poll_interval debe estar entre 0.01 y 10 segundos")
        self.poll_interval = poll_interval
        self.telemetry = TelemetryRingBuffer(telemetry_capacity)
        self._read_lock = threading.Lock()
        self._processing_sequence = None
        self._last_processed_at = None
        self._poll_failures = 0
        self._next_poll_at = None
        self.health = MonitorHealth(clock=health_clock)
        self.ups_reserve = UpsReserveTracker(clock=self.health.clock)
        self.ups_sequence = UpsSequenceTracker(clock=self.health.clock)
        self._health_fault = threading.Event()
        self._power_lock = threading.RLock()
        self._power_gate = threading.Event()
        self._poll_wake = threading.Event()
        self._power_state = "running"
        self._power_generation = self._power_applied_generation = 0
        self._power_resume_deadline = None
        self._resume_silent = False
        self._pending_health_failure = None
        self._last_health_notice = None
        self._reader_dead_since = None
        self._restart_failures = 0
        self.sample = None
        self.last_success = None
        self.connection = "disconnected"
        self.error = None
        self.error_kind = None
        self.last_power = None
        self.last_saved = 0
        self.last_saved_timestamp = None
        self.low = self.overload = False
        self._estimated_percentage_low = False
        self.test_deadline = None
        self.test_finished = False
        self.test_ups_delay = None
        self.test_ups_simulation = None
        self.csrf_token = secrets.token_urlsafe(32)
        self.action_callback = action_callback or windows_action
        try:
            policy = load_saved_policy(store)
        except Exception as exc:
            self._history_failed("settings", exc)
            policy = dict(DEFAULT_POLICY)
        self.engine = PolicyEngine(policy,
                                   self._act, event_callback=self._policy_event)
        self.webhooks = webhooks if webhooks is not None else WebhookManager(store, dry_run=self.dry_run)
        try:
            review = store.get_setting("policy_migration_review")
        except Exception as exc:
            # Advisory metadata must not prevent live monitoring with the
            # disabled fallback already selected after a settings failure.
            self._history_failed("settings", exc)
            review = None
        self._policy_review_required = isinstance(review, dict) and review.get("requires_review") is True
        if self._policy_review_required:
            self._event("policy_migration_review",
                "La protección automática quedó desactivada porque la espera anterior sumaba "
                + str(review.get("previous_total_seconds")) + " segundos. Elige un tiempo antes de actuar "
                "de 10 a 120 segundos y guarda la configuración para volver a activarla.")

    def _history_failed(self, operation, exc):
        if operation not in self._history_failures:
            log_failure("Historial no disponible: " + operation, exc)
        self._history_failures[operation] = type(exc).__name__

    def history_status(self):
        status = getattr(self.store, "status", None)
        if callable(status):
            return status()
        available = not self._history_failures
        return {"available": available, "state": "healthy" if available else "degraded",
                "message": None if available else "No se puede guardar o consultar todo el historial. El monitor continúa.",
                "pending": 0, "dropped": 0}

    def _event(self, kind, message, data=None):
        # Desktop delivery must never depend on SQLite accepting an event.
        self._notify_event(kind, message, data)
        self._notify_webhook(kind, message)
        return self._record_event(kind, message, data)

    def _notify_webhook(self, kind, message):
        if not self.stop_event.is_set():
            try:
                # Only an in-memory snapshot is queued here. HTTP belongs to
                # the separate worker and never owns the protection lock.
                self.webhooks.on_event(kind, message, self.sample)
            except Exception as exc:
                log_failure("No se pudo preparar el webhook", exc)

    def _notify_event(self, kind, message, data=None):
        if self.alerts is not None and not self.stop_event.is_set():
            try:
                self.alerts.on_event(kind, message, data)
            except Exception as exc:
                log_failure("No se pudo preparar un aviso local", exc)

    def _record_event(self, kind, message, data=None, *, timestamp=None):
        if kind == "ups_poweroff_blocked":
            timestamp = timestamp or iso_utc()
            self._remember_ups_block({"timestamp": timestamp, "message": message, "data": data or {}})
        try:
            event = (self.store.add_event(kind, message, data, timestamp=timestamp) if timestamp is not None else
                     self.store.add_event(kind, message, data))
            self._history_failures.pop("events_write", None)
            return event
        except Exception as exc:
            self._history_failed("events_write", exc)
            return None

    def _remember_ups_block(self, event):
        details = event.get("data") if isinstance(event.get("data"), dict) else {}
        self._last_ups_block = {**copy.deepcopy(details), "timestamp": event.get("timestamp"),
                               "reason": event.get("message")}

    def _record_ups_block(self, capability, *, reason=None, reason_code=None):
        sample = ({key: self.sample.get(key) for key in (
            "timestamp", "on_battery", "battery_voltage", "load_percent", "output_voltage",
            "low_battery", "fault", "raw_status")} if isinstance(self.sample, dict) else None)
        message = reason or capability.get("reason") or "El UPS no permite programar el corte en este momento."
        self._event("ups_poweroff_blocked", message, {
            "reason_code": reason_code or capability.get("reason_code") or "unavailable",
            "checks": copy.deepcopy(capability.get("checks", [])),
            "blockers": copy.deepcopy(capability.get("blockers", [])), "sample": _diagnostic_value(sample),
            "configured_countdown_seconds": self.engine.policy["countdown_seconds"],
            "configured_delay_seconds": self.engine.policy["ups_poweroff_delay_seconds"],
            "operation_state": capability.get("operation", {}).get("state")})

    def _update_alerts(self):
        if self.alerts is not None and not self.stop_event.is_set():
            try:
                protection = {**self.engine.status(), "simulation": self.power_actions_blocked}
                self.alerts.update(protection, {"state": self.connection}, self.sample)
            except Exception as exc:
                log_failure("No se pudieron actualizar los avisos locales", exc)

    def notification_status(self):
        if self.alerts is None:
            return {"settings": dict(DEFAULT_NOTIFICATION_SETTINGS),
                    "delivery": {"available": False, "ready": False, "channel": "disabled",
                                 "last_error": "Los avisos locales no están inicializados."},
                    "last_notification": None, "last_error": None, "pending": []}
        return self.alerts.status()

    def set_notifications(self, settings):
        if self.alerts is None or self.stop_event.is_set():
            raise ValueError("Los avisos locales no están disponibles.")
        self.alerts.update_settings(settings)
        return self.snapshot(refresh_recent_records=False)

    def test_notifications(self):
        if self.alerts is None or self.stop_event.is_set():
            raise ValueError("Los avisos locales no están disponibles.")
        self.alerts.test()
        return self.snapshot(refresh_recent_records=False)

    def acknowledge_notifications(self, data):
        if not isinstance(data, dict) or set(data) != {"ids"}:
            raise ValueError("Indica los avisos que has leído.")
        if self.alerts is None or self.stop_event.is_set():
            raise ValueError("Los avisos locales no están disponibles.")
        # The acknowledgement commits before returning the updated dashboard.
        # It neither cancels protection nor changes any power setting.
        self.alerts.acknowledge(data["ids"])
        return self.snapshot(refresh_recent_records=False)

    def webhook_status(self):
        return self.webhooks.status()

    def set_webhooks(self, settings):
        # Configuration may touch SQLite; never hold the telemetry lock here.
        with self.settings_lock:
            if self.stop_event.is_set():
                raise ValueError("Los webhooks no están disponibles: el monitor está detenido.")
            self.webhooks.update_settings(settings)
        return self.snapshot(refresh_recent_records=False)

    def test_webhooks(self):
        if self.stop_event.is_set():
            raise ValueError("Los webhooks no están disponibles: el monitor está detenido.")
        with self.lock:
            sample = dict(self.sample) if self.sample is not None else None
        self.webhooks.test(sample)
        return self.snapshot(refresh_recent_records=False)

    def _policy_event(self, kind, message, data):
        if kind == "policy_unknown" and self.system_power_status()["state"] != "running":
            return
        if self.power_actions_blocked and kind == "policy_action_started":
            return
        # Routine online/disabled states add no noise to the incident history.
        if kind not in ("policy_online", "policy_disabled", "policy_on_battery"):
            if kind == "policy_countdown":
                data = {**(data or {}), **self.engine.status(), "simulation": self.power_actions_blocked}
            self._event(kind, message, data)

    def handle_power_event(self, event):
        """SCM callback: only bounded memory updates, never USB, disk or decision locks."""
        if self.stop_event.is_set() or event not in ("suspend", "resume"):
            return
        if event == "suspend":
            self.ups_power.signal_interruption()
            self.ups_battery_test.signal_interruption()
            self.ups_cancel_test.signal_interruption()
        with self._power_lock:
            if event == "suspend":
                if self._power_state == "suspended":
                    return
                if self._power_state == "running":
                    self._resume_silent = self.connection == "connected" and not self._health_fault.is_set()
                self._power_state = "suspended"
                self._power_resume_deadline = None
            else:
                # Windows also sends a user-resume after its automatic-resume.
                # Duplicates must neither extend grace nor reset fresh telemetry.
                if self._power_state != "suspended":
                    return
                self._power_state = "resuming"
                self._power_resume_deadline = self.health.clock() + 15
            self._power_generation += 1
            self._power_gate.set()
            self._poll_wake.set()

    def system_power_status(self):
        with self._power_lock:
            return {"state": self._power_state, "generation": self._power_generation,
                    "resume_remaining": max(0, round(self._power_resume_deadline - self.health.clock(), 1))
                    if self._power_resume_deadline is not None else None}

    def ups_cancel_test_status(self):
        native = not self.power_actions_blocked and getattr(self.adapter, "bridge_required", True) is False
        try:
            operation = self.ups_power.status()
        except Exception:
            operation = {"state": "review_required"}
        blocked = (not native or self.stop_event.is_set() or self._power_gate.is_set()
                   or self.engine.policy["ups_poweroff"] or self.ups_battery_test.pending()
                   or operation.get("state") not in ("idle", "cancelled", "completed"))
        return self.ups_cancel_test.status(device=getattr(self.adapter, "device_identity", None), sample=self.sample,
                                           policy_enabled=self.engine.policy["enabled"], blocked=blocked)

    def physical_test_pending(self):
        return self.ups_cancel_test.pending() or self.ups_battery_test.pending()

    def ups_battery_test_status(self):
        native = not self.power_actions_blocked and getattr(self.adapter, "bridge_required", True) is False
        blocked = (not native or self.stop_event.is_set() or self._power_gate.is_set()
                   or self.engine.policy["ups_poweroff"] or self.ups_cancel_test.pending()
                   or self.ups_power.status().get("state") not in ("idle", "cancelled", "completed"))
        return self.ups_battery_test.status(device=getattr(self.adapter, "device_identity", None), sample=self.sample,
                                            policy_enabled=self.engine.policy["enabled"], blocked=blocked)

    def _observe_battery_test(self, sample):
        before = self.ups_battery_test_status()
        self.ups_battery_test.observe(device=getattr(self.adapter, "device_identity", None), sample=sample)
        after = self.ups_battery_test_status()
        if (after["state"], after.get("result")) != (before["state"], before.get("result")):
            self._record_event("ups_battery_test", after["message"],
                               {"operation_id": after.get("operation_id"), "state": after["state"],
                                "result": after.get("result"), "production_validated": False})

    def confirm_ups_battery_observation(self, data):
        with self.settings_lock, self.lock:
            result = self.ups_battery_test.confirm_observation(
                device=getattr(self.adapter, "device_identity", None), sample=self.sample, **data)
            self._record_event("ups_battery_test_reviewed", result["message"],
                               {"operation_id": result["operation_id"], "result": result.get("result"),
                                "source": "user_physical_observation", "production_validated": False})
            return self.snapshot(refresh_recent_records=False)

    def run_ups_battery_test(self, *, acknowledge_risk=False):
        # Only an explicit authenticated POST enters this path. Loss of mains,
        # polling, starting the service and resuming Windows never start a test.
        with self.settings_lock:
            if (self.stop_event.is_set() or self._power_gate.is_set() or self.power_actions_blocked
                    or getattr(self.adapter, "bridge_required", True) is not False):
                raise UpsPowerBlocked("La prueba requiere el servicio USB nativo y Windows sin transición de energía.")
            if acknowledge_risk is not True:
                raise UpsPowerBlocked("Confirma el riesgo de corte real y la hibernación inmediata de esta PC.")
            generation = self.system_power_status()["generation"]

            def guard():
                if self.stop_event.is_set() or self._power_gate.is_set() or not self._read_generation_current(generation):
                    raise UpsPowerBlocked("Windows cambió de estado; no se enviarán nuevas órdenes de prueba.")

            windows_result = {"requested": False, "error": None}

            def request_hibernation():
                # Invoked immediately after a possible USB write, before any
                # post-write journal/status I/O. Stopping the service alone
                # must not abandon Windows with an armed UPS output cutoff.
                try:
                    if self._power_gate.is_set() or not self._read_generation_current(generation):
                        raise WindowsActionUnconfirmed("Windows ya cambió de estado; no se repetirá su solicitud de hibernación.")
                    self.action_callback("hibernate")
                    if self._power_gate.is_set() or not self._read_generation_current(generation):
                        raise WindowsActionUnconfirmed("Se observó una transición de energía durante la solicitud; no se repetirá.")
                    windows_result["requested"] = True
                except (WindowsActionUnconfirmed, subprocess.TimeoutExpired) as exc:
                    windows_result["requested"] = None
                    windows_result["error"] = (str(exc)[:200] if isinstance(exc, WindowsActionUnconfirmed) else
                                               "La solicitud a Windows no confirmó su resultado dentro del plazo.")
                except Exception as exc:
                    if self._power_gate.is_set() or not self._read_generation_current(generation):
                        windows_result["requested"] = None
                        windows_result["error"] = "La solicitud atravesó una transición de energía; su resultado no se deduce del proceso."
                    else:
                        windows_result["error"] = str(exc)[:200]

            self._cancel_test_io.set()
            try:
                if not self._read_lock.acquire(timeout=2):
                    raise UpsPowerBlocked("El lector está ocupado; no se envió la orden de prueba.")
                try:
                    with self.lock:
                        guard()
                        if (self.engine.policy["enabled"] or self.engine.policy["ups_poweroff"]
                                or self.ups_cancel_test.pending()
                                or self.ups_power.status().get("state") not in ("idle", "cancelled", "completed")):
                            raise UpsPowerBlocked("Desactiva y guarda la protección y resuelve las demás operaciones del UPS.")
                        sample = self.adapter.read()
                        guard()
                        if not self._fresh_sample(sample):
                            raise UpsPowerBlocked("No hay una lectura reciente para iniciar la prueba en batería.")
                        device = getattr(self.adapter, "device_identity", None)
                        self.ups_battery_test.observe(device=device, sample=sample)
                        result = self.ups_battery_test.start(device=device, sample=sample, policy_enabled=False,
                                                             blocked=False, acknowledge_risk=True, guard=guard,
                                                             on_submission=request_hibernation)
                finally:
                    self._read_lock.release()
                if result.get("operation_id") is None:
                    raise UpsPowerBlocked("No se creó una operación propia de prueba; no se solicitó hibernación.")
                if self._power_gate.is_set() or not self._read_generation_current(generation):
                    windows_result["requested"] = None
                    windows_result["error"] = "Se observó una transición de energía durante la prueba; no se repetirá la solicitud a Windows."
                if not windows_result["requested"]:
                    self.ups_battery_test.record_windows_request(windows_result["requested"], error=windows_result["error"])
                    if not self._power_gate.is_set() and self._read_generation_current(generation):
                        try:
                            if not self._read_lock.acquire(timeout=2):
                                raise UpsPowerBlocked("No se obtuvo una lectura inmediata para cancelar.")
                            try:
                                current = self.adapter.read()
                                guard()
                                self.ups_battery_test.cancel_after_windows_failure(device=device, sample=current, guard=guard)
                            finally:
                                self._read_lock.release()
                        except Exception:
                            pass  # Preserve the durable uncertain result, never retry.
                    uncertain = windows_result["requested"] is None
                    transitioned = self.system_power_status()["generation"] != generation
                    if transitioned:
                        self._event("ups_battery_test_windows_unconfirmed", "Hubo una transición de energía; el resultado del proceso no confirma la finalización. "
                                    "Registra la observación física; no se repetirán órdenes.")
                    else:
                        self._event("ups_battery_test_windows_unconfirmed" if uncertain else "ups_battery_test_unconfirmed",
                                    ("No se confirmó el resultado de la solicitud de hibernación. " if uncertain else "Windows rechazó la hibernación. ") +
                                    "El corte del UPS podría seguir pendiente; no repitas la prueba. "
                                    "Si Windows sigue activo, guarda tu trabajo e hiberna manualmente cuanto antes.")
                else:
                    self.ups_battery_test.record_windows_request(True)
                    self._record_event("ups_battery_test", "Prueba en batería: hibernación solicitada a Windows; "
                                       "no significa que haya terminado de guardar la memoria.",
                                       {"operation_id": result["operation_id"], "production_validated": False})
            finally:
                self._cancel_test_io.clear()
                self._poll_wake.set()
            return self.snapshot(refresh_recent_records=False)

    def _observe_cancel_test(self, sample):
        before = self.ups_cancel_test_status()
        self.ups_cancel_test.observe(device=getattr(self.adapter, "device_identity", None), sample=sample)
        after = self.ups_cancel_test_status()
        if (after["state"], after.get("result")) != (before["state"], before.get("result")):
            payload = {"operation_id": after["operation_id"], "state": after["state"],
                       "result": after.get("result"), "production_validated": False}
            if after["state"] == "uncertain":
                self._event("ups_cancel_test_unconfirmed", after["message"], payload)
            else:
                self._record_event("ups_cancel_test", after["message"], payload)

    def close_ups_cancel_test(self, data):
        with self.settings_lock, self.lock:
            result = self.ups_cancel_test.close_observed_cut(
                device=getattr(self.adapter, "device_identity", None), sample=self.sample, **data)
            self._record_event("ups_cancel_test_reviewed", result["message"],
                               {"operation_id": result["operation_id"], "result": result["result"],
                                "source": "user_physical_observation", "production_validated": False})
            return self.snapshot(refresh_recent_records=False)

    def confirm_ups_cancel_observation(self, data):
        with self.settings_lock, self.lock:
            result = self.ups_cancel_test.confirm_observation(
                device=getattr(self.adapter, "device_identity", None), sample=self.sample, **data)
            self._record_event("ups_cancel_test_reviewed", result["message"],
                               {"operation_id": result["operation_id"], "result": result["result"],
                                "source": "user_independent_load", "production_validated": False})
            return self.snapshot(refresh_recent_records=False)

    def run_ups_cancel_test(self, *, cancel=False, acknowledge_risk=False):
        with self.settings_lock:
            return self._run_ups_cancel_test(cancel=cancel, acknowledge_risk=acknowledge_risk)

    def _run_ups_cancel_test(self, *, cancel=False, acknowledge_risk=False):
        # Both HTTP operations are manual. Nothing in polling, policy or resume
        # can enter this path, and USB remains exclusive across query + control.
        if self.stop_event.is_set() or self._power_gate.is_set() or self.power_actions_blocked:
            raise UpsPowerBlocked("La prueba requiere el servicio nativo activo y Windows sin transición de energía.")
        if getattr(self.adapter, "bridge_required", True) is not False:
            raise UpsPowerBlocked("La prueba física requiere el lector USB nativo.")
        if not cancel and acknowledge_risk is not True:
            raise UpsPowerBlocked("Debes confirmar el riesgo de una prueba física real.")
        self._cancel_test_io.set()
        power_generation = self.system_power_status()["generation"]
        def check_power_transition():
            if self.stop_event.is_set() or self._power_gate.is_set() or not self._read_generation_current(power_generation):
                raise UpsPowerBlocked("Windows cambió de estado; la prueba queda bloqueada.")
        try:
            if not self._read_lock.acquire(timeout=2):
                raise UpsPowerBlocked("El lector está ocupado; no se envió ninguna orden de prueba.")
            try:
                with self.lock:
                    if self.stop_event.is_set() or self._power_gate.is_set():
                        raise UpsPowerBlocked("Windows cambió de estado; no se enviará una orden de prueba.")
                    if not cancel and (self.engine.policy["enabled"] or self.engine.policy["ups_poweroff"]):
                        raise UpsPowerBlocked("Desactiva y guarda la protección automática antes de una prueba física.")
                    if self.ups_battery_test.pending():
                        raise UpsPowerBlocked("Resuelve primero la prueba en batería; no se enviará otra orden.")
                    if self.ups_power.status().get("state") not in ("idle", "cancelled", "completed"):
                        raise UpsPowerBlocked("Hay una operación de producción del UPS pendiente; no se iniciará una prueba.")
                    sample = self.adapter.read()
                    check_power_transition()
                    if not self._fresh_sample(sample):
                        raise UpsPowerBlocked("No se obtuvo una lectura reciente; no se envió control.")
                    device = getattr(self.adapter, "device_identity", None)
                    self._observe_cancel_test(sample)
                    if cancel:
                        result = self.ups_cancel_test.cancel(device=device, sample=sample, guard=check_power_transition)
                    else:
                        result = self.ups_cancel_test.start(device=device, sample=sample, policy_enabled=False,
                                                            blocked=False, acknowledge_risk=acknowledge_risk, guard=check_power_transition)
                    self._record_event("ups_cancel_test", "Prueba física manual de cancelación: " + result["state"],
                                       {"operation_id": result["operation_id"], "state": result["state"],
                                        "production_validated": False})
            finally:
                self._read_lock.release()
        finally:
            self._cancel_test_io.clear()
            self._poll_wake.set()
        return self.snapshot(refresh_recent_records=False)

    def _read_generation_current(self, generation):
        with self._power_lock:
            return self._power_state != "suspended" and generation == self._power_generation

    def _sync_power_state(self):
        """Called under the decision lock; no Windows callback waits for this work."""
        with self._power_lock:
            state, generation = self._power_state, self._power_generation
            changed = generation != self._power_applied_generation
            expired = (state == "resuming" and self._power_resume_deadline is not None
                       and self.health.clock() >= self._power_resume_deadline)
            if expired:
                self._power_state, self._power_resume_deadline = "running", None
        if changed:
            self.ups_reserve.invalidate()
            self.ups_sequence.invalidate()
            self._power_applied_generation = generation
            self.sample = None
            self._pending_health_failure = self._last_health_notice = None
            self._last_processed_at = self.health.clock()
            self._next_poll_at = None
            self._poll_failures = 0
            self._reader_dead_since = None
            self.health.reset_progress_after_resume()
            self.engine.evaluate(None)
            self.ups_cancel_test.interrupted()
            self.ups_battery_test.interrupted()
            self.ups_power.interrupted()
            if state == "resuming":
                pending_ups = self.ups_power.status()
                started = pending_ups.get("started_monotonic")
                if (pending_ups.get("state") == "review_required" and pending_ups.get("submission_complete") is True
                        and pending_ups.get("owner_pid") == os.getpid() and type(started) in (int, float)
                        and math.isfinite(started)):
                    remaining = pending_ups.get("delay_seconds", 0) + 30 - (self.health.clock() - started)
                    if remaining > 0:
                        self._event("ups_poweroff_unconfirmed", "Windows se reanudó antes de terminar el plazo de la orden del UPS. "
                                    "El corte todavía podría ocurrir: hiberna la PC y no repitas órdenes al UPS.",
                                    {"operation_id": pending_ups.get("operation_id"), "nominal_remaining_seconds": math.ceil(remaining)})
            self._record_event("system_" + state, "Windows suspendió el equipo; lecturas del UPS en pausa."
                               if state == "suspended" else "Windows se reanudó; comprobando lecturas nuevas del UPS.")
            self.test_deadline = self.test_ups_delay = self.test_ups_simulation = None
            self.connection = state
            self.error = ("Windows suspendió las lecturas del UPS."
                          if state == "suspended" else "Esperando una lectura nueva del UPS después de reanudar Windows.")
            self.error_kind = None
        if expired:
            self._resume_silent = False
            self._unavailable("El UPS no volvió a entregar datos válidos en los 15 segundos posteriores a reanudar Windows.",
                              kind="resume")
        return state != "running" and not expired

    @property
    def power_actions_blocked(self):
        return self.dry_run or self.simulation_source

    def _windows_action_readiness(self, action, *, refresh=False):
        current = self.health.clock()
        cached = self._windows_readiness_cache
        if not refresh and cached and cached[0] == action and 0 <= current - cached[1] < 30:
            return dict(cached[2])
        try:
            value = self._windows_readiness_probe(action)
            if not isinstance(value, dict) or type(value.get("available")) is not bool:
                raise ValueError("Respuesta de disponibilidad de Windows no válida")
            value = {"available": value["available"], "reason": None if value["available"] else
                     value.get("reason") or "Windows no está preparado para ejecutar la acción configurada."}
        except Exception:
            value = {"available": False, "reason": "No se pudo verificar la disponibilidad de Windows para actuar."}
        self._windows_readiness_cache = (action, current, value)
        return dict(value)

    @staticmethod
    def _ups_blockers(result):
        """The same four categories drive the decision and the visible status."""
        labels = {"connection": "UPS conectado y compatible", "windows": "Windows preparado para hibernar",
                  "operation": "Sin otra operación de energía pendiente", "controlled_shutdown": "Apagado controlado disponible"}
        categories = {"power_permission": "windows", "power_transition": "windows", "windows_ready": "windows",
            "power_actions_not_authorized": "windows", "windows_not_ready": "windows",
            "manual_test_clear": "operation", "previous_operation_clear": "operation",
            "previous_operation_pending": "operation", "physical_test_pending": "operation",
            "battery_test": "operation", "shutdown_pending": "operation",
            "low_battery": "controlled_shutdown", "fault": "controlled_shutdown",
            "battery_critical": "controlled_shutdown", "output_voltage": "controlled_shutdown"}
        checks = result["checks"]
        if not result["available"] and result.get("reason"):
            code = result.get("reason_code")
            category = next((item.get("category") for item in checks if item["key"] == code
                             and item.get("category") in labels), categories.get(code, "connection"))
            checks.append({"key": code or "control_unavailable", "category": category,
                           "passed": False, "reason": result["reason"]})
        blockers = {key: {"key": key, "label": label, "blocked": False, "reason": None, "reasons": []}
                    for key, label in labels.items()}
        for check in checks:
            if check.get("passed") is not False or check.get("blocking") is False:
                continue
            category = check.get("category") or categories.get(check["key"], "connection")
            group = blockers[category if category in labels else "connection"]
            reason = check.get("reason") or check.get("label") or "Control no disponible."
            group["blocked"] = True
            if reason not in group["reasons"]:
                group["reasons"].append(reason)
            group["reason"] = " ".join(group["reasons"])
        result["blockers"] = list(blockers.values())
        available = not any(item["blocked"] for item in result["blockers"])
        if result["available"] and not available:
            first = next(item for item in checks if item.get("passed") is False and item.get("blocking") is not False)
            result.update(reason_code=first["key"], reason=first.get("reason") or first["label"])
        result["available"] = available

    def ups_power_status(self, action=None, *, policy=None, refresh_windows=False):
        explicit_policy = policy is not None
        policy = self.engine.policy if policy is None else policy
        action = action or policy["action"]
        device = getattr(self.adapter, "device_identity", None)
        try:
            capability = self.ups_power.capabilities(device=device, action=action,
                delay_seconds=policy["ups_poweroff_delay_seconds"])
            operation = self.ups_power.status()
            if not isinstance(capability, dict) or not isinstance(operation, dict):
                raise ValueError("Estado de control del UPS no válido")
        except Exception as exc:
            self._ups_error = "No se pudo consultar el control del UPS: " + str(exc)[:200]
            capability = {"available": False, "validation": "pending", "reason": self._ups_error,
                          "delay_seconds": None, "manual_restart": False}
            operation = {"state": "review_required", "reason": self._ups_error, "confirmation": "unconfirmed"}
        raw_validation = capability.get("validation")
        result = {**capability, "validation": raw_validation if raw_validation in ("validated", "incompatible") else "pending",
                  "reason_code": raw_validation, "manual_restart": capability.get("manual_restart", False),
                  "authorized": self.allow_ups_poweroff, "operation": operation,
                  "state": operation.get("state", "idle"), "windows_request": self._windows_request,
                  "enabled": policy["ups_poweroff"]}
        result["last_block"] = copy.deepcopy(self._last_ups_block)
        result["manual_restart_verified"] = capability.get("manual_restart") is True
        review_check = getattr(self.ups_power, "can_review", None)
        result["can_review"] = bool(callable(review_check) and review_check(device=device, sample=self.sample))
        result["state"] = {"review_required": "unconfirmed", "uncertain": "unconfirmed", "arming": "pending",
                           "armed": "pending", "cancel_submitted": "pending", "cancelling": "pending"}.get(result["state"], result["state"])
        native = not self.simulation_source and getattr(self.adapter, "bridge_required", True) is False
        if not native:
            result.update(available=False, reason_code="native_source_required", reason="El corte del UPS requiere lectura USB nativa; puente y simulación no envían controles.")
        elif device is None or self.connection != "connected" or not self._fresh_sample(self.sample):
            result.update(available=False, reason_code="telemetry_unavailable", reason="El corte del UPS requiere identidad y lecturas USB recientes.")
        result["can_configure"] = (result["available"] is True and native and self.allow_ups_poweroff
                                   and not self.power_actions_blocked and not self.stop_event.is_set()
                                   and not self._power_gate.is_set() and not self.physical_test_pending()
                                   and operation.get("state") in ("idle", "cancelled", "completed"))
        if operation.get("state") in ("review_required", "arming", "submitted", "armed", "uncertain", "cancelling", "cancel_submitted"):
            result.update(available=False, reason_code="previous_operation_pending", reason=operation.get("reason", "La operación anterior del UPS requiere revisión."))
        if self.physical_test_pending():
            result.update(available=False, reason_code="physical_test_pending", reason="Resuelve la prueba física pendiente antes de habilitar el corte automático del UPS.")
        if not self.allow_ups_poweroff or self.power_actions_blocked:
            result["available"] = False
            if raw_validation == "validated" and native:
                result["reason_code"] = "power_actions_not_authorized"
                result["reason"] = "Falta la habilitación administrativa de acciones reales del UPS."
        if ("operational_limits" not in capability and self.sample is not None
                and self.sample.get("low_battery") is not False):
            result.update(available=False, reason_code="battery_critical" if self.sample.get("low_battery") is True
                          else "battery_low_or_unknown", reason=(
                "El UPS reporta batería crítica; una orden podría cortar inmediatamente. Se prioriza hibernar Windows."
                if self.sample.get("low_battery") is True else "El estado de batería crítica no está confirmado; se prioriza la protección de Windows."))
        if capability.get("reserve_mode") == "observed_sequence":
            result["reserve_remaining_seconds"] = None
            sequence_policy = policy
            engine_status = self.engine.status()
            effective = engine_status.get("effective_countdown_seconds")
            if (not explicit_policy and engine_status.get("emergency", {}).get("active") is True
                    and type(effective) is int and 10 <= effective <= policy["countdown_seconds"]):
                sequence_policy = {**policy, "countdown_seconds": effective}
            admission = self.ups_sequence.status(capability, sample=self.sample, policy=sequence_policy)
            result["sequence_admission"] = admission
            result["checks"] = copy.deepcopy(admission.get("checks", []))
            if result["available"] and admission.get("available") is not True:
                result.update(available=False, reason_code=admission.get("reason_code") or "sequence_unavailable",
                              reason=admission.get("reason") or "Falta continuidad de lecturas; se conserva la protección de Windows.")
        else:
            result["reserve_remaining_seconds"] = self.ups_reserve.remaining(capability, self.sample)
            if result["available"] and (result["reserve_remaining_seconds"] is None
                    or result["reserve_remaining_seconds"] < (capability.get("delay_seconds") or 180)):
                result.update(available=False, reason_code="reserve_unavailable", reason="No hay reserva restante verificable para cubrir el corte del UPS; se conserva la protección de Windows.")
        global_checks = [
            ("native_source", "Lectura USB directa", native, "lectura USB directa"),
            ("device_identified", "Unidad USB identificada", device is not None, "descriptor USB identificado"),
            ("unit_profile", "Perfil de esta unidad", raw_validation == "validated", "perfil vinculado a la huella de esta unidad"),
            ("telemetry_connected", "Comunicación reciente", self.connection == "connected" and self._fresh_sample(self.sample), "lecturas recientes"),
            ("power_permission", "Permiso de acciones reales", self.allow_ups_poweroff and not self.power_actions_blocked, "permiso administrativo activo"),
            ("monitor_running", "Monitor en ejecución", not self.stop_event.is_set() and not self._health_fault.is_set(), "monitor operativo"),
            ("power_transition", "Windows disponible para actuar", not self._power_gate.is_set(), "sin suspensión ni reanudación pendiente"),
            ("manual_test_clear", "Pruebas manuales cerradas", not self.physical_test_pending(), "ninguna prueba pendiente"),
            ("previous_operation_clear", "Operación anterior resuelta", operation.get("state") not in (
                "review_required", "arming", "submitted", "armed", "uncertain", "cancelling", "cancel_submitted"), "ninguna orden pendiente"),
        ]
        result["checks"] = [{"key": key, "label": label, "passed": passed, "value": passed, "expected": expected}
                            for key, label, passed, expected in global_checks] + result.get("checks", [])
        explanations = {
            "native_source": "El control del UPS requiere la conexión USB directa.",
            "device_identified": "No se pudo identificar la unidad USB conectada.",
            "unit_profile": capability.get("reason") or "La unidad o la acción configurada no es compatible con el control del UPS.",
            "telemetry_connected": "No hay comunicación USB reciente; esperando una lectura válida.",
            "power_permission": "El servicio no tiene habilitadas las acciones reales de energía.",
            "monitor_running": "El monitor no está operativo para enviar una orden al UPS.",
            "power_transition": "Windows está suspendido o recuperando sus lecturas tras reanudar.",
            "manual_test_clear": "Hay una prueba física pendiente; debe finalizar o revisarse antes de otra orden.",
            "previous_operation_clear": operation.get("reason") or "Ya existe una operación de energía pendiente; no se enviará otra orden.",
        }
        for check in result["checks"]:
            if check["key"] in explanations and not check["passed"]:
                check["reason"] = explanations[check["key"]]
        readiness = self._windows_action_readiness(action, refresh=refresh_windows)
        result["checks"].append({"key": "windows_ready", "category": "windows", "label": "Windows preparado para actuar",
            "passed": readiness["available"], "value": readiness["available"], "expected": "hibernación y permiso disponibles",
            "reason": readiness["reason"]})
        if result["available"] and not readiness["available"]:
            result.update(available=False, reason_code="windows_not_ready", reason=readiness["reason"])
        if result["available"] and (self.stop_event.is_set() or self._health_fault.is_set() or self._power_gate.is_set()):
            result.update(available=False, reason_code="power_transition" if self._power_gate.is_set() else "monitor_not_ready",
                          reason="El monitor o Windows están en transición; espera lecturas nuevas antes de programar el UPS.")
        result["simulation_delay_seconds"] = policy["ups_poweroff_delay_seconds"]
        result["validated_delay"] = capability.get("delay_hardware_validated",
            raw_validation == "validated" and capability.get("delay_seconds") is not None)
        if self._ups_error:
            result["last_error"] = self._ups_error
        if self.test_ups_simulation is not None:
            result["simulation"] = dict(self.test_ups_simulation)
        restart_observation = self.ups_battery_test.compatibility(device)
        if restart_observation is not None:
            result.update(restart_observation)
            if raw_validation == "profile_unvalidated":
                result["reason"] = "El retorno automático de las salidas al volver la electricidad está aceptado. Falta completar el perfil de control y la reserva para el corte automático."
        self._ups_blockers(result)
        if not result["available"] and result["state"] == "idle":
            result["state"] = "blocked"
        return result

    def _observe_ups(self, sample):
        try:
            self.ups_power.observe(device=getattr(self.adapter, "device_identity", None), sample=sample)
        except Exception as exc:
            self._ups_error = "No se pudo confirmar el estado del corte del UPS: " + str(exc)[:200]

    def complete_ups_review(self, data):
        with self.settings_lock, self.lock:
            if self.stop_event.is_set() or self._power_gate.is_set():
                raise UpsPowerBlocked("Espera a recuperar lecturas recientes antes de cerrar la revisión.")
            result = self.ups_power.complete_review(device=getattr(self.adapter, "device_identity", None),
                                                     sample=self.sample, **data)
            self._record_event("ups_poweroff_reviewed", "Revisión cerrada por el usuario; el corte anterior no se confirma automáticamente.",
                               {"operation_id": result.get("operation_id"), "physical_confirmation": False})
            return self.snapshot(refresh_recent_records=False)

    def _cancel_own_ups_after_windows_failure(self):
        # A cancel can also turn an output back on. Never issue it based only on
        # an old journal or a successful write: obtain fresh shutdown-state evidence.
        if not self._read_lock.acquire(timeout=0.25):
            raise UpsPowerBlocked("No se pudo verificar el UPS para cancelar su corte pendiente.")
        try:
            sample = self.adapter.read()
        finally:
            self._read_lock.release()
        self._observe_ups(sample)
        return self.ups_power.cancel(device=getattr(self.adapter, "device_identity", None), sample=sample)

    def _act(self, action):
        if self.physical_test_pending():
            raise RuntimeError("Hay una prueba física del UPS pendiente; las acciones automáticas están bloqueadas.")
        power_generation = self.system_power_status()["generation"]
        if self._power_gate.is_set():
            raise RuntimeError("Windows está suspendido o esperando lecturas nuevas tras reanudar; acción bloqueada.")
        if self.stop_event.is_set():
            raise RuntimeError("Monitor detenido; acción bloqueada.")
        if self._health_fault.is_set():
            raise RuntimeError("El lector no está sano; acción bloqueada.")
        if self.supervisor_thread is not None and not self.supervisor_thread.is_alive():
            raise RuntimeError("La supervisión del lector no está activa; acción bloqueada.")
        if self.processor_thread is not None and not self.processor_thread.is_alive():
            raise RuntimeError("El procesador de telemetría no está activo; acción bloqueada.")
        if self._processing_sequence is not None and not self.telemetry.is_latest(self._processing_sequence):
            raise RuntimeError("Hay telemetría más reciente pendiente; acción bloqueada.")
        if self.power_actions_blocked:
            self._event("simulation", "Modo de desarrollo: acción simulada; Windows sigue encendido.")
        else:
            if not self._fresh_sample(self.sample):
                raise RuntimeError("Sin telemetría reciente; acción bloqueada.")
            if self.stop_event.is_set():
                raise RuntimeError("Monitor detenido; acción bloqueada.")
            attempted_ups = False
            windows_attempted, windows_failure = False, None
            windows_invoked = False

            def request_windows_after_submission():
                nonlocal windows_attempted, windows_failure, windows_invoked
                windows_attempted = True
                self._windows_request = "pending"
                try:
                    if self._power_gate.is_set() or not self._read_generation_current(power_generation):
                        raise WindowsActionUnconfirmed("Windows cambió de estado antes de solicitar el cierre; no se repetirá.")
                    windows_invoked = True
                    self.action_callback(action)
                    if self._power_gate.is_set() or not self._read_generation_current(power_generation):
                        raise WindowsActionUnconfirmed("Se observó una transición de energía durante la solicitud a Windows; no se repetirá.")
                    self._windows_request = "requested"
                except (WindowsActionUnconfirmed, subprocess.TimeoutExpired) as exc:
                    windows_failure = (exc if isinstance(exc, WindowsActionUnconfirmed) else WindowsActionUnconfirmed(
                        "La solicitud a Windows no confirmó su resultado dentro del plazo."))
                    self._windows_request = "unconfirmed"
                except Exception as exc:
                    if self._power_gate.is_set() or not self._read_generation_current(power_generation):
                        windows_failure = WindowsActionUnconfirmed("La solicitud atravesó una transición de energía; su resultado no se deduce del proceso.")
                        self._windows_request = "unconfirmed"
                    else:
                        windows_failure = exc
                        self._windows_request = "failed"

            if self.engine.policy["ups_poweroff"]:
                capability = self.ups_power_status(action, refresh_windows=True)
                if capability["available"]:
                    if self._power_gate.is_set() or not self._read_generation_current(power_generation):
                        raise RuntimeError("Windows cambió de estado antes de programar el UPS; acción bloqueada.")
                    try:
                        previous_operation = self.ups_power.status().get("operation_id")
                        try:
                            reserve = capability.get("reserve_remaining_seconds")
                            arm_options = {"reserve_seconds": reserve, "on_submission": request_windows_after_submission,
                                           "delay_seconds": self.engine.policy["ups_poweroff_delay_seconds"]}
                            if capability.get("reserve_mode") == "observed_sequence":
                                arm_options["sequence_evidence"] = capability.get("sequence_admission", {}).get("evidence")
                            submitted = self.ups_power.arm(action, device=self.adapter.device_identity, sample=self.sample, **arm_options)
                        finally:
                            attempted_ups = self.ups_power.status().get("operation_id") not in (None, previous_operation)
                        if submitted.get("submission_complete") is True:
                            self._record_event("ups_poweroff_submitted", "Orden de corte retardado enviada al UPS; el corte físico sigue sin confirmar.", {
                                "operation_id": submitted.get("operation_id"), "delay_seconds": submitted.get("delay_seconds"),
                                "submitted_at": submitted.get("submitted_at"), "submission_complete": True,
                                "physical_cut_confirmed": False}, timestamp=submitted.get("submitted_at"))
                        elif submitted.get("state") in ("submitted", "armed"):
                            self._event("ups_poweroff_submitted", "Corte retardado del UPS solicitado; Windows aún debe cerrar.")
                        if submitted.get("state") not in ("submitted", "armed"):
                            self._ups_error = submitted.get("reason", "El resultado del corte del UPS es incierto.")
                            self._event("ups_poweroff_unconfirmed", "Envío al UPS sin confirmar. " + self._ups_error + " Se solicita el cierre de Windows.")
                    except Exception as exc:
                        self._ups_error = str(exc)[:300]
                        if not attempted_ups:
                            self._record_ups_block(capability, reason=self._ups_error, reason_code="preflight_rejected")
                        else:
                            self._event("ups_poweroff_unconfirmed", "El corte del UPS no se confirmó. " + self._ups_error + " Se conserva el cierre de Windows.")
                else:
                    self._record_ups_block(capability)
            if not windows_attempted:
                request_windows_after_submission()
            if attempted_ups and self._windows_request == "requested":
                try:
                    self.ups_power.mark_windows_requested()
                except Exception as exc:
                    self._ups_error = "No se pudo registrar la solicitud de Windows: " + str(exc)[:200]
            try:
                if windows_failure is not None:
                    raise windows_failure
            except Exception:
                uncertain = (isinstance(windows_failure, WindowsActionUnconfirmed) or self._power_gate.is_set()
                             or not self._read_generation_current(power_generation))
                self._windows_request = "unconfirmed" if uncertain else "failed"
                if attempted_ups and (self._power_gate.is_set() or not self._read_generation_current(power_generation)):
                    # Windows may already be entering sleep independently. Do
                    # not send controls across that boundary or replay on wake.
                    self._ups_error = "Windows cambió de estado con una operación propia del UPS pendiente; requiere revisión."
                    self._event("ups_poweroff_unconfirmed", self._ups_error)
                elif attempted_ups:
                    try:
                        cancellation = self._cancel_own_ups_after_windows_failure()
                        self._event("ups_poweroff_cancel_requested", "Cancelación del corte del UPS solicitada; pendiente de corroborar.",
                                    {"state": cancellation.get("state")})
                    except Exception as exc:
                        self._ups_error = str(exc)[:300]
                        self._event("ups_poweroff_unconfirmed", ("No se confirmó la solicitud a Windows" if uncertain else "Windows rechazó el cierre") +
                                    " y no se confirmó la cancelación del corte del UPS.")
                if uncertain:
                    self._event("windows_action_unconfirmed", "El resultado de la solicitud a Windows es incierto; una transición observada impide repetirla o enviar controles al despertar.")
                    if (windows_invoked or attempted_ups) and self.system_power_status()["generation"] != power_generation:
                        # The command's wait crossed sleep/resume. Do not turn
                        # an observed transition into a false policy failure.
                        return
                raise

    @staticmethod
    def _fresh_sample(sample):
        try:
            stamp = datetime.fromisoformat(sample["timestamp"].replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
            return -5 <= age <= 15 and sample.get("connected", True) is True
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            return False

    def _unavailable(self, message, state="disconnected", kind="telemetry", *, notify=True):
        self.ups_reserve.invalidate()
        self.ups_sequence.invalidate()
        self.ups_power.interrupted()
        if self.ups_battery_test.pending():
            self.ups_battery_test.interrupted()
        if self.ups_cancel_test.pending():
            if not self.stop_event.is_set() and self.ups_cancel_test_status()["state"] != "review_required":
                self._event("ups_cancel_test_unconfirmed", "Se perdió la comunicación durante una prueba real del UPS. "
                            "El corte podría seguir pendiente: hiberna Windows y no repitas la orden.")
            self.ups_cancel_test.interrupted()
        changed = self.connection != state or self.error_kind != kind
        self.connection, self.error, self.sample, self.error_kind = state, message, None, kind
        self._health_fault.set()
        if changed:
            if kind != "stopped":
                self.health.problem(kind)
            event_kind = "monitor_error" if kind == "backend" else "connection_lost"
            if notify:
                self._event(event_kind, message, {"error_kind": kind})
            else:
                self._record_event(event_kind, message, {"error_kind": kind})
        # Record the cause before the policy transition so incident timelines
        # can associate its suspension with the actual communication failure.
        # The independent action gate above already blocks operating-system work.
        self.engine.evaluate(None)
        self._update_alerts()

    def _check_health(self):
        if not self.stop_event.is_set() and self._sync_power_state():
            return
        if self.stop_event.is_set():
            self._unavailable("Monitor detenido. Protección suspendida.", kind="stopped")
        elif self.thread is not None and not self.thread.is_alive():
            self._unavailable("El lector dejó de ejecutarse. Protección suspendida.", kind="backend")
        elif self.processor_thread is not None and not self.processor_thread.is_alive():
            self._unavailable("El procesador de telemetría dejó de ejecutarse. Protección suspendida.", kind="backend")
        elif self.sample is not None and not self._fresh_sample(self.sample):
            self._unavailable("La última lectura tiene más de 15 segundos o una fecha inválida. Protección suspendida.", "stale")

    def health_status(self):
        status = self.health.snapshot()
        power_state = self.system_power_status()["state"]
        supervisor_failed = self.supervisor_thread is not None and not self.supervisor_thread.is_alive()
        processor_failed = self.processor_thread is not None and not self.processor_thread.is_alive()
        status.update({
            "reader_alive": self.thread is not None and self.thread.is_alive(),
            "supervisor_alive": self.supervisor_thread is not None and self.supervisor_thread.is_alive(),
            "state": "stopped" if self.stop_event.is_set() else power_state if power_state != "running" else (
                "degraded" if self._health_fault.is_set() or self.error is not None or supervisor_failed or processor_failed else (
                    "healthy" if self.connection == "connected" else "starting")),
            "message": ("El supervisor del lector dejó de ejecutarse. Las acciones automáticas están bloqueadas."
                        if supervisor_failed and not self.stop_event.is_set() else (
                            "El procesador de telemetría dejó de ejecutarse. Las acciones automáticas están bloqueadas."
                            if processor_failed and not self.stop_event.is_set() else self.error)),
            "history": self.history_status(),
            "processor_alive": self.processor_thread is not None and self.processor_thread.is_alive(),
            "telemetry_buffer": self.telemetry.status(),
            "reconnect_attempts": self._poll_failures,
            "next_poll_in_seconds": max(0, round(self._next_poll_at - self.health.clock(), 2))
                if self._next_poll_at is not None else 0,
        })
        ownership_status = getattr(type(self.adapter), "ownership_status", None)
        if callable(ownership_status):
            status["usb_ownership"] = self.adapter.ownership_status()
        return status

    def _health_problem(self):
        """Inspect independent counters without waiting for the reader's lock."""
        with self._power_lock:
            if self._power_state != "running":
                return None
        if self.thread is not None and not self.thread.is_alive():
            return ("El lector dejó de ejecutarse. Protección suspendida.", "disconnected", "backend")
        if self.processor_thread is not None and not self.processor_thread.is_alive():
            return ("El procesador de telemetría dejó de ejecutarse. Protección suspendida.", "disconnected", "backend")
        if self.processor_thread is not None and self._last_processed_at is not None and self.health.clock() - self._last_processed_at > 15:
            return ("El procesador lleva más de 15 segundos sin consumir telemetría. Protección suspendida.", "stale", "backend")
        if self.health.read_stalled(15):
            return ("El lector lleva más de 15 segundos sin terminar una lectura. Protección suspendida.", "stale", "backend")
        waiting_retry = self._next_poll_at is not None and self.health.clock() <= self._next_poll_at
        if self.thread is not None and self.health.progress_stalled(15) and not waiting_retry:
            return ("El lector lleva más de 15 segundos sin procesar lecturas. Protección suspendida.", "stale", "backend")
        sample = self.sample
        if sample is not None and not self._fresh_sample(sample):
            return ("La última lectura tiene más de 15 segundos o una fecha inválida. Protección suspendida.", "stale", "telemetry")
        return None

    def _supervise_once(self):
        if self.stop_event.is_set():
            return
        self.health.check()
        power_generation = self.system_power_status()["generation"]
        problem = self._health_problem()
        if not self._read_generation_current(power_generation) or self.system_power_status()["state"] != "running":
            problem = None
        if problem is not None:
            # This gate is set before acquiring the decision lock. A blocked
            # consumer must not resume an old countdown while we wait for it.
            self._health_fault.set()
            self._pending_health_failure = problem
            self.health.problem(problem[2])
            if self._last_health_notice != problem:
                self._last_health_notice = problem
                self._notify_event("monitor_error" if problem[2] == "backend" else "connection_lost",
                                   problem[0], {"error_kind": problem[2]})
        if not self.lock.acquire(timeout=0.25):
            return
        try:
            if self.stop_event.is_set():
                return
            if self._sync_power_state():
                self._update_alerts()
                return
            if self._pending_health_failure is not None:
                message, state, kind = self._pending_health_failure
                self._pending_health_failure = None
                self._unavailable(message, state, kind, notify=False)
            # Critical repetitions and cancellation of obsolete alerts continue
            # even when adapter.read() never returns and no panel is open.
            self._update_alerts()
            self._restart_dead_reader()
        finally:
            self.lock.release()

    def _restart_dead_reader(self):
        if self.thread is None or self.thread.is_alive() or self.stop_event.is_set():
            self._reader_dead_since = None
            return
        now = self.health.clock()
        if self._reader_dead_since is None:
            self._reader_dead_since = now
            return
        if now - self._reader_dead_since < min(30, 2 ** min(self._restart_failures + 1, 5)):
            return
        # Never replace a live thread, even if its USB read is overdue. Native
        # transport already owns its bounded subprocess and USB transaction.
        self._reader_dead_since = None
        self._restart_failures += 1
        self.health.reader_restarts += 1
        self.thread = threading.Thread(target=self.run, name="ups-hardware-polling", daemon=True)
        self.thread.start()

    def supervise(self):
        while not self.stop_event.is_set():
            try:
                self._supervise_once()
            except Exception as exc:
                self._health_fault.set()
                self.health.problem("supervisor")
                log_failure("Error en la supervisión del lector", exc)
            self.stop_event.wait(1)

    def _read_hardware(self):
        """Only the dedicated polling worker (or synchronous test tick) owns USB."""
        with self._power_lock:
            generation = self._power_generation
            if self._power_state == "suspended":
                raise _PowerTransition()
        if not self._read_lock.acquire(blocking=False):
            if self._cancel_test_io.is_set():
                raise _ReadDeferred()
            raise RuntimeError("Ya hay una lectura de hardware en curso")
        try:
            self.health.begin_read()
            try:
                sample = self.adapter.read()
            except Exception as exc:
                if not self._read_generation_current(generation):
                    self.health.abandon_read()
                    raise _PowerTransition() from None
                known = isinstance(exc, (TelemetryError, OSError, ValueError))
                kind = "telemetry" if known else "backend"
                if self.system_power_status()["state"] == "resuming":
                    self.health.abandon_read()
                else:
                    self.health.finish_read(False, error_kind=kind)
                if not known:
                    raise
                return None, str(exc), kind
            if not self._read_generation_current(generation):
                self.health.abandon_read()
                raise _PowerTransition()
            fresh = self._fresh_sample(sample)
            if not fresh and self.system_power_status()["state"] == "resuming":
                self.health.abandon_read()
            else:
                self.health.finish_read(fresh, timestamp=sample.get("timestamp") if isinstance(sample, dict) else None,
                                        error_kind=None if fresh else "stale")
            return sample, None, None
        finally:
            self._read_lock.release()

    def tick(self):
        """Synchronous compatibility hook; runtime uses separate polling/consumer."""
        if self.stop_event.is_set():
            return
        if self.processor_thread is not None and threading.current_thread() is not self.thread:
            raise RuntimeError("El sondeo dedicado ya está activo")
        with self.lock:
            self._sync_power_state()
        generation = self.system_power_status()["generation"]
        try:
            sample, error, kind = self._read_hardware()
        except (_PowerTransition, _ReadDeferred):
            return
        if not self._read_generation_current(generation):
            return
        if error is not None:
            with self.lock:
                if not self.stop_event.is_set() and not self._sync_power_state():
                    self._unavailable(error, "stale" if "obsoleto" in error else "disconnected", kind)
            return
        self._process_sample(sample, power_generation=generation)

    def _process_sample(self, sample, *, power_generation=None):
        with self.lock:
            if self.stop_event.is_set():
                return
            self._sync_power_state()
            if power_generation is None:
                power_generation = self.system_power_status()["generation"]
            if not self._read_generation_current(power_generation):
                return
            if self.system_power_status()["state"] == "suspended":
                return
            if self._processing_sequence is not None and not self.telemetry.is_latest(self._processing_sequence):
                # A result superseded while waiting for this lock cannot reset
                # manual cancellation or finish a protected deadline.
                self.engine.evaluate(None)
                self._update_alerts()
                return
            if not self._fresh_sample(sample):
                if self.system_power_status()["state"] == "resuming":
                    return
                self.health.problem("stale")
                self._unavailable("La lectura no contiene una fecha reciente y válida. Protección suspendida.", "stale")
                return
            resuming = self.system_power_status()["state"] == "resuming"
            if resuming:
                with self._power_lock:
                    if not self._read_generation_current(power_generation):
                        return
                    self._power_state, self._power_resume_deadline = "running", None
            if self._health_fault.is_set():
                # The supervisor may have detected a stalled reader before it
                # could acquire this lock. Reset continuity before fresh data.
                if self._pending_health_failure is not None:
                    message, state, kind = self._pending_health_failure
                    self._unavailable(message, state, kind, notify=False)
                self.engine.evaluate(None)
                self._pending_health_failure = None
            self._health_fault.clear()
            self._last_health_notice = None
            self._restart_failures = 0
            if self.connection != "connected" and not (resuming and self._resume_silent):
                self._event("connection_restored", "Telemetría del UPS conectada.")
            self.connection, self.error, self.error_kind = "connected", None, None
            previous_sample = self.sample
            sample = {**sample, **battery_estimate_fields(sample)}
            self.sample, self.last_success = sample, sample["timestamp"]
            with self._power_lock:
                if self._read_generation_current(power_generation):
                    self._power_gate.clear()
            power = sample.get("on_battery")
            if power is True and self.last_power is not True:
                self._event("power_lost", "UPS funcionando con batería: suministro de red interrumpido.")
            elif power is False and self.last_power is True:
                self._event("power_restored", "Volvió el suministro eléctrico.")
            changed = type(power) is bool and power is not self.last_power
            if type(power) is bool:
                self.last_power = power
            if sample.get("fault") is True and (not previous_sample or previous_sample.get("fault") is not True):
                self._event("ups_fault", "El UPS reporta modo de falla.")
            percent, percent_source = battery_percentage(sample)
            threshold = self.engine.policy["low_battery_percent"]
            low = sample.get("low_battery") is True or (threshold > 0
                and percent_source == "reported_percentage" and percent <= threshold)
            estimated_low = (power is True and threshold > 0 and percent_source == "estimated_percentage"
                             and percent <= threshold)
            overload = sample.get("load_percent") is not None and sample["load_percent"] >= 100
            if low and not self.low:
                self._event("battery_low", "El UPS reporta una alarma crítica de batería." if sample.get("low_battery") is True else
                    f"Batería reportada de {percent:g} %, en el umbral de {threshold} %.")
            if estimated_low and not self._estimated_percentage_low:
                self._event("battery_low_percentage", f"Carga estimada de {percent:g} % (umbral {threshold} %). "
                            "Se prioriza la protección de Windows en un máximo de 10 segundos.",
                            {"percent": percent, "source": percent_source, "threshold_percent": threshold})
            self._estimated_percentage_low = estimated_low
            if overload and not self.overload:
                self._event("overload", "La carga reportada alcanzó o superó el 100 %.")
            self.low, self.overload = low, overload
            try:
                reserve_capability = self.ups_power.capabilities(device=getattr(self.adapter, "device_identity", None),
                                                               action=self.engine.policy["action"])
                self.ups_reserve.observe(sample, reserve_capability)
                self.ups_sequence.observe(sample, reserve_capability)
            except Exception:
                self.ups_reserve.invalidate()
                self.ups_sequence.invalidate()
            # Decisions use fresh telemetry even if recording it fails.
            self._observe_ups(sample)
            self._observe_cancel_test(sample)
            self._observe_battery_test(sample)
            own_shutdown = (sample.get("raw_status") == "Shutdown mode" and
                            self._windows_request in ("pending", "requested") and
                            self.ups_power_status()["operation"].get("state") in ("submitted", "armed", "uncertain"))
            if not own_shutdown and self._read_generation_current(power_generation):
                self.engine.evaluate(sample)
                # Publish the observation immediately, before persistence can
                # delay it. The supervisor is a watchdog, not the telemetry
                # clock for the desktop countdown.
                self._update_alerts()
            if (time.monotonic() - self.last_saved >= 10 or changed) and sample["timestamp"] != self.last_saved_timestamp:
                try:
                    self.store.add_sample(sample)
                    self._history_failures.pop("samples_write", None)
                    self.last_saved, self.last_saved_timestamp = time.monotonic(), sample["timestamp"]
                except Exception as exc:
                    self._history_failed("samples_write", exc)

    def run(self):
        """Hardware thread: no database, alert delivery, or decision lock."""
        while not self.stop_event.is_set():
            if self._cancel_test_io.is_set():
                self.stop_event.wait(0.1)
                continue
            if self.system_power_status()["state"] == "suspended":
                self._poll_wake.wait(0.25)
                self._poll_wake.clear()
                continue
            generation = self.system_power_status()["generation"]
            try:
                self._next_poll_at = None
                sample, error, kind = self._read_hardware()
                if self.stop_event.is_set():
                    return
                with self._power_lock:
                    if not self._read_generation_current(generation):
                        continue
                    self.telemetry.publish(sample, error=error, error_kind=kind,
                                           captured_at=self.health.clock(), power_generation=generation)
                self._poll_failures = self._poll_failures + 1 if error is not None else 0
            except (_PowerTransition, _ReadDeferred):
                continue
            except Exception as exc:
                if not self._read_generation_current(generation):
                    continue
                self._poll_failures += 1
                self.telemetry.publish(error="Error interno al leer telemetría. Protección suspendida.",
                                       error_kind="backend", captured_at=self.health.clock(), power_generation=generation)
                # Do not touch the logfile from hardware polling. The consumer
                # records the bounded backend error through its normal path.
            delay = self.poll_interval if not self._poll_failures else min(
                30, self.poll_interval * (2 ** min(self._poll_failures - 1, 10)))
            retry_after = getattr(self.adapter, "retry_after_seconds", 0)
            if type(retry_after) in (int, float) and math.isfinite(retry_after):
                delay = max(delay, retry_after)
            self._next_poll_at = self.health.clock() + delay
            self._poll_wake.wait(delay)
            self._poll_wake.clear()

    def _consume_once(self):
        entries, overflow = self.telemetry.drain()
        if not entries:
            # An idle consumer waiting through intentional USB reconnection
            # backoff is healthy; a consumer blocked on the decision lock is not.
            self._last_processed_at = self.health.clock()
            return
        if self.stop_event.is_set():
            return
        with self.lock:
            if self.stop_event.is_set():
                return
            paused = self._sync_power_state()
            generation = self.system_power_status()["generation"]
            entries = [entry for entry in entries if entry.get("power_generation", 0) == generation]
            if not entries or self.system_power_status()["state"] == "suspended":
                return
            if overflow is not None or len(entries) > 1:
                # No unobserved interval or replayed online state may count as
                # continuous battery time, release cancellation, or fire actions.
                self.engine.evaluate(None)
            if overflow is not None:
                self._record_event("telemetry_overflow",
                                   "El consumidor se retrasó; se reinició la continuidad de protección.", overflow)
                for kind, detail in overflow["transitions"].items():
                    self._record_event("telemetry_delayed_transition",
                                       "Transición histórica retenida durante el retraso: " + kind,
                                       {"transition": kind, "historical": True, **detail})
            for entry in entries[:-1]:
                for kind in entry["facts"]:
                    self._record_event("telemetry_delayed_transition",
                                       "Transición histórica recibida con retraso: " + kind,
                                       {"transition": kind, "historical": True,
                                        "observed_at": (entry["sample"] or {}).get("timestamp"),
                                        "sequence": entry["sequence"]})
            latest = entries[-1]
            self._processing_sequence = latest["sequence"]
            try:
                if latest["error"] is not None:
                    if not paused:
                        self._unavailable(latest["error"], "disconnected", latest["error_kind"] or "telemetry")
                elif (latest["captured_at"] is not None and self.health.clock() - latest["captured_at"] > 15):
                    self._unavailable("La entrega de telemetría tiene más de 15 segundos. Protección suspendida.", "stale")
                else:
                    self._process_sample(latest["sample"], power_generation=latest.get("power_generation", 0))
                self._last_processed_at = self.health.clock()
            finally:
                self._processing_sequence = None

    def consume(self):
        """Decision worker; a blocked USB call never occupies this thread."""
        while not self.stop_event.is_set():
            self.telemetry.wait(timeout=0.5)
            try:
                self._consume_once()
            except Exception as exc:
                log_failure("Error interno al procesar telemetría", exc)
                self._health_fault.set()
                with self.lock:
                    try:
                        self._unavailable("Error interno al procesar telemetría. Protección suspendida.", kind="backend")
                    except Exception as storage_error:
                        log_failure("No se pudo registrar el error del monitor", storage_error)

    def start(self):
        if self.thread is not None:
            raise RuntimeError("El monitor ya fue iniciado; crea otro para reiniciar.")
        try:
            self.webhooks.start()
        except Exception as exc:
            log_failure("No se pudo iniciar el envío de webhooks", exc)
        self._last_processed_at = self.health.clock()
        self.processor_thread = threading.Thread(target=self.consume, name="ups-telemetry-consumer", daemon=True)
        self.thread = threading.Thread(target=self.run, name="ups-hardware-polling", daemon=True)
        self.processor_thread.start()
        self.supervisor_thread = threading.Thread(target=self.supervise, name="ups-supervisor", daemon=True)
        self.thread.start()
        self.supervisor_thread.start()

    def stop(self):
        self.stop_event.set()
        self._poll_wake.set()
        self._health_fault.set()
        try:
            self.webhooks.stop()
        except Exception as exc:
            log_failure("No se pudo detener el envío de webhooks", exc)
        if self.lock.acquire(timeout=1):
            try:
                self.ups_cancel_test.interrupted()
                self.ups_battery_test.interrupted()
                self.ups_power.interrupted()
                self.ups_reserve.invalidate()
                self.ups_sequence.invalidate()
                self.sample = None
                self.engine.evaluate(None)
            finally:
                self.lock.release()
        if self.supervisor_thread:
            self.supervisor_thread.join(timeout=2)
        if self.thread:
            self.thread.join(timeout=12)
            if self.thread.is_alive():
                LOGGER.error("El lector no terminó en 12 segundos; no se cerrará SQLite mientras siga activo.")
                raise RuntimeError("No se pudo detener el lector a tiempo.")
        if self.processor_thread:
            self.processor_thread.join(timeout=2)
            if self.processor_thread.is_alive():
                LOGGER.error("El procesador no terminó; no se cerrará SQLite mientras siga activo.")
                raise RuntimeError("No se pudo detener el procesador a tiempo.")
        if self.supervisor_thread and self.supervisor_thread.is_alive():
            LOGGER.error("El supervisor no terminó; no se cerrará SQLite mientras siga activo.")
            raise RuntimeError("No se pudo detener el supervisor a tiempo.")

    @staticmethod
    def _event_severity(kind):
        return "critical" if kind in ("overload", "ups_fault", "policy_action_failed", "monitor_error", "ups_poweroff_unconfirmed", "ups_cancel_test_unconfirmed", "ups_battery_test_unconfirmed") else (
            "warning" if kind in ("power_lost", "battery_low", "battery_low_percentage", "connection_lost", "policy_countdown", "policy_unknown", "ups_poweroff_blocked") else "info")

    def events(self):
        try:
            events = self.store.events(100)
            self._history_failures.pop("events_read", None)
        except Exception as exc:
            self._history_failed("events_read", exc)
            events = []
        for event in events:
            event["severity"] = self._event_severity(event["kind"])
        return events

    def _recent_records_snapshot(self, *, refresh=True):
        if not refresh:
            # Mutation responses can be built under the protection lock. They
            # reuse memory only; the next regular status poll refreshes history.
            # Omission before the first poll lets older/raw event rendering work.
            return copy.deepcopy(self._recent_records_snapshot_cache or {})
        try:
            records = self.store.recent_records()
            self._history_failures.pop("recent_records_read", None)
            error = None
        except Exception as exc:
            self._history_failed("recent_records_read", exc)
            pending = getattr(self.store, "pending_records", None)
            records = pending()[:100] if callable(pending) else None
            error = ("No se pudo consultar el historial. Se muestran los eventos pendientes de guardar."
                     if records else "No se pudieron cargar los eventos recientes.")
        for record in records or ():
            if record["record_type"] == "event":
                record["severity"] = self._event_severity(record["kind"])
        result = {"recent_records": records, "recent_records_error": error}
        self._recent_records_snapshot_cache = copy.deepcopy(result)
        return result

    def snapshot(self, *, refresh_recent_records=True):
        # A cache rebuild scans retained events; ordinary status polling must
        # not hold the telemetry/protection lock while reading that archive.
        recent_records = self._recent_records_snapshot(refresh=refresh_recent_records)
        with self.lock:
            self._check_health()
            raw = self.engine.status()
            state = {"countdown": "countdown", "cancelled": "cancelled", "unknown": "blocked",
                     "action_failed": "blocked", "action_started": "executed"}.get(raw["state"], "idle")
            remaining = raw["countdown_remaining"]
            notice = self.test_deadline is not None and raw["state"] != "countdown"
            simulation = self.power_actions_blocked or notice
            reason = raw["reason"]
            if raw["state"] == "action_started" and self._windows_request == "unconfirmed":
                state = "blocked"
                reason = "La solicitud a Windows quedó sin confirmar; una transición de energía no prueba que haya terminado."
            phase = "windows_countdown"
            if self.power_actions_blocked and raw["state"] == "action_started":
                reason = "Acción simulada; Windows sigue encendido."
            if notice:
                elapsed_deadline = self.test_deadline
                remaining = max(0, math.ceil(elapsed_deadline - time.monotonic()))
                if self.test_ups_delay is not None and remaining == 0:
                    phase = "ups_delay"
                    remaining = max(0, math.ceil(elapsed_deadline + self.test_ups_delay - time.monotonic()))
                state = "countdown" if remaining else "idle"
                if self.test_ups_delay is not None:
                    self.test_ups_simulation = {"phase": phase if remaining else "complete", "remaining": remaining,
                                                "delay_seconds": self.test_ups_delay,
                                                "windows_requested": phase == "ups_delay", "ups_cut_simulated": not bool(remaining),
                                                "validated_delay": self.ups_power_status().get("validated_delay", False)}
                if not remaining and not self.test_finished:
                    self.test_finished = True
                    self._record_event("simulation", "Prueba finalizada. No se ejecutó ninguna acción en Windows.")
                if not remaining:
                    self.test_deadline = None
                    phase = "complete"
                reason = "Prueba sin apagado real"
            elif self.test_ups_simulation is not None:
                phase = self.test_ups_simulation["phase"]
                simulation = True
            actionable_countdown = not simulation and self._countdown_actionable(raw)
            return {
                "csrf_token": self.csrf_token,
                "connection": {"state": self.connection, "source": self.adapter.source,
                               "last_success": self.last_success, "error": self.error, "error_kind": self.error_kind},
                "sample": ({**self.sample, **battery_estimate_fields(self.sample)}
                           if self.sample is not None else None),
                "events": self.events(),
                **recent_records,
                "history": self.history_status(),
                "health": self.health_status(),
                "system_power": self.system_power_status(),
                "policy": dict(self.engine.policy),
                "ups_poweroff": self.ups_power_status(),
                "ups_cancel_test": self.ups_cancel_test_status(),
                "ups_battery_test": self.ups_battery_test_status(),
                "notifications": self.notification_status(),
                "webhooks": self.webhook_status(),
                "protection": {"state": state, "reason": reason,
                               "configured_countdown_seconds": raw.get("configured_countdown_seconds"),
                               "effective_countdown_seconds": raw.get("effective_countdown_seconds"),
                               "emergency": copy.deepcopy(raw.get("emergency")),
                               "phase": phase, "windows_request": self._windows_request,
                               "cancelable": (state == "countdown" and phase == "windows_countdown"
                                              if simulation else actionable_countdown),
                               "countdown_id": raw.get("countdown_id") if actionable_countdown else None,
                               "valid_for_ms": raw.get("countdown_valid_for_ms", 0) if actionable_countdown else 0,
                               "countdown_remaining": remaining, "simulation": simulation,
                               "deadline": iso_utc(datetime.now(timezone.utc) + timedelta(seconds=remaining)) if remaining else None},
                "meta": {"name": "Elipro UPS", "model": "Forza NT-2402PRO", "capacity_watts": 1200,
                         "capacity_va": 2400, "history_interval_seconds": 10, "retention_days": self.store.retention_days,
                         "dry_run": self.power_actions_blocked, "simulation_source": self.simulation_source, "version": __version__,
                         "allow_ups_poweroff": self.allow_ups_poweroff,
                         "runtime_mode": getattr(self, "runtime_mode", "session"),
                         "source": "simulation" if self.simulation_source else (
                             "bridge" if getattr(self.adapter, "bridge_required", True) else "native"),
                         "bridge_required": getattr(self.adapter, "bridge_required", True)},
            }

    def set_policy(self, policy):
        value = validate_policy(policy)
        if value["enabled"] and self.physical_test_pending():
            raise ValueError("Resuelve la prueba física del UPS antes de activar la protección automática.")
        if (value["ups_poweroff"] and (value["enabled"] or not self.engine.policy["ups_poweroff"])
                and not self.ups_power_status(value["action"], policy=value)["can_configure"]):
            raise ValueError("No se puede habilitar el corte del UPS: " + str(self.ups_power_status(value["action"], policy=value)["reason"]))
        # A settings request may wait for disk, but never while holding the
        # telemetry/decision lock. Serialize requests to preserve write order.
        with self.settings_lock:
            if value["enabled"] and self.physical_test_pending():
                raise ValueError("Resuelve la prueba física del UPS antes de activar la protección automática.")
            if not value["enabled"]:
                with self.lock:
                    self.test_deadline = None
                    self.test_ups_delay = None
                    self.test_ups_simulation = None
                    self.engine.update_policy(value)
            try:
                if self._policy_review_required:
                    self.store.set_settings({"policy": value, "policy_migration_review": None})
                else:
                    self.store.set_setting("policy", value)
            except Exception as exc:
                self._history_failed("settings_write", exc)
                # A failed disk must not prevent the user disabling an action.
                if not value["enabled"]:
                    raise HistoryUnavailable("La protección se desactivó en esta sesión, pero no se pudieron guardar los ajustes.") from exc
                raise HistoryUnavailable("No se pudieron guardar los ajustes; la política anterior se mantiene.") from exc
            self._history_failures.pop("settings_write", None)
            if self._policy_review_required:
                self._policy_review_required = False
                self._notify_event("policy_configured", "Configuración de protección revisada.")
            with self.lock:
                self.test_deadline = None
                self.test_ups_delay = None
                self.test_ups_simulation = None
                if value["enabled"]:
                    self.engine.update_policy(value)
                self._event("policy_changed", "Protección automática " + ("activada." if value["enabled"] else "desactivada."))
                return self.snapshot(refresh_recent_records=False)

    def cancel(self):
        with self.lock:
            if self.test_deadline is not None:
                if self.test_ups_delay is not None and time.monotonic() >= self.test_deadline:
                    raise ValueError("La cuenta para cancelar terminó. La espera del UPS solo representa el corte programado.")
                self.test_deadline = None
                self._record_event("simulation", "Prueba cancelada sin acción en Windows.")
                if self.test_ups_simulation is not None:
                    self.test_ups_simulation.update(phase="cancelled", remaining=0, ups_cut_simulated=False)
                self.test_ups_delay = None
            self.engine.cancel()
            return self.snapshot(refresh_recent_records=False)

    def _countdown_actionable(self, raw):
        """Inspect current state under the decision lock, without new USB I/O."""
        return raw.get("cancelable") is True and self._countdown_cancel_context(raw)

    def _countdown_cancel_context(self, raw):
        return (raw.get("state") == "countdown"
                and isinstance(raw.get("countdown_id"), str)
                and not self.power_actions_blocked and self.test_deadline is None
                and not self.stop_event.is_set() and not self._power_gate.is_set()
                and not self._health_fault.is_set() and self.system_power_status()["state"] == "running"
                and self.connection == "connected" and self._fresh_sample(self.sample)
                and self.sample.get("on_battery") is True)

    def _countdown_cancellable_on_request(self, raw):
        """Validate a live request against the current engine, not a UI lease.

        The caller holds the decision lock. Snapshot validity is deliberately
        short, but an authenticated cancellation can stop the same pending
        episode while the normal telemetry and monitor guards still hold.
        """
        remaining = raw.get("countdown_remaining")
        return (type(remaining) in (int, float) and math.isfinite(remaining) and remaining > 0
                and self._countdown_cancel_context(raw))

    def cancel_countdown(self, countdown_id):
        """Cancel exactly one still-current real countdown, never a later one."""
        if (not isinstance(countdown_id, str) or len(countdown_id) != 32
                or any(character not in "0123456789abcdef" for character in countdown_id)):
            raise ValueError("El identificador de la cuenta regresiva no es válido.")
        if not self.lock.acquire(timeout=0.25):
            raise CountdownBusyError("El monitor está ocupado; no se confirmó la cancelación.")
        try:
            current = self.engine.status()
            if current.get("countdown_id") != countdown_id or not self._countdown_cancellable_on_request(current):
                raise CountdownConflictError("La cuenta cambió, terminó o ya no admite cancelación. Revisa el estado actual.")
            self.engine.cancel()
            self._update_alerts()
            result = self.snapshot(refresh_recent_records=False)
            result["cancelled_countdown_id"] = countdown_id
            return result
        finally:
            self.lock.release()

    def test(self, *, include_ups=False):
        with self.lock:
            if self.physical_test_pending():
                raise ValueError("Resuelve la prueba física del UPS antes de iniciar una simulación.")
            if type(include_ups) is not bool:
                raise ValueError("include_ups debe ser booleano")
            if self.engine.policy["enabled"]:
                raise ValueError("Desactiva la protección automática antes de probar el aviso sin apagado.")
            seconds = self.engine.policy["countdown_seconds"]
            self.test_ups_delay = (self.engine.policy["ups_poweroff_delay_seconds"]
                if include_ups and self.engine.policy["ups_poweroff"] else None)
            self.test_ups_simulation = None
            self.test_deadline, self.test_finished = time.monotonic() + seconds, False
            self._record_event("simulation", f"Prueba de aviso iniciada ({seconds} segundos, sin apagado).")
            return self.snapshot(refresh_recent_records=False)


class Handler(BaseHTTPRequestHandler):
    server_version = "EliproUPS/" + __version__

    def log_message(self, *args):
        pass

    @property
    def monitor(self):
        return self.server.monitor

    def _local(self):
        allowed = (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")
        if self.headers.get("Host", "") not in allowed:
            self._json({"error": "Host no permitido."}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in tuple("http://" + host for host in allowed):
            self._json({"error": "Origen no permitido."}, 403)
            return False
        return True

    def _send(self, content, mime, code=200, extra=None):
        self._response_headers(len(content), mime, code, extra)
        self.wfile.write(content)

    def _response_headers(self, length, mime, code=200, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _send_file(self, source, mime, filename):
        source.seek(0, io.SEEK_END)
        length = source.tell()
        source.seek(0)
        self._response_headers(length, mime, extra={"Content-Disposition": f'attachment; filename="{filename}"'})
        while chunk := source.read(65536):
            self.wfile.write(chunk)

    def _events_csv(self, options):
        # Generate completely before sending headers, so a corrupt database
        # returns a clear error instead of a silently truncated download.
        with tempfile.SpooledTemporaryFile(max_size=4_000_000, mode="w+b") as binary:
            text_output = io.TextIOWrapper(binary, encoding="utf-8-sig", newline="")
            try:
                write_events_csv(self.monitor.store, options, text_output)
                text_output.flush()
                return self._send_file(binary, "text/csv; charset=utf-8", "elipro-events.csv")
            finally:
                text_output.detach()

    def _records_csv(self, options):
        with tempfile.SpooledTemporaryFile(max_size=4_000_000, mode="w+b") as binary:
            text_output = io.TextIOWrapper(binary, encoding="utf-8-sig", newline="")
            try:
                write_records_csv(self.monitor.store, options, text_output)
                text_output.flush()
                return self._send_file(binary, "text/csv; charset=utf-8", "elipro-registro.csv")
            finally:
                text_output.detach()

    def _measurements_csv(self):
        with tempfile.SpooledTemporaryFile(max_size=4_000_000, mode="w+b") as binary:
            text_output = io.TextIOWrapper(binary, encoding="utf-8-sig", newline="")
            try:
                # Preserve all existing column positions for consumers of older
                # exports; newly derived metadata is appended after watts.
                fields = (("timestamp",) + tuple(field for field in SAMPLE_FIELDS if field not in BATTERY_ESTIMATE_FIELDS)
                          + ("watts_is_estimate",) + BATTERY_ESTIMATE_FIELDS)
                writer = csv.DictWriter(text_output, fieldnames=fields)
                writer.writeheader()
                for sample in self.monitor.store.iter_samples():
                    writer.writerow({key: csv_cell(value) for key, value in sample.items()})
                text_output.flush()
                return self._send_file(binary, "text/csv; charset=utf-8", "elipro-ups.csv")
            finally:
                text_output.detach()

    def _json(self, value, code=200):
        self._send(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"), "application/json; charset=utf-8", code)

    def do_GET(self):
        if not self._local():
            return
        parsed = urlsplit(self.path)
        try:
            if parsed.path == "/api/status":
                return self._json(self.monitor.snapshot())
            if parsed.path == "/api/events":
                return self._json({"events": self.monitor.events()})
            if parsed.path == "/api/records":
                return self._json(self.monitor.store.records(**archive_options(parsed.query)))
            if parsed.path == "/api/records/export.csv":
                return self._records_csv(archive_options(parsed.query, pagination=False))
            if parsed.path == "/api/events/search":
                return self._json(self.monitor.store.events_page(**archive_options(parsed.query)))
            if parsed.path == "/api/events/export.csv":
                return self._events_csv(archive_options(parsed.query, pagination=False))
            if parsed.path == "/api/incidents":
                return self._json(self.monitor.store.incidents(**archive_options(parsed.query, incidents=True)))
            if parsed.path == "/api/health":
                return self._json(self.monitor.health_status())
            if parsed.path == "/api/notifications":
                return self._json(self.monitor.notification_status())
            if parsed.path == "/api/webhooks":
                return self._json(self.monitor.webhook_status())
            if parsed.path == "/api/history":
                hours = float(parse_qs(parsed.query).get("hours", ["24"])[0])
                return self._json({"samples": self.monitor.store.history(hours)})
            if parsed.path == "/api/export.csv":
                return self._measurements_csv()
            files = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/styles.css": "styles.css", "/style.css": "style.css", "/favicon.ico": "assets/elipro.ico"}
            filename = files.get(parsed.path)
            if filename and (ROOT / "static" / filename).is_file():
                mime = "image/vnd.microsoft.icon" if filename.endswith(".ico") else "text/html" if filename.endswith("html") else "text/javascript" if filename.endswith("js") else "text/css"
                return self._send((ROOT / "static" / filename).read_bytes(), mime if filename.endswith(".ico") else mime + "; charset=utf-8")
            self._json({"error": "No encontrado."}, 404)
        except RecordCursorExpired as exc:
            self._json({"error": str(exc), "code": "record_cursor_expired"}, 400)
        except IncidentCursorExpired as exc:
            self._json({"error": str(exc), "code": "incident_cursor_expired"}, 400)
        except HistoryUnavailable as exc:
            self._json({"error": str(exc), "history": self.monitor.history_status()}, 503)
        except (ValueError, TypeError):
            self._json({"error": "Parámetro no válido."}, 400)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            log_failure("Error interno en GET", exc)
            self._json({"error": "Error interno del monitor. Revisa el registro local."}, 500)

    def do_POST(self):
        if not self._local():
            return
        if not secrets.compare_digest(self.headers.get("X-UPS-Token", "").encode("utf-8"), self.monitor.csrf_token.encode("utf-8")):
            return self._json({"error": "Recarga el panel antes de guardar cambios."}, 403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 4096:
                raise ValueError("Solicitud demasiado grande.")
            if self.headers.get_content_type() != "application/json":
                raise ValueError("Se requiere JSON.")
            data = json.loads(self.rfile.read(length) or b"{}")
            path = urlsplit(self.path).path
            if path == "/api/policy":
                return self._json(self.monitor.set_policy(data))
            if path == "/api/notifications":
                return self._json(self.monitor.set_notifications(data))
            if path == "/api/notifications/test":
                return self._json(self.monitor.test_notifications())
            if path == "/api/notifications/ack":
                return self._json(self.monitor.acknowledge_notifications(data))
            if path == "/api/webhooks":
                return self._json(self.monitor.set_webhooks(data))
            if path == "/api/webhooks/test":
                if not isinstance(data, dict) or data:
                    raise ValueError("La prueba usa la configuración de webhooks guardada.")
                return self._json(self.monitor.test_webhooks())
            if path == "/api/session/poll":
                broker = getattr(self.server, "session_broker", None)
                if broker is None:
                    return self._json({"error": "El monitor no usa un agente de sesión."}, 409)
                return self._json(broker.poll(data))
            if path == "/api/protection/cancel":
                return self._json(self.monitor.cancel())
            if path == "/api/protection/countdown/cancel":
                if not isinstance(data, dict) or set(data) != {"countdown_id"}:
                    raise ValueError("La cancelación identificada requiere únicamente countdown_id.")
                return self._json(self.monitor.cancel_countdown(data["countdown_id"]))
            if path == "/api/ups/cancel-test/start":
                if not isinstance(data, dict) or set(data) != {"acknowledge_risk"} or data["acknowledge_risk"] is not True:
                    raise ValueError("La prueba real requiere confirmación explícita del riesgo.")
                return self._json(self.monitor.run_ups_cancel_test(acknowledge_risk=True))
            if path == "/api/ups/cancel-test/cancel":
                if data != {}:
                    raise ValueError("La cancelación solo admite una operación propia corroborada.")
                return self._json(self.monitor.run_ups_cancel_test(cancel=True))
            if path == "/api/ups/cancel-test/close":
                required = {"operation_id", "output_cut_observed", "pending_signal_not_reported", "manual_start_observed"}
                if (not isinstance(data, dict) or set(data) != required or not isinstance(data.get("operation_id"), str)
                        or any(data[key] is not True for key in required - {"operation_id"})):
                    raise ValueError("El cierre requiere confirmar el corte observado, la señal ausente y el encendido manual.")
                return self._json(self.monitor.close_ups_cancel_test(data))
            if path == "/api/ups/cancel-test/confirm-observation":
                if (not isinstance(data, dict) or set(data) != {"operation_id", "outputs_stayed_on", "independent_load_observed"}
                        or not isinstance(data["operation_id"], str) or type(data["outputs_stayed_on"]) is not bool
                        or data["independent_load_observed"] is not True):
                    raise ValueError("Confirma la observación de una carga independiente después del plazo completo.")
                return self._json(self.monitor.confirm_ups_cancel_observation(data))
            if path == "/api/ups/battery-test/start":
                if not isinstance(data, dict) or set(data) != {"acknowledge_risk"} or data["acknowledge_risk"] is not True:
                    raise ValueError("La prueba en batería requiere confirmación explícita del corte y la hibernación real.")
                return self._json(self.monitor.run_ups_battery_test(acknowledge_risk=True))
            if path == "/api/ups/battery-test/confirm-observation":
                required = {"operation_id", "cut_observed", "ac_restored", "off_after_ac_seconds", "manual_restart",
                            "hibernate_resumed", "acknowledge_observation", "automatic_return_observed"}
                if (not isinstance(data, dict) or set(data) != required or not isinstance(data["operation_id"], str)
                        or any(type(data[key]) is not bool for key in required - {"operation_id", "off_after_ac_seconds"})
                        or data["acknowledge_observation"] is not True
                        or type(data["off_after_ac_seconds"]) not in (int, float)
                        or not math.isfinite(data["off_after_ac_seconds"]) or not 0 <= data["off_after_ac_seconds"] <= 3600):
                    raise ValueError("Registra las observaciones físicas y el tiempo sin tensión después de restaurar la electricidad.")
                return self._json(self.monitor.confirm_ups_battery_observation(data))
            if path == "/api/ups/poweroff/review":
                if (not isinstance(data, dict) or set(data) != {"operation_id", "acknowledge_review"}
                        or not isinstance(data["operation_id"], str) or data["acknowledge_review"] is not True):
                    raise ValueError("Confirma la revisión de la operación propia del UPS.")
                return self._json(self.monitor.complete_ups_review(data))
            if path == "/api/protection/test":
                if not isinstance(data, dict) or set(data) - {"include_ups"}:
                    raise ValueError("La simulación solo admite include_ups.")
                return self._json(self.monitor.test(include_ups=data.get("include_ups", False)))
            if path == "/api/service/stop":
                if self.monitor.physical_test_pending():
                    raise ValueError("No cierres el monitor mientras la prueba del UPS esté pendiente.")
                mark_exit = getattr(self.server, "mark_exit_requested", None)
                if callable(mark_exit):
                    mark_exit()
                self.monitor.stop_event.set()
                callback = getattr(self.server, "request_exit", self.server.shutdown)
                threading.Thread(target=callback, daemon=True).start()
                self._json({"stopped": True})
                return
            return self._json({"error": "No encontrado."}, 404)
        except CountdownConflictError as exc:
            self._json({"error": str(exc)}, 409)
        except CountdownBusyError as exc:
            self._json({"error": str(exc)}, 503)
        except HistoryUnavailable as exc:
            self._json({"error": str(exc), "history": self.monitor.history_status()}, 503)
        except (ValueError, TypeError) as exc:
            self._json({"error": str(exc)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            log_failure("Error interno en POST", exc)
            self._json({"error": "Error interno del monitor. Revisa el registro local."}, 500)


def main(argv=None, *, stop_event=None, ready_callback=None):
    parser = argparse.ArgumentParser(description="Panel local de UPS Elipro")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--forza-url", default="http://127.0.0.1:15179/Forza")
    parser.add_argument("--source", choices=("auto", "native", "bridge", "simulation"), default="auto")
    parser.add_argument("--usb-info", action="store_true", help="Inspecciona metadatos HID sin enviar consultas")
    parser.add_argument("--dry-run", action="store_true", help="Simula las acciones de energía y los webhooks, sin realizar POST")
    parser.add_argument("--diagnose", action="store_true", help="Lee una muestra real y termina")
    parser.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--allow-ups-poweroff", action="store_true", help="Permiso adicional para perfiles UPS físicamente validados")
    args = parser.parse_args(argv)
    if args.allow_ups_poweroff and (not args.headless or args.dry_run or args.source != "native"):
        parser.error("--allow-ups-poweroff requiere servicio nativo y acciones reales de Windows.")
    if args.source == "simulation":
        args.dry_run = True
        if args.usb_info:
            parser.error("La simulación no permite inspeccionar USB")
    if args.usb_info:
        from .hid_windows import enumerate_ups
        print(json.dumps(enumerate_ups(), indent=2, ensure_ascii=False))
        return
    ownership = MonitorInstanceGuard()
    try:
        ownership.acquire()
    except InstanceGuardError as exc:
        parser.exit(2, str(exc) + "\n")
    ownership.user_exit_requested = False
    try:
        if stop_event is None and ready_callback is None:
            _run_owned_monitor(args, ownership)
        else:
            _run_owned_monitor(args, ownership, stop_event=stop_event, ready_callback=ready_callback)
    except Exception as exc:
        if ownership.user_exit_requested is not True:
            raise
        # A requested stop must remain final even when teardown reports a
        # stuck worker or a full disk. Retained mutexes release at process exit.
        log_failure("Elipro se cerró a petición del usuario con un error de limpieza", exc)


def _run_owned_monitor(args, ownership, *, stop_event=None, ready_callback=None):
    """Release process ownership only after the reader and persistence stop."""
    monitor = server = alerts = store = log_handler = None
    safe_to_release = True
    try:
        if args.source == "simulation":
            args.dry_run = True
        adapter = make_adapter(args.source, args.forza_url)
        if args.diagnose:
            print(json.dumps(adapter.read(), indent=2, ensure_ascii=False))
            return
        LOGGER.setLevel(logging.INFO)
        try:
            args.data_dir.mkdir(parents=True, exist_ok=True)
            log_handler = RotatingFileHandler(args.data_dir / "monitor.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
            log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            LOGGER.addHandler(log_handler)
        except OSError as exc:
            # Disk trouble must not prevent live monitoring with safe defaults.
            log_failure("No se pudo abrir el registro local", exc)
        store = ResilientStore.open(args.data_dir / "monitor.sqlite3")
        if getattr(args, "headless", False):
            from .session_broker import SessionBroker
            notifier = SessionBroker()
        else:
            notifier = WindowsNotifier(f"http://127.0.0.1:{args.port}")
        alerts = AlertManager(store, notifier)
        monitor = Monitor(store, adapter, dry_run=args.dry_run, alerts=alerts,
                          allow_ups_poweroff=getattr(args, "allow_ups_poweroff", False),
                          ups_journal_path=args.data_dir / "ups-power-operation.json",
                          ups_cancel_test_journal=args.data_dir / "ups-cancel-test-operation.json",
                          ups_battery_test_journal=args.data_dir / "ups-battery-test-operation.json")
        monitor.runtime_mode = "service" if getattr(args, "headless", False) else "session"
        notifier.cancel_callback = lambda: monitor.cancel() if not monitor.stop_event.is_set() else None
        notifier.cancel_countdown_callback = monitor.cancel_countdown
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        server.monitor = monitor
        server.session_broker = notifier if getattr(args, "headless", False) else None
        def mark_exit_requested():
            if ownership.user_exit_requested is not True:
                ownership.user_exit_requested = True
                LOGGER.info("Salida de Elipro solicitada por el usuario.")
            monitor.stop_event.set()
        def request_exit():
            mark_exit_requested()
            if stop_event is not None:
                stop_event.set()
            request_parent_stop(args.data_dir, args.port)
            server.shutdown()
        server.mark_exit_requested = mark_exit_requested
        server.request_exit = request_exit
        notifier.exit_callback = request_exit
        store.start()
        alerts.start()
        try:
            restart_count = int(os.environ.get("ELIPRO_GUARDIAN_RESTARTS", "0"))
        except ValueError:
            restart_count = 0
        if 0 < restart_count <= 1000:
            monitor._event("monitor_restarted", "Elipro se reinició tras un cierre inesperado. Verificando la conexión con el UPS.",
                           {"restart_count": restart_count})
        monitor.start()
        if stop_event is not None:
            def wait_for_service_stop():
                stop_event.wait()
                monitor.stop_event.set()
                server.shutdown()
            threading.Thread(target=wait_for_service_stop, name="elipro-service-stop", daemon=True).start()
        if ready_callback is not None:
            ready_callback(monitor)
        LOGGER.info("Monitor iniciado (version=%s, runtime_mode=%s, source=%s, port=%d, dry_run=%s).",
                    __version__, monitor.runtime_mode, args.source, args.port, args.dry_run)
        print(f"Elipro UPS disponible en http://127.0.0.1:{args.port}", flush=True)
        print("Protección automática: " + ("activada" if monitor.engine.policy["enabled"] else "desactivada"), flush=True)
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        ownership.user_exit_requested = True
        request_parent_stop(args.data_dir, args.port)
        pass
    finally:
        if monitor is not None:
            monitor.stop_event.set()
        try:
            if monitor is not None:
                monitor.stop()
        finally:
            try:
                if server is not None:
                    server.server_close()
                if alerts is not None:
                    alerts.stop()
                reader_stopped = monitor is None or monitor.thread is None or not monitor.thread.is_alive()
                supervisor = getattr(monitor, "supervisor_thread", None)
                reader_stopped = reader_stopped and (supervisor is None or not supervisor.is_alive())
                processor = getattr(monitor, "processor_thread", None)
                reader_stopped = reader_stopped and (processor is None or not processor.is_alive())
                if not reader_stopped:
                    safe_to_release = False
                elif store is not None:
                    safe_to_release = store.close() is not False
            finally:
                error_type = sys.exc_info()[0]
                reason = ("user_requested" if ownership.user_exit_requested is True else
                          "windows_stop" if stop_event is not None and stop_event.is_set() else
                          "error" if error_type is not None else "server_return")
                LOGGER.info("Monitor finalizado (reason=%s, error_type=%s).",
                            reason, error_type.__name__ if error_type is not None else "none")
                if log_handler is not None:
                    LOGGER.removeHandler(log_handler)
                    log_handler.close()
            # If a worker cannot finish, keep ownership until process exit;
            # Windows then releases its handle, including after a crash.
            if safe_to_release:
                ownership.close()


if __name__ == "__main__":
    main()
