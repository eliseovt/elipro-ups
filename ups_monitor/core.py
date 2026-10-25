"""Persistence and conservative, deterministic automatic shutdown policy.

The policy never performs an OS action itself. Its injected callback receives
``shutdown`` or ``hibernate`` once, only after continuously fresh battery data.
"""

from __future__ import annotations

import json
import base64
import hashlib
import math
import copy
import queue
import secrets
import sqlite3
import threading
import time
import unicodedata
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .battery_estimation import BATTERY_ESTIMATE_METHOD

DEFAULT_POLICY = {
    "enabled": False,
    "low_battery_percent": 20,
    "min_runtime_seconds": 180,
    "countdown_seconds": 15,
    "action": "shutdown",
    "ups_poweroff": False,
    "ups_poweroff_delay_seconds": 180,
}
BATTERY_ESTIMATE_FIELDS = ("battery_percent_estimate", "battery_estimate_method")
SAMPLE_FIELDS = (
    "input_voltage", "output_voltage", "frequency_hz", "load_percent",
    "battery_percent", "battery_voltage", "runtime_seconds", "temperature_c",
    "on_battery", "low_battery", "fault", "estimated_watts", "raw_status",
) + BATTERY_ESTIMATE_FIELDS


def battery_estimate_fields(sample):
    """Keep a derived percentage only with its bounded method provenance.

    Keep estimates distinct from reported battery_percent and low_battery.
    The explicitly labelled percentage policy may use the known estimate;
    neither native field is rewritten. No value is inferred here.
    """
    percent = sample.get("battery_percent_estimate")
    method = sample.get("battery_estimate_method")
    valid = (type(percent) in (int, float) and 0 <= percent <= 100 and math.isfinite(percent)
             and isinstance(method, str) and 1 <= len(method) <= 80 and bool(method.strip()))
    return {"battery_percent_estimate": percent if valid else None,
            "battery_estimate_method": method if valid else None}


def battery_percentage(sample):
    """Prefer a reported percentage; only the known unit estimate may trigger policy."""
    if not isinstance(sample, dict):
        return None, None
    reported = sample.get("battery_percent")
    if type(reported) in (int, float) and math.isfinite(reported) and 0 <= reported <= 100:
        return reported, "reported_percentage"
    estimate = battery_estimate_fields(sample)
    if (estimate["battery_estimate_method"] == BATTERY_ESTIMATE_METHOD
            and estimate["battery_percent_estimate"] is not None):
        return estimate["battery_percent_estimate"], "estimated_percentage"
    return None, None


def utc_now():
    return datetime.now(timezone.utc)


def iso_utc(value=None):
    return (value or utc_now()).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp debe ser una fecha ISO con zona horaria")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp requiere zona horaria")
    return result.astimezone(timezone.utc)


class IncidentCursorExpired(ValueError):
    """A retained-history snapshot can no longer locate its pagination anchor."""

    def __init__(self):
        super().__init__("La consulta venció porque cambió el historial conservado. Vuelve a consultar incidentes.")


class RecordCursorExpired(ValueError):
    """The grouped register no longer contains the cursor's retained anchor."""

    def __init__(self):
        super().__init__("La consulta venció porque cambió el historial conservado. Vuelve a consultar el registro.")


def validate_policy(value):
    """Validate partial/full policy; booleans and numbers are never coerced."""
    if not isinstance(value, dict):
        raise ValueError("La política debe ser un objeto")
    unknown = set(value) - set(DEFAULT_POLICY)
    if unknown:
        raise ValueError("Opciones desconocidas: " + ", ".join(sorted(unknown)))
    result = {**DEFAULT_POLICY, **value}
    if type(result["enabled"]) is not bool:
        raise ValueError("enabled debe ser booleano")
    if type(result["ups_poweroff"]) is not bool:
        raise ValueError("ups_poweroff debe ser booleano")
    limits = {
        "low_battery_percent": (0, 100),
        "min_runtime_seconds": (0, 86400),
        "countdown_seconds": (10, 120),
    }
    for key, (lower, upper) in limits.items():
        if type(result[key]) is not int or not lower <= result[key] <= upper:
            raise ValueError(f"{key} debe ser entero entre {lower} y {upper}")
    delay = result["ups_poweroff_delay_seconds"]
    if type(delay) is not int or not 60 <= delay <= 540 or delay % 60:
        raise ValueError("El tiempo para apagar el UPS debe ser de 1 a 9 minutos enteros")
    if result["action"] not in ("shutdown", "hibernate"):
        raise ValueError("action debe ser shutdown o hibernate")
    return result


def migrate_saved_policy(value):
    """Convert stored two-stage timers without silently advancing an action.

    This is deliberately separate from API validation: removed fields must not
    be accepted by new settings requests. Historical hardware journals are not
    policy settings and are never rewritten by this migration.
    """
    if not isinstance(value, dict):
        return validate_policy(value), None
    legacy_wait = value.get("on_battery_seconds", 0)
    countdown = value.get("countdown_seconds", DEFAULT_POLICY["countdown_seconds"])
    if type(legacy_wait) is not int or not 0 <= legacy_wait <= 86400:
        raise ValueError("La espera guardada debe ser un entero entre 0 y 86400")
    if type(countdown) is not int or not 10 <= countdown <= 3600:
        raise ValueError("El tiempo guardado antes de actuar no es válido")
    candidate = {key: item for key, item in value.items() if key != "on_battery_seconds"}
    total = legacy_wait + countdown
    candidate["countdown_seconds"] = min(total, 120)
    result = validate_policy(candidate)
    if total > 120:
        result["enabled"] = False
    if "on_battery_seconds" not in value and countdown <= 120:
        return result, None
    return result, {"previous_total_seconds": total, "requires_review": total > 120}


def load_saved_policy(store):
    """Persist the original policy before replacing its obsolete timer schema."""
    original = store.get_setting("policy", DEFAULT_POLICY)
    policy, migration = migrate_saved_policy(original)
    if migration is not None:
        stamp = iso_utc()
        settings = {"policy_migration_backup": {"timestamp": stamp, "policy": original}, "policy": policy}
        if migration["requires_review"]:
            settings["policy_migration_review"] = {**migration, "timestamp": stamp}
        store.set_settings(settings)
    return policy


class Store:
    """Small SQLite store; one protected connection can be shared by threads."""

    def __init__(self, path, retention_days=30, *, busy_timeout_ms=5000):
        if type(retention_days) is not int or not 1 <= retention_days <= 3650:
            raise ValueError("retention_days debe ser entero entre 1 y 3650")
        if type(busy_timeout_ms) is not int or not 0 <= busy_timeout_ms <= 60000:
            raise ValueError("busy_timeout_ms debe ser entero entre 0 y 60000")
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self.path = str(path)
        self._lock = threading.RLock()
        self._batch_depth = 0
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=busy_timeout_ms / 1000)
        try:
            with self._db:
                self._db.execute("PRAGMA journal_mode=WAL")
                # Critical barriers rely on an actual durable WAL commit, not
                # merely a transfer into the operating system's page cache.
                self._db.execute("PRAGMA synchronous=FULL")
                self._db.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
                self._db.executescript("""
                    CREATE TABLE IF NOT EXISTS samples (
                        id INTEGER PRIMARY KEY, ts REAL NOT NULL,
                        timestamp TEXT NOT NULL, payload TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts);
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY, ts REAL NOT NULL,
                        timestamp TEXT NOT NULL, kind TEXT NOT NULL,
                        message TEXT NOT NULL, data TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
                    CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS webhook_deliveries (
                        id TEXT PRIMARY KEY, ts REAL NOT NULL, payload TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS notifications (
                        id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL,
                        acknowledged_at TEXT, payload TEXT NOT NULL);
                """)
            self._last_prune = 0.0
            self._prune()
        except BaseException:
            # Recovery can retry opening a damaged/locked database repeatedly.
            # A failed constructor must not leave its connection behind.
            self._db.close()
            raise

    def _prune(self):
        now = utc_now().timestamp()
        if now - self._last_prune < 60:
            return
        cutoff = now - self.retention_days * 86400
        with self._transaction():
            self._db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            self._db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            # Unread notices never expire with telemetry retention.
            self._db.execute("DELETE FROM notifications WHERE acknowledged_at IS NOT NULL AND acknowledged_at < ? "
                             "AND (json_extract(payload,'$.resolved_at') IS NOT NULL OR json_extract(payload,'$.kind') "
                             "NOT IN ('power_lost','connection_lost','monitor_error','battery_low','battery_low_percentage','overload','ups_fault','policy_countdown','policy_migration_review'))",
                             (iso_utc(datetime.fromtimestamp(cutoff, timezone.utc)),))
            self._last_prune = now

    def add_sample(self, sample):
        normalized = {field: sample.get(field) for field in SAMPLE_FIELDS}
        normalized.update(battery_estimate_fields(sample))
        normalized["timestamp"] = iso_utc(_timestamp(sample["timestamp"]))
        normalized["watts_is_estimate"] = sample.get("watts_is_estimate") is True
        ts = _timestamp(normalized["timestamp"]).timestamp()
        # Prune before the insert: a cleanup failure must not turn a committed
        # write into an apparent failure that a persistence worker would retry.
        self._prune()
        with self._transaction():
            self._db.execute("INSERT INTO samples(ts,timestamp,payload) VALUES(?,?,?)",
                             (ts, normalized["timestamp"], json.dumps(normalized, allow_nan=False)))
        return normalized

    def add_event(self, kind, message, data=None, *, timestamp=None):
        stamp = iso_utc(_timestamp(timestamp)) if timestamp is not None else iso_utc()
        data = {} if data is None else data
        self._prune()
        with self._transaction():
            cursor = self._db.execute(
                "INSERT INTO events(ts,timestamp,kind,message,data) VALUES(?,?,?,?,?)",
                (_timestamp(stamp).timestamp(), stamp, kind, message, json.dumps(data, allow_nan=False)))
            event_id = cursor.lastrowid
        return {"id": event_id, "timestamp": stamp, "kind": kind, "message": message, "data": data}

    @contextmanager
    def _transaction(self):
        """Allow public write hooks to share one atomic batch transaction."""
        with self._lock:
            if self._batch_depth:
                yield
            else:
                with self._db:
                    yield

    def write_batch(self, entries):
        """Commit ordered sample/event pairs atomically, or roll them all back.

        Pruning happens before the batch starts, so cleanup cannot report an
        already committed batch as failed. Event IDs are returned only after
        the transaction commits; a retry after SQLite busy/disk full is safe.
        Public hooks remain in the path for instrumented/custom stores.
        """
        entries = list(entries)
        if any(kind not in ("sample", "event", "webhook_delivery", "notification") for kind, _ in entries):
            raise ValueError("Tipo de escritura desconocido")
        self._prune()
        results = []
        with self._transaction():
            self._batch_depth += 1
            try:
                for kind, value in entries:
                    if kind == "sample":
                        results.append(self.add_sample(value))
                    elif kind == "webhook_delivery":
                        results.append(self.add_webhook_delivery(value))
                    elif kind == "notification":
                        results.append(self.put_notification(value))
                    else:
                        results.append(self.add_event(value["kind"], value["message"], value["data"],
                                                      timestamp=value["timestamp"]))
            finally:
                self._batch_depth -= 1
        return results

    def get_setting(self, key, default=None):
        with self._lock:
            row = self._db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        self.set_settings({key: value})

    def set_settings(self, values):
        """Commit related settings together, including policy review metadata."""
        rows = [(key, json.dumps(value, allow_nan=False)) for key, value in values.items()]
        with self._lock, self._db:
            self._db.executemany("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", rows)

    @staticmethod
    def _notification(record):
        value = copy.deepcopy(record)
        if (not isinstance(value, dict) or type(value.get("id")) is not int or
                not 0 < value["id"] <= 2**53 - 1):
            raise ValueError("Identificador de aviso inválido.")
        for key in ("kind", "title", "message", "severity"):
            if not isinstance(value.get(key), str) or not value[key]:
                raise ValueError("Registro de aviso inválido.")
        value["timestamp"] = iso_utc(_timestamp(value.get("timestamp")))
        for key in ("resolved_at", "acknowledged_at"):
            value[key] = iso_utc(_timestamp(value[key])) if value.get(key) is not None else None
        json.dumps(value, allow_nan=False)
        return value

    def put_notification(self, record):
        """Upsert a stable notice; delayed writes cannot undo a user ACK."""
        value = self._notification(record)
        with self._transaction():
            row = self._db.execute("SELECT acknowledged_at,payload FROM notifications WHERE id=?", (value["id"],)).fetchone()
            if row and row[0] is not None:
                value["acknowledged_at"] = row[0]
            if row:
                previous = json.loads(row[1])
                if previous.get("resolved_at") is not None:
                    value["resolved_at"] = previous["resolved_at"]
            self._db.execute("INSERT INTO notifications(id,timestamp,acknowledged_at,payload) VALUES(?,?,?,?) "
                             "ON CONFLICT(id) DO UPDATE SET acknowledged_at=excluded.acknowledged_at,payload=excluded.payload",
                             (value["id"], value["timestamp"], value["acknowledged_at"], json.dumps(value, allow_nan=False)))
        return value

    def pending_notifications(self):
        with self._lock:
            rows = self._db.execute("SELECT payload FROM notifications WHERE acknowledged_at IS NULL ORDER BY timestamp,id").fetchall()
        return [json.loads(row[0]) for row in rows]

    def notification_incidents(self):
        with self._lock:
            rows = self._db.execute("SELECT payload FROM notifications ORDER BY timestamp,id").fetchall()
        return [item for row in rows if (item := json.loads(row[0])).get("resolved_at") is None]

    def acknowledge_notifications(self, records):
        # Include not-yet-flushed records so an immediate click is atomic too.
        return self.write_batch(("notification", value) for value in records)

    @staticmethod
    def _webhook_delivery(record):
        if (not isinstance(record, dict) or not isinstance(record.get("id"), str)
                or not 1 <= len(record["id"]) <= 64):
            raise ValueError("Registro de entrega de webhook no válido.")
        value = copy.deepcopy(record)
        value["timestamp"] = iso_utc(_timestamp(value.get("timestamp")))
        json.dumps(value, allow_nan=False)
        return value

    def add_webhook_delivery(self, record):
        """Update a delivery by stable ID, retaining only the latest 50.

        Delivery state has its own ledger so retries never appear as new UPS
        incidents or alter the desktop notification/event history.
        """
        value = self._webhook_delivery(record)
        with self._transaction():
            self._db.execute(
                "INSERT INTO webhook_deliveries(id,ts,payload) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET ts=excluded.ts,payload=excluded.payload",
                (value["id"], _timestamp(value["timestamp"]).timestamp(), json.dumps(value, allow_nan=False)))
            self._db.execute("DELETE FROM webhook_deliveries WHERE id NOT IN "
                             "(SELECT id FROM webhook_deliveries ORDER BY ts DESC,id DESC LIMIT 50)")
        return value

    def webhook_deliveries(self, limit=50):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit debe ser entero entre 1 y 50")
        with self._lock:
            rows = self._db.execute("SELECT payload FROM webhook_deliveries ORDER BY ts DESC,id DESC LIMIT ?",
                                    (limit,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    @staticmethod
    def _hours(hours):
        if type(hours) not in (int, float) or not math.isfinite(hours) or not 0 < hours <= 24 * 365:
            raise ValueError("hours debe estar entre 0 y 8760")
        return hours

    def history(self, hours=24, max_points=2000):
        """Return bounded plot data without concealing collection gaps.

        gap_before describes the interval since the previous returned point,
        including gaps in original samples that decimation would otherwise hide.
        """
        self._hours(hours)
        if type(max_points) is not int or not 2 <= max_points <= 2000:
            raise ValueError("max_points debe ser entero entre 2 y 2000")
        cutoff = (utc_now() - timedelta(hours=hours)).timestamp()
        with self._lock:
            count = self._db.execute("SELECT COUNT(*) FROM samples WHERE ts>=?", (cutoff,)).fetchone()[0]
            if count <= max_points:
                rows = self._db.execute("SELECT payload,ts FROM samples WHERE ts>=? ORDER BY ts,id", (cutoff,)).fetchall()
            else:
                # Mark raw gaps before selecting points. A five-minute display
                # interval must not hide a shorter outage between its endpoints.
                step = math.ceil(count / (max_points - 1))
                rows = self._db.execute("""
                    WITH ordered AS (
                        SELECT id,ts,payload,
                            ROW_NUMBER() OVER (ORDER BY ts,id) AS rn,
                            LAG(ts) OVER (ORDER BY ts,id) AS previous_ts
                        FROM samples WHERE ts>=?
                    ), marked AS (
                        SELECT *, SUM(CASE WHEN ts-previous_ts>30 THEN 1 ELSE 0 END)
                            OVER (ORDER BY ts,id ROWS UNBOUNDED PRECEDING) AS gap_sequence
                        FROM ordered
                    )
                    SELECT payload,gap_sequence FROM marked
                    WHERE (rn-1) % ? = 0 OR rn = ? ORDER BY rn LIMIT ?
                """, (cutoff, step, count, max_points)).fetchall()
        samples = []
        previous_marker = None
        for payload, marker in rows:
            sample = json.loads(payload)
            sample["gap_before"] = previous_marker is not None and (
                marker - previous_marker > 30 if count <= max_points else marker != previous_marker)
            samples.append(sample)
            previous_marker = marker
        return samples

    def iter_samples(self, hours=720):
        """Export in pages; release the connection lock between each batch."""
        self._hours(hours)
        cutoff = (utc_now() - timedelta(hours=hours)).timestamp()
        with self._lock:
            high_id = self._db.execute("SELECT COALESCE(MAX(id),0) FROM samples").fetchone()[0]
        after_id = 0
        while True:
            with self._lock:
                rows = self._db.execute("SELECT id,payload FROM samples WHERE ts>=? AND id>? AND id<=? ORDER BY id LIMIT 500", (cutoff, after_id, high_id)).fetchall()
            if not rows:
                return
            for row_id, payload in rows:
                after_id = row_id
                yield json.loads(payload)

    def events(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit debe ser entero entre 1 y 1000")
        with self._lock:
            rows = self._db.execute("SELECT id,timestamp,kind,message,data FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": row[0], "timestamp": row[1], "kind": row[2], "message": row[3], "data": json.loads(row[4])} for row in rows]

    @staticmethod
    def _event_filters(start=None, end=None, kind=None, query=None):
        start_value = _timestamp(start) if start is not None else None
        end_value = _timestamp(end) if end is not None else None
        if start_value and end_value and start_value > end_value:
            raise ValueError("La fecha inicial debe ser anterior o igual a la fecha final")
        if kind is not None and (not isinstance(kind, str) or not 1 <= len(kind) <= 100):
            raise ValueError("kind debe ser un texto de 1 a 100 caracteres")
        if query is not None and (not isinstance(query, str) or len(query) > 200):
            raise ValueError("La búsqueda no puede superar 200 caracteres")
        return {"from": iso_utc(start_value) if start_value else None,
                "to": iso_utc(end_value) if end_value else None,
                "kind": kind, "query": query or None}

    @staticmethod
    def _page_limit(limit):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit debe ser entero entre 1 y 200")
        return limit

    @staticmethod
    def _cursor_fingerprint(filters, mode):
        value = json.dumps({"mode": mode, **filters}, sort_keys=True).encode("utf-8")
        return hashlib.sha256(value).hexdigest()[:24]

    @classmethod
    def _decode_cursor(cls, cursor, filters, mode):
        if cursor is None:
            return None
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 512:
            raise ValueError("Cursor de historial inválido")
        try:
            value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if (set(value) != {"v", "high", "before", "filter"} or value["v"] != 1 or
                    type(value["high"]) is not int or type(value["before"]) is not int or
                    not 0 <= value["high"] < 2 ** 63 - 1 or not 0 < value["before"] <= value["high"] + 1 or
                    value["filter"] != cls._cursor_fingerprint(filters, mode)):
                raise ValueError()
        except (ValueError, TypeError, KeyError, UnicodeError, OverflowError) as exc:
            raise ValueError("Cursor inválido o perteneciente a otros filtros") from exc
        return value

    @classmethod
    def _encode_cursor(cls, high, before, filters, mode):
        value = {"v": 1, "high": high, "before": before,
                 "filter": cls._cursor_fingerprint(filters, mode)}
        return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")

    @staticmethod
    def _incident_order(sort=None, direction=None):
        allowed = {"type", "status", "started_at", "ended_at", "duration_seconds", "action_result"}
        if sort is None:
            if direction is not None:
                raise ValueError("La dirección requiere una columna de ordenación.")
            return None, None
        if not isinstance(sort, str) or sort not in allowed:
            raise ValueError("Columna de incidentes no válida.")
        direction = "desc" if direction is None else direction
        if not isinstance(direction, str) or direction not in {"asc", "desc"}:
            raise ValueError("Dirección de incidentes no válida.")
        return sort, direction

    @classmethod
    def _decode_incident_cursor(cls, cursor, filters, sort=None, direction=None):
        if sort is None:
            return cls._decode_cursor(cursor, filters, "incidents")
        if cursor is None:
            return None
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 512:
            raise ValueError("Cursor de historial inválido")
        try:
            value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            fingerprint = cls._cursor_fingerprint({**filters, "sort": sort, "direction": direction}, "incidents")
            if (set(value) != {"v", "high", "before", "as_of", "filter"} or
                    type(value["v"]) is not int or value["v"] != 2 or
                    type(value["high"]) is not int or type(value["before"]) is not int or
                    not 0 < value["before"] <= value["high"] < 2 ** 63 - 1 or
                    value["filter"] != fingerprint):
                raise ValueError()
            value["as_of"] = iso_utc(_timestamp(value["as_of"]))
        except (ValueError, TypeError, KeyError, UnicodeError, OverflowError) as exc:
            raise ValueError("Cursor inválido o perteneciente a otros filtros u ordenación") from exc
        return value

    @classmethod
    def _encode_incident_cursor(cls, high, before, as_of, filters, sort, direction):
        value = {"v": 2, "high": high, "before": before, "as_of": as_of,
                 "filter": cls._cursor_fingerprint({**filters, "sort": sort, "direction": direction}, "incidents")}
        return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")

    @staticmethod
    def _incident_sort_value(incident, sort):
        labels = {
            "type": {"power": "Corte eléctrico", "communication": "Problema de comunicación"},
            "status": {"recovered": "Recuperado", "open": "Sin recuperación registrada"},
            "action_result": {"cancelled": "Cancelada", "failed": "Fallida", "started": "Iniciada",
                              "simulated": "Simulada", "none": "Sin acciones"},
        }
        value = incident.get(sort)
        if sort in labels:
            label = labels[sort].get(value)
            return ("".join(character for character in unicodedata.normalize("NFKD", label.casefold())
                            if not unicodedata.combining(character)) if label is not None else None)
        if sort in {"started_at", "ended_at"}:
            if value is None or (sort == "started_at" and incident.get("start_known") is False):
                return None
            return _timestamp(value).timestamp()
        return value if type(value) in (int, float) and math.isfinite(value) else None

    @classmethod
    def _sort_incidents(cls, incidents, sort, direction):
        # Stable sorting preserves this ID tie-break in both primary directions.
        incidents.sort(key=lambda item: item["opening_event_id"], reverse=True)
        known, unknown = [], []
        for incident in incidents:
            value = cls._incident_sort_value(incident, sort)
            if value is None:
                unknown.append(incident)
            else:
                known.append((value, incident))
        known.sort(key=lambda item: item[0], reverse=direction == "desc")
        return [incident for _, incident in known] + unknown

    @staticmethod
    def _event_where(filters):
        clauses, values = [], []
        for key, operator in (("from", ">="), ("to", "<=")):
            if filters[key] is not None:
                clauses.append("ts " + operator + " ?")
                values.append(_timestamp(filters[key]).timestamp())
        if filters["kind"] is not None:
            clauses.append("kind = ?")
            values.append(filters["kind"])
        if filters["query"]:
            # Literal substring search: %, _ and quotes are data, not patterns
            # or SQL. instr also avoids surprising wildcard expansion.
            clauses.append("(instr(lower(message),lower(?))>0 OR instr(lower(kind),lower(?))>0 OR instr(lower(data),lower(?))>0)")
            values.extend([filters["query"]] * 3)
        return clauses, values

    @staticmethod
    def _event_rows(rows):
        return [{"id": row[0], "timestamp": row[1], "kind": row[2],
                 "message": row[3], "data": json.loads(row[4])} for row in rows]

    def events_page(self, *, start=None, end=None, kind=None, query=None, limit=50, cursor=None):
        """Page immutable IDs, excluding events inserted after the first page."""
        filters = self._event_filters(start, end, kind, query)
        self._page_limit(limit)
        position = self._decode_cursor(cursor, filters, "events")
        clauses, values = self._event_where(filters)
        with self._lock:
            high = position["high"] if position else self._db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
            before = position["before"] if position else high + 1
            clauses.append("id <= ?")
            values.append(high)
            where = " AND ".join(clauses)
            total = self._db.execute("SELECT COUNT(*) FROM events WHERE " + where, values).fetchone()[0]
            rows = self._db.execute("SELECT id,timestamp,kind,message,data FROM events WHERE " + where +
                                    " AND id < ? ORDER BY id DESC LIMIT ?", (*values, before, limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        return {"events": self._event_rows(rows), "total": total, "limit": limit,
                "next_cursor": self._encode_cursor(high, rows[-1][0], filters, "events") if more else None,
                **filters}

    def iter_events(self, *, start=None, end=None, kind=None, query=None, _high_id=None):
        """Export all retained matching events in 500-row batches, oldest first."""
        filters = self._event_filters(start, end, kind, query)
        clauses, values = self._event_where(filters)
        with self._lock:
            high = self._db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0] if _high_id is None else _high_id
        clauses.extend(("id > ?", "id <= ?"))
        after = 0
        while True:
            with self._lock:
                rows = self._db.execute("SELECT id,timestamp,kind,message,data FROM events WHERE " +
                                        " AND ".join(clauses) + " ORDER BY id LIMIT 500", (*values, after, high)).fetchall()
            if not rows:
                return
            for event in self._event_rows(rows):
                after = event["id"]
                yield event

    def incidents(self, *, start=None, end=None, limit=50, cursor=None, sort=None, direction=None):
        """Group evidence of power and communication failures independently.

        Scan retained events before filtering overlaps, so an incident starting
        before the requested interval is still visible. Missing earlier records
        cannot be reconstructed; no synthetic outage is invented on restoration.
        """
        filters = self._event_filters(start, end)
        self._page_limit(limit)
        sort, direction = self._incident_order(sort, direction)
        position = self._decode_incident_cursor(cursor, filters, sort, direction)
        with self._lock:
            high = position["high"] if position else self._db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
        before = position["before"] if position else high + 1
        active, incidents = {}, []
        opening = {"power_lost": "power", "connection_lost": "communication", "monitor_error": "communication"}
        recovery = {"power_restored": "power", "connection_restored": "communication"}
        action_kinds = {"policy_countdown", "policy_cancelled", "policy_action_started", "policy_action_failed", "policy_unknown", "simulation",
                        "ups_poweroff_submitted", "ups_poweroff_unconfirmed", "ups_poweroff_cancel_requested", "ups_poweroff_blocked"}
        for event in self.iter_events(_high_id=high):
            if (event["kind"] in ("connection_lost", "monitor_error") and
                    isinstance(event["data"], dict) and event["data"].get("error_kind") == "stopped"):
                continue
            event_type = opening.get(event["kind"])
            if event_type:
                if event_type not in active:
                    incident = {"id": event_type + ":" + str(event["id"]), "type": event_type,
                                "status": "open", "started_at": event["timestamp"], "ended_at": None,
                                "opening_event_id": event["id"], "recovery_event_id": None,
                                "message": event["message"], "recovery_message": None,
                                "actions": [], "start_known": True, "error_kind": event["data"].get("error_kind") if isinstance(event["data"], dict) else None}
                    active[event_type] = incident
                    incidents.append(incident)
            elif event["kind"] in recovery:
                incident = active.pop(recovery[event["kind"]], None)
                if incident is not None:
                    incident.update(status="recovered", ended_at=event["timestamp"],
                                    recovery_event_id=event["id"], recovery_message=event["message"])
            elif event["kind"] in action_kinds:
                for incident in active.values():
                    incident["actions"].append(event)
        lower = _timestamp(filters["from"]) if filters["from"] else None
        upper = _timestamp(filters["to"]) if filters["to"] else None
        now = _timestamp(position["as_of"]) if sort is not None and position else utc_now()
        as_of = iso_utc(now)
        selected = []
        for incident in incidents:
            began = _timestamp(incident["started_at"])
            ended = _timestamp(incident["ended_at"]) if incident["ended_at"] else None
            incident["duration_seconds"] = max(0, round(((ended or now) - began).total_seconds(), 3))
            kinds = {event["kind"] for event in incident["actions"]}
            incident["action_result"] = ("failed" if "policy_action_failed" in kinds else
                                         "started" if "policy_action_started" in kinds else
                                         "cancelled" if "policy_cancelled" in kinds else
                                         "simulated" if "simulation" in kinds else "none")
            if (lower is None or ended is None or ended >= lower) and (upper is None or began <= upper):
                selected.append(incident)
        total = len(selected)
        if sort is None:
            selected.sort(key=lambda item: item["opening_event_id"], reverse=True)
            rows = [item for item in selected if item["opening_event_id"] < before][:limit + 1]
        else:
            selected = self._sort_incidents(selected, sort, direction)
            offset = 0
            if position:
                anchor = next((index for index, item in enumerate(selected)
                               if item["opening_event_id"] == before), None)
                if anchor is None:
                    raise IncidentCursorExpired()
                offset = anchor + 1
            rows = selected[offset:offset + limit + 1]
        more, rows = len(rows) > limit, rows[:limit]
        next_cursor = None
        if more:
            next_cursor = (self._encode_cursor(high, rows[-1]["opening_event_id"], filters, "incidents") if sort is None else
                           self._encode_incident_cursor(high, rows[-1]["opening_event_id"], as_of, filters, sort, direction))
        return {"incidents": rows, "total": total, "limit": limit, **filters,
                "next_cursor": next_cursor,
                **({"sort": sort, "direction": direction, "as_of": as_of} if sort is not None else {}),
                "retention_days": self.retention_days,
                "note": "Duración detectada entre lecturas; los incidentes anteriores a la retención pueden estar incompletos."}

    @classmethod
    def _record_filters(cls, start=None, end=None, kind=None, query=None):
        filters = cls._event_filters(start, end, kind, query)
        if kind is not None and kind not in {"power", "connection", "protection", "alerts", "settings", "other"}:
            raise ValueError("Categoría del registro no válida")
        return filters

    @staticmethod
    def _record_category(kind):
        if kind in {"power_lost", "power_restored"}:
            return "power"
        if kind in {"connection_lost", "connection_restored", "monitor_error", "monitor_restarted"}:
            return "connection"
        if kind in {"policy_changed", "notifications_changed", "notification_settings_changed"}:
            return "settings"
        if kind in {"battery_low", "overload", "ups_fault"}:
            return "alerts"
        if kind == "simulation" or kind.startswith(("policy_", "ups_poweroff_", "ups_battery_test", "ups_cancel_test", "windows_action_")):
            return "protection"
        return "other"

    @classmethod
    def _decode_record_cursor(cls, cursor, filters):
        if cursor is None:
            return None
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 512:
            raise ValueError("Cursor del registro inválido")
        try:
            value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if (set(value) != {"v", "high", "before", "as_of", "filter"}
                    or type(value["v"]) is not int or value["v"] != 1
                    or type(value["high"]) is not int or not 0 < value["high"] < 2 ** 63 - 1
                    or not isinstance(value["before"], str) or len(value["before"]) > 64
                    or value["filter"] != cls._cursor_fingerprint(filters, "records")):
                raise ValueError()
            category, event_id = value["before"].split(":")
            if category not in {"power", "connection", "event"} or not event_id.isascii() or not event_id.isdecimal():
                raise ValueError()
            if not 0 < int(event_id) <= value["high"]:
                raise ValueError()
            value["as_of"] = iso_utc(_timestamp(value["as_of"]))
        except (ValueError, TypeError, KeyError, UnicodeError, OverflowError, AttributeError) as exc:
            raise ValueError("Cursor inválido o perteneciente a otros filtros") from exc
        return value

    @classmethod
    def _encode_record_cursor(cls, high, before, as_of, filters):
        value = {"v": 1, "high": high, "before": before, "as_of": as_of,
                 "filter": cls._cursor_fingerprint(filters, "records")}
        return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")

    @classmethod
    def _group_records(cls, events, as_of):
        """Associate only outage evidence and protection actions with incidents.

        Alerts and configuration are independent conditions, even during an
        outage. A shared protection action can appear in two incident details
        while its event ID is suppressed only once in the standalone feed.
        """
        active, incidents, members = {}, [], set()
        opening = {"power_lost": "power", "connection_lost": "connection", "monitor_error": "connection"}
        recovery = {"power_restored": "power", "connection_restored": "connection"}
        for event in events:
            kind = event["kind"]
            if (kind in {"connection_lost", "monitor_error"} and isinstance(event["data"], dict)
                    and event["data"].get("error_kind") == "stopped"):
                continue
            event_type = opening.get(kind)
            if event_type is not None:
                if event_type not in active:
                    incident = {"id": event_type + ":" + str(event["id"]), "record_type": "incident",
                                "type": event_type, "category": event_type,
                                "timestamp": event["timestamp"], "description": event["message"],
                                "status": "open", "started_at": event["timestamp"], "ended_at": None,
                                "opening_event_id": event["id"], "recovery_event_id": None,
                                "message": event["message"], "recovery_message": None,
                                "actions": [], "events": [], "start_known": True,
                                "error_kind": event["data"].get("error_kind") if isinstance(event["data"], dict) else None}
                    active[event_type] = incident
                    incidents.append(incident)
                active[event_type]["events"].append(event)
                members.add(event["id"])
            elif kind in recovery:
                incident = active.pop(recovery[kind], None)
                if incident is not None:
                    incident.update(status="recovered", ended_at=event["timestamp"],
                                    recovery_event_id=event["id"], recovery_message=event["message"])
                    incident["events"].append(event)
                    members.add(event["id"])
            elif (kind == "simulation" or kind.startswith(("policy_", "ups_poweroff_", "windows_action_"))) and kind != "policy_changed":
                for incident in active.values():
                    incident["actions"].append(event)
                    incident["events"].append(event)
                    members.add(event["id"])
        for incident in incidents:
            end = _timestamp(incident["ended_at"]) if incident["ended_at"] else as_of
            incident["duration_seconds"] = max(0, round((end - _timestamp(incident["started_at"])).total_seconds(), 3))
            kinds = {event["kind"] for event in incident["actions"]}
            incident["action_result"] = ("failed" if "policy_action_failed" in kinds else
                                         "started" if "policy_action_started" in kinds else
                                         "cancelled" if "policy_cancelled" in kinds else
                                         "simulated" if "simulation" in kinds else "none")
        records = list(incidents)
        for event in events:
            if event["id"] not in members:
                records.append({"id": "event:" + str(event["id"]), "record_type": "event",
                                "type": event["kind"], "category": cls._record_category(event["kind"]),
                                "timestamp": event["timestamp"], "description": event["message"],
                                "status": None, "duration_seconds": None, "action_result": None,
                                "kind": event["kind"], "message": event["message"], "data": event["data"],
                                "events": [event]})
        return records

    def _record_feed(self, filters, position=None):
        with self._lock:
            high = position["high"] if position else self._db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
        now = _timestamp(position["as_of"]) if position else utc_now()
        records = self._group_records(list(self.iter_events(_high_id=high)), now)
        lower = _timestamp(filters["from"]) if filters["from"] else None
        upper = _timestamp(filters["to"]) if filters["to"] else None
        query = filters["query"].casefold() if filters["query"] else None
        selected = []
        for record in records:
            began = _timestamp(record["timestamp"])
            ended = _timestamp(record["ended_at"]) if record.get("ended_at") else None
            if record["record_type"] == "incident":
                in_range = (lower is None or ended is None or ended >= lower) and (upper is None or began <= upper)
            else:
                in_range = (lower is None or began >= lower) and (upper is None or began <= upper)
            if not in_range:
                continue
            if filters["kind"] and filters["kind"] not in ({record["category"]} | {
                    self._record_category(event["kind"]) for event in record["events"]}):
                continue
            if query and not any(query in (str(event["message"]) + "\n" + event["kind"] + "\n" +
                                         json.dumps(event["data"], ensure_ascii=False)).casefold() for event in record["events"]):
                continue
            selected.append(record)
        selected.sort(key=lambda item: (_timestamp(item["timestamp"]),
                                       item.get("opening_event_id", item["events"][0]["id"])), reverse=True)
        return selected, high, iso_utc(now)

    def records(self, *, start=None, end=None, kind=None, query=None, limit=50, cursor=None):
        """Filter and page the complete grouped register, never raw event pages."""
        filters = self._record_filters(start, end, kind, query)
        self._page_limit(limit)
        position = self._decode_record_cursor(cursor, filters)
        records, high, as_of = self._record_feed(filters, position)
        offset = 0
        if position:
            anchor = next((index for index, record in enumerate(records) if record["id"] == position["before"]), None)
            if anchor is None:
                raise RecordCursorExpired()
            offset = anchor + 1
        rows = records[offset:offset + limit]
        more = offset + len(rows) < len(records)
        return {"records": rows, "total": len(records), "limit": limit, **filters,
                "next_cursor": self._encode_record_cursor(high, rows[-1]["id"], as_of, filters) if more else None,
                "as_of": as_of, "retention_days": self.retention_days}

    def iter_records(self, *, start=None, end=None, kind=None, query=None):
        """Export all matching grouped rows from one fixed event/time snapshot."""
        records, _high, _as_of = self._record_feed(self._record_filters(start, end, kind, query))
        yield from records

    def close(self):
        with self._lock:
            self._db.close()


class TelemetryRingBuffer:
    """Bounded hardware-to-consumer mailbox with one coalesced wake token.

    The producer never waits for disk, the decision lock, or a slow consumer.
    Samples and pending deliveries have a fixed capacity; overwritten deliveries
    leave a bounded summary of their electrical transitions. A consumer must
    invalidate policy continuity on overflow and must never execute an action
    from historical entries. Returned dictionaries are independent snapshots.
    """

    _TRANSITIONS = ("power_lost", "power_restored", "battery_low", "ups_fault",
                    "overload", "connection_lost", "connection_restored")

    def __init__(self, capacity=256):
        if type(capacity) is not int or not 2 <= capacity <= 10000:
            raise ValueError("capacity debe ser entero entre 2 y 10000")
        self.capacity = capacity
        self._lock = threading.Lock()
        self._samples, self._pending = deque(maxlen=capacity), deque(maxlen=capacity)
        self._wake = queue.SimpleQueue()
        self._wake_pending = False
        self._sequence = self._dropped = 0
        self._overflow = None
        self._last_overflow = None
        self._previous = None

    def _facts(self, sample, error):
        previous = self._previous
        if error is not None:
            self._previous = {"connected": False}
            return ["connection_lost"] if previous is None or previous.get("connected") else []
        current = {"connected": True, "on_battery": sample.get("on_battery"),
                   "low_battery": sample.get("low_battery") is True or (
                       type(sample.get("battery_percent")) in (int, float) and sample["battery_percent"] <= 20),
                   "fault": sample.get("fault") is True,
                   "overload": type(sample.get("load_percent")) in (int, float) and sample["load_percent"] >= 100}
        result = []
        if previous is None or not previous.get("connected"):
            result.append("connection_restored")
        if current["on_battery"] is True and (previous is None or previous.get("on_battery") is not True):
            result.append("power_lost")
        elif current["on_battery"] is False and previous is not None and previous.get("on_battery") is True:
            result.append("power_restored")
        for field, kind in (("low_battery", "battery_low"), ("fault", "ups_fault"), ("overload", "overload")):
            if current[field] and (previous is None or previous.get(field) is not True):
                result.append(kind)
        self._previous = current
        return result

    def publish(self, sample=None, *, error=None, error_kind="telemetry", captured_at=None, power_generation=0):
        owned = copy.deepcopy(sample) if sample is not None else None
        if error is None and not isinstance(owned, dict):
            raise ValueError("La telemetría debe ser un objeto")
        with self._lock:
            self._sequence += 1
            entry = {"sequence": self._sequence, "sample": owned,
                     "error": str(error)[:1000] if error is not None else None,
                     "error_kind": error_kind, "captured_at": captured_at, "power_generation": power_generation,
                     "facts": self._facts(owned, error)}
            if owned is not None:
                self._samples.append(owned)
            if len(self._pending) == self.capacity:
                lost = self._pending[0]
                self._dropped += 1
                if self._overflow is None:
                    self._overflow = {"count": 0, "first_sequence": lost["sequence"],
                                      "last_sequence": lost["sequence"], "transitions": {}}
                self._overflow["count"] += 1
                self._overflow["last_sequence"] = lost["sequence"]
                stamp = (lost["sample"] or {}).get("timestamp")
                for fact in lost["facts"]:
                    summary = self._overflow["transitions"].setdefault(
                        fact, {"count": 0, "first_observed_at": stamp, "last_observed_at": stamp})
                    summary["count"] += 1
                    summary["last_observed_at"] = stamp
            self._pending.append(entry)
            if not self._wake_pending:
                self._wake_pending = True
                self._wake.put(True)
            return self._sequence

    def wait(self, timeout=0.5):
        try:
            self._wake.get(timeout=timeout)
            return True
        except queue.Empty:
            return False

    def drain(self):
        with self._lock:
            entries, overflow = list(self._pending), self._overflow
            self._pending.clear()
            self._overflow = None
            if overflow is not None:
                self._last_overflow = copy.deepcopy(overflow)
            self._wake_pending = False
            try:
                self._wake.get_nowait()
            except queue.Empty:
                pass
        return copy.deepcopy(entries), copy.deepcopy(overflow)

    def is_latest(self, sequence):
        with self._lock:
            return sequence == self._sequence

    def samples(self):
        with self._lock:
            samples = list(self._samples)
        return copy.deepcopy(samples)

    def status(self):
        with self._lock:
            return {"capacity": self.capacity, "samples": len(self._samples),
                    "pending": len(self._pending), "dropped": self._dropped,
                    "latest_sequence": self._sequence, "wake_pending": self._wake_pending,
                    "last_overflow": copy.deepcopy(self._last_overflow or self._overflow)}


class PolicyEngine:
    """Advance via evaluate(sample); call it regularly, including on data loss.

    A missing, invalid, repeated stale, or explicitly disconnected sample breaks
    battery continuity and cancels an active countdown. Manual cancellation stays
    in effect until a fresh online sample or disabling and re-enabling the policy.
    """

    def __init__(self, policy=None, action_callback=None, *, clock=time.monotonic,
                 utc_clock=utc_now, stale_after=15, event_callback=None):
        self.policy = validate_policy({} if policy is None else policy)
        self._action_callback = action_callback
        self._event_callback = event_callback
        self._clock, self._utc_clock = clock, utc_clock
        self.stale_after = stale_after
        self._battery_since = self._deadline = self._last_fresh_at = None
        self._countdown_id = None
        self._effective_countdown_seconds = None
        self._emergency_source = self._emergency_percent = None
        self._suppressed = self._fired = False
        self._state, self._reason = "disabled", None

    def _transition(self, state, reason=None):
        changed = state != self._state or reason != self._reason
        self._state, self._reason = state, reason
        if state != "countdown":
            self._countdown_id = None
        if changed and self._event_callback:
            data = {"state": state}
            if state == "countdown":
                current = self.status()
                data.update({key: current[key] for key in (
                    "countdown_id", "countdown_remaining", "countdown_valid_for_ms", "cancelable", "action")})
            self._event_callback("policy_" + state, reason or state, data)

    def update_policy(self, policy):
        value = validate_policy(policy)
        changed = value != self.policy
        self.policy = value
        if changed:
            self._deadline = self._battery_since = self._last_fresh_at = None
            self._effective_countdown_seconds = None
            self._emergency_source = self._emergency_percent = None
            self._fired = False
            if not value["enabled"]:
                self._suppressed = False
            self._transition("unknown" if value["enabled"] else "disabled",
                             "Esperando una lectura válida del UPS después de actualizar la protección."
                             if value["enabled"] else "Protección automática desactivada.")
        return self.status()

    def cancel(self):
        if self._battery_since is not None and not self._fired:
            self._deadline = None
            self._suppressed = True
            self._transition("cancelled", "Cancelado hasta el próximo corte de energía")
        return self.status()

    def _fresh(self, sample):
        if not isinstance(sample, dict):
            return False
        if "connected" in sample and sample["connected"] is not True:
            return False
        try:
            age = (self._utc_clock() - _timestamp(sample.get("timestamp"))).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return False
        return -5 <= age <= self.stale_after and type(sample.get("on_battery")) is bool

    @staticmethod
    def _number(value):
        return type(value) in (int, float) and math.isfinite(value) and value >= 0

    def evaluate(self, sample):
        now = self._clock()
        if not self.policy["enabled"]:
            self._deadline = self._battery_since = self._last_fresh_at = None
            self._effective_countdown_seconds = None
            self._emergency_source = self._emergency_percent = None
            self._transition("disabled")
            return self.status()
        if not self._fresh(sample):
            self._deadline = self._battery_since = self._last_fresh_at = None
            self._effective_countdown_seconds = None
            self._emergency_source = self._emergency_percent = None
            self._transition("unknown", "Sin datos recientes y confirmados del UPS")
            return self.status()
        # Calling evaluate infrequently must not count unobserved time as safe.
        if self._last_fresh_at is not None and now - self._last_fresh_at > self.stale_after:
            self._deadline = self._battery_since = None
            self._effective_countdown_seconds = None
            self._emergency_source = self._emergency_percent = None
        self._last_fresh_at = now
        if sample["on_battery"] is False:
            self._deadline = self._battery_since = None
            self._effective_countdown_seconds = None
            self._emergency_source = self._emergency_percent = None
            self._suppressed = self._fired = False
            self._transition("online", "Suministro eléctrico disponible")
            return self.status()
        if self._battery_since is None:
            self._battery_since = now
        if self._fired:
            return self.status()
        if self._suppressed:
            self._transition("cancelled", "Cancelado hasta el próximo corte de energía")
            return self.status()
        percent, source = battery_percentage(sample)
        threshold = self.policy["low_battery_percent"]
        hardware_critical = sample.get("low_battery") is True
        percentage_low = threshold > 0 and percent is not None and percent <= threshold
        starting_countdown = self._deadline is None
        if starting_countdown:
            self._countdown_id = secrets.token_hex(16)
            self._deadline = now + self.policy["countdown_seconds"]
            self._effective_countdown_seconds = self.policy["countdown_seconds"]
        if hardware_critical or percentage_low:
            urgent_deadline = now if hardware_critical else now + 10
            if urgent_deadline < self._deadline:
                self._deadline = urgent_deadline
            self._effective_countdown_seconds = min(self.policy["countdown_seconds"],
                max(0 if hardware_critical else 10, math.floor(self._deadline - self._battery_since + 1e-6)))
            if hardware_critical:
                self._emergency_source, self._emergency_percent = "ups_critical_alarm", percent
                reason = "El UPS reporta batería crítica: se solicita la protección de Windows inmediatamente."
            else:
                self._emergency_source, self._emergency_percent = source, percent
                origin = "estimado" if source == "estimated_percentage" else "reportado"
                reason = f"Batería baja: {percent:g} % {origin} (umbral {threshold} %). Protección prioritaria en un máximo de 10 segundos."
            self._transition("countdown", reason)
        elif starting_countdown:
            self._transition("countdown", "Corte de energía detectado")
        if self._deadline is not None and now >= self._deadline:
            self._fired = True
            self._deadline = None
            if self._action_callback is None:
                self._transition("action_failed", "No hay ejecutor de apagado configurado")
            else:
                self._transition("action_started", "Ejecutando " + self.policy["action"])
                try:
                    self._action_callback(self.policy["action"])
                except Exception as exc:
                    self._transition("action_failed", str(exc))
        return self.status()

    def status(self):
        now = self._clock()
        countdown = self._state == "countdown" and self._deadline is not None and self._countdown_id is not None
        age = now - self._last_fresh_at if self._last_fresh_at is not None else None
        valid_for_ms = max(0, int(3000 - age * 1000)) if countdown and age is not None and age >= 0 else 0
        return {
            "state": self._state, "enabled": self.policy["enabled"], "reason": self._reason,
            "countdown_id": self._countdown_id if countdown else None,
            "countdown_valid_for_ms": valid_for_ms,
            "cancelable": bool(countdown and self._deadline > now and valid_for_ms > 0 and not self._fired),
            "countdown_remaining": max(0, math.ceil(self._deadline - now)) if self._deadline is not None else None,
            "on_battery_seconds": max(0, int(now - self._battery_since)) if self._battery_since is not None else 0,
            "action": self.policy["action"], "suppressed": self._suppressed,
            "configured_countdown_seconds": self.policy["countdown_seconds"],
            "effective_countdown_seconds": self._effective_countdown_seconds,
            "emergency": {"active": self._emergency_source is not None, "source": self._emergency_source,
                          "percent": self._emergency_percent, "threshold_percent": self.policy["low_battery_percent"],
                          "seconds_limit": 10, "immediate": self._emergency_source == "ups_critical_alarm"},
            "policy": dict(self.policy),
        }
