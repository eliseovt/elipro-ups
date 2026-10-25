"""Desktop alert rules, independent of the dashboard and shutdown policy.

The injected notifier owns its nonblocking delivery queue and the Windows UI.
This module only chooses which messages to enqueue. It never executes power
actions, replays stored events, or changes a protection setting.
"""
from __future__ import annotations

import math
import secrets
import threading
import time
from datetime import datetime, timezone

from .core import battery_percentage, iso_utc, utc_now
from .notification_priority import age_notice_metadata, normalize_notice_metadata, notification_priority


DEFAULT_NOTIFICATION_SETTINGS = {
    "enabled": True,
    "sound_enabled": True,
    "persistent_critical": False,
    "repeat_seconds": 30,
}

_EVENTS = {
    "power_lost": ("Corte de energía", "warning"),
    "power_restored": ("Suministro eléctrico recuperado", "info"),
    "battery_low": ("Batería baja del UPS", "critical"),
    "battery_low_percentage": ("Porcentaje estimado de batería bajo", "critical"),
    "overload": ("Sobrecarga del UPS", "critical"),
    "ups_fault": ("Falla del UPS", "critical"),
    "connection_lost": ("Sin comunicación con el UPS", "warning"),
    "connection_restored": ("Comunicación con el UPS recuperada", "info"),
    "monitor_error": ("Error del monitor Elipro", "critical"),
    "monitor_restarted": ("Elipro se reinició", "info"),
    "policy_countdown": ("Protección: cuenta regresiva", "critical"),
    "policy_cancelled": ("Protección cancelada", "info"),
    "policy_action_failed": ("No se pudo ejecutar la protección", "critical"),
    "policy_migration_review": ("Revisa el tiempo de protección", "warning"),
    "ups_poweroff_blocked": ("No se programó el apagado del UPS", "warning"),
    "ups_poweroff_unconfirmed": ("Corte del UPS sin confirmar", "critical"),
    "ups_cancel_test_unconfirmed": ("Prueba del UPS sin confirmar", "critical"),
    "ups_battery_test_unconfirmed": ("Prueba del UPS en batería sin confirmar", "critical"),
    "ups_battery_test_windows_unconfirmed": ("Solicitud de hibernación sin confirmar", "info"),
    "windows_action_unconfirmed": ("Solicitud a Windows sin confirmar", "info"),
    "ups_poweroff_reviewed": ("Revisión del corte del UPS cerrada", "info"),
    "simulation": ("Prueba de protección sin apagado", "info"),
}
_PERSISTENT = frozenset(("battery_low", "battery_low_percentage", "overload", "ups_fault", "policy_countdown"))
# Acknowledgement and recovery are independent. Every real incident remains in
# this inbox until a user explicitly presses Entendido. A recovered condition
# can close its desktop window without acknowledging or deleting the record.
_INBOX_EVENTS = frozenset(_EVENTS) - {"simulation"}
_INFORMATIONAL_EVENTS = frozenset((
    "power_restored", "connection_restored", "monitor_restarted",
    "policy_cancelled", "ups_poweroff_reviewed",
))
# These observations form persistence barriers. The history worker flushes
# queued telemetry and the transition together; selecting an alert never waits
# for SQLite or asks the desktop notifier to make a durability decision.
CRITICAL_HISTORY_EVENTS = frozenset((
    "power_lost", "power_restored", "battery_low", "battery_low_percentage", "overload", "ups_fault",
    "connection_lost", "connection_restored", "monitor_error", "monitor_restarted",
    "policy_countdown", "policy_cancelled", "policy_action_started", "policy_action_failed",
    "policy_unknown", "policy_migration_review",
    "ups_poweroff_submitted", "ups_poweroff_unconfirmed", "ups_poweroff_cancel_requested", "ups_poweroff_blocked",
    "ups_cancel_test_unconfirmed", "ups_cancel_test_reviewed",
    "ups_battery_test_unconfirmed", "ups_battery_test_reviewed",
    "ups_battery_test_windows_unconfirmed", "windows_action_unconfirmed",
    "ups_poweroff_reviewed",
))
_CONDITION_MESSAGES = {
    "battery_low": "El UPS continúa indicando batería baja. Revisa el estado del equipo.",
    "battery_low_percentage": "La carga estimada continúa en el umbral de batería baja; se prioriza la protección de Windows.",
    "overload": "El UPS continúa indicando una carga igual o superior al 100 %.",
    "ups_fault": "El UPS continúa reportando una falla. Revisa el equipo.",
}


def _requires_attention(notice):
    """Keep recovery separate from user acknowledgement and event severity."""
    return notice.get("resolved_at") is None and notice["kind"] not in _INFORMATIONAL_EVENTS


def validate_notification_settings(value):
    """Validate settings without coercing strings, floats or boolean integers."""
    if not isinstance(value, dict):
        raise ValueError("Los avisos deben configurarse con un objeto")
    unknown = set(value) - set(DEFAULT_NOTIFICATION_SETTINGS)
    if unknown:
        raise ValueError("Opciones de avisos desconocidas: " + ", ".join(sorted(unknown)))
    result = {**DEFAULT_NOTIFICATION_SETTINGS, **value}
    for key in ("enabled", "sound_enabled", "persistent_critical"):
        if type(result[key]) is not bool:
            raise ValueError(key + " debe ser booleano")
    if type(result["repeat_seconds"]) is not int or not 10 <= result["repeat_seconds"] <= 300:
        raise ValueError("repeat_seconds debe ser entero entre 10 y 300")
    return result


class AlertManager:
    """Thread-safe selection and repetition of backend-owned desktop alerts.

    ``notifier`` implements start(), notify(title, message, severity, sound),
    status(), and stop(). notify() must enqueue without waiting for Windows and
    returns whether its queue accepted the message. status() reports actual
    delivery availability separately from that acceptance.

    Call update() from real monitor ticks, never from an HTTP read. status() is
    side-effect free. A 30 second cooldown limits repeated event emissions;
    persistent critical conditions repeat at the configured interval only while
    fresh telemetry (or an active real protection countdown) confirms them.
    """

    def __init__(self, store, notifier, *, clock=time.monotonic, utc_clock=utc_now):
        self.store, self.notifier = store, notifier
        self._clock, self._utc_clock = clock, utc_clock
        self._lock = threading.RLock()
        self._settings_lock = threading.Lock()
        self._started = self._stopped = False
        self._connection_was_lost = False
        self._last_attempt = {}
        self._active = {}
        self._event_confirmations = {}
        self._countdown = None
        self._last_notification = self._last_error = None
        self._context_power = self._context_connection = "unknown"
        self._pending = {}
        self._incidents = {}
        self._incident_records = {}
        self._battery_low_sources = set()
        self._dirty = set()
        self._last_persist_retry = None
        self._inbox_error = None
        try:
            self._pending = {item["id"]: dict(item) for item in store.pending_notifications()}
            self._incident_records = {item["id"]: dict(item) for item in store.notification_incidents()}
        except Exception as exc:
            self._inbox_error = "No se pudieron cargar los avisos pendientes: " + str(exc)[:300]
        # Both views must share a record. Sources learned after a restart must
        # survive a later acknowledgement taken from the pending inbox.
        self._incident_records.update(self._pending)
        for item in self._incident_records.values():
            # Prior countdowns never assert that a current action is pending.
            # First live observations will confirm recovery independently.
            if item.get("resolved_at") is None and item["kind"] in (_PERSISTENT | {"power_lost", "connection_lost", "monitor_error", "policy_migration_review"}):
                self._incidents[item["kind"]] = item["id"]
                if item["kind"] == "battery_low":
                    self._battery_low_sources.update(source for source in item.get("condition_sources", [])
                                                     if source in ("signal", "reported_percentage"))
        self._loaded_notice_ids = set(self._pending) | set(self._incident_records)
        self._restored_countdown_notice = self._incidents.get("policy_countdown")
        self.notifier.acknowledge_callback = self.acknowledge
        try:
            self._settings = validate_notification_settings(
                store.get_setting("notification_settings", DEFAULT_NOTIFICATION_SETTINGS))
        except (ValueError, TypeError):
            self._settings = dict(DEFAULT_NOTIFICATION_SETTINGS)
            self._last_error = "La configuración de avisos guardada no era válida; se usan los valores predeterminados."

    def start(self):
        with self._lock:
            if self._started or self._stopped:
                return
            try:
                self.notifier.start()
                self._started = True
                self._sync_persistent()
            except Exception as exc:
                self._last_error = "No se pudieron iniciar los avisos de Windows: " + str(exc)[:300]

    def stop(self):
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._active.clear()
        # The notifier joins its window thread. A final tray callback on that
        # thread may reenter on_event(), so never join while holding our lock.
        try:
            self.notifier.stop()
        except Exception as exc:
            with self._lock:
                self._last_error = "No se pudieron cerrar los avisos: " + str(exc)[:300]

    def _emit(self, kind, message, *, repeat=False, explicit=False):
        if self._stopped:
            return False
        now = self._clock()
        previous = self._last_attempt.get(kind)
        interval = self._settings["repeat_seconds"] if repeat else 30
        limited = not explicit and previous is not None and now - previous < interval
        title, severity = _EVENTS.get(kind, ("Prueba de Elipro UPS", "info"))
        notice = None
        acknowledged_incident = False
        if kind in _INBOX_EVENTS:
            notice_id = self._incidents.get(kind)
            notice = self._pending.get(notice_id)
            acknowledged_incident = notice_id is not None and notice is None
            if not repeat and notice is None and notice_id is None:
                notice_id = secrets.randbits(52) or 1
                while notice_id in self._pending:
                    notice_id = secrets.randbits(52) or 1
                notice = {"id": notice_id, "kind": kind, "title": title, "message": str(message),
                          "severity": severity, "timestamp": iso_utc(self._utc_clock()),
                          "resolved_at": None, "acknowledged_at": None}
                if kind == "battery_low" and self._battery_low_sources:
                    notice["condition_sources"] = sorted(self._battery_low_sources)
                self._pending[notice_id] = notice
                self._loaded_notice_ids.add(notice_id)
                self._incident_records[notice_id] = notice
                if kind in (_PERSISTENT | {"power_lost", "connection_lost", "monitor_error", "policy_migration_review"}):
                    self._incidents[kind] = notice_id
                self._save_notice(notice)
            self._sync_persistent()
        if limited:
            # Rate-limit desktop delivery, never a new unread incident.
            return False
        sound = self._settings["sound_enabled"]
        # Count attempts too, so an unavailable backend cannot create a busy
        # retry loop or an unbounded Windows queue during telemetry errors.
        self._last_attempt[kind] = now
        if kind in self._active:
            self._active[kind]["last_attempt"] = now
        if acknowledged_incident or (not explicit and not self._settings["enabled"]):
            return False
        try:
            options = {"severity": severity, "sound": sound}
            if (notice is not None and _requires_attention(notice)
                    and callable(getattr(self.notifier, "sync_persistent", None))):
                options.update(persistent=True, key="notice:" + str(notice["id"]))
            elif kind not in _INBOX_EVENTS and self._settings["persistent_critical"] and kind in _PERSISTENT:
                options.update(persistent=True, key=kind)
            publisher = getattr(type(self.notifier), "publish_event", None)
            if getattr(type(self.notifier), "supports_notification_priority", False) is True:
                options["priority"] = self._notice_metadata(notice)["priority"] if notice else notification_priority(kind)
            accepted = (self.notifier.publish_event(kind, title, message, repeat=repeat, **options)
                        if callable(publisher) else self.notifier.notify(title, message, **options)) is True
            self._last_error = None if accepted else "El canal de avisos no aceptó el aviso en la cola de entrega. Revisa el estado de los avisos."
        except Exception as exc:
            accepted = False
            self._last_error = "No se pudo enviar el aviso de Windows: " + str(exc)[:300]
        self._last_notification = {
            "kind": kind, "title": title, "message": message, "severity": severity,
            "sound": sound, "timestamp": iso_utc(self._utc_clock()),
            "repeat": repeat, "accepted": accepted,
            "persistent": options.get("persistent", False),
        }
        return accepted

    def _save_notice(self, notice):
        try:
            self.store.put_notification(notice)
            self._dirty.discard(notice["id"])
            if not self._dirty:
                self._inbox_error = None
        except Exception as exc:
            self._dirty.add(notice["id"])
            self._inbox_error = "El aviso sigue pendiente, pero no se pudo guardar en disco: " + str(exc)[:300]

    def _refresh_inbox(self):
        # ResilientStore reads its cache here; recovery after an unavailable
        # startup can restore older unread notices without restarting Elipro.
        try:
            records = self.store.pending_notifications() + self.store.notification_incidents()
        except Exception:
            return
        for item in records:
            if item["id"] in self._loaded_notice_ids:
                continue
            self._loaded_notice_ids.add(item["id"])
            value = dict(item)
            self._incident_records[item["id"]] = value
            if value.get("acknowledged_at") is None:
                self._pending[item["id"]] = value
            if value.get("resolved_at") is None and value["kind"] in (_PERSISTENT | {"power_lost", "connection_lost", "monitor_error", "policy_migration_review"}):
                self._incidents.setdefault(value["kind"], value["id"])
                if value["kind"] == "battery_low" and self._incidents["battery_low"] == value["id"]:
                    self._battery_low_sources.update(source for source in value.get("condition_sources", [])
                                                     if source in ("signal", "reported_percentage"))

    def _resolve(self, *kinds):
        stamp = iso_utc(self._utc_clock())
        for kind in kinds:
            notice_id = self._incidents.pop(kind, None)
            notice = self._pending.get(notice_id) or self._incident_records.get(notice_id)
            if notice is not None and notice.get("resolved_at") is None:
                notice["resolved_at"] = stamp
                self._save_notice(notice)
            if kind == "battery_low":
                self._battery_low_sources.clear()

    def _notice_message(self, notice):
        kind = notice["kind"]
        if notice.get("resolved_at") is not None:
            return notice["message"] + " La condición terminó; el aviso quedó guardado en el historial."
        if kind == "policy_countdown":
            if kind in self._active:
                return "Elipro confirma una cuenta regresiva de protección activa. Abre el panel para consultar el tiempo actual o cancelarla."
            return "Se registró una cuenta regresiva de protección anterior. Revisa el estado actual y su resultado en Elipro."
        return notice["message"]

    def acknowledge(self, ids):
        """Commit explicit user acknowledgement before removing any notice."""
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 200 or
                any(type(value) is not int or not 0 < value <= 2**53 - 1 for value in ids)):
            raise ValueError("Selecciona entre 1 y 200 identificadores de avisos válidos.")
        with self._lock:
            stamp = iso_utc(self._utc_clock())
            records = [{**self._pending[value], "acknowledged_at": stamp}
                       for value in set(ids) if value in self._pending]
            if records:
                self.store.acknowledge_notifications(records)
                for item in records:
                    self._pending.pop(item["id"], None)
                    self._incident_records[item["id"]] = item
                    self._dirty.discard(item["id"])
                self._sync_persistent()
            return self.status()

    def _sync_persistent(self):
        synchronize_conditions = getattr(type(self.notifier), "sync_conditions", None)
        if callable(synchronize_conditions):
            # Session-zero delivery needs semantic state even when users do
            # not want sticky popups. Queued countdowns must never imply that
            # an expired/cancelled power action is still pending after unlock.
            current = ({kind: self._notice_metadata({"kind": kind, "title": _EVENTS[kind][0],
                                                    "message": value["message"], "resolved_at": None})
                        for kind, value in self._active.items()} if self._settings["enabled"] else {})
            self.notifier.sync_conditions(current, power_state=self._context_power,
                                          connection_state=self._context_connection,
                                          enabled=self._settings["enabled"])
        synchronize = getattr(self.notifier, "sync_persistent", None)
        if not callable(synchronize):
            return
        messages = ({"notice:" + str(item["id"]): self._notice_metadata(item)
                     for item in sorted(self._pending.values(), key=self._notice_sort_key)
                     if _requires_attention(item)}
                    if self._settings["enabled"] else {})
        try:
            synchronize(messages)
        except Exception as exc:
            self._last_error = "No se pudo actualizar el aviso persistente: " + str(exc)[:300]

    def _notice_sort_key(self, notice):
        return (-self._notice_metadata(notice)["priority"], notice["timestamp"], notice["id"])

    def _countdown_snapshot(self):
        if self._countdown is None:
            return None
        return age_notice_metadata(self._countdown["value"], self._clock() - self._countdown["received"])["countdown"]

    def _observe_countdown(self, protection):
        value = normalize_notice_metadata({"title": "Protección", "message": "Cuenta activa", "countdown": {
            "countdown_id": protection.get("countdown_id"), "action": protection.get("action"),
            "remaining_seconds": protection.get("countdown_remaining"),
            "valid_for_ms": protection.get("countdown_valid_for_ms", 0),
            "cancelable": protection.get("cancelable", True)}})
        countdown = value.get("countdown")
        if countdown is None:
            self._countdown = None
            return
        previous = self._countdown_snapshot()
        if ((previous is not None and previous["countdown_id"] != countdown["countdown_id"])
                or self._restored_countdown_notice is not None):
            self._resolve("policy_countdown")
            self._active.pop("policy_countdown", None)
            self._last_attempt.pop("policy_countdown", None)
            self._restored_countdown_notice = None
        if previous is not None and previous["countdown_id"] == countdown["countdown_id"]:
            countdown["remaining_seconds"] = min(previous["remaining_seconds"], countdown["remaining_seconds"])
        self._countdown = {"value": value, "received": self._clock()}

    def _notice_metadata(self, notice):
        kind = notice["kind"]
        active = self._active.get(kind)
        confirmed_until = active.get("confirmed_until", 0) if active else self._event_confirmations.get(kind, 0)
        confirmed_for_ms = max(0, min(3000, (confirmed_until - self._clock()) * 1000))
        confirmed = bool(confirmed_for_ms and _requires_attention(notice))
        countdown = self._countdown_snapshot() if kind == "policy_countdown" else None
        current_countdown = (confirmed and countdown is not None and countdown["valid_for_ms"] > 0
                             and countdown["remaining_seconds"] > 0)
        priority = notification_priority(kind, countdown_active=current_countdown)
        if not _requires_attention(notice):
            priority = 0
        result = {"title": notice["title"], "message": self._notice_message(notice),
                  "priority": priority, "severity": _EVENTS.get(kind, (None, "info"))[1],
                  "sound": self._settings["sound_enabled"], "repeat_seconds": self._settings["repeat_seconds"],
                  "confirmed": confirmed, "confirmed_for_ms": confirmed_for_ms}
        if countdown is not None and _requires_attention(notice):
            result["countdown"] = countdown
            if not current_countdown:
                result["confirmed"] = False
        return result

    def on_event(self, kind, message, data=None):
        """Consume newly observed events; loading stored event history is forbidden."""
        if kind == "policy_configured":
            with self._lock:
                self._resolve("policy_migration_review")
                self._sync_persistent()
            return
        if kind in ("policy_action_started", "policy_unknown", "policy_disabled", "policy_online", "policy_on_battery"):
            with self._lock:
                if kind == "policy_unknown":
                    self._active.clear()
                    self._countdown = None
                    for condition in _PERSISTENT:
                        self._event_confirmations.pop(condition, None)
                    self._context_power = self._context_connection = "unknown"
                else:
                    self._active.pop("policy_countdown", None)
                    self._countdown = None
                self._resolve("policy_countdown")
                self._sync_persistent()
            return
        if kind not in _EVENTS:
            return
        data = data if isinstance(data, dict) else {}
        with self._lock:
            if self._stopped or data.get("error_kind") == "stopped":
                return
            self._event_confirmations[kind] = self._clock() + 3
            if kind == "policy_countdown" and data.get("simulation") is True:
                self._active.pop("policy_countdown", None)
                self._sync_persistent()
                self._emit("simulation", "Prueba de protección: cuenta regresiva simulada. "
                           "No se apagará ni hibernará tu PC.")
                return
            if kind in ("connection_lost", "monitor_error"):
                self._context_power = self._context_connection = "unknown"
                self._connection_was_lost = True
                self._active.clear()
                self._countdown = None
                for condition in _PERSISTENT:
                    self._event_confirmations.pop(condition, None)
                self._last_attempt.pop("connection_restored", None)
            elif kind == "connection_restored":
                self._context_connection = "connected"
                if not self._connection_was_lost:
                    return
                self._connection_was_lost = False
                self._last_attempt.pop("connection_lost", None)
                self._last_attempt.pop("monitor_error", None)
                self._resolve("connection_lost", "monitor_error")
            elif kind in ("power_restored", "policy_cancelled", "policy_action_failed"):
                self._active.pop("policy_countdown", None)
                self._countdown = None
                self._last_attempt.pop("policy_countdown", None)
                self._resolve("policy_countdown")
            if kind == "power_lost":
                self._context_power = "battery"
                self._last_attempt.pop("power_restored", None)
            elif kind == "power_restored":
                self._context_power = "online"
                self._last_attempt.pop("power_lost", None)
                self._resolve("power_lost")
            if kind == "policy_countdown":
                message = (self._countdown_message(data) if self._number(data.get("countdown_remaining")) else
                           "Cuenta regresiva de protección iniciada. " + str(message).rstrip(".") +
                           ". Abre Elipro para revisar o cancelar la protección.")
            if kind == "policy_countdown":
                self._observe_countdown(data)
            if kind in _PERSISTENT:
                self._active.setdefault(kind, {"last_attempt": None})["message"] = str(message)
                self._active[kind]["confirmed_until"] = self._clock() + 3
            self._sync_persistent()
            self._emit(kind, str(message))

    @staticmethod
    def _number(value):
        return type(value) in (int, float) and math.isfinite(value)

    def _countdown_message(self, protection):
        action = "hibernar" if protection.get("action") == "hibernate" else "apagar"
        remaining = protection.get("countdown_remaining")
        return (f"Elipro solicitará {action} Windows en {max(0, math.ceil(remaining))} segundos. "
                "Abre el panel para revisar o cancelar la protección."
                if self._number(remaining) else
                f"Elipro tiene una cuenta regresiva activa para {action} Windows. Abre el panel para revisarla.")

    def _battery_low_condition(self, sample, threshold, percent, percent_source):
        """Require the sources that raised this incident to confirm recovery."""
        signal = sample.get("low_battery")
        reported = percent_source == "reported_percentage"
        if signal is True:
            self._battery_low_sources.add("signal")
        if threshold > 0 and reported and percent <= threshold:
            self._battery_low_sources.add("reported_percentage")
        notice = self._incident_records.get(self._incidents.get("battery_low"))
        if notice is not None and self._battery_low_sources:
            sources = sorted(self._battery_low_sources)
            if notice.get("condition_sources") != sources:
                notice["condition_sources"] = sources
                self._save_notice(notice)
        if signal is True or threshold > 0 and reported and percent <= threshold:
            return True
        normal = {"signal": signal is False,
                  "reported_percentage": threshold == 0 or reported and percent > threshold}
        if self._battery_low_sources:
            return False if all(normal[source] for source in self._battery_low_sources) else None
        return False if signal is False or reported and (threshold == 0 or percent > threshold) else None

    def _fresh(self, sample, connection):
        if not isinstance(sample, dict) or sample.get("connected", True) is not True:
            return False
        if isinstance(connection, dict):
            connection = connection.get("state")
        if connection is not None and connection != "connected":
            return False
        try:
            stamp = datetime.fromisoformat(sample["timestamp"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                return False
            age = (self._utc_clock() - stamp.astimezone(timezone.utc)).total_seconds()
            return -5 <= age <= 15
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            return False

    def update(self, protection, connection=None, sample=None):
        """Repeat confirmed conditions from a monitor tick, including without a panel."""
        with self._lock:
            if self._stopped:
                return
            self._refresh_inbox()
            protection = protection if isinstance(protection, dict) else {}
            fresh = self._fresh(sample, connection)
            self._context_connection = "connected" if fresh else "unknown"
            self._context_power = ("battery" if sample.get("on_battery") is True else
                                   "online" if sample.get("on_battery") is False else "unknown") if fresh else "unknown"
            if fresh and sample.get("on_battery") is True:
                self._event_confirmations["power_lost"] = self._clock() + 3
            connection_state = connection.get("state") if isinstance(connection, dict) else connection
            if not fresh and self._connection_was_lost and connection_state in ("disconnected", "stale"):
                self._event_confirmations["connection_lost"] = self._clock() + 3
                self._event_confirmations["monitor_error"] = self._clock() + 3
            conditions = {"battery_low": None, "battery_low_percentage": None, "overload": None, "ups_fault": None}
            if fresh:
                load = sample.get("load_percent")
                threshold = protection.get("policy", {}).get("low_battery_percent", 20)
                if type(threshold) is not int or not 0 <= threshold <= 100:
                    threshold = 20
                percent, percent_source = battery_percentage(sample)
                estimated_low = (False if sample.get("on_battery") is False or threshold == 0
                                 or percent_source == "reported_percentage" else
                                 percent <= threshold if sample.get("on_battery") is True
                                 and percent_source == "estimated_percentage" else None)
                conditions = {
                    "battery_low": self._battery_low_condition(sample, threshold, percent, percent_source),
                    "battery_low_percentage": estimated_low,
                    "overload": load >= 100 if self._number(load) else None,
                    "ups_fault": (True if sample.get("fault") is True else False
                                  if sample.get("fault") is False or sample.get("raw_status") in
                                  ("Line mode", "AVR mode", "Battery mode", "Battery test mode", "Shutdown mode") else None),
                }
            # A countdown must be real and backed by current telemetry. A visual
            # browser simulation is deliberately not a repeated protection alert.
            countdown = fresh and protection.get("state") == "countdown" and protection.get("simulation") is not True
            if countdown:
                self._observe_countdown(protection)
            elif fresh:
                self._countdown = None
            conditions["policy_countdown"] = countdown
            emissions = []
            for kind, active in conditions.items():
                if active is not True:
                    self._active.pop(kind, None)
                    self._event_confirmations.pop(kind, None)
                    if active is False and fresh:
                        self._resolve(kind)
                        # Recovery ends this incident. A new alarm must be
                        # immediate even if it starts inside the old cooldown.
                        self._last_attempt.pop(kind, None)
                    continue
                if kind == "policy_countdown":
                    message = self._countdown_message(protection)
                else:
                    message = _CONDITION_MESSAGES[kind]
                record = self._active.setdefault(kind, {"last_attempt": self._last_attempt.get(kind)})
                record["message"] = message
                record["confirmed_until"] = self._clock() + 3
                previous = record["last_attempt"]
                if previous is None or self._clock() - previous >= self._settings["repeat_seconds"]:
                    emissions.append((kind, message, previous is not None))
            # Publish current conditions before queueing notices. The native
            # thread can then reject a warning invalidated while it was queued.
            if fresh:
                if self._context_power == "online":
                    self._resolve("power_lost")
                self._resolve("connection_lost", "monitor_error")
            self._sync_persistent()
            if self._dirty and (self._last_persist_retry is None or self._clock() - self._last_persist_retry >= 30):
                self._last_persist_retry = self._clock()
                for notice_id in tuple(self._dirty):
                    notice = self._pending.get(notice_id) or self._incident_records.get(notice_id)
                    if notice is not None:
                        self._save_notice(notice)
            for kind, message, repeat in emissions:
                self._emit(kind, message, repeat=repeat)

    def update_settings(self, value):
        with self._settings_lock:
            # Validate the partial object before merging, then preserve omitted
            # settings rather than silently resetting them to global defaults.
            validate_notification_settings(value)
            with self._lock:
                settings = validate_notification_settings({**self._settings, **value})
            # Never hold the delivery lock while settings wait for disk.
            self.store.set_setting("notification_settings", settings)
            with self._lock:
                self._settings = settings
                self._sync_persistent()
                return self.status()

    def test(self):
        """Send a desktop-only test, even if automatic alerts are disabled."""
        with self._lock:
            self._emit("notification_test", "Prueba de Elipro. No se apagará ni hibernará tu PC.", explicit=True)
            return self.status()

    def status(self):
        """Return diagnostics without sending, repeating or acknowledging alerts."""
        with self._lock:
            self._refresh_inbox()
            try:
                delivery = self.notifier.status()
            except Exception as exc:
                delivery = {"state": "error", "error": "No se pudo consultar el sistema de avisos: " + str(exc)[:300]}
            return {
                "settings": dict(self._settings), "delivery": delivery,
                "last_notification": dict(self._last_notification) if self._last_notification else None,
                "last_error": self._inbox_error or self._last_error,
                "pending": [{**item, **self._notice_metadata(item),
                             "requires_attention": _requires_attention(item)} for item in
                            sorted(self._pending.values(), key=self._notice_sort_key)],
            }
