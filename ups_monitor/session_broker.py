"""Bounded, short-lived desktop delivery from a session-zero monitor.

This is not an execution broker. Its only client operation reads notices and
reports delivery availability; the normal same-origin/CSRF checks wrap it in
the HTTP handler. Neither messages nor clients may supply executable paths.
"""
from __future__ import annotations

from collections import deque
import re
import threading
import time


DELIVERY_FIELDS = {"last_error": 200, "last_sound_error": 200, "popup_error": 200,
                   "last_delivered_at": 80, "last_popup_shown_at": 80}


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
    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._queue = deque(maxlen=32)
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

    def _prune(self, now):
        self._clients = {key: value for key, value in self._clients.items()
                         if now - value["seen"] < 10}
        while self._queue and now - self._queue[0]["created"] > 10:
            self._queue.popleft()

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

    def sync_persistent(self, messages):
        with self._lock:
            self._persistent = {str(key)[:80]: {"title": str(value["title"])[:160],
                                               "message": str(value["message"])[:2000]}
                                for key, value in list(messages.items())[:16]}

    def poll(self, data):
        if not isinstance(data, dict) or set(data) != {"client_id", "after", "delivery"}:
            raise ValueError("Solicitud de agente no válida.")
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
            self._clients[client_id] = {"seen": now, "heartbeat": self._heartbeat_serial, **delivery}
            messages = [] if new_client or after is None or after > self._serial else [
                {key: value for key, value in item.items() if key != "created"}
                for item in self._queue if item["id"] > after and (
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
                    "diagnostic_scope": "most_recent_active_agent", **diagnostics}
