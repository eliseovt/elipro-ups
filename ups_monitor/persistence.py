"""Bounded, best-effort history writes outside the protection thread.

Telemetry and events are queued in memory; a slow or unavailable SQLite database
never owns the state lock used by the monitor. Configuration writes are explicit
and synchronous: callers receive an error unless the value really was committed.
"""
from __future__ import annotations

import copy
import itertools
import json
import math
import threading
import time
from collections import deque
from dataclasses import dataclass

from .alerts import CRITICAL_HISTORY_EVENTS, DEFAULT_NOTIFICATION_SETTINGS, validate_notification_settings
from .core import (DEFAULT_POLICY, SAMPLE_FIELDS, IncidentCursorExpired, RecordCursorExpired, Store, _timestamp,
                   battery_estimate_fields, iso_utc, validate_policy)


class HistoryUnavailable(RuntimeError):
    """The requested history/configuration operation could not be completed."""


@dataclass(eq=False)
class _Write:
    kind: str
    value: dict
    queued_at: float
    urgent: bool = False


class ResilientStore:
    """Store-compatible facade with an independent, bounded persistence worker.

    Call ``open(path)``, then ``start()`` before starting the monitor. ``events``
    and ``status`` only read memory. ``close(timeout)`` returns True only when the
    worker stopped and all accepted writes were flushed; a still-running worker
    keeps ownership of its connection and closes it itself after its operation.

    Continuous records share transactions of up to 32 entries and wait at most
    30 seconds. Electrical transitions/alarms bypass that wait; a failed disk
    keeps its independent retry backoff. Configuration stays synchronous.
    """

    @classmethod
    def open(cls, path, *, store_factory=None, **options):
        factory = store_factory or (lambda value: Store(value, busy_timeout_ms=100))
        result = cls(path, store_factory=factory, **options)
        result._open_initial()
        return result

    def __init__(self, path, *, store_factory=None, capacity=512, event_limit=1000,
                 retry_seconds=2.0, max_retry_seconds=30.0, batch_size=32,
                 batch_latency_seconds=30.0, clock=time.monotonic):
        if type(capacity) is not int or capacity < 1:
            raise ValueError("capacity debe ser un entero positivo")
        if type(event_limit) is not int or not 1 <= event_limit <= 1000:
            raise ValueError("event_limit debe estar entre 1 y 1000")
        if retry_seconds <= 0 or max_retry_seconds < retry_seconds:
            raise ValueError("Intervalo de reintento inválido")
        if type(batch_size) is not int or not 1 <= batch_size <= 512:
            raise ValueError("batch_size debe ser entero entre 1 y 512")
        if (type(batch_latency_seconds) not in (int, float) or
                not math.isfinite(batch_latency_seconds) or not 0 < batch_latency_seconds <= 60):
            raise ValueError("batch_latency_seconds debe estar entre 0 y 60 segundos")
        self.path = path
        self._factory = store_factory or (lambda value: Store(value, busy_timeout_ms=100))
        self._capacity, self._clock = capacity, clock
        self._batch_size = min(batch_size, capacity)
        self._batch_latency = batch_latency_seconds
        self._retry_seconds, self._max_retry_seconds = retry_seconds, max_retry_seconds
        self._retry_delay, self._retry_at = retry_seconds, 0.0
        self._condition = threading.Condition(threading.RLock())
        # No storage operation is performed while holding _condition.
        self._io_lock = threading.Lock()
        self._queue, self._events = deque(), deque(maxlen=event_limit)
        self._settings = {}
        self._next_event_id = -1
        self._backend = self._thread = None
        self._inflight = ()
        self._last_sample_state = None
        self._closing = self._closed = False
        self._close_deadline = None
        self._dropped = 0
        self._error = self._last_failure_at = self._last_success_at = None
        self._errors = {}
        self._configuration_warnings = []
        self._needs_probe = False
        self._drained = True

    @property
    def retention_days(self):
        with self._condition:
            return getattr(self._backend, "retention_days", 30)

    def _failed(self, exc, operation="write"):
        with self._condition:
            self._errors[operation] = str(exc)[:300] or type(exc).__name__
            self._error = self._errors[operation]
            self._last_failure_at = iso_utc()
            self._needs_probe = any(key in self._errors for key in ("open", "read", "events"))
            self._retry_at = self._clock() + self._retry_delay
            self._retry_delay = min(self._max_retry_seconds, self._retry_delay * 2)
            self._condition.notify_all()

    def _succeeded(self, operation="write"):
        with self._condition:
            self._errors.pop(operation, None)
            self._error = next(reversed(self._errors.values()), None)
            self._needs_probe = any(key in self._errors for key in ("open", "read", "events"))
            self._last_success_at = iso_utc()
            if not self._errors:
                self._retry_delay, self._retry_at = self._retry_seconds, 0.0

    def _open_initial(self):
        try:
            self._backend = self._factory(self.path)
        except Exception as exc:
            self._failed(exc, "open")
        if self._backend is not None:
            try:
                records = self._backend.events(self._events.maxlen)
                with self._condition:
                    self._events.extend(copy.deepcopy(records))
                self._succeeded("events")
            except Exception as exc:
                self._failed(exc, "events")
        # Capture settings exactly once. Recovery must not silently enable a
        # previously unreadable policy while the monitor is already running.
        self._load_setting("policy", DEFAULT_POLICY, validate_policy)
        self._load_setting("notification_settings", DEFAULT_NOTIFICATION_SETTINGS,
                           validate_notification_settings)

    def _load_setting(self, key, default, validator=None):
        value = copy.deepcopy(default)
        if self._backend is not None:
            try:
                value = self._backend.get_setting(key, copy.deepcopy(default))
                if validator:
                    value = validator(value)
            except Exception as exc:
                value = copy.deepcopy(default)
                with self._condition:
                    self._configuration_warnings.append(
                        "No se pudo leer la configuración " + key + "; se usan valores predeterminados seguros.")
                self._failed(exc, "open")
        elif key == "policy":
            with self._condition:
                self._configuration_warnings.append(
                    "No se pudo leer la política guardada; el apagado automático continúa desactivado.")
        with self._condition:
            self._settings[key] = copy.deepcopy(value)
        return copy.deepcopy(value)

    def get_setting(self, key, default=None):
        with self._condition:
            if key in self._settings:
                return copy.deepcopy(self._settings[key])
            started = self._thread is not None
        if started:
            # All current monitor settings are primed before start(). Unknown
            # future settings remain safe without entering SQLite on a tick.
            with self._condition:
                self._settings[key] = copy.deepcopy(default)
            return copy.deepcopy(default)
        return self._load_setting(key, default)

    def set_setting(self, key, value):
        # Validate serializability before touching storage or cached settings.
        json.dumps(value, allow_nan=False)
        if not self._io_lock.acquire(blocking=False):
            raise HistoryUnavailable("El historial está ocupado; no se guardó la configuración. Inténtalo de nuevo.")
        try:
            with self._condition:
                if self._closing or self._backend is None:
                    raise HistoryUnavailable("El historial no está disponible; no se guardó la configuración.")
                backend = self._backend
            try:
                backend.set_setting(key, value)
            except Exception as exc:
                self._failed(exc, "settings")
                raise HistoryUnavailable("No se guardó la configuración: " + str(exc)[:300]) from exc
            with self._condition:
                self._settings[key] = copy.deepcopy(value)
            self._succeeded("settings")
        finally:
            self._io_lock.release()

    def _enqueue(self, entry):
        with self._condition:
            if self._closing:
                self._dropped += 1
                return
            if len(self._queue) >= self._capacity:
                # Keep critical events ahead of expendable graph samples, and
                # never evict an entry currently being written.
                candidate = next((value for value in self._queue
                                  if value.kind == "sample" and value not in self._inflight), None)
                if candidate is None and entry.kind == "event":
                    candidate = next((value for value in self._queue if value not in self._inflight), None)
                self._dropped += 1
                if candidate is None:
                    return
                self._queue.remove(candidate)
            self._queue.append(entry)
            self._condition.notify_all()

    def add_sample(self, sample, *, urgent=False):
        value = {field: sample.get(field) for field in SAMPLE_FIELDS}
        value.update(battery_estimate_fields(sample))
        value["timestamp"] = iso_utc(_timestamp(sample["timestamp"]))
        value["watts_is_estimate"] = sample.get("watts_is_estimate") is True
        json.dumps(value, allow_nan=False)
        load, battery = value.get("load_percent"), value.get("battery_percent")
        sample_state = (value.get("on_battery") is True, value.get("low_battery") is True,
                        value.get("fault") is True,
                        type(load) in (int, float) and load >= 100,
                        type(battery) in (int, float) and battery <= 20)
        with self._condition:
            # A first healthy read may wait for its batch; a first alarm cannot.
            changed = (sample_state != self._last_sample_state if self._last_sample_state is not None
                       else any(sample_state))
            self._last_sample_state = sample_state
            self._enqueue(_Write("sample", copy.deepcopy(value), self._clock(), urgent or changed))
        return value

    def add_event(self, kind, message, data=None, *, timestamp=None):
        data = {} if data is None else copy.deepcopy(data)
        json.dumps(data, allow_nan=False)
        stamp = iso_utc() if timestamp is None else iso_utc(_timestamp(timestamp))
        with self._condition:
            value = {"id": self._next_event_id, "timestamp": stamp, "kind": kind,
                     "message": message, "data": data}
            self._next_event_id -= 1
            self._events.appendleft(value)
        self._enqueue(_Write("event", value, self._clock(), kind in CRITICAL_HISTORY_EVENTS))
        return copy.deepcopy(value)

    def events(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit debe ser entero entre 1 y 1000")
        with self._condition:
            return copy.deepcopy(list(self._events)[:limit])

    def _read(self, method, *args, **kwargs):
        if not self._io_lock.acquire(timeout=0.1):
            raise HistoryUnavailable("El historial está ocupado; vuelve a intentarlo.")
        try:
            with self._condition:
                if self._closing or self._backend is None:
                    raise HistoryUnavailable("El historial no está disponible temporalmente.")
                backend = self._backend
            try:
                result = getattr(backend, method)(*args, **kwargs)
            except (IncidentCursorExpired, RecordCursorExpired):
                raise
            except Exception as exc:
                self._failed(exc, "read")
                raise HistoryUnavailable("No se pudo leer el historial: " + str(exc)[:300]) from exc
            self._succeeded("read")
            return result
        finally:
            self._io_lock.release()

    def history(self, hours=24, max_points=2000):
        Store._hours(hours)
        if type(max_points) is not int or not 2 <= max_points <= 2000:
            raise ValueError("max_points debe ser entero entre 2 y 2000")
        return self._read("history", hours=hours, max_points=max_points)

    def iter_samples(self, hours=720):
        Store._hours(hours)
        return self._export("iter_samples", hours=hours)

    def events_page(self, *, start=None, end=None, kind=None, query=None, limit=50, cursor=None):
        filters = Store._event_filters(start, end, kind, query)
        Store._page_limit(limit)
        Store._decode_cursor(cursor, filters, "events")
        return self._read("events_page", start=start, end=end, kind=kind, query=query,
                          limit=limit, cursor=cursor)

    def incidents(self, *, start=None, end=None, limit=50, cursor=None, sort=None, direction=None):
        filters = Store._event_filters(start, end)
        Store._page_limit(limit)
        sort, direction = Store._incident_order(sort, direction)
        Store._decode_incident_cursor(cursor, filters, sort, direction)
        return self._read("incidents", start=start, end=end, limit=limit, cursor=cursor,
                          sort=sort, direction=direction)

    def iter_events(self, *, start=None, end=None, kind=None, query=None):
        Store._event_filters(start, end, kind, query)
        return self._export("iter_events", start=start, end=end, kind=kind, query=query)

    def records(self, *, start=None, end=None, kind=None, query=None, limit=50, cursor=None):
        filters = Store._record_filters(start, end, kind, query)
        Store._page_limit(limit)
        Store._decode_record_cursor(cursor, filters)
        return self._read("records", start=start, end=end, kind=kind, query=query, limit=limit, cursor=cursor)

    def iter_records(self, *, start=None, end=None, kind=None, query=None):
        Store._record_filters(start, end, kind, query)
        return self._export("iter_records", start=start, end=end, kind=kind, query=query)

    def _export(self, method, **options):
        iterator = None
        while True:
            if not self._io_lock.acquire(timeout=0.1):
                raise HistoryUnavailable("El historial está ocupado; vuelve a intentarlo.")
            try:
                with self._condition:
                    if self._closing or self._backend is None:
                        raise HistoryUnavailable("El historial no está disponible temporalmente.")
                    backend = self._backend
                try:
                    if iterator is None:
                        iterator = iter(getattr(backend, method)(**options))
                    batch = list(itertools.islice(iterator, 500))
                except Exception as exc:
                    self._failed(exc, "read")
                    raise HistoryUnavailable("No se pudo exportar el historial: " + str(exc)[:300]) from exc
                self._succeeded("read")
            finally:
                self._io_lock.release()
            if not batch:
                return
            # Release ownership before delivering each page, so configuration,
            # new history writes and bounded shutdown can proceed in between.
            yield from batch

    def status(self):
        with self._condition:
            available = self._backend is not None and self._error is None and not self._closed
            state = "stopped" if self._closed else "healthy" if available else "degraded"
            if self._error:
                message = ("Historial no disponible temporalmente. La lectura del UPS, los avisos "
                           "y la protección continúan.")
            elif self._closed:
                message = "Historial detenido."
            elif self._dropped:
                message = "Historial activo; algunas entradas no pudieron guardarse."
            elif self._configuration_warnings:
                message = self._configuration_warnings[0]
            else:
                message = "Historial activo."
            return {"available": available, "state": state, "message": message,
                    "pending": len(self._queue), "dropped": self._dropped,
                    "batch_size": self._batch_size,
                    "batch_latency_seconds": self._batch_latency,
                    "last_error": self._error, "last_failure_at": self._last_failure_at,
                    "last_success_at": self._last_success_at,
                    "configuration_warning": " ".join(self._configuration_warnings) or None}

    def start(self):
        with self._condition:
            if self._thread is not None or self._closing:
                return
            self._thread = threading.Thread(target=self._run, name="ups-history", daemon=True)
            self._thread.start()

    def _acknowledge(self, entries, results):
        """Publish committed IDs and remove only the confirmed queue prefix."""
        with self._condition:
            for entry, result in zip(entries, results):
                if not self._queue or self._queue[0] is not entry:
                    raise RuntimeError("Se perdió el orden de la cola del historial")
                self._queue.popleft()
                if entry.kind == "event":
                    entry.value["id"] = result["id"]
            self._condition.notify_all()

    def _persist_batch(self, entries):
        # An explicitly implemented optional method is the atomic contract.
        # Avoid treating a Mock's automatically invented attribute as support.
        batch_writer = getattr(type(self._backend), "write_batch", None)
        if callable(batch_writer):
            results = self._backend.write_batch([(entry.kind, entry.value) for entry in entries])
            self._acknowledge(entries, results)
            return
        # Compatible adapters commit one at a time. Acknowledge each success
        # before attempting the next, so a partial failure cannot duplicate it.
        for entry in entries:
            value = entry.value
            if entry.kind == "event":
                result = self._backend.add_event(value["kind"], value["message"], value["data"],
                                                 timestamp=value["timestamp"])
            else:
                result = self._backend.add_sample(value)
            self._acknowledge((entry,), (result,))

    def _run(self):
        try:
            while True:
                with self._condition:
                    now = self._clock()
                    if self._closing and (not self._queue or now >= self._close_deadline):
                        self._drained = not self._queue
                        if self._queue:
                            self._dropped += len(self._queue)
                            self._queue.clear()
                        return
                    needed = self._queue or self._backend is None or self._needs_probe
                    if not needed:
                        self._condition.wait()
                        continue
                    delay = self._retry_at - now
                    if delay > 0:
                        if self._closing:
                            delay = min(delay, max(0, self._close_deadline - now))
                        self._condition.wait(timeout=delay)
                        continue
                    if self._queue and not self._closing:
                        deadline = self._queue[0].queued_at + self._batch_latency
                        urgent = any(value.urgent for value in self._queue)
                        if len(self._queue) < self._batch_size and not urgent and now < deadline:
                            self._condition.wait(timeout=deadline - now)
                            continue
                    entries = tuple(itertools.islice(self._queue, self._batch_size))
                    self._inflight = entries
                try:
                    with self._io_lock:
                        if self._backend is None:
                            backend = self._factory(self.path)
                            with self._condition:
                                self._backend = backend
                            self._succeeded("open")
                        if not entries:
                            self._backend.events(1)
                            self._succeeded("events")
                            with self._condition:
                                probe_reads = "read" in self._errors
                            if probe_reads:
                                self._backend.history(hours=0.001, max_points=2)
                                self._succeeded("read")
                            self._succeeded("open")
                        else:
                            self._persist_batch(entries)
                    with self._condition:
                        self._inflight = ()
                    if entries:
                        self._succeeded("write")
                except Exception as exc:
                    with self._condition:
                        self._inflight = ()
                    self._failed(exc, "write" if entries else "read")
        finally:
            # Only the worker closes its connection, even if close() timed out.
            # No request can enter _read/set_setting after _closing was set.
            with self._io_lock:
                if self._backend is not None:
                    try:
                        self._backend.close()
                    except Exception as exc:
                        self._failed(exc)
            with self._condition:
                self._closed = True
                self._condition.notify_all()

    def close(self, timeout=2.0):
        if type(timeout) not in (int, float) or timeout < 0:
            raise ValueError("timeout debe ser positivo")
        with self._condition:
            if not self._closing:
                self._closing = True
                self._close_deadline = self._clock() + timeout
                self._condition.notify_all()
            if self._thread is None:
                # Even an unstarted facade may have a concurrent HTTP reader.
                # Let the same bounded join/worker-owned close rule handle it.
                self._thread = threading.Thread(target=self._run, name="ups-history", daemon=True)
                self._thread.start()
            thread = self._thread
        thread.join(timeout=timeout)
        with self._condition:
            return not thread.is_alive() and self._drained
