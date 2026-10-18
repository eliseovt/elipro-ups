"""Bounded daily-use observation. This observer never reads USB or acts on Windows."""
from __future__ import annotations

from collections import Counter
import json
import math
from pathlib import Path
import secrets
import threading
import time

from .core import iso_utc


_FLAGS = {
    "health_unavailable": 1, "history_unavailable": 2, "reader_not_alive": 4,
    "supervisor_not_alive": 8, "stale_read": 16, "monitor_degraded": 32,
    "policy_enabled": 64, "policy_unknown": 128, "real_actions_enabled": 256,
    "counter_reset": 512, "history_records_dropped": 1024, "sample_gap": 2048,
    "awaiting_first_read": 4096,
}
_COLUMNS = ["elapsed_seconds", "read_age_seconds", "read_failures_delta", "history_dropped_delta",
            "memory_bytes", "cpu_percent", "flags"]


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _counter(value):
    return value if type(value) is int and value >= 0 else None


class StabilityObservation:
    """Observe cached monitor health, keeping file I/O off all protection threads.

    Completion only means the requested observation time elapsed. Every report
    requires review; interruptions, missing samples and unsafe policy conditions
    remain explicit. Old reports are never replaced by a later observation.
    """

    MAX_RECORDS = 5000

    def __init__(self, monitor, directory, *, clock=None, interval_seconds=60):
        if type(interval_seconds) not in (int, float) or not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("El intervalo de observación debe ser positivo.")
        self.monitor, self.directory = monitor, Path(directory)
        self.clock = clock or time.monotonic
        self.interval_seconds = float(interval_seconds)
        self.lock = threading.RLock()
        self.thread = None
        self._stop = threading.Event()
        self._state = "idle"
        self._reset()

    def _reset(self):
        self._started_at = self._finished_at = self._started_monotonic = None
        self._finished_elapsed = self._target_seconds = self._samples = 0
        self._max_age = self._memory_peak = self._cpu_peak = None
        self._read_failures = self._history_dropped = 0
        self._last_read_counter = self._last_drop_counter = None
        self._collection_error = self._persistence_error = None
        self._rows, self._last_row, self._record_stride = [], None, 1
        self._gaps, self._gap_count = [], 0
        self._issue_counts, self._unprotected = Counter(), set()
        self._report_id = self._report_path = None

    def _elapsed(self):
        if self._started_monotonic is None:
            return 0
        if self._state != "running":
            return self._finished_elapsed
        return max(0, self.clock() - self._started_monotonic)

    def status(self):
        """Read memory only; never advance state or write a report from a GET."""
        with self.lock:
            return {
                "state": self._state, "started_at": self._started_at,
                "elapsed_seconds": round(self._elapsed(), 1), "target_seconds": self._target_seconds,
                "samples": self._samples, "max_read_age_seconds": self._max_age,
                "read_failures": self._read_failures, "history_dropped": self._history_dropped,
                "memory_peak_bytes": self._memory_peak, "cpu_peak_percent": self._cpu_peak,
                "last_error": self._persistence_error or self._collection_error,
                "report_available": self._samples > 0,
            }

    def start(self, hours=48):
        if type(hours) not in (int, float) or not math.isfinite(hours) or not 0.01 <= hours <= 168:
            raise ValueError("La observación debe durar entre 0,01 y 168 horas.")
        with self.lock:
            monitor_stop = getattr(self.monitor, "stop_event", None)
            if monitor_stop is not None and monitor_stop.is_set() is True:
                raise ValueError("El monitor se está deteniendo.")
            if self._state == "running" or self.thread is not None and self.thread.is_alive():
                raise ValueError("Ya hay una observación en ejecución.")
            self._reset()
            self._state = "running"
            self._started_at, self._started_monotonic = iso_utc(), self.clock()
            self._target_seconds = float(hours) * 3600
            self._report_id = self._started_at.replace(":", "").replace("-", "").replace(".", "") + "-" + secrets.token_hex(4)
            self._report_path = self.directory / ("stability-" + self._report_id + ".json")
            self._stop = threading.Event()
            self.thread = threading.Thread(target=self._run, name="ups-stability", daemon=True)
            self.thread.start()
            return self.status()

    def _collect(self):
        flags = set()
        try:
            health = self.monitor.health_status()
            if not isinstance(health, dict):
                raise TypeError("health")
            collection_error = None
        except Exception as exc:
            health = {}
            flags.add("health_unavailable")
            collection_error = "No se pudo consultar la salud del monitor (" + type(exc).__name__ + ")."
        try:
            history = self.monitor.history_status()
            if not isinstance(history, dict):
                raise TypeError("history")
        except Exception:
            history = {}
        try:
            enabled = self.monitor.engine.policy.get("enabled")
        except (AttributeError, TypeError):
            enabled = None
        dry_run = getattr(self.monitor, "dry_run", None)
        if enabled is True:
            flags.add("policy_enabled")
            if dry_run is not True:
                flags.add("real_actions_enabled")
        elif enabled is not False:
            flags.add("policy_unknown")
        if health.get("reader_alive") is not True:
            flags.add("reader_not_alive")
        if health.get("supervisor_alive") is not True:
            flags.add("supervisor_not_alive")
        age = _number(health.get("read_age_seconds"))
        starting = (health.get("state") == "starting" and health.get("read_successes") == 0
                    and self.clock() - self._started_monotonic <= 15)
        if starting:
            flags.add("awaiting_first_read")
        elif age is None or age > 15:
            flags.add("stale_read")
        if health.get("state") != "healthy" and not starting:
            flags.add("monitor_degraded")
        if history.get("available") is not True:
            flags.add("history_unavailable")
        process = health.get("process")
        process = process if isinstance(process, dict) else {}
        memory = _counter(process.get("memory_bytes"))
        cpu = _number(process.get("cpu_percent"))
        read_counter = _counter(health.get("read_failures"))
        drop_counter = _counter(history.get("dropped"))
        with self.lock:
            elapsed = round(max(0, self.clock() - self._started_monotonic), 3)
            self._collection_error = collection_error
            read_delta, self._last_read_counter = self._delta(read_counter, self._last_read_counter, flags)
            drop_delta, self._last_drop_counter = self._delta(drop_counter, self._last_drop_counter, flags)
            self._read_failures += read_delta
            self._history_dropped += drop_delta
            if drop_delta:
                flags.add("history_records_dropped")
            gap_limit = max(5, self.interval_seconds * 1.75)
            previous_elapsed = self._last_row[0] if self._last_row is not None else 0
            if elapsed - previous_elapsed > gap_limit:
                flags.add("sample_gap")
                self._gap_count += 1
                self._gaps.append({"from_seconds": previous_elapsed, "to_seconds": elapsed})
                self._gaps = self._gaps[-256:]
            self._samples += 1
            self._issue_counts.update(flags)
            self._unprotected.update(flags.intersection(("policy_enabled", "policy_unknown", "real_actions_enabled")))
            if age is not None:
                self._max_age = age if self._max_age is None else max(self._max_age, age)
            if memory is not None:
                self._memory_peak = memory if self._memory_peak is None else max(self._memory_peak, memory)
            if cpu is not None:
                self._cpu_peak = cpu if self._cpu_peak is None else max(self._cpu_peak, cpu)
            row = [elapsed, age, read_delta, drop_delta, memory, cpu, sum(_FLAGS[flag] for flag in flags)]
            self._last_row = row
            if (self._samples - 1) % self._record_stride == 0:
                self._rows.append(row)
                if len(self._rows) > self.MAX_RECORDS:
                    self._rows = self._rows[::2]
                    self._record_stride *= 2

    def record_policy_change(self, enabled, dry_run):
        """Latch unsafe settings between samples, without disk or monitor calls."""
        flags = set()
        if enabled is True:
            flags.add("policy_enabled")
            if dry_run is not True:
                flags.add("real_actions_enabled")
        elif enabled is not False:
            flags.add("policy_unknown")
        if not flags:
            return
        with self.lock:
            if self._state != "running":
                return
            self._issue_counts.update(flags)
            self._unprotected.update(flags)

    @staticmethod
    def _delta(current, previous, flags):
        if current is None:
            return 0, previous
        if previous is None:
            return 0, current
        if current < previous:
            flags.add("counter_reset")
            return current, current
        return current - previous, current

    def report(self):
        with self.lock:
            rows = [list(row) for row in self._rows]
            if self._last_row is not None and (not rows or rows[-1] != self._last_row):
                if len(rows) >= self.MAX_RECORDS:
                    rows.pop(-1)
                rows.append(list(self._last_row))
            complete = (self._state == "completed_pending_review" and self._finished_elapsed >= self._target_seconds
                        and self._samples >= 2 and self._gap_count == 0
                        and not self._issue_counts.get("health_unavailable")
                        and bool(rows) and rows[0][0] <= max(5, self.interval_seconds * 1.75))
            return {
                "schema_version": 1, "report_id": self._report_id,
                "snapshot_semantics": "last_recorded_state",
                "summary": self.status(), "finished_at": self._finished_at,
                "review_required": True, "coverage_complete": complete,
                "validation": "pending_review" if self._state == "completed_pending_review" else "incomplete",
                "safe_test_conditions": not self._unprotected,
                "unprotected_conditions": sorted(self._unprotected),
                "issues": dict(self._issue_counts), "sample_gap_count": self._gap_count,
                "sample_gaps": [dict(gap) for gap in self._gaps],
                "expected_interval_seconds": self.interval_seconds,
                "observations": {"columns": list(_COLUMNS), "rows": rows,
                                 "record_stride": self._record_stride, "flag_bits": dict(_FLAGS)},
                "limits": {"max_records": self.MAX_RECORDS, "max_sample_gaps": 256,
                           "scope": "Salud del proceso y del historial; no comprueba autonomía ni acciones reales de Windows.",
                           "crash_reporting": "Tras una caída abrupta, el último informe puede conservar el estado running. "
                                              "Ese estado describe la última muestra guardada y no demuestra que el proceso siga activo."},
            }

    def _persist(self):
        # Copy state before filesystem operations. Neither status() nor the UPS
        # monitor needs to wait for a slow, full or unavailable disk.
        report = self.report()
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = self._report_path.with_suffix(".json.tmp")
            with temporary.open("w", encoding="utf-8", newline="\n") as file:
                json.dump(report, file, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                file.write("\n")
            temporary.replace(self._report_path)
        except Exception as exc:
            with self.lock:
                self._persistence_error = "No se pudo guardar el informe de estabilidad (" + type(exc).__name__ + ")."
        else:
            with self.lock:
                self._persistence_error = None

    def _finish(self, state):
        with self.lock:
            if self._state != "running":
                return
            self._finished_elapsed = max(0, self.clock() - self._started_monotonic)
            self._finished_at = iso_utc()
            self._state = state

    def _run(self):
        try:
            while not self._stop.is_set():
                self._collect()
                with self.lock:
                    remaining = self._target_seconds - self._elapsed()
                if remaining <= 0:
                    self._finish("completed_pending_review")
                    break
                self._persist()
                self._stop.wait(min(self.interval_seconds, remaining))
        except Exception as exc:
            with self.lock:
                self._collection_error = "La observación se interrumpió (" + type(exc).__name__ + ")."
        finally:
            self._finish("interrupted")
            self._persist()

    def close(self, timeout=2):
        with self.lock:
            self._finish("interrupted")
            self._stop.set()
            thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0, min(float(timeout), 10)))
        return thread is None or not thread.is_alive()

    def stop(self):
        self.close()
        return self.status()
