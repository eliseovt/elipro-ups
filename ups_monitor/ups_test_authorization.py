"""Short-lived administrative permission for the isolated UPS cancellation test.

This module never opens USB or changes service registration. The permission is
separate from production UPS capabilities; only the running, owned LocalSystem
service may consume it. Expiry is checked by the coordinator before arming, not
by the worker gate, so an already-owned operation can still be cancelled.
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid

from . import windows_service as service

AUTHORIZATION_FILENAME = "ups-cancel-test-authorization.json"
JOURNAL_FILENAME = "ups-cancel-test-operation.json"
AUTHORIZATION_OWNER = "elipro-ups-cancel-test"
JOURNAL_OWNER = "elipro-ups-cancel-test-operation"
BATTERY_AUTHORIZATION_FILENAME = "ups-battery-test-authorization.json"
BATTERY_JOURNAL_FILENAME = "ups-battery-test-operation.json"
BATTERY_AUTHORIZATION_OWNER = "elipro-ups-battery-test"
BATTERY_JOURNAL_OWNER = "elipro-ups-battery-test-operation"
_AUTHORIZATION_KEYS = {"schema_version", "owner", "authorization_id", "issued_at", "expires_at"}
_OPERATION_STATES = {"arming", "submitted", "armed", "cancelling", "cancel_submitted",
                     "observing", "uncertain", "review_required", "finished"}
_MAX_AUTHORIZATION_BYTES = 4096
_MAX_JOURNAL_BYTES = 65536
_MAX_SECONDS = 900


def _object_without_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("El archivo contiene campos JSON duplicados.")
        value[key] = item
    return value


def _read_object(path, limit):
    try:
        with Path(path).open("rb") as source:
            raw = source.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("El archivo de la prueba excede el límite permitido.")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_without_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("No se pudo leer un archivo JSON válido de la prueba.") from exc
    if type(value) is not dict:
        raise ValueError("El archivo de la prueba debe contener un objeto JSON.")
    return value


def _utc_datetime(value):
    if type(value) is not str or not 1 <= len(value) <= 64:
        raise ValueError("La autorización debe contener fechas UTC válidas.")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError) as exc:
        raise ValueError("La autorización debe contener fechas UTC válidas.") from exc
    if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
        raise ValueError("Las fechas de autorización deben incluir la zona UTC.")
    return stamp


def _now_utc(now=None):
    stamp = datetime.now(timezone.utc) if now is None else now
    if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("El reloj de autorización debe incluir una zona horaria.")
    return stamp.astimezone(timezone.utc)


def read_test_authorization(path, *, allow_expired=False, now=None):
    """Read bounded, strictly typed UTC permission, or raise ``ValueError``."""
    return _read_authorization(path, AUTHORIZATION_OWNER, allow_expired=allow_expired, now=now)


def read_battery_test_authorization(path, *, allow_expired=False, now=None):
    """Battery testing has its own namespace; cancellation permission cannot arm it."""
    return _read_authorization(path, BATTERY_AUTHORIZATION_OWNER, allow_expired=allow_expired, now=now)


def _read_authorization(path, owner, *, allow_expired=False, now=None):
    if type(allow_expired) is not bool:
        raise ValueError("La opción de expiración debe ser booleana.")
    value = _read_object(path, _MAX_AUTHORIZATION_BYTES)
    if (set(value) != _AUTHORIZATION_KEYS or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1 or value.get("owner") != owner
            or type(value.get("authorization_id")) is not str
            or not re.fullmatch(r"[0-9a-f]{32}", value["authorization_id"])):
        raise ValueError("La autorización de la prueba no es propia o válida.")
    issued, expires = _utc_datetime(value["issued_at"]), _utc_datetime(value["expires_at"])
    current = _now_utc(now)
    if not 0 < (expires - issued).total_seconds() <= _MAX_SECONDS:
        raise ValueError("La autorización no puede durar más de quince minutos.")
    if issued > current:
        raise ValueError("La autorización tiene una fecha de emisión futura.")
    if not allow_expired and expires <= current:
        raise ValueError("La autorización temporal de la prueba ha caducado.")
    return value


def _command_line_args(command):
    """Use Windows' parser rather than guessing quoting around trusted paths."""
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
    count = ctypes.c_int()
    arguments = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not arguments:
        raise OSError("No se pudo interpretar el registro del servicio.")
    try:
        if not 1 <= count.value <= 128:
            raise ValueError("El registro del servicio contiene argumentos inválidos.")
        return [arguments[index] for index in range(count.value)]
    finally:
        kernel.LocalFree(arguments)


def _registered_data_dir():
    """Resolve only an owned, running native service using read-only SCM rights."""
    if os.name != "nt":
        raise ValueError("La autorización de la prueba requiere Windows.")
    api = service._apis()
    manager = api.OpenSCManagerW(None, None, service.SC_MANAGER_CONNECT)
    if not manager:
        raise OSError("No se pudo consultar el administrador de servicios.")
    handle = None
    try:
        handle = api.OpenServiceW(manager, service.NAME, service.SERVICE_QUERY_STATUS | service.SERVICE_QUERY_CONFIG)
        if not handle:
            raise ValueError("El servicio Elipro debe estar instalado y ejecutándose.")
        status = service._query(api, handle)
        if status.get("installed") is not True or status.get("state") != "running":
            raise ValueError("El servicio Elipro debe estar instalado y ejecutándose.")
        binary = service._configuration(api, handle)["binary_path"]
        if type(binary) is not str or not 1 <= len(binary) <= 32768 or not service._owned_binary(binary):
            raise ValueError("El servicio registrado pertenece a otro ejecutable.")
        arguments = _command_line_args(binary)
        if ("--allow-power-actions" not in arguments or "--self-test" in arguments
                or "--simulate" in arguments or "--dry-run" in arguments):
            raise ValueError("La prueba requiere el servicio nativo autorizado para acciones de Windows.")
        positions = [index for index, argument in enumerate(arguments) if argument == "--data-dir"]
        if len(positions) != 1 or positions[0] + 1 >= len(arguments):
            raise ValueError("El servicio no registra un directorio de datos único.")
        data_dir = Path(arguments[positions[0] + 1])
        if not data_dir.is_absolute() or not data_dir.is_dir():
            raise ValueError("El directorio de datos registrado no es válido.")
        return data_dir.resolve()
    finally:
        if handle:
            api.CloseServiceHandle(handle)
        api.CloseServiceHandle(manager)


def _test_paths(data_dir, authorization_path, journal_path, *,
                authorization_filename=AUTHORIZATION_FILENAME, journal_filename=JOURNAL_FILENAME):
    paths = []
    for supplied, filename in ((authorization_path, authorization_filename), (journal_path, journal_filename)):
        path = Path(supplied)
        if not path.is_absolute() or path.name != filename:
            raise ValueError("Los archivos de prueba deben usar sus nombres y rutas propios.")
        resolved = path.resolve()
        if resolved.name != filename or resolved.parent != data_dir:
            raise ValueError("Los archivos de prueba deben pertenecer al directorio registrado del servicio.")
        paths.append(resolved)
    return tuple(paths)


def authorize_test_worker(authorization_path, journal_path):
    """Return exactly True for an owned LocalSystem test worker; otherwise deny.

    An expired permission can identify an existing operation for cancellation.
    The coordinator separately requires an unexpired permission before arming.
    No production UPS flag or validated production profile is consulted here.
    """
    if not service._is_local_system():
        raise ValueError("Solo el servicio LocalSystem puede ejecutar la prueba del UPS.")
    data_dir = _registered_data_dir()
    authorization_path, _ = _test_paths(data_dir, authorization_path, journal_path)
    read_test_authorization(authorization_path, allow_expired=True)
    return True


def authorize_battery_test_worker(authorization_path, journal_path):
    if not service._is_local_system():
        raise ValueError("Solo el servicio LocalSystem puede ejecutar la prueba del UPS en batería.")
    data_dir = _registered_data_dir()
    authorization_path, _ = _test_paths(data_dir, authorization_path, journal_path,
        authorization_filename=BATTERY_AUTHORIZATION_FILENAME, journal_filename=BATTERY_JOURNAL_FILENAME)
    read_battery_test_authorization(authorization_path, allow_expired=True)
    return True


def _is_admin():
    if os.name != "nt":
        return False
    try:
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        shell.IsUserAnAdmin.argtypes, shell.IsUserAnAdmin.restype = [], ctypes.c_int
        return bool(shell.IsUserAnAdmin())
    except Exception:
        return False


def _require_finished_journal(path):
    try:
        path.stat()
    except FileNotFoundError:
        return
    # A broken or unreadable existing journal must never become a new test.
    value = _read_object(path, _MAX_JOURNAL_BYTES)
    if (value.get("owner") != JOURNAL_OWNER or type(value.get("state")) is not str
            or value["state"] not in _OPERATION_STATES):
        raise ValueError("El registro de la prueba necesita revisión antes de autorizar otra.")
    if value["state"] != "finished":
        raise ValueError("Hay una prueba pendiente o incierta; no se renovará su autorización.")


def _write_authorization(path, journal_path, value, *, journal_validator=None):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            json.dump(value, output, ensure_ascii=False, allow_nan=False, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        (journal_validator or _require_finished_journal)(journal_path)
        os.replace(temporary, path)
        temporary = None
        with path.open("r+b") as output:
            os.fsync(output.fileno())
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Autoriza temporalmente la prueba manual de cancelación del UPS; no controla hardware")
    parser.add_argument("--minutes", type=int, default=15, help="Vigencia administrativa: de 5 a 15 minutos")
    args = parser.parse_args(argv)
    if not 5 <= args.minutes <= 15:
        parser.error("--minutes debe estar entre 5 y 15")
    try:
        if os.name != "nt" or not _is_admin():
            raise ValueError("Abre esta autorización con permisos de administrador de Windows.")
        data_dir = _registered_data_dir()
        authorization_path, journal_path = _test_paths(data_dir, data_dir / AUTHORIZATION_FILENAME,
                                                       data_dir / JOURNAL_FILENAME)
        _require_finished_journal(journal_path)
        issued = _now_utc()
        value = {"schema_version": 1, "owner": AUTHORIZATION_OWNER, "authorization_id": uuid.uuid4().hex,
                 "issued_at": issued.isoformat().replace("+00:00", "Z"),
                 "expires_at": (issued + timedelta(minutes=args.minutes)).isoformat().replace("+00:00", "Z")}
        _write_authorization(authorization_path, journal_path, value)
        print(json.dumps({"path": str(authorization_path), "expires_at": value["expires_at"]}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _require_finished_battery_journal(path):
    try:
        path.stat()
    except FileNotFoundError:
        return
    value = _read_object(path, _MAX_JOURNAL_BYTES)
    if value.get("owner") != BATTERY_JOURNAL_OWNER or value.get("state") != "finished":
        raise ValueError("Hay una prueba en batería pendiente o incierta; requiere revisión antes de autorizar otra.")


def battery_main(argv=None):
    parser = argparse.ArgumentParser(description="Autoriza una prueba física manual del UPS en batería; no controla hardware")
    parser.add_argument("--minutes", type=int, default=15)
    args = parser.parse_args(argv)
    if not 5 <= args.minutes <= 15:
        parser.error("--minutes debe estar entre 5 y 15")
    try:
        if os.name != "nt" or not _is_admin():
            raise ValueError("Abre esta autorización con permisos de administrador de Windows.")
        data_dir = _registered_data_dir()
        authorization_path, journal_path = _test_paths(data_dir,
            data_dir / BATTERY_AUTHORIZATION_FILENAME, data_dir / BATTERY_JOURNAL_FILENAME,
            authorization_filename=BATTERY_AUTHORIZATION_FILENAME, journal_filename=BATTERY_JOURNAL_FILENAME)
        _require_finished_battery_journal(journal_path)
        issued = _now_utc()
        value = {"schema_version": 1, "owner": BATTERY_AUTHORIZATION_OWNER, "authorization_id": uuid.uuid4().hex,
                 "issued_at": issued.isoformat().replace("+00:00", "Z"),
                 "expires_at": (issued + timedelta(minutes=args.minutes)).isoformat().replace("+00:00", "Z")}
        _write_authorization(authorization_path, journal_path, value, journal_validator=_require_finished_battery_journal)
        print(json.dumps({"path": str(authorization_path), "expires_at": value["expires_at"]}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(battery_main(sys.argv[2:]) if len(sys.argv) > 1 and sys.argv[1] == "--battery-test" else main())
