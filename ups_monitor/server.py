"""Loopback dashboard and local protection controller, using only the standard library."""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import math
from logging.handlers import RotatingFileHandler
import os
import secrets
import subprocess
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
from .core import DEFAULT_POLICY, SAMPLE_FIELDS, PolicyEngine, TelemetryRingBuffer, iso_utc, validate_policy
from .alerts import AlertManager, DEFAULT_NOTIFICATION_SETTINGS
from .instance_guard import MonitorInstanceGuard, InstanceGuardError
from .health import MonitorHealth
from .persistence import ResilientStore, HistoryUnavailable
from .windows_notifications import WindowsNotifier
from .reports import backup_archive, csv_cell, diagnostics, save_validation, validation_record
from .stability import StabilityObservation
from .guardian import request_parent_stop
from .runtime import default_data_dir, resource_root

ROOT = resource_root()
LOGGER = logging.getLogger("elipro.monitor")


def log_failure(message, exc):
    # Keep traceback locations, but never log request bodies, tokens or exception payloads.
    LOGGER.error("%s (%s)\n%s", message, type(exc).__name__, "".join(traceback.format_tb(exc.__traceback__)))


def windows_action(action):
    if os.name != "nt":
        raise RuntimeError("La protección solo puede actuar en Windows.")
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "shutdown.exe"
    # Countdown is handled by us. /t > 0 would imply forced app termination.
    args = [str(executable), "/s", "/t", "0"] if action == "shutdown" else [str(executable), "/h"]
    result = subprocess.run(args, capture_output=True, timeout=10,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError("Windows rechazó la acción. Revisa permisos o disponibilidad de hibernación.")


def archive_options(query, *, incidents=False, pagination=True):
    values = parse_qs(query, keep_blank_values=True)
    allowed = {"from", "to"} | (set() if incidents else {"kind", "q"})
    if pagination:
        allowed |= {"limit", "cursor"}
    if set(values) - allowed or any(len(value) != 1 for value in values.values()):
        raise ValueError("Parámetros de historial no válidos.")
    result = {"start": values.get("from", [""])[0] or None,
              "end": values.get("to", [""])[0] or None}
    if not incidents:
        result.update(kind=values.get("kind", [""])[0] or None,
                      query=values.get("q", [""])[0] or None)
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


class Monitor:
    def __init__(self, store, adapter, *, dry_run=False, action_callback=None, alerts=None, health_clock=None,
                 telemetry_capacity=256, poll_interval=2):
        self.store, self.adapter, self.dry_run = store, adapter, dry_run
        self.alerts = alerts
        self.lock = threading.RLock()
        self.settings_lock = threading.Lock()
        self._history_failures = {}
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
        self._health_fault = threading.Event()
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
        self.test_deadline = None
        self.test_finished = False
        self.csrf_token = secrets.token_urlsafe(32)
        self.action_callback = action_callback or windows_action
        try:
            policy = validate_policy(store.get_setting("policy", DEFAULT_POLICY))
        except Exception as exc:
            self._history_failed("settings", exc)
            policy = dict(DEFAULT_POLICY)
        self.engine = PolicyEngine(policy,
                                   self._act, event_callback=self._policy_event)

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
        return self._record_event(kind, message, data)

    def _notify_event(self, kind, message, data=None):
        if self.alerts is not None and not self.stop_event.is_set():
            try:
                self.alerts.on_event(kind, message, data)
            except Exception as exc:
                log_failure("No se pudo preparar un aviso local", exc)

    def _record_event(self, kind, message, data=None):
        try:
            event = self.store.add_event(kind, message, data)
            self._history_failures.pop("events_write", None)
            return event
        except Exception as exc:
            self._history_failed("events_write", exc)
            return None

    def _update_alerts(self):
        if self.alerts is not None and not self.stop_event.is_set():
            try:
                protection = {**self.engine.status(), "simulation": self.dry_run}
                self.alerts.update(protection, {"state": self.connection}, self.sample)
            except Exception as exc:
                log_failure("No se pudieron actualizar los avisos locales", exc)

    def notification_status(self):
        if self.alerts is None:
            return {"settings": dict(DEFAULT_NOTIFICATION_SETTINGS),
                    "delivery": {"available": False, "ready": False, "channel": "disabled",
                                 "last_error": "Los avisos locales no están inicializados."},
                    "last_notification": None, "last_error": None}
        return self.alerts.status()

    def set_notifications(self, settings):
        if self.alerts is None or self.stop_event.is_set():
            raise ValueError("Los avisos locales no están disponibles.")
        self.alerts.update_settings(settings)
        return self.snapshot()

    def test_notifications(self):
        if self.alerts is None or self.stop_event.is_set():
            raise ValueError("Los avisos locales no están disponibles.")
        self.alerts.test()
        return self.snapshot()

    def _policy_event(self, kind, message, data):
        if self.dry_run and kind == "policy_action_started":
            return
        # Routine online/disabled states add no noise to the incident history.
        if kind not in ("policy_online", "policy_disabled", "policy_on_battery"):
            if kind == "policy_countdown":
                data = {**(data or {}), **self.engine.status(), "simulation": self.dry_run}
            self._event(kind, message, data)

    def _act(self, action):
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
        if self.dry_run:
            self._event("simulation", "Modo de desarrollo: acción simulada; Windows sigue encendido.")
        else:
            if not self._fresh_sample(self.sample):
                raise RuntimeError("Sin telemetría reciente; acción bloqueada.")
            if self.stop_event.is_set():
                raise RuntimeError("Monitor detenido; acción bloqueada.")
            self.action_callback(action)

    @staticmethod
    def _fresh_sample(sample):
        try:
            stamp = datetime.fromisoformat(sample["timestamp"].replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
            return -5 <= age <= 15 and sample.get("connected", True) is True
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            return False

    def _unavailable(self, message, state="disconnected", kind="telemetry", *, notify=True):
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

    def _check_health(self):
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
        supervisor_failed = self.supervisor_thread is not None and not self.supervisor_thread.is_alive()
        processor_failed = self.processor_thread is not None and not self.processor_thread.is_alive()
        status.update({
            "reader_alive": self.thread is not None and self.thread.is_alive(),
            "supervisor_alive": self.supervisor_thread is not None and self.supervisor_thread.is_alive(),
            "state": "stopped" if self.stop_event.is_set() else (
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
        observation = getattr(self, "observation", None)
        if observation is not None:
            status["observation"] = observation.status()
        return status

    def _health_problem(self):
        """Inspect independent counters without waiting for the reader's lock."""
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
        problem = self._health_problem()
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
        if not self._read_lock.acquire(blocking=False):
            raise RuntimeError("Ya hay una lectura de hardware en curso")
        try:
            self.health.begin_read()
            try:
                sample = self.adapter.read()
            except Exception as exc:
                known = isinstance(exc, (TelemetryError, OSError, ValueError))
                kind = "telemetry" if known else "backend"
                self.health.finish_read(False, error_kind=kind)
                if not known:
                    raise
                return None, str(exc), kind
            fresh = self._fresh_sample(sample)
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
        sample, error, kind = self._read_hardware()
        if error is not None:
            with self.lock:
                if not self.stop_event.is_set():
                    self._unavailable(error, "stale" if "obsoleto" in error else "disconnected", kind)
            return
        self._process_sample(sample)

    def _process_sample(self, sample):
        with self.lock:
            if self.stop_event.is_set():
                return
            if self._processing_sequence is not None and not self.telemetry.is_latest(self._processing_sequence):
                # A result superseded while waiting for this lock cannot reset
                # manual cancellation or finish a protected deadline.
                self.engine.evaluate(None)
                return
            if not self._fresh_sample(sample):
                self.health.problem("stale")
                self._unavailable("La lectura no contiene una fecha reciente y válida. Protección suspendida.", "stale")
                return
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
            if self.connection != "connected":
                self._event("connection_restored", "Telemetría del UPS conectada.")
            self.connection, self.error, self.error_kind = "connected", None, None
            previous_sample = self.sample
            self.sample, self.last_success = sample, sample["timestamp"]
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
            low = sample.get("low_battery") is True or (
                sample.get("battery_percent") is not None and sample["battery_percent"] <= 20)
            overload = sample.get("load_percent") is not None and sample["load_percent"] >= 100
            if low and not self.low:
                self._event("battery_low", "El UPS reporta batería baja o carga de batería ≤ 20 %.")
            if overload and not self.overload:
                self._event("overload", "La carga reportada alcanzó o superó el 100 %.")
            self.low, self.overload = low, overload
            # Decisions use fresh telemetry even if recording it fails.
            self.engine.evaluate(sample)
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
            try:
                self._next_poll_at = None
                sample, error, kind = self._read_hardware()
                if self.stop_event.is_set():
                    return
                self.telemetry.publish(sample, error=error, error_kind=kind,
                                       captured_at=self.health.clock())
                self._poll_failures = self._poll_failures + 1 if error is not None else 0
            except Exception as exc:
                self._poll_failures += 1
                self.telemetry.publish(error="Error interno al leer telemetría. Protección suspendida.",
                                       error_kind="backend", captured_at=self.health.clock())
                # Do not touch the logfile from hardware polling. The consumer
                # records the bounded backend error through its normal path.
            delay = self.poll_interval if not self._poll_failures else min(
                30, self.poll_interval * (2 ** min(self._poll_failures - 1, 10)))
            retry_after = getattr(self.adapter, "retry_after_seconds", 0)
            if type(retry_after) in (int, float) and math.isfinite(retry_after):
                delay = max(delay, retry_after)
            self._next_poll_at = self.health.clock() + delay
            self.stop_event.wait(delay)

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
                    self._unavailable(latest["error"], "disconnected", latest["error_kind"] or "telemetry")
                elif (latest["captured_at"] is not None and self.health.clock() - latest["captured_at"] > 15):
                    self._unavailable("La entrega de telemetría tiene más de 15 segundos. Protección suspendida.", "stale")
                else:
                    self._process_sample(latest["sample"])
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
        self._last_processed_at = self.health.clock()
        self.processor_thread = threading.Thread(target=self.consume, name="ups-telemetry-consumer", daemon=True)
        self.thread = threading.Thread(target=self.run, name="ups-hardware-polling", daemon=True)
        self.processor_thread.start()
        self.supervisor_thread = threading.Thread(target=self.supervise, name="ups-supervisor", daemon=True)
        self.thread.start()
        self.supervisor_thread.start()

    def stop(self):
        self.stop_event.set()
        self._health_fault.set()
        if self.lock.acquire(timeout=1):
            try:
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

    def events(self):
        try:
            events = self.store.events(100)
            self._history_failures.pop("events_read", None)
        except Exception as exc:
            self._history_failed("events_read", exc)
            events = []
        for event in events:
            kind = event["kind"]
            event["severity"] = "critical" if kind in ("overload", "ups_fault", "policy_action_failed", "monitor_error") else (
                "warning" if kind in ("power_lost", "battery_low", "connection_lost", "policy_countdown", "policy_unknown") else "info")
        return events

    def snapshot(self):
        with self.lock:
            self._check_health()
            raw = self.engine.status()
            state = {"countdown": "countdown", "cancelled": "cancelled", "unknown": "blocked",
                     "action_failed": "blocked", "action_started": "executed"}.get(raw["state"], "idle")
            remaining = raw["countdown_remaining"]
            notice = self.test_deadline is not None and raw["state"] != "countdown"
            simulation = self.dry_run or notice
            reason = raw["reason"]
            if self.dry_run and raw["state"] == "action_started":
                reason = "Acción simulada; Windows sigue encendido."
            if notice:
                remaining = max(0, int(self.test_deadline - time.monotonic() + 0.999))
                state = "countdown" if remaining else "idle"
                if not remaining and not self.test_finished:
                    self.test_finished = True
                    self._record_event("simulation", "Prueba finalizada. No se ejecutó ninguna acción en Windows.")
                if not remaining:
                    self.test_deadline = None
                reason = "Prueba sin apagado real"
            return {
                "csrf_token": self.csrf_token,
                "connection": {"state": self.connection, "source": self.adapter.source,
                               "last_success": self.last_success, "error": self.error, "error_kind": self.error_kind},
                "sample": self.sample,
                "events": self.events(),
                "history": self.history_status(),
                "health": self.health_status(),
                "policy": dict(self.engine.policy),
                "notifications": self.notification_status(),
                "protection": {"state": state, "reason": reason,
                               "countdown_remaining": remaining, "simulation": simulation,
                               "deadline": iso_utc(datetime.now(timezone.utc) + timedelta(seconds=remaining)) if remaining else None},
                "meta": {"name": "Elipro UPS", "model": "Forza NT-2402PRO", "capacity_watts": 1200,
                         "capacity_va": 2400, "history_interval_seconds": 10, "retention_days": self.store.retention_days,
                         "dry_run": self.dry_run, "version": __version__,
                         "runtime_mode": getattr(self, "runtime_mode", "session"),
                         "source": "bridge" if getattr(self.adapter, "bridge_required", True) else "native",
                         "bridge_required": getattr(self.adapter, "bridge_required", True)},
            }

    def set_policy(self, policy):
        value = validate_policy(policy)
        # A settings request may wait for disk, but never while holding the
        # telemetry/decision lock. Serialize requests to preserve write order.
        with self.settings_lock:
            if not value["enabled"]:
                with self.lock:
                    self.test_deadline = None
                    self.engine.update_policy(value)
            try:
                self.store.set_setting("policy", value)
            except Exception as exc:
                self._history_failed("settings_write", exc)
                # A failed disk must not prevent the user disabling an action.
                if not value["enabled"]:
                    raise HistoryUnavailable("La protección se desactivó en esta sesión, pero no se pudieron guardar los ajustes.") from exc
                raise HistoryUnavailable("No se pudieron guardar los ajustes; la política anterior se mantiene.") from exc
            self._history_failures.pop("settings_write", None)
            with self.lock:
                self.test_deadline = None
                if value["enabled"]:
                    self.engine.update_policy(value)
                observation = getattr(self, "observation", None)
                if observation is not None:
                    try:
                        observation.record_policy_change(value["enabled"], self.dry_run)
                    except Exception as exc:
                        log_failure("No se pudo registrar el cambio de política en la observación", exc)
                self._event("policy_changed", "Protección automática " + ("activada." if value["enabled"] else "desactivada."))
                return self.snapshot()

    def cancel(self):
        with self.lock:
            if self.test_deadline is not None:
                self.test_deadline = None
                self._record_event("simulation", "Prueba cancelada sin acción en Windows.")
            self.engine.cancel()
            return self.snapshot()

    def test(self):
        with self.lock:
            if self.engine.policy["enabled"]:
                raise ValueError("Desactiva la protección automática antes de probar el aviso sin apagado.")
            self.test_deadline, self.test_finished = time.monotonic() + 15, False
            self._record_event("simulation", "Prueba de aviso iniciada (15 segundos, sin apagado).")
            return self.snapshot()


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

    def _measurements_csv(self):
        with tempfile.SpooledTemporaryFile(max_size=4_000_000, mode="w+b") as binary:
            text_output = io.TextIOWrapper(binary, encoding="utf-8-sig", newline="")
            try:
                writer = csv.DictWriter(text_output, fieldnames=("timestamp",) + SAMPLE_FIELDS + ("watts_is_estimate",))
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
            if parsed.path == "/api/events/search":
                return self._json(self.monitor.store.events_page(**archive_options(parsed.query)))
            if parsed.path == "/api/events/export.csv":
                return self._events_csv(archive_options(parsed.query, pagination=False))
            if parsed.path == "/api/incidents":
                return self._json(self.monitor.store.incidents(**archive_options(parsed.query, incidents=True)))
            if parsed.path == "/api/health":
                return self._json(self.monitor.health_status())
            if parsed.path == "/api/validation":
                return self._json(validation_record(self.monitor))
            if parsed.path == "/api/stability":
                return self._json(self.monitor.observation.status())
            if parsed.path == "/api/stability/report":
                payload = json.dumps(self.monitor.observation.report(), ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
                return self._send(payload, "application/json; charset=utf-8",
                                  extra={"Content-Disposition": 'attachment; filename="elipro-stability.json"'})
            if parsed.path == "/api/diagnostics":
                payload = json.dumps(diagnostics(self.monitor), ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
                return self._send(payload, "application/json; charset=utf-8",
                                  extra={"Content-Disposition": 'attachment; filename="elipro-diagnostics.json"'})
            if parsed.path == "/api/backup.zip":
                return self._send(backup_archive(self.monitor), "application/zip",
                                  extra={"Content-Disposition": 'attachment; filename="elipro-backup.zip"'})
            if parsed.path == "/api/notifications":
                return self._json(self.monitor.notification_status())
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
            if path == "/api/session/poll":
                broker = getattr(self.server, "session_broker", None)
                if broker is None:
                    return self._json({"error": "El monitor no usa un agente de sesión."}, 409)
                return self._json(broker.poll(data))
            if path == "/api/protection/cancel":
                return self._json(self.monitor.cancel())
            if path == "/api/protection/test":
                return self._json(self.monitor.test())
            if path == "/api/validation/run":
                from .validation import run_controlled
                result = run_controlled()
                result["saved"] = save_validation(self.monitor, result)
                return self._json(result)
            if path == "/api/stability/start":
                if not isinstance(data, dict) or set(data) - {"hours"}:
                    raise ValueError("La observación solo admite la duración en horas.")
                if self.monitor.stop_event.is_set():
                    raise ValueError("El monitor se está deteniendo.")
                return self._json(self.monitor.observation.start(hours=data.get("hours", 48)))
            if path == "/api/stability/stop":
                return self._json(self.monitor.observation.stop())
            if path == "/api/service/stop":
                mark_exit = getattr(self.server, "mark_exit_requested", None)
                if callable(mark_exit):
                    mark_exit()
                self.monitor.stop_event.set()
                callback = getattr(self.server, "request_exit", self.server.shutdown)
                threading.Thread(target=callback, daemon=True).start()
                self._json({"stopped": True})
                return
            return self._json({"error": "No encontrado."}, 404)
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
    parser.add_argument("--source", choices=("auto", "native", "bridge"), default="auto")
    parser.add_argument("--usb-info", action="store_true", help="Inspecciona metadatos HID sin enviar consultas")
    parser.add_argument("--dry-run", action="store_true", help="Bloquea acciones reales en Windows para desarrollo")
    parser.add_argument("--diagnose", action="store_true", help="Lee una muestra real y termina")
    parser.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--observe-hours", type=float, default=0,
                        help="Inicia una observación de estabilidad (0 desactiva; hasta 168 horas)")
    args = parser.parse_args(argv)
    if not math.isfinite(args.observe_hours) or not (args.observe_hours == 0 or 0.01 <= args.observe_hours <= 168):
        parser.error("--observe-hours debe ser 0 o un valor entre 0.01 y 168")
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
    monitor = server = alerts = store = log_handler = observation = None
    safe_to_release = True
    try:
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
        monitor = Monitor(store, adapter, dry_run=args.dry_run, alerts=alerts)
        monitor.runtime_mode = "service" if getattr(args, "headless", False) else "session"
        observation = StabilityObservation(monitor, args.data_dir / "stability")
        monitor.observation = observation
        notifier.cancel_callback = lambda: monitor.cancel() if not monitor.stop_event.is_set() else None
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        server.monitor = monitor
        server.session_broker = notifier if getattr(args, "headless", False) else None
        def mark_exit_requested():
            ownership.user_exit_requested = True
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
        if args.observe_hours:
            observation.start(hours=args.observe_hours)
        if stop_event is not None:
            def wait_for_service_stop():
                stop_event.wait()
                monitor.stop_event.set()
                server.shutdown()
            threading.Thread(target=wait_for_service_stop, name="elipro-service-stop", daemon=True).start()
        if ready_callback is not None:
            ready_callback()
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
            if observation is not None:
                observation.close(timeout=2)
            if monitor is not None:
                monitor.stop()
        finally:
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
            if log_handler is not None:
                LOGGER.removeHandler(log_handler)
                log_handler.close()
            # If a worker cannot finish, keep ownership until process exit;
            # Windows then releases its handle, including after a crash.
            if safe_to_release:
                ownership.close()


if __name__ == "__main__":
    main()
