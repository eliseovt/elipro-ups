"""Deterministic integration scenarios with isolated memory data and fake I/O.

No real UPS, desktop notifier or Windows executor is instantiated. These checks
exercise rules and cancellation; they cannot certify battery autonomy or replace
a physical USB/reboot test.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading

from .adapters import TelemetryError
from .core import Store, iso_utc

_gate = threading.Lock()


class _Adapter:
    source = "Validación aislada"
    bridge_required = False

    def __init__(self):
        self.battery, self.low, self.stale, self.failed = False, False, False, False

    def read(self):
        if self.failed:
            raise TelemetryError("Comunicación interrumpida en la simulación")
        timestamp = iso_utc(datetime.now(timezone.utc) - timedelta(seconds=30)) if self.stale else iso_utc()
        return {"timestamp": timestamp, "connected": True, "on_battery": self.battery,
                "low_battery": self.low, "fault": False, "load_percent": 7}


def _scenario(name, callback):
    from .server import Monitor

    store, adapter, actions = Store(":memory:"), _Adapter(), []
    # Always inject a recorder instead of windows_action, including if a rule
    # regresses. The live monitor and its settings are never involved.
    monitor = Monitor(store, adapter, dry_run=True, action_callback=lambda action: actions.append(action))
    clock = [100.0]
    monitor.engine._clock = lambda: clock[0]
    def tick(seconds=0):
        clock[0] += seconds
        monitor.tick()
        return monitor.engine.status()
    try:
        result = callback(monitor, adapter, tick, actions)
        if actions:
            raise AssertionError("Se alcanzó el ejecutor simulado; la barrera de acciones no pasó la comprobación.")
        return {"name": name, "passed": True, "detail": result}
    finally:
        monitor.stop()
        store.close()


def _require(condition, message):
    if not condition:
        raise AssertionError(message)


def _cancel(monitor, adapter, tick, actions):
    monitor.set_policy({"enabled": True, "countdown_seconds": 10})
    adapter.battery = adapter.low = True
    _require(tick()["state"] == "countdown", "No se inició la cuenta regresiva simulada.")
    monitor.cancel()
    for _ in range(10):
        status = tick(2)
    _require(status["state"] == "cancelled", "La cancelación no se mantuvo durante el corte simulado.")
    adapter.battery = adapter.low = False
    _require(tick(2)["state"] == "online", "No volvió a estado de red.")
    adapter.battery = adapter.low = True
    _require(tick(2)["state"] == "countdown", "Un nuevo corte no recuperó las reglas.")
    return "Cancelar impide la acción durante ese corte y un nuevo corte vuelve a evaluar las reglas."


def _mains(monitor, adapter, tick, actions):
    monitor.set_policy({"enabled": True, "countdown_seconds": 10})
    adapter.battery = adapter.low = True
    _require(tick()["state"] == "countdown", "Falta la cuenta regresiva simulada.")
    adapter.battery = adapter.low = False
    _require(tick(5)["state"] == "online", "El regreso de red no canceló la cuenta.")
    _require(tick(11)["countdown_remaining"] is None, "Quedó una cuenta antigua pendiente.")
    return "El regreso de electricidad cancela la cuenta regresiva pendiente."


def _stale(monitor, adapter, tick, actions):
    monitor.set_policy({"enabled": True, "countdown_seconds": 10})
    adapter.battery = adapter.low = True
    tick()
    adapter.stale = True
    _require(tick(11)["state"] == "unknown", "Los datos antiguos no bloquearon la protección.")
    adapter.stale = False
    _require(tick(1)["countdown_remaining"] == 10, "Se reutilizó una cuenta anterior a perder datos.")
    return "Una lectura antigua bloquea acciones y recuperar datos inicia una cuenta nueva."


def _communication(monitor, adapter, tick, actions):
    tick()
    adapter.failed = True
    tick(2)
    _require(monitor.connection == "disconnected" and monitor.sample is None, "No se detectó la pérdida de comunicación.")
    adapter.failed = False
    tick(2)
    kinds = [event["kind"] for event in monitor.events()]
    _require(monitor.connection == "connected" and "connection_lost" in kinds and "connection_restored" in kinds,
             "No se registró la recuperación de comunicación.")
    return "La pérdida y recuperación de comunicación se registran por separado de los cortes eléctricos."


def _dry_run(monitor, adapter, tick, actions):
    monitor.set_policy({"enabled": True, "countdown_seconds": 10})
    adapter.battery = adapter.low = True
    tick()
    _require(tick(11)["state"] == "action_started", "No se alcanzó el final de la simulación.")
    kinds = [event["kind"] for event in monitor.events()]
    _require("simulation" in kinds and "policy_action_started" not in kinds, "La acción no quedó identificada como simulación.")
    return "Al vencer el aviso, el modo de prueba registra una simulación sin llamar al ejecutor."


def _disabled(monitor, adapter, tick, actions):
    adapter.battery = adapter.low = True
    for _ in range(120):
        _require(tick(10)["state"] == "disabled", "La política desactivada inició una protección.")
    _require(not monitor.engine.policy["enabled"], "La prueba cambió la política desactivada.")
    return "Con apagado desactivado, batería baja y tiempo prolongado no inician ninguna acción."


_SCENARIOS = (
    ("cancel_countdown", "Cancelar la cuenta regresiva", _cancel),
    ("restored_mains", "Regreso de la electricidad", _mains),
    ("stale_blocks_action", "Datos antiguos", _stale),
    ("communication_recovery", "Recuperación de comunicación simulada", _communication),
    ("dry_run_blocks_action", "Bloqueo de acciones reales", _dry_run),
    ("disabled_blocks_action", "Apagado automático desactivado", _disabled),
)


def run_controlled():
    if not _gate.acquire(blocking=False):
        raise ValueError("Ya hay una comprobación de reglas en curso.")
    try:
        results = []
        for name, label, callback in _SCENARIOS:
            try:
                row = _scenario(name, callback)
            except Exception as exc:
                row = {"name": name, "passed": False, "detail": str(exc)[:300]}
            row["label"] = label
            results.append(row)
        return {"schema_version": 1, "generated_at": iso_utc(), "source": "isolated_simulation",
                "os_actions_executed": 0, "live_settings_changed": False,
                "passed": all(row["passed"] for row in results), "scenarios": results,
                "physical_usb_test": "pending_user_test", "physical_shutdown_test": "not_tested_disabled"}
    finally:
        _gate.release()


def main():
    parser = argparse.ArgumentParser(description="Comprobaciones de Elipro sin UPS ni acciones de Windows")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_controlled()
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload)
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
