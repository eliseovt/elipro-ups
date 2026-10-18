"""Unsigned, bounded webhook delivery outside the UPS reading/protection loop.

Only fresh monitor events enter this queue. Settings and telemetry are copied
before enqueueing; reading/protection never waits for HTTP or delivery history.
Failures are logged without exposing destination URLs or response bodies.
"""
from __future__ import annotations

import copy
import heapq
import http.client
import json
import logging
import math
import queue
import socket
import ssl
import threading
import time
import urllib.parse
import uuid
from collections import OrderedDict, deque
from datetime import timedelta

from .core import SAMPLE_FIELDS, iso_utc, utc_now


LOGGER = logging.getLogger("elipro.monitor.webhooks")
DEFAULT_WEBHOOK_SETTINGS = {
    "webhook_enabled": False,
    "webhook_url": "",
    "webhook_events": ["power_lost", "battery_low"],
}
EVENT_DEFINITIONS = {
    "power_lost": "critical",
    "power_restored": "info",
    "battery_low": "critical",
    "overload": "critical",
    "ups_fault": "critical",
}
HTTP_TIMEOUT_SECONDS = 5
MAX_DELIVERY_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 5
_SAMPLE_FIELDS = tuple(dict.fromkeys((*SAMPLE_FIELDS, "watts_is_estimate", "simulated")))
_TERMINAL_STATES = frozenset(("success", "error", "cancelled", "simulated"))
# At most one unresolved OS lookup can remain alive after a deadline. A hung
# resolver never sends HTTP and cannot create an unbounded number of threads.
_DNS_SLOTS = threading.BoundedSemaphore(1)


def validate_webhook_settings(value):
    """Validate a partial/full settings object without type coercion.

    A master switch is independent from the stored URL and selected events.
    Turning it off never erases either. Empty selections are intentionally valid.
    """
    if not isinstance(value, dict):
        raise ValueError("Los webhooks deben configurarse con un objeto.")
    if any(key not in DEFAULT_WEBHOOK_SETTINGS for key in value):
        raise ValueError("Opciones de webhooks desconocidas.")
    result = {**DEFAULT_WEBHOOK_SETTINGS, **value}
    if type(result["webhook_enabled"]) is not bool:
        raise ValueError("webhook_enabled debe ser booleano.")
    url = result["webhook_url"]
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("webhook_url debe ser una URL de hasta 2048 caracteres.")
    if url:
        try:
            parts = urllib.parse.urlsplit(url)
            valid = (parts.scheme in ("http", "https") and bool(parts.hostname)
                     and parts.port != 0 and parts.username is None and parts.password is None
                     and not parts.fragment and not any(character.isspace() or ord(character) < 32
                                                        or ord(character) == 127 for character in url))
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError("La URL del webhook debe ser HTTP o HTTPS, sin credenciales ni fragmento.")
    events = result["webhook_events"]
    if (not isinstance(events, list) or len(events) > len(EVENT_DEFINITIONS)
            or any(not isinstance(event, str) or event not in EVENT_DEFINITIONS for event in events)
            or len(set(events)) != len(events)):
        raise ValueError("webhook_events debe contener solo los cinco eventos permitidos, sin duplicados.")
    if result["webhook_enabled"] and not url:
        raise ValueError("Configura una URL antes de activar los webhooks.")
    result["webhook_events"] = list(events)
    return result


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Se agotó el plazo del webhook")
    return remaining


def _resolve_addresses(host, port, deadline):
    if not _DNS_SLOTS.acquire(timeout=_remaining(deadline)):
        raise TimeoutError("La resolución DNS no terminó dentro del plazo")
    result = queue.Queue(maxsize=1)

    def resolve():
        try:
            result.put_nowait((socket.getaddrinfo(host, port, type=socket.SOCK_STREAM), None))
        except Exception as exc:
            result.put_nowait((None, exc))
        finally:
            _DNS_SLOTS.release()

    try:
        threading.Thread(target=resolve, name="elipro-webhook-dns", daemon=True).start()
    except Exception:
        _DNS_SLOTS.release()
        raise
    try:
        addresses, error = result.get(timeout=_remaining(deadline))
    except queue.Empty:
        raise TimeoutError("La resolución DNS no terminó dentro del plazo") from None
    _remaining(deadline)  # A late DNS result is never allowed to start a POST.
    if error is not None:
        raise error
    return addresses


def _close_socket(connection):
    if connection is not None:
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass


class _AttemptDeadline:
    """One wall-clock budget, also interrupting a slow stream of HTTP headers."""

    def __init__(self, timeout):
        self.deadline = time.monotonic() + timeout
        self._lock = threading.Lock()
        self._socket = None
        self._expired = False
        self._timer = threading.Timer(timeout, self._expire)
        self._timer.daemon = True
        self._timer.start()

    def _expire(self):
        with self._lock:
            self._expired = True
            connection = self._socket
        _close_socket(connection)

    def register(self, connection):
        with self._lock:
            expired = self._expired or time.monotonic() >= self.deadline
            if not expired:
                self._socket = connection
        if expired:
            _close_socket(connection)
            raise TimeoutError("Se agotó el plazo del webhook")

    def close(self):
        self._timer.cancel()
        self._expire()


def _connect(addresses, budget):
    error = OSError("No se pudo conectar con el destino")
    for family, socket_type, protocol, _, address in addresses:
        remaining = _remaining(budget.deadline)
        connection = socket.socket(family, socket_type, protocol)
        try:
            budget.register(connection)
            connection.settimeout(remaining)
            connection.connect(address)
            _remaining(budget.deadline)
            return connection
        except OSError as exc:
            error = exc
            _close_socket(connection)
    raise error


def _send_json(url, payload, *, timeout=HTTP_TIMEOUT_SECONDS):
    """Direct unsigned POST with a total budget across DNS/TCP/TLS/headers.

    http.client never follows redirects. Response bodies are not read. The DNS
    helper only resolves addresses; if the OS resolver hangs, its caller still
    returns on time and no late POST can escape from the abandoned lookup.
    """
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("El timeout del webhook debe ser positivo.")
    budget = _AttemptDeadline(timeout)
    connection = response = None
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("El webhook requiere una URL HTTP o HTTPS.")
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        host, port = parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
        addresses = _resolve_addresses(host, port, budget.deadline)
        transport = _connect(addresses, budget)
        if parts.scheme == "https":
            context = ssl.create_default_context()
            # Register the SSLSocket before the handshake, so the deadline can
            # interrupt it even when individual TLS reads keep making progress.
            transport = context.wrap_socket(transport, server_hostname=host, do_handshake_on_connect=False)
            budget.register(transport)
            transport.settimeout(_remaining(budget.deadline))
            transport.do_handshake()
        transport.settimeout(_remaining(budget.deadline))
        connection = http.client.HTTPConnection(host, port, timeout=_remaining(budget.deadline))
        connection.sock = transport
        path = urllib.parse.quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
        if parts.query:
            path += "?" + urllib.parse.quote(parts.query, safe="/?%:@!$&'()*+,;=-._~")
        _remaining(budget.deadline)
        connection.request("POST", path, body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        _remaining(budget.deadline)
        return response.status
    finally:
        if response is not None:
            response.close()
        if connection is not None:
            connection.close()
        budget.close()


def _payload(kind, message, sample, hostname):
    payload = {"event": kind, "severity": EVENT_DEFINITIONS.get(kind, "info"),
               "timestamp": iso_utc(), "message": message[:2000] if isinstance(message, str) else "",
               "hostname": hostname}
    if isinstance(sample, dict):
        for field in _SAMPLE_FIELDS:
            value = sample.get(field)
            if type(value) is bool:
                payload[field] = value
            elif type(value) is int:
                # Reject enormous integers as well as non-finite floats. Only
                # JSON-safe telemetry primitives can leave this process.
                if -(10 ** 100) < value < 10 ** 100:
                    payload[field] = value
            elif type(value) is float and math.isfinite(value):
                payload[field] = value
            elif isinstance(value, str) and len(value) <= 1024:
                payload[field] = value
    return payload


class WebhookManager:
    """Thread-safe webhook configuration and bounded delivery scheduling.

    ``sender(url, payload, timeout=5)`` returns an HTTP status integer. It runs
    only on one dedicated daemon worker. The monitor only takes a small memory
    lock to schedule a snapshot. A failed delivery gets at most one retry, five
    seconds after the first attempt finishes; other events can run in that gap.
    Settings changes invalidate pending snapshots/retries, including reactivation.
    An HTTP request already in progress can finish after the switch is turned off.
    """

    def __init__(self, store, *, dry_run=False, sender=None, queue_capacity=32,
                 retry_delay_seconds=RETRY_DELAY_SECONDS):
        if type(queue_capacity) is not int or not 1 <= queue_capacity <= 1024:
            raise ValueError("queue_capacity debe ser entero entre 1 y 1024.")
        if (type(retry_delay_seconds) not in (int, float) or not math.isfinite(retry_delay_seconds)
                or not 0 < retry_delay_seconds <= 300):
            raise ValueError("retry_delay_seconds debe estar entre 0 y 300 segundos.")
        self.store = store
        self.dry_run = bool(dry_run)
        self._sender = sender if sender is not None else _send_json
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._settings_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._generation = 0
        self._capacity = queue_capacity
        self._retry_delay = retry_delay_seconds
        self._sequence = 0
        self._pending = []
        self._deliveries = deque(maxlen=50)
        self._dirty = OrderedDict()
        self._pending_drops = 0
        self._last_test = None
        try:
            self._hostname = socket.gethostname()[:255]
        except OSError:
            self._hostname = "unknown"
        # Prime ResilientStore before its worker starts; this is never done on
        # a reading/protection tick or by the HTTP delivery worker.
        try:
            self._settings = validate_webhook_settings(
                store.get_setting("webhooks", copy.deepcopy(DEFAULT_WEBHOOK_SETTINGS)))
        except Exception as exc:
            self._settings = copy.deepcopy(DEFAULT_WEBHOOK_SETTINGS)
            LOGGER.error("No se pudo leer la configuración de webhooks; se usan valores seguros (%s).",
                         type(exc).__name__)
        load = getattr(store, "webhook_deliveries", None)
        if callable(load):
            try:
                records = load(limit=50)
                if not isinstance(records, list):
                    records = []
                # The store returns newest first; memory appends new records.
                for record in reversed(records[:50]):
                    with self._lock:
                        self._restore_record(record)
            except Exception as exc:
                LOGGER.error("No se pudo leer el registro de webhooks (%s).", type(exc).__name__)

    def start(self):
        with self._lock:
            if self._thread is not None or self._stop_event.is_set():
                return
            self._thread = threading.Thread(target=self._run, name="elipro-webhooks", daemon=True)
            self._thread.start()

    def stop(self):
        with self._lock:
            self._stop_event.set()
            self._generation += 1
            self._cancel_pending("Entrega cancelada: se detuvo el servicio de webhooks.")
            thread = self._thread
            self._condition.notify_all()
        # Shutdown never waits for a slow destination or drains queued sends.
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.05)

    def update_settings(self, settings):
        # Persistence may wait for SQLite, but never owns the fast lock used
        # by the monitor. Publish the new configuration only after commit.
        with self._settings_lock:
            if not isinstance(settings, dict):
                raise ValueError("Los webhooks deben configurarse con un objeto.")
            with self._lock:
                proposed = {**self._settings, **settings}
            validated = validate_webhook_settings(proposed)
            self.store.set_setting("webhooks", copy.deepcopy(validated))
            with self._lock:
                if validated != self._settings:
                    self._generation += 1
                    self._cancel_pending("Entrega cancelada: cambió la configuración de webhooks.")
                self._settings = validated
                self._condition.notify_all()
                return copy.deepcopy(self._settings)

    def status(self):
        with self._lock:
            return {"settings": copy.deepcopy(self._settings), "dry_run": self.dry_run,
                    "test": copy.deepcopy(self._last_test), "deliveries": copy.deepcopy(list(self._deliveries))}

    def on_event(self, kind, message, sample=None):
        """Queue one newly observed selected event, without HTTP or storage IO."""
        if kind not in EVENT_DEFINITIONS:
            return False
        with self._lock:
            if (self._stop_event.is_set() or not self._settings["webhook_enabled"]
                    or not self._settings["webhook_url"] or kind not in self._settings["webhook_events"]):
                return False
            record = self._new_record(kind)
            if self.dry_run:
                self._change_record(record, state="simulated",
                                    message="Entrega simulada: --dry-run está activo; no se envió ningún POST.")
                return False
            item = {"generation": self._generation, "url": self._settings["webhook_url"],
                    "payload": _payload(kind, message, sample, self._hostname), "record": record}
            return self._enqueue(item, time.monotonic())

    def test(self, sample=None):
        """Queue a test from saved settings; dry-run explicitly simulates it."""
        with self._lock:
            if self._stop_event.is_set():
                raise ValueError("El servicio de webhooks está detenido.")
            if not self._settings["webhook_enabled"] or not self._settings["webhook_url"]:
                raise ValueError("Activa y guarda una URL de webhook antes de probarlo.")
            job = self._new_record("webhook_test")
            self._last_test = job
            if self.dry_run:
                self._change_record(job, state="simulated",
                                    message="Prueba simulada: --dry-run está activo; no se envió ningún POST.")
            else:
                item = {"generation": self._generation, "url": self._settings["webhook_url"],
                        "payload": _payload("webhook_test", "Prueba de integración de Elipro UPS.", sample, self._hostname),
                        "record": job}
                self._enqueue(item, time.monotonic())
            result = copy.deepcopy(job)
        self.start()
        return result

    def _restore_record(self, saved):
        if not isinstance(saved, dict) or saved.get("event") not in (*EVENT_DEFINITIONS, "webhook_test"):
            return
        keys = ("id", "event", "timestamp", "state", "attempts", "max_attempts", "message", "http_status", "next_retry_at")
        record = {key: value for key, value in saved.items() if key in keys and type(value) in (str, int)}
        if (not all(isinstance(record.get(key), str) for key in ("id", "timestamp", "state", "message"))
                or type(record.get("attempts")) is not int or not 0 <= record["attempts"] <= MAX_DELIVERY_ATTEMPTS):
            return
        record["max_attempts"] = MAX_DELIVERY_ATTEMPTS
        self._deliveries.append(record)
        if record["event"] == "webhook_test":
            self._last_test = record
        if record["state"] not in _TERMINAL_STATES:
            self._change_record(record, state="cancelled", message="Entrega cancelada al reiniciar el monitor.")

    def _new_record(self, kind):
        record = {"id": uuid.uuid4().hex, "event": kind, "state": "queued", "timestamp": iso_utc(),
                  "attempts": 0, "max_attempts": MAX_DELIVERY_ATTEMPTS, "message": "Entrega de webhook en cola."}
        self._deliveries.append(record)
        self._mark_dirty(record)
        return record

    def _mark_dirty(self, record):
        self._dirty[record["id"]] = copy.deepcopy(record)
        self._dirty.move_to_end(record["id"])
        if len(self._dirty) > 50:
            self._dirty.popitem(last=False)
        self._condition.notify_all()

    def _change_record(self, record, **changes):
        if changes.get("state") != "retrying":
            record.pop("next_retry_at", None)
        if changes.get("state") == "sending":
            record.pop("http_status", None)
        record.update(changes)
        self._mark_dirty(record)

    def _enqueue(self, item, due):
        if len(self._pending) >= self._capacity:
            self._pending_drops += 1
            self._change_record(item["record"], state="error", message="La cola de webhooks está llena. Inténtalo de nuevo.")
            return False
        self._sequence += 1
        heapq.heappush(self._pending, (due, self._sequence, item))
        self._condition.notify_all()
        return True

    def _cancel_pending(self, message):
        for _, _, item in self._pending:
            self._change_record(item["record"], state="cancelled", message=message)
        self._pending.clear()

    def _persist_updates(self):
        with self._lock:
            records = list(self._dirty.values())
            self._dirty.clear()
            dropped = self._pending_drops
            self._pending_drops = 0
        if dropped:
            LOGGER.error("Se descartaron %s entregas de webhook porque la cola está llena.", dropped)
        save = getattr(self.store, "add_webhook_delivery", None)
        if callable(save):
            for record in records:
                try:
                    save(record)
                except Exception as exc:
                    LOGGER.error("No se pudo guardar el registro de webhook (%s).", type(exc).__name__)

    def _deliver(self, item):
        record = item["record"]
        with self._lock:
            if (self._stop_event.is_set() or item["generation"] != self._generation
                    or not self._settings["webhook_enabled"]):
                self._change_record(record, state="cancelled", message="Entrega cancelada antes del envío: cambió la configuración o se detuvo el servicio.")
                return
        status, error = None, None
        try:
            status = self._sender(item["url"], item["payload"], timeout=HTTP_TIMEOUT_SECONDS)
            if type(status) is not int or not 100 <= status <= 599:
                raise ValueError("Estado HTTP no válido")
            if not 200 <= status < 300:
                error = "El destino rechazó el webhook (HTTP " + str(status) + ")."
                LOGGER.error("No se pudo enviar webhook %s (HTTP %s).", record["event"], status)
        except Exception as exc:
            # Exception text may contain a secret URL, body or token.
            LOGGER.error("No se pudo enviar webhook %s (%s).", record["event"], type(exc).__name__)
            error = "No se pudo conectar con el webhook o se agotó el plazo de 5 segundos."
        with self._lock:
            if status is not None and type(status) is int and 100 <= status <= 599:
                record["http_status"] = status
            if error is None:
                self._change_record(record, state="success", message="Webhook enviado correctamente.")
            elif self._stop_event.is_set() or item["generation"] != self._generation:
                self._change_record(record, state="cancelled", message=error + " Entrega cancelada: cambió la configuración o se detuvo el servicio.")
            elif record["attempts"] < MAX_DELIVERY_ATTEMPTS:
                retry_at = time.monotonic() + self._retry_delay
                self._change_record(record, state="retrying", message=error + " Se reintentará en " + str(self._retry_delay) + " segundos.",
                                    next_retry_at=iso_utc(utc_now() + timedelta(seconds=self._retry_delay)))
                self._enqueue(item, retry_at)
            else:
                self._change_record(record, state="error", message=error + " Se agotaron los 2 intentos.")

    def _run(self):
        while True:
            self._persist_updates()
            with self._condition:
                if self._stop_event.is_set():
                    break
                if self._dirty:
                    continue
                if not self._pending:
                    self._condition.wait()
                    continue
                due, _, item = self._pending[0]
                delay = due - time.monotonic()
                if delay > 0:
                    self._condition.wait(timeout=delay)
                    continue
                heapq.heappop(self._pending)
                if item["generation"] != self._generation or not self._settings["webhook_enabled"]:
                    self._change_record(item["record"], state="cancelled", message="Entrega cancelada: cambió la configuración.")
                    continue
                self._change_record(item["record"], state="sending", attempts=item["record"]["attempts"] + 1,
                                    message="Enviando webhook…")
            self._deliver(item)
        self._persist_updates()
