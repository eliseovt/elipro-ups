"""Desktop alert rules, independent of the dashboard and shutdown policy.

The injected notifier owns its nonblocking delivery queue and the Windows UI.
This module only chooses which messages to enqueue. It never executes power
actions, replays stored events, or changes a protection setting.
"""
from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timezone

from .core import iso_utc, utc_now


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
    "overload": ("Sobrecarga del UPS", "critical"),
    "ups_fault": ("Falla del UPS", "critical"),
    "connection_lost": ("Sin comunicación con el UPS", "warning"),
    "connection_restored": ("Comunicación con el UPS recuperada", "info"),
    "monitor_error": ("Error del monitor Elipro", "critical"),
    "monitor_restarted": ("Elipro se reinició", "info"),
    "policy_countdown": ("Protección: cuenta regresiva", "critical"),
    "policy_cancelled": ("Protección cancelada", "info"),
    "policy_action_failed": ("No se pudo ejecutar la protección", "critical"),
    "simulation": ("Prueba de protección sin apagado", "info"),
}
_PERSISTENT = frozenset(("battery_low", "overload", "ups_fault", "policy_countdown"))
_CONDITION_MESSAGES = {
    "battery_low": "El UPS continúa indicando batería baja. Revisa el estado del equipo.",
    "overload": "El UPS continúa indicando una carga igual o superior al 100 %.",
    "ups_fault": "El UPS continúa reportando una falla. Revisa el equipo.",
}


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
        self._last_notification = self._last_error = None
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
        if self._stopped or (not explicit and not self._settings["enabled"]):
            return False
        now = self._clock()
        previous = self._last_attempt.get(kind)
        interval = self._settings["repeat_seconds"] if repeat else 30
        if not explicit and previous is not None and now - previous < interval:
            return False
        title, severity = _EVENTS.get(kind, ("Prueba de Elipro UPS", "info"))
        sound = self._settings["sound_enabled"]
        # Count attempts too, so an unavailable backend cannot create a busy
        # retry loop or an unbounded Windows queue during telemetry errors.
        self._last_attempt[kind] = now
        if kind in self._active:
            self._active[kind]["last_attempt"] = now
        try:
            options = {"severity": severity, "sound": sound}
            if self._settings["persistent_critical"] and kind in _PERSISTENT:
                options.update(persistent=True, key=kind)
            accepted = self.notifier.notify(title, message, **options) is True
            self._last_error = None if accepted else "Windows no aceptó el aviso en la cola de entrega. Revisa el estado de los avisos."
        except Exception as exc:
            accepted = False
            self._last_error = "No se pudo enviar el aviso de Windows: " + str(exc)[:300]
        self._last_notification = {
            "kind": kind, "title": title, "message": message, "severity": severity,
            "sound": sound, "timestamp": iso_utc(self._utc_clock()),
            "repeat": repeat, "accepted": accepted,
            "persistent": self._settings["persistent_critical"] and kind in _PERSISTENT,
        }
        return accepted

    def _sync_persistent(self):
        synchronize = getattr(self.notifier, "sync_persistent", None)
        if not callable(synchronize):
            return
        messages = {}
        if self._settings["enabled"] and self._settings["persistent_critical"]:
            messages = {kind: {"title": _EVENTS[kind][0], "message": value["message"]}
                        for kind, value in self._active.items() if kind in _PERSISTENT}
        try:
            synchronize(messages)
        except Exception as exc:
            self._last_error = "No se pudo actualizar el aviso persistente: " + str(exc)[:300]

    def on_event(self, kind, message, data=None):
        """Consume newly observed events; loading stored event history is forbidden."""
        if kind not in _EVENTS:
            return
        data = data if isinstance(data, dict) else {}
        with self._lock:
            if self._stopped or data.get("error_kind") == "stopped":
                return
            if kind == "policy_countdown" and data.get("simulation") is True:
                self._active.pop("policy_countdown", None)
                self._sync_persistent()
                self._emit("simulation", "Prueba de protección: cuenta regresiva simulada. "
                           "No se apagará ni hibernará tu PC.")
                return
            if kind in ("connection_lost", "monitor_error"):
                self._connection_was_lost = True
                self._active.clear()
                self._last_attempt.pop("connection_restored", None)
            elif kind == "connection_restored":
                if not self._connection_was_lost:
                    return
                self._connection_was_lost = False
                self._last_attempt.pop("connection_lost", None)
                self._last_attempt.pop("monitor_error", None)
            elif kind in ("power_restored", "policy_cancelled", "policy_action_failed"):
                self._active.pop("policy_countdown", None)
                self._last_attempt.pop("policy_countdown", None)
            if kind == "power_lost":
                self._last_attempt.pop("power_restored", None)
            elif kind == "power_restored":
                self._last_attempt.pop("power_lost", None)
            if kind == "policy_countdown":
                message = (self._countdown_message(data) if self._number(data.get("countdown_remaining")) else
                           "Cuenta regresiva de protección iniciada. " + str(message).rstrip(".") +
                           ". Abre Elipro para revisar o cancelar la protección.")
            if kind in _PERSISTENT:
                self._active.setdefault(kind, {"last_attempt": None})["message"] = str(message)
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
            protection = protection if isinstance(protection, dict) else {}
            fresh = self._fresh(sample, connection)
            conditions = {}
            if fresh:
                battery, load = sample.get("battery_percent"), sample.get("load_percent")
                conditions = {
                    "battery_low": sample.get("low_battery") is True or (self._number(battery) and battery <= 20),
                    "overload": self._number(load) and load >= 100,
                    "ups_fault": sample.get("fault") is True,
                }
            # A countdown must be real and backed by current telemetry. A visual
            # browser simulation is deliberately not a repeated protection alert.
            countdown = fresh and protection.get("state") == "countdown" and protection.get("simulation") is not True
            conditions["policy_countdown"] = countdown
            emissions = []
            for kind, active in conditions.items():
                if not active:
                    self._active.pop(kind, None)
                    if fresh:
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
                previous = record["last_attempt"]
                if previous is None or self._clock() - previous >= self._settings["repeat_seconds"]:
                    emissions.append((kind, message, previous is not None))
            # Publish current conditions before queueing notices. The native
            # thread can then reject a warning invalidated while it was queued.
            self._sync_persistent()
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
            try:
                delivery = self.notifier.status()
            except Exception as exc:
                delivery = {"state": "error", "error": "No se pudo consultar el sistema de avisos: " + str(exc)[:300]}
            return {
                "settings": dict(self._settings), "delivery": delivery,
                "last_notification": dict(self._last_notification) if self._last_notification else None,
                "last_error": self._last_error,
            }
