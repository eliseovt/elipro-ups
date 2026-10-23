"""One explicitly authorized battery/AC-return test; never validates production."""
from __future__ import annotations

import math
import os
from pathlib import Path
import threading
import time
import uuid

from .ups_cancel_test import (candidate_profile, load_test_operation, _date, _elapsed, _monotonic_start,
                             _now, _online, _output_active, _stamp, _unreported_pending)
from .ups_power import OperationJournal, UpsPowerBlocked, _fresh_sample, _observed_after, _qs_shutdown_bit
from .ups_test_authorization import (BATTERY_AUTHORIZATION_FILENAME as AUTH_FILENAME,
    BATTERY_JOURNAL_FILENAME as JOURNAL_FILENAME, BATTERY_JOURNAL_OWNER as OWNER,
    _read_object, read_battery_test_authorization)

STATES = frozenset(("arming", "submitted", "uncertain", "windows_requested", "windows_failed",
                    "cancelling", "cancel_submitted", "review_required", "finished"))


def _healthy_powered(sample):
    return (_fresh_sample(sample) and _output_active(sample) and sample.get("low_battery") is False
            and sample.get("fault") is False and sample.get("raw_status") in
            ("Line mode", "AVR mode", "Battery mode", "Shutdown mode"))


def _battery_sample(sample):
    load = sample.get("load_percent") if isinstance(sample, dict) else None
    return (_healthy_powered(sample) and sample.get("on_battery") is True
            and sample.get("raw_status") == "Battery mode" and _qs_shutdown_bit(sample) is False
            and type(load) in (int, float) and math.isfinite(load) and 0 <= load <= 40)


def _cancel_evidence(value, profile):
    observation = value.get("physical_observation") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or value.get("state") != "finished"
            or value.get("owner") != "elipro-ups-cancel-test-operation"
            or value.get("result") != "cancel_observed_by_user" or value.get("profile") != profile
            or value.get("cancel_attempted") is not True or value.get("submission_complete") is not True
            or not isinstance(observation, dict) or observation.get("source") != "user_independent_load"
            or observation.get("outputs_stayed_on") is not True):
        raise UpsPowerBlocked("Falta una cancelación previa observada físicamente con esta misma unidad.")
    _date(observation["recorded_at"])
    uuid.UUID(value["operation_id"])
    return value


def load_battery_test_operation(path):
    value = _read_object(path, 32768)
    if (value.get("schema_version") != 1 or type(value.get("schema_version")) is not int
            or value.get("owner") != OWNER or value.get("state") not in STATES
            or type(value.get("delay_seconds")) is not int or value["delay_seconds"] != 180
            or type(value.get("service_pid")) is not int or value["service_pid"] <= 0):
        raise UpsPowerBlocked("El registro no corresponde a una prueba propia en batería.")
    uuid.UUID(value["operation_id"])
    uuid.UUID(hex=value["authorization_id"])
    _date(value["created_at"])
    _monotonic_start(value)
    _cancel_evidence(value.get("cancellation_evidence"), value.get("profile"))
    return value


def _battery_window(operation):
    started, baseline = operation.get("battery_started_monotonic"), operation.get("online_seen_monotonic")
    if (any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
            for value in (started, baseline)) or not 0 <= started - baseline <= 15
            or not 0 <= time.monotonic() - started <= 30):
        raise UpsPowerBlocked("La prueba requiere un corte de red recién observado, de hasta 30 segundos.")
    if not _online_baseline(operation.get("online_baseline")):
        raise UpsPowerBlocked("Falta una lectura válida de red previa al corte observado.")


def _online_baseline(sample):
    return (isinstance(sample, dict) and sample.get("on_battery") is False
            and sample.get("low_battery") is False and sample.get("fault") is False
            and sample.get("raw_status") in ("Line mode", "AVR mode") and _output_active(sample)
            and _qs_shutdown_bit({**sample, "timestamp": _stamp()}) is False)


def _cancel_allowed(operation, sample):
    if operation.get("submission_complete") is not True or not _healthy_powered(sample):
        return False
    if _qs_shutdown_bit(sample) is True:
        return True
    return operation.get("submission_complete") is True and _unreported_pending(operation)


def validate_battery_test_worker_request(authorization_path, journal_path, operation_id, selector):
    if selector not in ("start", "cancel"):
        raise UpsPowerBlocked("Control de prueba en batería no admitido.")
    auth = read_battery_test_authorization(authorization_path, allow_expired=selector == "cancel")
    operation = load_battery_test_operation(journal_path)
    if (operation["operation_id"] != operation_id or operation["authorization_id"] != auth["authorization_id"]
            or operation["service_pid"] not in (os.getpid(), os.getppid())
            or not _date(auth["issued_at"]) <= _date(operation["created_at"]) <= _date(auth["expires_at"])):
        raise UpsPowerBlocked("La prueba no pertenece a esta autorización o proceso actual.")
    elapsed = _elapsed(operation)
    if selector == "start":
        _battery_window(operation)
        if operation["state"] != "arming" or elapsed > 10 or not _battery_sample(operation.get("start_evidence")):
            raise UpsPowerBlocked("La orden de prueba ya se intentó o faltan condiciones recientes de batería.")
    elif (operation["state"] != "cancelling" or operation.get("windows_request") not in ("failed", "unconfirmed")
          or operation.get("cancel_attempted") is not True or elapsed >= 170
          or not _observed_after(operation.get("cancel_evidence"), operation.get("submitted_at") or operation.get("submission_finished_at"))
          or not _cancel_allowed(operation, operation.get("cancel_evidence"))):
        raise UpsPowerBlocked("La cancelación sólo corresponde a la solicitud propia de Windows rechazada y dentro del plazo.")
    return operation["profile"]


def validate_battery_test_preflight(authorization_path, journal_path, operation_id, selector, *, sample):
    validate_battery_test_worker_request(authorization_path, journal_path, operation_id, selector)
    operation = load_battery_test_operation(journal_path)
    if selector == "start":
        _battery_window(operation)
        if not _battery_sample(sample):
            raise UpsPowerBlocked("La consulta inmediata requiere batería sin alerta baja, sin falla y carga máxima del 40 %.")
    elif not _cancel_allowed(operation, sample) or _elapsed(operation) >= 170:
        raise UpsPowerBlocked("La consulta inmediata no permite cancelar con la salida y el margen seguros.")
    return True


class UpsBatteryTest:
    def __init__(self, journal_path=None, *, authorization_path=None, transport_factory=None):
        self._journal = OperationJournal(journal_path) if journal_path is not None else None
        self.authorization_path = (Path(authorization_path) if authorization_path is not None else
            Path(journal_path).with_name(AUTH_FILENAME) if journal_path is not None else None)
        self._transport_factory = transport_factory
        self._lock = threading.RLock()
        self._interruption = threading.Event()
        self._active_transport = None
        self._operation, self._error, self._orphaned = None, None, False
        self._device = self._sample = self._profile = None
        self._online_baseline = self._online_tick = self._battery_tick = self._last_tick = None
        try:
            if self._journal is not None and self._journal.path.exists():
                self._operation = load_battery_test_operation(self._journal.path)
                self._orphaned = self._operation["state"] != "finished"
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self._error = "El registro de prueba en batería necesita revisión; no se enviarán órdenes."

    def pending(self):
        with self._lock:
            return bool(self._error or self._operation and self._operation["state"] != "finished")

    def _save(self, **updates):
        value = {**self._operation, **updates}
        self._journal.write(value)
        self._operation = value

    def _save_after_send(self, **updates):
        try:
            self._save(**updates)
        except (OSError, ValueError):
            self._operation.update(updates)
            self._error = "No se pudo guardar el resultado; el corte puede estar pendiente y requiere revisión."

    def _auth(self, *, allow_expired=False):
        if self.authorization_path is None:
            raise UpsPowerBlocked("Falta la autorización administrativa temporal de prueba en batería.")
        return read_battery_test_authorization(self.authorization_path, allow_expired=allow_expired)

    def _previous_cancel(self, profile):
        if self._journal is None:
            raise UpsPowerBlocked("Falta un registro durable para la prueba en batería.")
        return _cancel_evidence(load_test_operation(self._journal.path.with_name("ups-cancel-test-operation.json")), profile)

    def _start_window(self):
        return {"battery_started_monotonic": self._battery_tick, "online_seen_monotonic": self._online_tick,
                "online_baseline": self._online_baseline}

    def _can_confirm(self, device, sample):
        try:
            return bool(self._operation and self._operation["state"] != "finished"
                and _elapsed(self._operation) >= 210
                and _online(sample) and candidate_profile(device) == self._operation["profile"])
        except (ValueError, TypeError, KeyError):
            return False

    def status(self, *, device=None, sample=None, policy_enabled=False, blocked=False):
        with self._lock:
            operation = self._operation
            state = "review_required" if self._error or self._orphaned else operation["state"] if operation else "idle"
            raw_observation = operation.get("physical_observation") if operation else None
            observation = {}
            if isinstance(raw_observation, dict):
                if raw_observation.get("source") == "user_physical_observation":
                    observation["source"] = raw_observation["source"]
                recorded_at = raw_observation.get("recorded_at")
                if isinstance(recorded_at, str) and len(recorded_at) <= 80:
                    try:
                        _date(recorded_at)
                        observation["recorded_at"] = recorded_at
                    except (ValueError, TypeError):
                        pass
                for key in ("cut_observed", "ac_restored", "manual_restart", "hibernate_resumed", "automatic_return_observed"):
                    if type(raw_observation.get(key)) is bool:
                        observation[key] = raw_observation[key]
                seconds = raw_observation.get("off_after_ac_seconds")
                if type(seconds) in (int, float) and math.isfinite(seconds) and 0 <= seconds <= 3600:
                    observation["off_after_ac_seconds"] = seconds
            finished_messages = {
                "automatic_return_observed": "El usuario observó que las salidas se encendieron solas al regresar la electricidad. Este comportamiento está aceptado. Prueba cerrada; la habilitación del apagado automático sigue pendiente de las demás comprobaciones.",
                "battery_cut_manual_restart_observed": "Observación física favorable registrada: corte, permanencia apagada al volver la electricidad y encendido manual, con recuperación de Windows. Prueba cerrada; el perfil automático del UPS sigue sin validar.",
                "physical_test_incomplete": "Prueba incompleta registrada. Faltan comprobaciones físicas; el perfil automático del UPS sigue bloqueado. El registro de esta prueba está cerrado.",
            }
            auth, available, reason = None, False, None
            try:
                auth = self._auth()
            except (OSError, ValueError):
                pass
            try:
                if self.pending():
                    raise UpsPowerBlocked("Hay una prueba en batería pendiente; no se repetirá ninguna orden.")
                if blocked or policy_enabled:
                    raise UpsPowerBlocked("Desactiva y guarda la protección automática antes de la prueba en batería.")
                profile = candidate_profile(device)
                self._previous_cancel(profile)
                if auth is None:
                    auth = self._auth()
                if operation and operation["authorization_id"] == auth["authorization_id"]:
                    raise UpsPowerBlocked("La autorización de prueba en batería ya fue utilizada.")
                if not _battery_sample(sample):
                    raise UpsPowerBlocked("Requiere batería recién activada, sin alerta baja ni falla, salida activa y carga máxima del 40 %.")
                _battery_window(self._start_window())
                available = True
            except (OSError, ValueError, TypeError, KeyError) as exc:
                reason = str(exc)
            remaining = None
            if operation and state != "finished":
                try:
                    remaining = max(0, math.ceil(180 - _elapsed(operation)))
                except (ValueError, TypeError):
                    state = "review_required"
            return {"available": available, "reason": self._error or reason, "state": state,
                "operation_id": operation["operation_id"] if operation else None, "delay_seconds": 180,
                "remaining": remaining, "windows_request": operation.get("windows_request") if operation else None,
                "authorization_valid": auth is not None,
                "authorization_expires_at": auth["expires_at"] if auth else None,
                "can_confirm_observation": self._can_confirm(device, sample),
                "result": operation.get("result") if operation else None, "production_validated": False,
                "physical_observation": observation or None,
                "message": (finished_messages.get(operation.get("result"), "Observación registrada; la prueba está cerrada y no habilita el perfil automático del UPS.")
                    if operation and state == "finished" else
                    "Windows rechazó la hibernación. El corte podría seguir pendiente; la cancelación de emergencia no está confirmada."
                    if operation and operation.get("windows_request") == "failed" else
                    "La solicitud a Windows quedó sin confirmar. No repitas órdenes; registra la observación física al recuperar la sesión."
                    if operation and operation.get("windows_request") == "unconfirmed" else
                    "Prueba registrada; la hibernación de Windows debe solicitarse inmediatamente."
                    if operation and operation.get("windows_request") == "pending" else
                    "Observa el corte y registra si las salidas vuelven solas al restaurar la electricidad o requieren el botón. El retorno automático está aceptado; esta observación no habilita por sí sola el perfil automático."
                    if operation and state != "finished" else
                    "Observación registrada; no habilita automáticamente el control del UPS."
                    if operation else "Prueba real de corte en batería y retorno de red, con hibernación sin forzar aplicaciones.")}

    def compatibility(self, device=None):
        """Expose the accepted restart behavior observed on this unit only."""
        with self._lock:
            operation = self._operation
            observation = operation.get("physical_observation") if operation else None
            if (self._error or self._orphaned or not operation or operation.get("state") != "finished"
                    or operation.get("result") != "automatic_return_observed" or not isinstance(observation, dict)
                    or observation.get("source") != "user_physical_observation"
                    or any(observation.get(key) is not True for key in ("cut_observed", "ac_restored", "automatic_return_observed"))):
                return None
            try:
                if candidate_profile(device) != operation["profile"]:
                    return None
                _date(observation["recorded_at"])
            except (ValueError, TypeError, KeyError, AttributeError):
                return None
            return {"restart_behavior": "automatic_on_mains", "restart_behavior_observed": True,
                "automatic_restart_accepted": True, "manual_restart": False,
                "source_operation_id": operation["operation_id"]}

    def _transport(self, device):
        if self._active_transport is not None:
            return self._active_transport
        factory = self._transport_factory
        if factory is None:
            from .hid_windows import BatteryTestTransport
            factory = BatteryTestTransport
        transport = factory(device["path"], self.authorization_path, self._journal.path, self._operation["operation_id"])
        self._active_transport = transport
        if self._interruption.is_set():
            signal = getattr(transport, "signal_interruption", None)
            if callable(signal):
                signal()
        return transport

    def signal_interruption(self):
        """SCM callback path: no coordinator lock, journal IO or USB calls."""
        self._interruption.set()
        transport = self._active_transport
        if transport is not None:
            signal = getattr(transport, "signal_interruption", None)
            if callable(signal):
                try:
                    signal()
                except Exception:
                    # The monotonic/power gates still prevent a replay.
                    pass

    def start(self, *, device, sample, policy_enabled, blocked, acknowledge_risk, guard=None, on_submission=None):
        with self._lock:
            if acknowledge_risk is not True:
                raise UpsPowerBlocked("Confirma que guardaste tu trabajo y aceptas el riesgo de un corte real en batería.")
            if on_submission is not None and not callable(on_submission):
                raise UpsPowerBlocked("La solicitud inmediata de Windows debe ser una función interna.")
            capability = self.status(device=device, sample=sample, policy_enabled=policy_enabled, blocked=blocked)
            if not capability["available"]:
                raise UpsPowerBlocked(capability["reason"])
            profile = candidate_profile(device)
            evidence, auth = self._previous_cancel(profile), self._auth()
            self._interruption.clear()
            self._active_transport = None
            operation = {"schema_version": 1, "owner": OWNER, "operation_id": str(uuid.uuid4()),
                "authorization_id": auth["authorization_id"], "profile": profile,
                "created_at": _stamp(), "started_monotonic": time.monotonic(), "service_pid": os.getpid(),
                "state": "arming", "delay_seconds": 180, "windows_request": "pending", "start_evidence": sample,
                "cancellation_evidence": evidence, **self._start_window()}
            if _unreported_pending(evidence):
                operation["compatibility_evidence"] = evidence["compatibility_evidence"]
            self._journal.write(operation)
            self._operation = operation
            completed_at = None
            try:
                transport = self._transport(device)
                if guard is not None and guard() is False:
                    raise UpsPowerBlocked("Windows cambió de estado antes del envío de prueba.")
                if self._interruption.is_set():
                    raise UpsPowerBlocked("Windows interrumpió la operación antes del envío.")
                complete = False
                try:
                    result = transport.submit_test()
                    complete = isinstance(result, dict) and result.get("state") == "submitted"
                finally:
                    # No journal flush, status read or event logging may come
                    # between a possibly effective USB write and requesting S4.
                    completed_at = _stamp()
                    if on_submission is not None:
                        try:
                            on_submission()
                        except Exception:
                            self._operation["windows_callback_failed"] = True
                if complete:
                    self._save_after_send(state="submitted", submission_complete=True, submitted_at=completed_at)
                else:
                    self._save_after_send(state="uncertain", submission_finished_at=completed_at)
            except Exception:
                self._save_after_send(state="uncertain", submission_finished_at=completed_at or _stamp())
            return self.status(device=device, sample=sample)

    def record_windows_request(self, success, error=None):
        with self._lock:
            if self._operation is not None and self._operation["state"] != "finished":
                self._save_after_send(windows_request="requested" if success is True else "unconfirmed" if success is None else "failed",
                    windows_error=str(error)[:300] if error is not None else None,
                    state="review_required" if self._orphaned else "windows_requested" if success is True else "uncertain" if success is None else "windows_failed")
            return self.status(device=self._device, sample=self._sample)

    def cancel_after_windows_failure(self, *, device, sample, guard=None):
        with self._lock:
            operation = self._operation
            if (self._orphaned or self._interruption.is_set() or not operation or operation.get("windows_request") not in ("failed", "unconfirmed")
                    or operation.get("cancel_attempted") is True or _elapsed(operation) >= 170
                    or candidate_profile(device) != operation["profile"] or not _cancel_allowed(operation, sample)
                    or not _observed_after(sample, operation.get("submitted_at") or operation.get("submission_finished_at"))):
                raise UpsPowerBlocked("No hay una operación propia de Windows rechazada que pueda cancelarse dentro del margen seguro.")
            self._save(state="cancelling", cancel_attempted=True, cancel_evidence=sample)
            try:
                if guard is not None and guard() is False:
                    raise UpsPowerBlocked("Windows cambió de estado antes de cancelar.")
                result = self._transport(device).cancel_after_windows_failure()
                if not isinstance(result, dict) or result.get("state") != "submitted":
                    raise OSError("Cancelación de prueba sin confirmar.")
                self._save(state="cancel_submitted", cancel_submitted_at=_stamp())
            except Exception:
                self._save_after_send(state="uncertain", cancel_finished_at=_stamp())
            return self.status(device=device, sample=sample)

    def observe(self, *, device, sample):
        with self._lock:
            self._device, self._sample = device, sample
            tick = time.monotonic()
            try:
                profile = candidate_profile(device)
            except (ValueError, TypeError, KeyError):
                profile = None
            if (profile != self._profile or not _fresh_sample(sample)
                    or self._last_tick is not None and tick - self._last_tick > 15):
                self._online_baseline = self._online_tick = self._battery_tick = None
            self._profile, self._last_tick = profile, tick
            if profile is None or not _fresh_sample(sample):
                return
            if _online(sample):
                self._online_baseline, self._online_tick, self._battery_tick = sample, tick, None
            elif _battery_sample(sample) and self._battery_tick is None and self._online_tick is not None:
                if 0 <= tick - self._online_tick <= 15:
                    self._battery_tick = tick
            elif not _battery_sample(sample):
                self._online_baseline = self._online_tick = self._battery_tick = None

    def interrupted(self):
        self.signal_interruption()
        with self._lock:
            self._online_baseline = self._online_tick = self._battery_tick = self._last_tick = None
            if self._operation and self._operation["state"] != "finished":
                self._save_after_send(state="review_required")
                self._orphaned = True

    def confirm_observation(self, *, operation_id, cut_observed, ac_restored, off_after_ac_seconds,
                            manual_restart, hibernate_resumed, automatic_return_observed, acknowledge_observation,
                            device=None, sample=None):
        with self._lock:
            device = self._device if device is None else device
            sample = self._sample if sample is None else sample
            flags = (cut_observed, ac_restored, manual_restart, hibernate_resumed, automatic_return_observed)
            if (acknowledge_observation is not True or any(type(value) is not bool for value in flags)
                    or type(off_after_ac_seconds) not in (int, float) or not math.isfinite(off_after_ac_seconds)
                    or off_after_ac_seconds < 0 or not self._can_confirm(device, sample)
                    or operation_id != self._operation["operation_id"]):
                raise UpsPowerBlocked("La observación requiere la operación propia vencida, red y datos recientes y respuestas físicas explícitas.")
            result = ("automatic_return_observed" if cut_observed and ac_restored and automatic_return_observed else
                "battery_cut_manual_restart_observed" if cut_observed and ac_restored and manual_restart
                and hibernate_resumed and not automatic_return_observed and off_after_ac_seconds >= 60
                and self._operation.get("windows_request") in ("requested", "unconfirmed")
                and self._operation.get("cancel_attempted") is not True else
                "physical_test_incomplete")
            observation = {"source": "user_physical_observation", "recorded_at": _stamp(),
                "cut_observed": cut_observed, "ac_restored": ac_restored, "off_after_ac_seconds": off_after_ac_seconds,
                "manual_restart": manual_restart, "hibernate_resumed": hibernate_resumed,
                "automatic_return_observed": automatic_return_observed}
            self._save(state="finished", result=result, finished_at=_stamp(), physical_observation=observation)
            self._orphaned, self._error = False, None
            return self.status(device=device, sample=sample)
