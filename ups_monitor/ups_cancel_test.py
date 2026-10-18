"""Explicit, one-shot physical cancellation test. Never approves a production profile."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid

from .ups_power import (NT2402PRO_CANDIDATE_PROFILE, OperationJournal, UpsPowerBlocked,
                        _fresh_sample, _observed_after, _qs_shutdown_bit, device_fingerprint)
from .ups_test_authorization import read_test_authorization

AUTH_FILENAME = "ups-cancel-test-authorization.json"
JOURNAL_FILENAME = "ups-cancel-test-operation.json"
OWNER = "elipro-ups-cancel-test-operation"
STATES = frozenset(("arming", "submitted", "armed", "cancelling", "cancel_submitted",
                    "observing", "finished", "uncertain", "review_required"))


def _now():
    return datetime.now(timezone.utc)


def _stamp():
    return _now().isoformat()


def _date(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("La fecha debe incluir zona horaria.")
    return result


def _elapsed(operation):
    tick = operation.get("started_monotonic")
    if type(tick) not in (int, float) or not math.isfinite(tick) or tick < 0:
        raise UpsPowerBlocked("La operación no tiene un reloj monotónico válido.")
    elapsed = time.monotonic() - tick
    if elapsed < 0:
        raise UpsPowerBlocked("El reloj de la operación cambió; requiere revisión.")
    return elapsed


def candidate_profile(device):
    if not isinstance(device, dict) or not isinstance(device.get("path"), str) or not device["path"]:
        raise UpsPowerBlocked("Falta identificar la unidad USB candidata.")
    for key in ("vendor_id", "product_id", "version_number", "input_report_bytes",
                "output_report_bytes", "feature_report_bytes"):
        if type(device.get(key)) is not int or device[key] != NT2402PRO_CANDIDATE_PROFILE[key]:
            raise UpsPowerBlocked("El descriptor USB no coincide con la unidad candidata NT-2402PRO.")
    if device.get("protocol") != "T":
        raise UpsPowerBlocked("No se ha confirmado la comunicación QS/T de la unidad candidata.")
    return {**NT2402PRO_CANDIDATE_PROFILE, "device_fingerprint": device_fingerprint(device)}


def _output_active(sample):
    output = sample.get("output_voltage") if isinstance(sample, dict) else None
    return type(output) in (int, float) and math.isfinite(output) and output > 0


def _online(sample):
    return (_fresh_sample(sample) and sample.get("on_battery") is False
            and sample.get("low_battery") is False and sample.get("fault") is False
            and sample.get("raw_status") in ("Line mode", "AVR mode")
            and _qs_shutdown_bit(sample) is False and _output_active(sample))


def _unreported_pending(operation):
    evidence = operation.get("compatibility_evidence")
    return (isinstance(evidence, dict) and evidence.get("output_cut_observed") is True
            and evidence.get("pending_signal_not_reported") is True and evidence.get("manual_start_observed") is True
            and evidence.get("device_fingerprint") == operation["profile"]["device_fingerprint"]
            and isinstance(evidence.get("source_operation_id"), str))


def _cancel_sample_allowed(operation, sample):
    if (not _fresh_sample(sample) or not _output_active(sample) or sample.get("low_battery") is not False
            or sample.get("fault") is not False):
        return False
    if _qs_shutdown_bit(sample) is True:
        return True
    return _unreported_pending(operation) and operation.get("submission_complete") is True and _online(sample)


def load_test_operation(path):
    path = Path(path)
    if path.stat().st_size > 16384:
        raise ValueError("El registro de prueba es demasiado grande.")
    operation = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(operation, dict) or operation.get("schema_version") != 1
            or operation.get("owner") != OWNER or operation.get("state") not in STATES
            or operation.get("delay_seconds") != 180 or type(operation.get("delay_seconds")) is not int):
        raise ValueError("El registro no corresponde a una prueba propia de cancelación.")
    uuid.UUID(operation["operation_id"])
    uuid.UUID(hex=operation["authorization_id"])
    _date(operation["created_at"])
    if type(operation.get("service_pid")) is not int or operation["service_pid"] <= 0:
        raise ValueError("La operación no identifica su proceso propietario.")
    _elapsed(operation)
    profile = operation.get("profile")
    if (not isinstance(profile, dict) or any(profile.get(key) != value for key, value in NT2402PRO_CANDIDATE_PROFILE.items())
            or not isinstance(profile.get("device_fingerprint"), str) or len(profile["device_fingerprint"]) != 64):
        raise ValueError("El registro de prueba no identifica la unidad USB candidata.")
    return operation


def validate_test_worker_request(authorization_path, journal_path, operation_id, selector):
    """Worker repeats the durable ownership gate before opening USB."""
    if selector not in ("start", "cancel"):
        raise UpsPowerBlocked("Control de prueba no admitido.")
    auth = read_test_authorization(authorization_path, allow_expired=selector == "cancel")
    operation = load_test_operation(journal_path)
    if operation["operation_id"] != operation_id or operation["authorization_id"] != auth["authorization_id"]:
        raise UpsPowerBlocked("La operación no pertenece a esta autorización temporal.")
    created = _date(operation["created_at"])
    elapsed = _elapsed(operation)
    if operation["service_pid"] not in (os.getpid(), os.getppid()):
        raise UpsPowerBlocked("La operación pertenece a un proceso anterior; no se repetirá.")
    if not _date(auth["issued_at"]) <= created <= _date(auth["expires_at"]):
        raise UpsPowerBlocked("La operación no se creó durante su autorización.")
    if selector == "start":
        if operation["state"] != "arming" or not 0 <= elapsed <= 10 or not _online(operation.get("start_evidence")):
            raise UpsPowerBlocked("La orden de prueba ya se intentó o su ventana inicial venció.")
    elif (operation["state"] != "cancelling" or operation.get("cancel_attempted") is not True
          or not 0 <= elapsed < 170
          or not _observed_after(operation.get("cancel_evidence"), operation.get("submitted_at") or operation.get("submission_finished_at"))
          or not _cancel_sample_allowed(operation, operation.get("cancel_evidence"))):
        raise UpsPowerBlocked("No hay una cancelación propia, reciente y corroborada dentro de plazo.")
    return operation["profile"]


def validate_cancel_test_preflight(authorization_path, journal_path, operation_id, sample):
    validate_test_worker_request(authorization_path, journal_path, operation_id, "cancel")
    operation = load_test_operation(journal_path)
    if not _cancel_sample_allowed(operation, sample) or not 0 <= _elapsed(operation) < 170:
        raise UpsPowerBlocked("La consulta inmediata no permite cancelar con la salida y el margen seguros.")


class UpsCancellationTest:
    def __init__(self, journal_path=None, *, authorization_path=None, transport_factory=None):
        self._journal = OperationJournal(journal_path) if journal_path is not None else None
        self.authorization_path = (Path(authorization_path) if authorization_path is not None else
                                   Path(journal_path).with_name(AUTH_FILENAME) if journal_path is not None else None)
        self._transport_factory = transport_factory
        self._lock = threading.RLock()
        self._operation = None
        self._orphaned = False
        self._error = None
        self._sample = self._device = None
        self._active_transport = None
        self._interrupted = threading.Event()
        try:
            if self._journal is not None and self._journal.path.exists():
                self._operation = load_test_operation(self._journal.path)
                self._orphaned = self._operation["state"] != "finished"
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self._error = "El registro de prueba necesita revisión; no se enviarán órdenes."

    def pending(self):
        with self._lock:
            return bool(self._error or self._operation and self._operation["state"] != "finished")

    def _save(self, **updates):
        operation = {**self._operation, **updates}
        self._journal.write(operation)
        self._operation = operation

    def _auth(self, *, allow_expired=False):
        if self.authorization_path is None:
            raise UpsPowerBlocked("Falta la autorización administrativa temporal de prueba.")
        try:
            return read_test_authorization(self.authorization_path, allow_expired=allow_expired)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise UpsPowerBlocked("Falta una autorización administrativa temporal de prueba vigente.") from exc

    def _can_cancel(self, device, sample):
        operation = self._operation
        if (self._orphaned or not operation or operation["state"] not in ("submitted", "armed", "uncertain", "review_required")
                or operation.get("cancel_attempted") is True or not _cancel_sample_allowed(operation, sample)):
            return False
        try:
            auth = self._auth(allow_expired=True)
            return (auth["authorization_id"] == operation["authorization_id"]
                    and candidate_profile(device) == operation["profile"]
                    and _observed_after(sample, operation.get("submitted_at") or operation.get("submission_finished_at"))
                    and 0 <= _elapsed(operation) < 170)
        except (ValueError, TypeError, KeyError, OSError):
            return False

    def _can_close(self, device, sample):
        operation = self._operation
        if not operation or operation["state"] == "finished" or not _online(sample):
            return False
        try:
            return _elapsed(operation) >= 210 and candidate_profile(device) == operation["profile"]
        except (ValueError, TypeError, KeyError):
            return False

    def close_observed_cut(self, *, device, sample, operation_id, output_cut_observed,
                           pending_signal_not_reported, manual_start_observed):
        with self._lock:
            if (not self._can_close(device, sample) or self._operation["operation_id"] != operation_id
                    or output_cut_observed is not True or pending_signal_not_reported is not True
                    or manual_start_observed is not True or self._operation.get("cancel_attempted") is True):
                raise UpsPowerBlocked("El cierre requiere la operación propia vencida y las observaciones físicas indicadas.")
            evidence = {"source_operation_id": operation_id, "device_fingerprint": self._operation["profile"]["device_fingerprint"],
                        "output_cut_observed": True, "pending_signal_not_reported": True, "manual_start_observed": True,
                        "recorded_at": _stamp()}
            self._save(state="finished", finished_at=_stamp(), result="cut_observed_cancel_not_tested", observation=evidence)
            self._orphaned = False
            return self.status(device=device, sample=sample)

    def _can_confirm_observation(self, device, sample):
        operation = self._operation
        if not operation or operation.get("cancel_attempted") is not True or operation.get("physical_observation"):
            return False
        try:
            return _elapsed(operation) >= 210 and _online(sample) and candidate_profile(device) == operation["profile"]
        except (ValueError, TypeError, KeyError):
            return False

    def confirm_observation(self, *, device, sample, operation_id, outputs_stayed_on, independent_load_observed):
        with self._lock:
            if (not self._can_confirm_observation(device, sample) or self._operation["operation_id"] != operation_id
                    or type(outputs_stayed_on) is not bool or independent_load_observed is not True):
                raise UpsPowerBlocked("La observación física requiere la operación propia vencida y una carga independiente observada.")
            observation = {"source": "user_independent_load", "outputs_stayed_on": outputs_stayed_on, "recorded_at": _stamp()}
            self._save(state="finished", finished_at=_stamp(), physical_observation=observation,
                       result="cancel_observed_by_user" if outputs_stayed_on else "cut_observed_after_cancel")
            self._orphaned = False
            return self.status(device=device, sample=sample)

    def status(self, *, device=None, sample=None, policy_enabled=False, blocked=False):
        with self._lock:
            operation = self._operation
            state = "review_required" if self._orphaned or self._error else operation["state"] if operation else "idle"
            available, reason = False, None
            authorization = None
            try:
                authorization = self._auth()
            except (OSError, ValueError):
                pass
            try:
                if self.pending():
                    raise UpsPowerBlocked("Hay una prueba propia pendiente de resolver; no se repetirá.")
                if blocked or policy_enabled:
                    raise UpsPowerBlocked("Desactiva y guarda la protección automática antes de una prueba física.")
                candidate_profile(device)
                if not _online(sample):
                    raise UpsPowerBlocked("La prueba requiere red eléctrica y salida activa, sin batería baja ni falla.")
                auth = authorization if authorization is not None else self._auth()
                if operation and auth["authorization_id"] == operation["authorization_id"]:
                    raise UpsPowerBlocked("La autorización temporal ya fue utilizada para una prueba.")
                if self._journal is None:
                    raise UpsPowerBlocked("Falta un registro durable para la prueba.")
                available = True
            except (UpsPowerBlocked, OSError, ValueError) as exc:
                reason = str(exc)
            remaining = None
            if operation and state not in ("idle", "finished"):
                limit = 210 if state == "observing" else 180
                try:
                    remaining = max(0, math.ceil(limit - _elapsed(operation)))
                except (ValueError, TypeError):
                    state = "review_required"
            messages = {
                "idle": "Prueba real independiente: un corte no cancelado puede apagar esta PC.",
                "arming": "Registrando y enviando una única orden real de corte a 3 minutos.",
                "submitted": "Envío realizado; esperando que QS confirme el corte pendiente. Aún no está cancelado.",
                "armed": "El UPS reporta un corte pendiente. Cancela la prueba ahora.",
                "cancelling": "Enviando una única cancelación de la operación propia.",
                "cancel_submitted": "Cancelación enviada; el resultado todavía no está confirmado.",
                "observing": "QS ya no reporta corte pendiente. Mantén el UPS y la red encendidos y observa una carga independiente hasta superar el plazo original.",
                "finished": "Observación completada sin corte reportado. Esto no habilita el apagado automático del UPS.",
                "uncertain": "Resultado incierto: el UPS podría cortar la salida. No repitas la orden ni reinicies Windows.",
                "review_required": "La prueba quedó pendiente y requiere revisión; no se repetirá ninguna orden.",
            }
            message = self._error or messages[state]
            result = operation.get("result") if operation else None
            unreported = bool(operation and _unreported_pending(operation))
            if unreported and state == "armed":
                message = "Orden propia enviada; este UPS no publica la señal de pendiente. Cancela ahora dentro del plazo indicado."
            if unreported and state == "observing":
                message = "Cancelación enviada; este UPS no permite confirmarla por la señal QS. Hiberna la PC y observa la alimentación del router hasta superar el plazo original más 30 segundos."
            if result == "cut_observed_cancel_not_tested":
                message = "Corte físico observado y salida recuperada con el botón; cancelación no probada. Registro cerrado sin habilitar el UPS automático."
            elif result == "cancel_observed_by_user":
                message = "El usuario observó la carga independiente encendida después del plazo. Cancelación observada físicamente; el perfil automático sigue sin habilitar."
            elif result == "cut_observed_after_cancel":
                message = "El usuario observó un corte después de enviar la cancelación. La cancelación no quedó validada; el perfil automático sigue bloqueado."
            elif result == "pending_bit_not_reported" and state == "uncertain":
                message = "El UPS no informa si el corte está pendiente. Hiberna Windows ahora con Windows + R → shutdown.exe /h. No repitas la orden."
            elif result == "cancel_not_corroborated" and state == "uncertain":
                message = "La cancelación no se confirmó. Hiberna Windows ahora con Windows + R → shutdown.exe /h; el corte podría seguir pendiente."
            return {"available": available, "reason": reason, "state": state, "delay_seconds": 180,
                    "authorization_valid": authorization is not None,
                    "authorization_expires_at": authorization["expires_at"] if authorization is not None else None,
                    "remaining": remaining, "can_cancel": self._can_cancel(device, sample),
                    "can_close": self._can_close(device, sample) and operation.get("cancel_attempted") is not True,
                    "can_confirm_observation": self._can_confirm_observation(device, sample),
                    "result": result, "pending_signal_unreliable": unreported,
                    "operation_id": operation["operation_id"] if operation else None,
                    "message": message, "production_validated": False}

    def _transport(self, device):
        if self._active_transport is None:
            factory = self._transport_factory
            if factory is None:
                from .hid_windows import CancellationTestTransport
                factory = CancellationTestTransport
            self._active_transport = factory(device["path"], self.authorization_path, self._journal.path,
                                             self._operation["operation_id"])
            if self._interrupted.is_set():
                signal = getattr(self._active_transport, "signal_interruption", None)
                if callable(signal):
                    signal()
        return self._active_transport

    def signal_interruption(self):
        self._interrupted.set()
        signal = getattr(self._active_transport, "signal_interruption", None)
        if callable(signal):
            signal()

    def start(self, *, device, sample, policy_enabled, blocked, acknowledge_risk, guard=None):
        with self._lock:
            if acknowledge_risk is not True:
                raise UpsPowerBlocked("Debes confirmar que guardaste el trabajo y aceptas el riesgo de un corte real.")
            capability = self.status(device=device, sample=sample, policy_enabled=policy_enabled, blocked=blocked)
            if not capability["available"]:
                raise UpsPowerBlocked(capability["reason"])
            auth = self._auth()
            compatibility = None
            if (self._operation and self._operation.get("result") == "cut_observed_cancel_not_tested"
                    and self._operation["profile"] == candidate_profile(device)):
                compatibility = self._operation.get("observation")
            elif self._operation and _unreported_pending(self._operation) and self._operation["profile"] == candidate_profile(device):
                compatibility = self._operation["compatibility_evidence"]
            operation = {"schema_version": 1, "owner": OWNER, "operation_id": str(uuid.uuid4()),
                         "authorization_id": auth["authorization_id"], "profile": candidate_profile(device),
                         "created_at": _stamp(), "started_monotonic": time.monotonic(), "service_pid": os.getpid(),
                         "state": "arming", "delay_seconds": 180, "start_evidence": sample}
            if compatibility is not None:
                operation["compatibility_evidence"] = compatibility
            self._journal.write(operation)
            self._operation = operation
            self._active_transport = None
            self._interrupted.clear()
            try:
                transport = self._transport(device)
                if guard is not None:
                    guard()
                result = transport.submit_test()
                if not isinstance(result, dict) or result.get("state") != "submitted":
                    raise OSError("El envío no quedó completo y confirmado por el transporte.")
                self._save(state="submitted", submitted_at=_stamp(), submission_complete=True)
            except Exception:
                self._operation.update(state="uncertain", submission_finished_at=_stamp())
                self._journal.write(self._operation)
            return self.status(device=device, sample=sample)

    def cancel(self, *, device, sample, guard=None):
        with self._lock:
            if not self._can_cancel(device, sample):
                raise UpsPowerBlocked("Cancelar requiere la operación propia, corte pendiente corroborado y al menos 10 segundos de margen.")
            self._save(state="cancelling", cancel_attempted=True, pending_observed=_qs_shutdown_bit(sample) is True,
                       cancel_evidence=sample, cancel_started_monotonic=time.monotonic(), result=None)
            self._orphaned = False
            try:
                if guard is not None:
                    guard()
                result = self._transport(device).cancel_test()
                if not isinstance(result, dict) or result.get("state") != "submitted":
                    raise OSError("El envío de cancelación es incierto.")
                self._save(state="cancel_submitted", cancel_submitted_at=_stamp())
            except Exception:
                self._operation.update(state="uncertain", cancel_finished_at=_stamp())
                self._journal.write(self._operation)
            return self.status(device=device, sample=sample)

    def observe(self, *, device, sample):
        with self._lock:
            self._device, self._sample = device, sample
            if not self._operation or self._orphaned or self._error:
                return
            operation = self._operation
            try:
                same_device = candidate_profile(device) == operation["profile"]
            except (ValueError, TypeError, KeyError):
                same_device = False
            if not same_device or not _fresh_sample(sample):
                return
            if operation["state"] == "submitted" and self._can_cancel(device, sample):
                self._save(state="armed")
            elif operation["state"] == "submitted" and _elapsed(operation) >= 10:
                self._save(state="uncertain", result="pending_bit_not_reported")
            elif operation["state"] in ("cancel_submitted", "uncertain") and operation.get("cancel_attempted") is True:
                submitted = operation.get("cancel_submitted_at") or operation.get("cancel_finished_at")
                if _online(sample) and _observed_after(sample, submitted):
                    self._save(state="observing", cancellation_observed_at=_stamp(), result=None)
                elif (operation["state"] == "cancel_submitted" and
                      time.monotonic() - operation.get("cancel_started_monotonic", time.monotonic()) >= 10):
                    self._save(state="uncertain", result="cancel_not_corroborated")
            elif operation["state"] == "observing":
                if not _online(sample):
                    self._save(state="uncertain")
                elif _elapsed(operation) >= 210:
                    self._save(state="finished", finished_at=_stamp(), result=("cancel_observed_after_deadline"
                               if _unreported_pending(operation) else "cancel_observed_in_qs"))

    def interrupted(self):
        self.signal_interruption()
        with self._lock:
            if self._operation and self._operation["state"] != "finished":
                self._save(state="review_required")
                self._orphaned = True
