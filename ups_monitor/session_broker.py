"""Bounded, short-lived desktop delivery from a session-zero monitor.

This is not an execution broker. Its only client operation reads notices and
reports delivery availability; the normal same-origin/CSRF checks wrap it in
the HTTP handler. Neither messages nor clients may supply executable paths.
"""
from __future__ import annotations

from collections import deque
import re
import secrets
import threading
import time


DELIVERY_FIELDS = {"last_error": 200, "last_sound_error": 200, "popup_error": 200,
                   "last_delivered_at": 80, "last_popup_shown_at": 80}
SESSION_STATES = frozenset(("active", "locked", "disconnected", "unknown"))
RETAINED_EVENTS = frozenset(("power_lost", "power_restored", "battery_low", "overload", "ups_fault",
                             "connection_lost", "connection_restored", "monitor_error", "monitor_restarted",
                             "policy_countdown", "policy_cancelled", "policy_action_failed"))


def delivery_diagnostics(value):
    """Keep the existing desktop diagnostics, never arbitrary client metadata."""
    if not isinstance(value, dict) or type(value.get("ready")) is not bool:
        raise ValueError("Estado de entrega no válido.")
    result = {"ready": value["ready"]}
    for key, limit in DELIVERY_FIELDS.items():
        item = value.get(key)
        if item is not None and not isinstance(item, str):
            raise ValueError("Diagnóstico de entrega no válido.")
        result[key] = (item.replace("\0", "")[:limit] or None) if item is not None else None
    return result


class SessionBroker:
    def __init__(self, *, clock=time.monotonic, retention_seconds=86400, capacity=32):
        if type(capacity) is not int or not 1 <= capacity <= 256:
            raise ValueError("Capacidad de avisos inválida.")
        if type(retention_seconds) not in (int, float) or not 1 <= retention_seconds <= 86400:
            raise ValueError("Retención de avisos inválida.")
        self._clock = clock
        self._lock = threading.Lock()
        self._queue = deque(maxlen=32)
        self._retained = deque(maxlen=capacity)
        self._retention_seconds = retention_seconds
        self._receivers = {}
        self._conditions = {}
        self._conditions_at = None
        self._power_state = self._connection_state = "unknown"
        self._epoch = secrets.token_hex(16)
        self._dropped = 0
        self._clients = {}
        self._persistent = {}
        self._serial = 0
        self._heartbeat_serial = 0
        self._started = False
        self.cancel_callback = self.exit_callback = None

    def start(self):
        with self._lock:
            self._started = True

    def stop(self):
        with self._lock:
            self._started = False
            self._queue.clear()
            self._clients.clear()
            self._persistent.clear()
            self._retained.clear()
            self._receivers.clear()
            self._conditions.clear()

    def _prune(self, now):
        self._clients = {key: value for key, value in self._clients.items()
                         if now - value["seen"] < 10}
        while self._queue and now - self._queue[0]["created"] > 10:
            self._queue.popleft()
        while self._retained and now - self._retained[0]["created"] > self._retention_seconds:
            self._retained.popleft()
        self._receivers = {key: value for key, value in self._receivers.items()
                           if now - value["seen"] <= self._retention_seconds}
        live_ids = {item["id"] for item in self._queue} | {item["id"] for item in self._retained}
        for receiver in self._receivers.values():
            receiver["acked"].intersection_update(live_ids)
            receiver["offered"].intersection_update(live_ids)

    def notify(self, title, message, severity="info", sound=True, *, persistent=False, key=None):
        with self._lock:
            now = self._clock()
            self._prune(now)
            if not self._started or not any(item["ready"] for item in self._clients.values()):
                return False
            self._serial += 1
            self._queue.append({"id": self._serial, "created": now, "title": str(title)[:160],
                                "message": str(message)[:2000], "severity": severity,
                                "sound": bool(sound), "persistent": bool(persistent), "key": key})
            return True

    def publish_event(self, kind, title, message, severity="info", sound=True, *, persistent=False, key=None, repeat=False):
        """Retain new electrical events in RAM, independently of desktop readiness.

        Acceptance here confirms bounded broker storage, never Windows display.
        Retention does not survive service restart. Old countdown text is never
        replayed: dispatch derives its wording from freshly synchronized state.
        """
        if kind not in RETAINED_EVENTS:
            return self.notify(title, message, severity, sound, persistent=persistent, key=key)
        with self._lock:
            now = self._clock()
            self._prune(now)
            if not self._started:
                return False
            if repeat:
                # Repetitions while no desktop confirms delivery must not turn
                # one alarm into 32 sounds on unlock. A new incident's first
                # emission is never merged with an earlier occurrence.
                previous = next((item for item in reversed(self._retained) if item["kind"] == kind), None)
                if previous is not None and not previous["delivered_any"]:
                    previous.update(title=str(title)[:160], message=str(message)[:2000],
                                    severity=severity, sound=bool(sound), persistent=bool(persistent), key=key)
                    return True
            self._serial += 1
            if len(self._retained) == self._retained.maxlen:
                self._dropped += 1
            self._retained.append({"id": self._serial, "created": now, "title": str(title)[:160],
                                   "message": str(message)[:2000], "severity": severity,
                                   "sound": bool(sound), "persistent": bool(persistent), "key": key,
                                   "kind": kind, "delivered_any": False})
            return True

    def sync_conditions(self, messages, *, power_state="unknown", connection_state="unknown", enabled=True):
        """Current confirmed conditions; unrelated to sticky-popup preferences."""
        with self._lock:
            self._conditions = {str(key)[:80]: {"title": str(value["title"])[:160],
                                               "message": str(value["message"])[:2000]}
                                for key, value in list(messages.items())[:16]}
            self._conditions_at = self._clock()
            self._power_state = power_state if power_state in ("online", "battery") else "unknown"
            self._connection_state = connection_state if connection_state in ("connected", "disconnected") else "unknown"
            if enabled is False:
                self._retained.clear()

    def _delivery(self, item):
        result = {key: value for key, value in item.items()
                  if key not in ("created", "delivered_any")}
        kind = item.get("kind")
        if not kind:
            return result
        # A retained warning is an incident summary unless current telemetry
        # independently confirms that it remains active. Never infer state
        # from a title, a notification preference, or an old queued countdown.
        result["persistent"] = False
        conditions_fresh = self._conditions_at is not None and self._clock() - self._conditions_at <= 3
        power_state = self._power_state if conditions_fresh else "unknown"
        connection_state = self._connection_state if conditions_fresh else "unknown"
        if kind in self._conditions and conditions_fresh:
            result.update(self._conditions[kind])
            if kind == "policy_countdown":
                result["message"] = "Elipro confirma una cuenta regresiva de protección activa. Abre el panel para consultar el tiempo actual o cancelarla."
        elif kind == "power_lost" and power_state == "battery":
            result.update(title="Corte de energía", message="El UPS continúa alimentando desde batería. Abre Elipro para revisar el estado actual.")
        elif kind in ("power_lost", "power_restored") and power_state == "online":
            result.update(title="Corte de energía registrado", severity="info",
                          message="Se registró un corte de energía. El suministro eléctrico ya se recuperó. Puedes consultar el incidente en Elipro.")
        elif kind == "policy_countdown":
            result.update(title="Protección: aviso anterior", severity="info",
                          message="Se registró una cuenta regresiva anterior. Actualmente no hay una cuenta confirmada activa. Abre Elipro para revisar el resultado.")
        elif kind in ("battery_low", "overload", "ups_fault"):
            result.update(title=item["title"] + " · registro anterior", severity="warning",
                          message="Se registró este aviso mientras no se confirmó su entrega. La condición ya no está confirmada activa. Revisa el historial y el estado actual en Elipro.")
        elif kind in ("connection_lost", "connection_restored", "monitor_error"):
            result.update(title="Comunicación: aviso registrado", severity="info" if connection_state == "connected" else "warning",
                          message=("Se registró un problema de comunicación. El UPS vuelve a entregar datos recientes."
                                   if connection_state == "connected" else
                                   "Se registró un problema de comunicación. Revisa en Elipro si el UPS está conectado y la protección está disponible."))
        elif kind in ("power_lost", "power_restored"):
            result.update(title="Energía: aviso registrado", severity="warning",
                          message="Se registró un cambio de suministro eléctrico. Abre Elipro para comprobar el estado actual del UPS.")
        return result

    def sync_persistent(self, messages):
        with self._lock:
            self._persistent = {str(key)[:80]: {"title": str(value["title"])[:160],
                                               "message": str(value["message"])[:2000]}
                                for key, value in list(messages.items())[:16]}

    def poll(self, data):
        legacy = {"client_id", "after", "delivery"}
        version2 = legacy | {"protocol", "acks", "session_state", "epoch"}
        if not isinstance(data, dict) or set(data) not in (legacy, version2):
            raise ValueError("Solicitud de agente no válida.")
        v2 = set(data) == version2
        if v2:
            if (type(data["protocol"]) is not int or data["protocol"] != 2 or
                    not isinstance(data["session_state"], str) or data["session_state"] not in SESSION_STATES):
                raise ValueError("Protocolo de agente no válido.")
            if (not isinstance(data["acks"], list) or len(data["acks"]) > 64 or
                    any(type(value) is not int or not 0 < value <= 2**63 - 1 for value in data["acks"])):
                raise ValueError("Acuse de entrega no válido.")
            if data["epoch"] is not None and (not isinstance(data["epoch"], str) or not re.fullmatch(r"[a-f0-9]{32}", data["epoch"])):
                raise ValueError("Época de agente no válida.")
        client_id, after, delivery = data["client_id"], data["after"], data["delivery"]
        if not isinstance(client_id, str) or not re.fullmatch(r"[a-f0-9]{32}", client_id):
            raise ValueError("Agente no válido.")
        if after is not None and (type(after) is not int or not 0 <= after <= 2**63 - 1):
            raise ValueError("Cursor de agente no válido.")
        delivery = delivery_diagnostics(delivery)
        with self._lock:
            now = self._clock()
            self._prune(now)
            if not self._started:
                return {"cursor": self._serial, "messages": [], "persistent": {}, "running": False}
            if client_id not in self._clients and len(self._clients) >= 32:
                raise ValueError("Hay demasiados agentes de sesión.")
            # A fresh/restarted agent never replays the outage log. Active
            # critical conditions arrive separately through persistent state.
            new_client = client_id not in self._clients
            self._heartbeat_serial += 1
            if v2:
                delivery["session_state"] = data["session_state"]
                delivery["ready"] = delivery["ready"] and data["session_state"] == "active"
            self._clients[client_id] = {"seen": now, "heartbeat": self._heartbeat_serial, **delivery}
            all_items = sorted((*self._queue, *self._retained), key=lambda item: item["id"])
            if v2:
                if client_id not in self._receivers:
                    if len(self._receivers) >= 32:
                        raise ValueError("Hay demasiados receptores de sesión retenidos.")
                    self._receivers[client_id] = {"seen": now, "baseline": self._serial,
                                                  "acked": set(), "offered": set()}
                receiver = self._receivers[client_id]
                receiver["seen"] = now
                if data["epoch"] == self._epoch and delivery["ready"]:
                    acknowledgements = set(data["acks"]) & receiver["offered"]
                    receiver["acked"].update(acknowledgements)
                    for item in all_items:
                        if item["id"] in acknowledgements and "delivered_any" in item:
                            item["delivered_any"] = True
                messages = []
                if delivery["ready"]:
                    for item in all_items:
                        if item["id"] in receiver["acked"]:
                            continue
                        if not item.get("kind") and (new_client or after is None or item["id"] <= after):
                            continue
                        if (item.get("kind") and item["id"] <= receiver["baseline"] and
                                item["delivered_any"] and item["id"] not in receiver["offered"]):
                            continue
                        messages.append(self._delivery(item))
                        receiver["offered"].add(item["id"])
                        if len(messages) == 32:
                            break
                return {"protocol": 2, "epoch": self._epoch, "cursor": self._serial,
                        "messages": messages[:32], "persistent": dict(self._persistent) if delivery["ready"] else {},
                        "running": True, "acknowledgement": "windows-api-accepted"}
            messages = [] if new_client or after is None or after > self._serial else [
                self._delivery(item)
                for item in all_items if item["id"] > after and (
                    not item["persistent"] or item["key"] in self._persistent)]
            return {"cursor": self._serial, "messages": messages,
                    "persistent": {key: dict(value) for key, value in self._persistent.items()},
                    "running": True}

    def status(self):
        with self._lock:
            self._prune(self._clock())
            ready = self._started and any(value["ready"] for value in self._clients.values())
            # Multiple Windows sessions can be signed in. Readiness means at
            # least one desktop can receive requests; diagnostics explicitly
            # describe the session with the most recent heartbeat, not a claim
            # that every logged-in desktop showed or sounded a notification.
            latest = max(self._clients.values(), key=lambda value: value["heartbeat"], default=None)
            diagnostics = {key: latest.get(key) if latest else None for key in DELIVERY_FIELDS}
            if latest is None:
                diagnostics["last_error"] = "No hay un agente de avisos activo en una sesión de Windows. El servicio sigue vigilando el UPS."
            return {"available": ready, "ready": ready, "channel": "session-agent",
                    "session_agents": len(self._clients),
                    "pending_retained": sum(not item["delivered_any"] for item in self._retained),
                    "retained_records": len(self._retained), "retained_dropped": self._dropped,
                    "retention_seconds": self._retention_seconds, "retention_storage": "memory-until-service-stop",
                    "acknowledgement": "windows-api-accepted-not-user-read",
                    "diagnostic_scope": "most_recent_active_agent", **diagnostics}
