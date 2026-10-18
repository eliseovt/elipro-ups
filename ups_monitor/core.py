"""Persistence and conservative, deterministic automatic shutdown policy.

The policy never performs an OS action itself. Its injected callback receives
``shutdown`` or ``hibernate`` once, only after continuously fresh battery data.
"""

from __future__ import annotations

import json
import base64
import hashlib
import math
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_POLICY = {
    "enabled": False,
    "on_battery_seconds": 300,
    "low_battery_percent": 20,
    "min_runtime_seconds": 180,
    "countdown_seconds": 60,
    "action": "shutdown",
}
SAMPLE_FIELDS = (
    "input_voltage", "output_voltage", "frequency_hz", "load_percent",
    "battery_percent", "battery_voltage", "runtime_seconds", "temperature_c",
    "on_battery", "low_battery", "fault", "estimated_watts", "raw_status",
)


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
    limits = {
        "on_battery_seconds": (0, 86400),
        "low_battery_percent": (0, 100),
        "min_runtime_seconds": (0, 86400),
        "countdown_seconds": (10, 3600),
    }
    for key, (lower, upper) in limits.items():
        if type(result[key]) is not int or not lower <= result[key] <= upper:
            raise ValueError(f"{key} debe ser entero entre {lower} y {upper}")
    if result["action"] not in ("shutdown", "hibernate"):
        raise ValueError("action debe ser shutdown o hibernate")
    return result


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
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=busy_timeout_ms / 1000)
        try:
            with self._db:
                self._db.execute("PRAGMA journal_mode=WAL")
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
        with self._lock, self._db:
            self._db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            self._db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self._last_prune = now

    def add_sample(self, sample):
        normalized = {field: sample.get(field) for field in SAMPLE_FIELDS}
        normalized["timestamp"] = iso_utc(_timestamp(sample["timestamp"]))
        normalized["watts_is_estimate"] = sample.get("watts_is_estimate") is True
        ts = _timestamp(normalized["timestamp"]).timestamp()
        # Prune before the insert: a cleanup failure must not turn a committed
        # write into an apparent failure that a persistence worker would retry.
        self._prune()
        with self._lock, self._db:
            self._db.execute("INSERT INTO samples(ts,timestamp,payload) VALUES(?,?,?)",
                             (ts, normalized["timestamp"], json.dumps(normalized, allow_nan=False)))
        return normalized

    def add_event(self, kind, message, data=None, *, timestamp=None):
        stamp = iso_utc(_timestamp(timestamp)) if timestamp is not None else iso_utc()
        data = {} if data is None else data
        self._prune()
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT INTO events(ts,timestamp,kind,message,data) VALUES(?,?,?,?,?)",
                (_timestamp(stamp).timestamp(), stamp, kind, message, json.dumps(data, allow_nan=False)))
            event_id = cursor.lastrowid
        return {"id": event_id, "timestamp": stamp, "kind": kind, "message": message, "data": data}

    def get_setting(self, key, default=None):
        with self._lock:
            row = self._db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        payload = json.dumps(value, allow_nan=False)
        with self._lock, self._db:
            self._db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, payload))

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

    def incidents(self, *, start=None, end=None, limit=50, cursor=None):
        """Group evidence of power and communication failures independently.

        Scan retained events before filtering overlaps, so an incident starting
        before the requested interval is still visible. Missing earlier records
        cannot be reconstructed; no synthetic outage is invented on restoration.
        """
        filters = self._event_filters(start, end)
        self._page_limit(limit)
        position = self._decode_cursor(cursor, filters, "incidents")
        with self._lock:
            high = position["high"] if position else self._db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
        before = position["before"] if position else high + 1
        active, incidents = {}, []
        opening = {"power_lost": "power", "connection_lost": "communication", "monitor_error": "communication"}
        recovery = {"power_restored": "power", "connection_restored": "communication"}
        action_kinds = {"policy_countdown", "policy_cancelled", "policy_action_started", "policy_action_failed", "policy_unknown", "simulation"}
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
        now = utc_now()
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
        selected.sort(key=lambda item: item["opening_event_id"], reverse=True)
        total = len(selected)
        rows = [item for item in selected if item["opening_event_id"] < before][:limit + 1]
        more, rows = len(rows) > limit, rows[:limit]
        return {"incidents": rows, "total": total, "limit": limit, **filters,
                "next_cursor": self._encode_cursor(high, rows[-1]["opening_event_id"], filters, "incidents") if more else None,
                "retention_days": self.retention_days,
                "note": "Duración detectada entre lecturas; los incidentes anteriores a la retención pueden estar incompletos."}

    def backup(self, destination):
        """Write a consistent, standalone SQLite snapshot, including WAL data."""
        target_path = Path(destination)
        if self.path != ":memory:" and target_path.resolve() == Path(self.path).resolve():
            raise ValueError("El respaldo debe usar otro archivo")
        if target_path.exists():
            raise ValueError("El archivo de respaldo ya existe")
        connection = None
        deadline = time.monotonic() + 20
        def progress(status, remaining, total):
            if time.monotonic() > deadline:
                raise TimeoutError("El respaldo excedió el plazo de 20 segundos")
        try:
            connection = sqlite3.connect(str(target_path), timeout=0.1)
            with self._lock:
                self._db.backup(connection, pages=128, progress=progress, sleep=0.01)
            counts = {key + "_count": connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                      for key, table in (("event", "events"), ("sample", "samples"), ("settings", "settings"))}
            return {"created_at": iso_utc(), **counts}
        finally:
            if connection is not None:
                connection.close()

    def close(self):
        with self._lock:
            self._db.close()


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
        self._suppressed = self._fired = False
        self._state, self._reason = "disabled", None

    def _transition(self, state, reason=None):
        changed = state != self._state or reason != self._reason
        self._state, self._reason = state, reason
        if changed and self._event_callback:
            self._event_callback("policy_" + state, reason or state, {"state": state})

    def update_policy(self, policy):
        value = validate_policy(policy)
        changed = value != self.policy
        self.policy = value
        if changed:
            self._deadline = self._battery_since = self._last_fresh_at = None
            self._fired = False
            if not value["enabled"]:
                self._suppressed = False
            self._transition("unknown" if value["enabled"] else "disabled")
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
            self._transition("disabled")
            return self.status()
        if not self._fresh(sample):
            self._deadline = self._battery_since = self._last_fresh_at = None
            self._transition("unknown", "Sin datos recientes y confirmados del UPS")
            return self.status()
        # Calling evaluate infrequently must not count unobserved time as safe.
        if self._last_fresh_at is not None and now - self._last_fresh_at > self.stale_after:
            self._deadline = self._battery_since = None
        self._last_fresh_at = now
        if sample["on_battery"] is False:
            self._deadline = self._battery_since = None
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
        reasons = []
        if now - self._battery_since >= self.policy["on_battery_seconds"]:
            reasons.append("Tiempo configurado en batería alcanzado")
        if sample.get("low_battery") is True:
            reasons.append("El UPS indica batería baja")
        battery = sample.get("battery_percent")
        if self._number(battery) and battery <= self.policy["low_battery_percent"]:
            reasons.append("Carga de batería en el umbral configurado")
        runtime = sample.get("runtime_seconds")
        if self._number(runtime) and runtime <= self.policy["min_runtime_seconds"]:
            reasons.append("Autonomía en el umbral configurado")
        if self._deadline is None and reasons:
            self._deadline = now + self.policy["countdown_seconds"]
            self._transition("countdown", reasons[0])
        elif self._deadline is None:
            self._transition("on_battery", "UPS alimentando desde batería")
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
        return {
            "state": self._state, "enabled": self.policy["enabled"], "reason": self._reason,
            "countdown_remaining": max(0, math.ceil(self._deadline - now)) if self._deadline is not None else None,
            "on_battery_seconds": max(0, int(now - self._battery_since)) if self._battery_since is not None else 0,
            "action": self.policy["action"], "suppressed": self._suppressed,
            "policy": dict(self.policy),
        }
