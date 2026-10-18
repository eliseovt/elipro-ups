"""Windows HID discovery and isolated, explicitly allowlisted read-only queries.

SetupAPI locates the HID collection. CreateFile uses desiredAccess=0 and shared
read/write solely to inspect attributes, strings and cached report capabilities.
Enumeration sends no UPS commands. Normal query_status sends only QS/CR.
The separate battery diagnostic permits only BT?/CR and BL?/CR; it is never
used by native polling. Each query runs in a disposable process with a deadline.

Microsoft references:
https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew
https://learn.microsoft.com/en-us/windows/win32/api/setupapi/nf-setupapi-setupdigetdeviceinterfacedetailw
https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/hidpi/ns-hidpi-_hidp_caps
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import math
from pathlib import Path
import subprocess
import sys
import time
import threading
import uuid
from ctypes import wintypes
from .runtime import child_command, resource_root

VENDOR_ID = 0x0665
PRODUCT_ID = 0x5161
STATUS_QUERY = b"QS\r"
BATTERY_DIAGNOSTIC_QUERIES = {"BT": b"BT?\r", "BL": b"BL?\r"}
_cooldown_until = {}
_cooldown_lock = threading.Lock()
_active_workers = {}
_worker_lock = threading.Lock()
WORKER_TIMEOUT_SECONDS = 3
CANCELLATION_GRACE_SECONDS = 0.5


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", wintypes.DWORD), ("Reserved", ctypes.c_void_p)]


class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("ClassGuid", GUID),
                ("DevInst", wintypes.DWORD), ("Reserved", ctypes.c_void_p)]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Size", wintypes.ULONG), ("VendorID", wintypes.USHORT),
                ("ProductID", wintypes.USHORT), ("VersionNumber", wintypes.USHORT)]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [(name, wintypes.USHORT) for name in (
        "Usage", "UsagePage", "InputReportByteLength", "OutputReportByteLength",
        "FeatureReportByteLength")]
    _fields_ += [("Reserved", wintypes.USHORT * 17)]
    _fields_ += [(name, wintypes.USHORT) for name in (
        "NumberLinkCollectionNodes", "NumberInputButtonCaps", "NumberInputValueCaps",
        "NumberInputDataIndices", "NumberOutputButtonCaps", "NumberOutputValueCaps",
        "NumberOutputDataIndices", "NumberFeatureButtonCaps", "NumberFeatureValueCaps",
        "NumberFeatureDataIndices")]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE)]


def _apis():
    if os.name != "nt":
        raise OSError("La enumeración HID requiere Windows")
    setup = ctypes.WinDLL("setupapi", use_last_error=True)
    hid = ctypes.WinDLL("hid", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    setup.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
    setup.SetupDiGetClassDevsW.restype = wintypes.HANDLE
    setup.SetupDiEnumDeviceInterfaces.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID), wintypes.DWORD, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
    setup.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
    setup.SetupDiGetDeviceInterfaceDetailW.argtypes = [wintypes.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(SP_DEVINFO_DATA)]
    setup.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
    setup.SetupDiGetDeviceRegistryPropertyW.argtypes = [wintypes.HANDLE, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    setup.SetupDiGetDeviceRegistryPropertyW.restype = wintypes.BOOL
    setup.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
    setup.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
    hid.HidD_GetHidGuid.restype = None
    hid.HidD_GetAttributes.argtypes = [wintypes.HANDLE, ctypes.POINTER(HIDD_ATTRIBUTES)]
    hid.HidD_GetAttributes.restype = ctypes.c_ubyte
    hid.HidD_GetPreparsedData.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
    hid.HidD_GetPreparsedData.restype = ctypes.c_ubyte
    hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
    hid.HidD_FreePreparsedData.restype = ctypes.c_ubyte
    hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
    hid.HidP_GetCaps.restype = ctypes.c_int32
    for name in ("HidD_GetManufacturerString", "HidD_GetProductString"):
        function = getattr(hid, name)
        function.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]
        function.restype = ctypes.c_ubyte
    hid.HidD_SetOutputReport.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]
    hid.HidD_SetOutputReport.restype = ctypes.c_ubyte
    hid.HidD_FlushQueue.argtypes = [wintypes.HANDLE]
    hid.HidD_FlushQueue.restype = ctypes.c_ubyte
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.ReleaseMutex.restype = wintypes.BOOL
    kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateEventW.restype = wintypes.HANDLE
    kernel.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED)]
    kernel.ReadFile.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]
    kernel.CancelIoEx.restype = wintypes.BOOL
    kernel.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
    kernel.GetOverlappedResult.restype = wintypes.BOOL
    return setup, hid, kernel


def enumerate_ups(vendor_id=VENDOR_ID, product_id=PRODUCT_ID):
    """Inspect matching present HID devices without obtaining read/write access.

    A successful ``metadata_open`` proves shared attribute access only. It does
    not prove that GENERIC_READ/WRITE will coexist with ForzaTracker, nor that a
    particular HID query/report framing works. Those require later validation.
    """
    setup, hid, kernel = _apis()
    invalid = ctypes.c_void_p(-1).value
    guid = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(guid))
    devices = setup.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x12)
    if devices == invalid:
        raise ctypes.WinError(ctypes.get_last_error())
    results = []
    try:
        index = 0
        while True:
            interface = SP_DEVICE_INTERFACE_DATA()
            interface.cbSize = ctypes.sizeof(interface)
            if not setup.SetupDiEnumDeviceInterfaces(devices, None, ctypes.byref(guid), index, ctypes.byref(interface)):
                error = ctypes.get_last_error()
                if error == 259:  # ERROR_NO_MORE_ITEMS
                    break
                raise ctypes.WinError(error)
            index += 1
            required = wintypes.DWORD()
            setup.SetupDiGetDeviceInterfaceDetailW(devices, ctypes.byref(interface), None, 0, ctypes.byref(required), None)
            if required.value < 6 or required.value > 65536:
                raise OSError("Tamaño de ruta HID inesperado")
            detail = ctypes.create_string_buffer(required.value)
            ctypes.c_uint32.from_buffer(detail).value = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            info = SP_DEVINFO_DATA()
            info.cbSize = ctypes.sizeof(info)
            if not setup.SetupDiGetDeviceInterfaceDetailW(devices, ctypes.byref(interface), detail, required, None, ctypes.byref(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            path = ctypes.wstring_at(ctypes.addressof(detail) + 4)
            if f"vid_{vendor_id:04x}&pid_{product_id:04x}" not in path.lower():
                continue
            result = {"path": path, "vendor_id": vendor_id, "product_id": product_id,
                      "metadata_open": False, "desired_access": 0,
                      "share_mode": "FILE_SHARE_READ | FILE_SHARE_WRITE"}
            service = ctypes.create_unicode_buffer(256)
            if setup.SetupDiGetDeviceRegistryPropertyW(devices, ctypes.byref(info), 4, None, service, ctypes.sizeof(service), None):
                result["service"] = service.value
            handle = kernel.CreateFileW(path, 0, 3, None, 3, 0, None)
            if handle == invalid:
                result["open_error"] = ctypes.get_last_error()
                results.append(result)
                continue
            try:
                result["metadata_open"] = True
                attributes = HIDD_ATTRIBUTES()
                attributes.Size = ctypes.sizeof(attributes)
                if hid.HidD_GetAttributes(handle, ctypes.byref(attributes)):
                    result.update(vendor_id=attributes.VendorID, product_id=attributes.ProductID,
                                  version_number=attributes.VersionNumber)
                for key, name in (("manufacturer", "HidD_GetManufacturerString"), ("product", "HidD_GetProductString")):
                    buffer = ctypes.create_unicode_buffer(256)
                    if getattr(hid, name)(handle, buffer, ctypes.sizeof(buffer)):
                        result[key] = buffer.value
                preparsed = ctypes.c_void_p()
                if hid.HidD_GetPreparsedData(handle, ctypes.byref(preparsed)):
                    try:
                        caps = HIDP_CAPS()
                        status = hid.HidP_GetCaps(preparsed, ctypes.byref(caps))
                        if status == 0x00110000:  # HIDP_STATUS_SUCCESS
                            result.update(usage=caps.Usage, usage_page=caps.UsagePage,
                                          input_report_bytes=caps.InputReportByteLength,
                                          output_report_bytes=caps.OutputReportByteLength,
                                          feature_report_bytes=caps.FeatureReportByteLength)
                        else:
                            result["caps_status"] = status
                    finally:
                        hid.HidD_FreePreparsedData(preparsed)
            finally:
                kernel.CloseHandle(handle)
            results.append(result)
    finally:
        setup.SetupDiDestroyDeviceInfoList(devices)
    return results


def _validate_path(path):
    if (not isinstance(path, str) or not path.lower().startswith("\\\\?\\hid#")
            or "vid_0665&pid_5161#" not in path.lower() or "\x00" in path):
        raise ValueError("Solo se admite la colección HID del UPS identificado")
    return path


def _read_report(kernel, handle, event, deadline):
    """Keep buffer/OVERLAPPED alive until completion, even after cancellation."""
    if time.monotonic() >= deadline:
        raise TimeoutError("El plazo de lectura HID ya terminó")
    report = ctypes.create_string_buffer(9)
    overlapped = OVERLAPPED()
    overlapped.hEvent = event
    count = wintypes.DWORD()
    pending = False
    # ReadFile resets the event automatically. The actual count comes only from
    # GetOverlappedResult, as required for an overlapped handle.
    completed = kernel.ReadFile(handle, report, 9, None, ctypes.byref(overlapped))
    if not completed:
        error = ctypes.get_last_error()
        if error != 997:  # ERROR_IO_PENDING
            raise ctypes.WinError(error)
        pending = True
    try:
        if pending:
            remaining_ms = max(0, math.ceil((deadline - time.monotonic()) * 1000))
            result = kernel.WaitForSingleObject(event, remaining_ms)
            if result == 258:  # WAIT_TIMEOUT
                raise TimeoutError("El UPS no completó la respuesta QS a tiempo")
            if result != 0:
                raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(count), False):
            error = ctypes.get_last_error()
            pending = error == 996  # ERROR_IO_INCOMPLETE
            raise ctypes.WinError(error)
        pending = False
        if count.value != 9 or report.raw[0] != 0:
            raise OSError("El reporte HID no coincide con el formato validado de 9 bytes/report ID 0")
        return report.raw[1:]
    finally:
        if pending:
            # CancelIoEx is only a request. Wait for terminal completion before
            # releasing the buffer/event. Parent kills this worker if driver hangs.
            kernel.CancelIoEx(handle, ctypes.byref(overlapped))
            kernel.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(count), True)


def _query_status_once(path):
    """Normal status worker: the command cannot be selected by its caller."""
    return _query_read_only_once(path, STATUS_QUERY)


def _query_battery_diagnostic_once(path, command):
    if type(command) is not bytes or command not in BATTERY_DIAGNOSTIC_QUERIES.values():
        raise ValueError("El diagnóstico solo admite BT? o BL? seguidos de CR")
    return _query_read_only_once(path, command)


def _query_read_only_once(path, command):
    """Worker only: one fixed query using the same mutex, framing and deadline."""
    if type(command) is not bytes or command not in (STATUS_QUERY, *BATTERY_DIAGNOSTIC_QUERIES.values()):
        raise ValueError("Consulta HID no admitida")
    _validate_path(path)
    setup, hid, kernel = _apis()
    deadline = time.monotonic() + 2.5
    mutex_name = "Global\\EliproUPS_QS_" + hashlib.sha256(path.casefold().encode("utf-8")).hexdigest()
    mutex = kernel.CreateMutexW(None, False, mutex_name)
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    acquired = False
    handle = event = None
    try:
        wait = kernel.WaitForSingleObject(mutex, 500)
        if wait == 258:
            raise TimeoutError("Otro lector USB está usando este UPS")
        if wait not in (0, 128):
            raise ctypes.WinError(ctypes.get_last_error())
        acquired = True
        if wait == 128:  # Acquired an abandoned mutex: prior query is uncertain.
            raise OSError("El lector USB anterior terminó sin confirmar respuesta; espera antes de reintentar")
        handle = kernel.CreateFileW(path, 0xC0000000, 3, None, 3, 0x40000000, None)
        if handle == ctypes.c_void_p(-1).value:
            handle = None
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = HIDD_ATTRIBUTES()
        attributes.Size = ctypes.sizeof(attributes)
        if not hid.HidD_GetAttributes(handle, ctypes.byref(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
        if (attributes.VendorID, attributes.ProductID) != (VENDOR_ID, PRODUCT_ID):
            raise OSError("VID/PID no coincide; no se envió la consulta")
        preparsed = ctypes.c_void_p()
        if not hid.HidD_GetPreparsedData(handle, ctypes.byref(preparsed)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            caps = HIDP_CAPS()
            status = hid.HidP_GetCaps(preparsed, ctypes.byref(caps))
            if status != 0x00110000 or (caps.InputReportByteLength, caps.OutputReportByteLength, caps.FeatureReportByteLength) != (9, 9, 0):
                raise OSError("Descriptor HID distinto al validado; no se envió la consulta")
        finally:
            hid.HidD_FreePreparsedData(preparsed)
        event = kernel.CreateEventW(None, True, False, None)
        if not event:
            raise ctypes.WinError(ctypes.get_last_error())
        if not hid.HidD_FlushQueue(handle):
            raise ctypes.WinError(ctypes.get_last_error())
        if time.monotonic() >= deadline:
            raise TimeoutError("El plazo HID terminó antes de enviar la consulta")
        # Report ID 0 and eight payload bytes. The allowlist is checked above.
        output = ctypes.create_string_buffer(b"\x00" + command.ljust(8, b"\x00"), 9)
        if not hid.HidD_SetOutputReport(handle, output, 9):
            raise ctypes.WinError(ctypes.get_last_error())
        response = bytearray()
        while time.monotonic() < deadline and len(response) < 256:
            response.extend(_read_report(kernel, handle, event, deadline))
            if b"\r" in response:
                return bytes(response[:response.index(13) + 1])
        raise TimeoutError("Respuesta HID incompleta o demasiado larga")
    finally:
        if event:
            kernel.CloseHandle(event)
        if handle:
            kernel.CloseHandle(handle)
        if acquired:
            kernel.ReleaseMutex(mutex)
        kernel.CloseHandle(mutex)


def _run_bounded_worker(args, key):
    """Bound both response and cancellation, retaining uncertain live workers.

    subprocess.run(timeout=...) performs an unbounded communicate after killing
    on Windows. A driver may delay that termination. Keep such a child strongly
    referenced and quarantine its path until poll confirms exit; never open a
    second reader and never free its pending OVERLAPPED buffers in the parent.
    """
    with _worker_lock:
        previous = _active_workers.get(key)
        if previous is not None:
            if previous.poll() is None:
                raise TimeoutError("El lector HID anterior aún no terminó; USB en cuarentena")
            _active_workers.pop(key, None)
        child = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 cwd=str(resource_root()),
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _active_workers[key] = child
    try:
        try:
            stdout, stderr = child.communicate(timeout=WORKER_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            try:
                child.kill()
            except OSError:
                # Exit may race kill. If it is still alive, retain quarantine.
                if child.poll() is None:
                    raise TimeoutError("No se confirmó la terminación del lector HID; USB en cuarentena") from exc
            try:
                child.communicate(timeout=CANCELLATION_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                raise TimeoutError("El lector HID no terminó tras cancelar; USB en cuarentena") from exc
            raise TimeoutError("El diagnóstico HID excedió 3 segundos y fue terminado") from exc
        return subprocess.CompletedProcess(args, child.returncode, stdout, stderr)
    finally:
        with _worker_lock:
            if child.poll() is not None and _active_workers.get(key) is child:
                _active_workers.pop(key, None)


def enumerate_ups_bounded():
    """Discover metadata in the same bounded isolation as status transactions.

    SetupAPI/HidD metadata calls have no cancellable Win32 deadline. They must
    never execute in the monitor process, even before its first status query.
    This fixed internal role sends no UPS command and accepts no caller flags.
    """
    result = _run_bounded_worker(child_command("qs-worker", "--enumerate"), "metadata:0665:5161")
    if result.returncode:
        raise OSError("No se completó la enumeración HID aislada")
    if not result.stdout or len(result.stdout) > 65536:
        raise OSError("La enumeración HID excedió el tamaño permitido")
    try:
        devices = json.loads(result.stdout)
        if not isinstance(devices, list) or len(devices) > 16:
            raise ValueError("Lista HID no válida")
        for device in devices:
            if not isinstance(device, dict) or type(device.get("metadata_open")) is not bool:
                raise ValueError("Metadatos HID no válidos")
            _validate_path(device.get("path"))
            for name, expected in (("vendor_id", VENDOR_ID), ("product_id", PRODUCT_ID)):
                if type(device.get(name)) is not int or device[name] != expected:
                    raise ValueError("Identificador USB no válido")
            for name in ("input_report_bytes", "output_report_bytes", "feature_report_bytes"):
                if name in device and (type(device[name]) is not int or not 0 <= device[name] <= 65535):
                    raise ValueError("Tamaño de reporte HID no válido")
        return devices
    except (ValueError, TypeError, UnicodeError) as exc:
        raise OSError("El proceso devolvió metadatos HID no válidos") from exc


class StatusTransport:
    """QS polling with a separate, explicitly invoked battery diagnostic.

    Direct readings have been compared with the OEM collector, including a
    manual outage and return to mains. Automatic OS shutdown was not exercised.
    The observed device replies N/CR to BT?/BL?; these explicit diagnostics are
    never part of native polling and are not interpreted as battery capacity.

    HidD_SetOutputReport is synchronous; Windows cancellation may also wait for a
    driver. A child process contains both operations so a hung driver cannot block
    the caller beyond 3 seconds plus a 0.5-second cancellation grace. If process
    exit cannot be confirmed, that USB path remains quarantined until it exits.
    """

    def __init__(self, path):
        self.path = _validate_path(path)

    def query_status(self, command=STATUS_QUERY):
        if type(command) is not bytes or command != STATUS_QUERY:
            raise ValueError("Solo se permite la consulta de estado QS seguida de CR")
        return self._query_worker(child_command("qs-worker", self.path))

    def query_battery_diagnostic(self, command):
        """Explicit diagnostic only; normal polling remains strictly QS-only."""
        if type(command) is not bytes or command not in BATTERY_DIAGNOSTIC_QUERIES.values():
            raise ValueError("El diagnóstico solo admite BT? o BL? seguidos de CR")
        selector = next(name for name, value in BATTERY_DIAGNOSTIC_QUERIES.items() if value == command)
        return self._query_worker(child_command("battery-worker", self.path, selector))

    def _query_worker(self, args):
        key = self.path.casefold()
        with _cooldown_lock:
            if time.monotonic() < _cooldown_until.get(key, 0):
                raise TimeoutError("USB en espera de seguridad tras una respuesta fallida (5 segundos)")
        try:
            result = _run_bounded_worker(args, key)
            if result.returncode:
                raise OSError(result.stderr.decode("utf-8", "replace").strip()[:500] or "Falló el diagnóstico HID")
            if not result.stdout or len(result.stdout) > 256 or not result.stdout.endswith(b"\r"):
                raise OSError("El proceso devolvió una respuesta HID incompleta")
        except OSError:
            # Per-path state survives NativeHidAdapter recreating its transport.
            with _cooldown_lock:
                _cooldown_until[key] = time.monotonic() + 5
            raise
        return result.stdout


def _write_control_once(path, command, profile, *, before_write=None, before_each_report=None):
    """Control-only worker: hold the existing device mutex across every report.

    The caller has resolved an approved profile. No response is invented: these
    controls may act silently, and neither an empty read nor a timeout is ACK.
    """
    from .ups_power import validated_control_profile
    profile = validated_control_profile(profile.get("profile_id"), {profile.get("profile_id"): profile})
    return _write_control_reports_once(path, command, profile, before_write=before_write,
                                       before_each_report=before_each_report)


def _write_control_reports_once(path, command, profile, *, before_write=None, before_each_report=None):
    """One guarded transaction after the role-specific authorization gates.

    The normal control role requires physical validation. The separate manual
    cancellation-test role requires a unit-bound administrative authorization
    and a durable operation journal. Neither path retries a failed report.
    """
    from .ups_power import control_reports, profile_matches_device
    reports = control_reports(command)
    _validate_path(path)
    if before_write is not None and not callable(before_write):
        raise ValueError("La comprobación previa del control debe ser una función interna.")
    if before_each_report is not None and not callable(before_each_report):
        raise ValueError("La continuidad de control debe comprobarse mediante una función interna.")
    _, hid, kernel = _apis()
    deadline = time.monotonic() + 2.5
    mutex_name = "Global\\EliproUPS_QS_" + hashlib.sha256(path.casefold().encode("utf-8")).hexdigest()
    mutex = kernel.CreateMutexW(None, False, mutex_name)
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    acquired, handle = False, None
    try:
        wait = kernel.WaitForSingleObject(mutex, 500)
        if wait not in (0, 128):
            raise TimeoutError("Otro lector USB está usando este UPS; no se envió control.")
        acquired = True
        if wait == 128:
            raise OSError("Transacción USB anterior incierta; no se envió control.")
        handle = kernel.CreateFileW(path, 0xC0000000, 3, None, 3, 0, None)
        if handle == ctypes.c_void_p(-1).value:
            handle = None
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = HIDD_ATTRIBUTES()
        attributes.Size = ctypes.sizeof(attributes)
        if not hid.HidD_GetAttributes(handle, ctypes.byref(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
        preparsed = ctypes.c_void_p()
        if not hid.HidD_GetPreparsedData(handle, ctypes.byref(preparsed)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            caps = HIDP_CAPS()
            status = hid.HidP_GetCaps(preparsed, ctypes.byref(caps))
            if status != 0x00110000:
                raise OSError("No se verificó el descriptor del UPS; no se envió control.")
            device = {"path": path, "vendor_id": attributes.VendorID, "product_id": attributes.ProductID,
                      "version_number": attributes.VersionNumber, "input_report_bytes": caps.InputReportByteLength,
                      "output_report_bytes": caps.OutputReportByteLength, "feature_report_bytes": caps.FeatureReportByteLength}
            if not profile_matches_device(profile, device):
                raise OSError("La identidad física del UPS cambió; no se envió control.")
        finally:
            hid.HidD_FreePreparsedData(preparsed)
        if not hid.HidD_FlushQueue(handle):
            raise ctypes.WinError(ctypes.get_last_error())
        if before_write is not None:
            # The caller's final evidence check remains inside the same named
            # mutex. A direct QS query can reacquire this Windows mutex on the
            # same thread; releasing that nested acquisition leaves ours held.
            before_write()
        for report in reports:
            if before_each_report is not None:
                before_each_report()
            if time.monotonic() >= deadline:
                raise TimeoutError("Plazo de control USB agotado; no se repetirá la transacción.")
            output = ctypes.create_string_buffer(report, 9)
            if not hid.HidD_SetOutputReport(handle, output, 9):
                raise OSError("No se confirmó el envío completo al UPS; no se repetirá la transacción.")
        return {"state": "submitted", "confirmation": "unconfirmed", "reports_written": len(reports)}
    finally:
        if handle:
            kernel.CloseHandle(handle)
        if acquired:
            kernel.ReleaseMutex(mutex)
        kernel.CloseHandle(mutex)


def _require_control_service_authorization():
    from .ups_power import UpsPowerBlocked
    try:
        from .windows_service import control_worker_authorized
        authorized = control_worker_authorized()
    except Exception:
        authorized = False
    if authorized is not True:
        raise UpsPowerBlocked("El servicio actual no tiene autorización administrativa para enviar controles al UPS.")


def _operation_event_name(operation_id):
    try:
        return "Global\\EliproUPS_Control_" + uuid.UUID(operation_id).hex
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("La continuidad requiere una operación propia identificada.") from exc


def _event_api():
    if os.name != "nt":
        raise OSError("La continuidad del control requiere Windows.")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateEventW.argtypes, api.CreateEventW.restype = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE
    api.OpenEventW.argtypes, api.OpenEventW.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE
    api.SetEvent.argtypes, api.SetEvent.restype = [wintypes.HANDLE], wintypes.BOOL
    api.WaitForSingleObject.argtypes, api.WaitForSingleObject.restype = [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD
    api.CloseHandle.argtypes, api.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
    return api


class _OperationInterruption:
    """Parent-owned manual-reset event, never reset or recreated by a worker."""

    def __init__(self, operation_id):
        self.api = _event_api()
        ctypes.set_last_error(0)
        self.handle = self.api.CreateEventW(None, True, False, _operation_event_name(operation_id))
        if not self.handle:
            raise OSError("No se pudo proteger la continuidad del control del UPS.")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS: never clear an earlier signal.
            self.close()
            raise OSError("La operación ya tiene un evento de continuidad; no se repetirá.")

    def signal_interruption(self):
        # Called from SCM power notification: a single native call, no lock or IO.
        if self.handle:
            self.api.SetEvent(self.handle)

    def close(self):
        handle, self.handle = getattr(self, "handle", None), None
        if handle:
            self.api.CloseHandle(handle)

    def __del__(self):
        self.close()


def _require_operation_not_interrupted(operation_id):
    from .ups_power import UpsPowerBlocked
    api = _event_api()
    handle = api.OpenEventW(0x00100000, False, _operation_event_name(operation_id))  # SYNCHRONIZE only
    if not handle:
        raise UpsPowerBlocked("No existe el proceso propietario activo de esta operación del UPS.")
    try:
        if api.WaitForSingleObject(handle, 0) != 258:  # WAIT_TIMEOUT is the only non-signaled result.
            raise UpsPowerBlocked("La operación del UPS fue interrumpida; no se enviará control tras reanudar.")
    finally:
        api.CloseHandle(handle)
    return True


def _production_control_operation(path, profile, journal_path, operation_id, selector):
    """Read only the current service's fixed, durable operation before HID."""
    from .ups_power import OperationJournal, UpsPowerBlocked, profile_matches_device
    from .ups_test_authorization import _registered_data_dir
    from . import windows_service
    _require_control_service_authorization()
    data_dir = _registered_data_dir()
    journal = Path(journal_path)
    if (not journal.is_absolute() or journal.name != "ups-power-operation.json"
            or journal.resolve().name != "ups-power-operation.json" or journal.resolve().parent != data_dir):
        raise UpsPowerBlocked("El control requiere el registro propio del servicio actual.")
    operation = OperationJournal(journal.resolve()).load()
    expected = "arming" if selector == "shutdown" else "cancelling"
    status = windows_service.manage("status")
    if (not isinstance(operation, dict) or operation.get("operation_id") != operation_id or operation.get("state") != expected
            or operation.get("profile_id") != profile["profile_id"] or operation.get("interrupted") is True
            or operation.get("device_fingerprint") != profile["device_fingerprint"]
            or operation.get("worker_claimed_for") == selector
            or type(operation.get("owner_pid")) is not int or operation["owner_pid"] <= 0
            or status.get("installed") is not True or status.get("state") != "running" or status.get("pid") != operation["owner_pid"]
            or not profile_matches_device(profile, {**profile, "path": path})):
        raise UpsPowerBlocked("El control no pertenece a una operación vigente del servicio actual.")
    return operation


class ControlTransport:
    """Dedicated, approved-profile controls; ordinary status stays QS-only."""

    def __init__(self, path, profile_id, *, journal_path=None, operation_id=None):
        from .ups_power import validated_control_profile
        self.profile = validated_control_profile(profile_id)
        _require_control_service_authorization()
        self.path = _validate_path(path)
        if journal_path is None or operation_id is None:
            raise ValueError("El control requiere registro durable e identificador de operación propios.")
        self.journal_path, self.operation_id = os.fspath(journal_path), operation_id
        self._interruption_event = _OperationInterruption(operation_id)

    def signal_interruption(self):
        self._interruption_event.signal_interruption()

    def submit_shutdown(self, delay_seconds):
        from .ups_power import UpsPowerBlocked, configured_delay_seconds, shutdown_command
        _require_control_service_authorization()
        operation = _production_control_operation(self.path, self.profile, self.journal_path, self.operation_id, "shutdown")
        allowed = configured_delay_seconds(self.profile, operation["action"], delay_seconds)
        if type(delay_seconds) is not int or allowed != operation.get("delay_seconds"):
            raise UpsPowerBlocked("El retardo no coincide con la operación propia del servicio.")
        shutdown_command(delay_seconds)
        return self._submit("shutdown", str(delay_seconds), reports=2)

    def cancel_shutdown(self):
        return self._submit("cancel", reports=1)

    def _submit(self, selector, *values, reports):
        from .ups_power import validated_control_profile
        validated_control_profile(self.profile["profile_id"])
        _require_control_service_authorization()
        _require_operation_not_interrupted(self.operation_id)
        _production_control_operation(self.path, self.profile, self.journal_path, self.operation_id, selector)
        key = self.path.casefold()
        with _cooldown_lock:
            if time.monotonic() < _cooldown_until.get(key, 0):
                raise TimeoutError("USB en espera de seguridad; no se envió control.")
        try:
            result = _run_bounded_worker(child_command("control-worker", self.path,
                                         self.profile["profile_id"], selector, *values, self.journal_path, self.operation_id), key)
            if result.returncode or not result.stdout or len(result.stdout) > 2048:
                raise OSError("Control del UPS sin envío completo confirmado; resultado incierto.")
            data = json.loads(result.stdout)
            if (not isinstance(data, dict) or data.get("state") != "submitted"
                    or data.get("confirmation") != "unconfirmed" or type(data.get("reports_written")) is not int
                    or data["reports_written"] != reports):
                raise OSError("El proceso devolvió una confirmación de control no válida.")
            return data
        except (OSError, ValueError, TypeError, UnicodeError):
            with _cooldown_lock:
                _cooldown_until[key] = time.monotonic() + 5
            raise


def _cancellation_test_profile(path, authorization_path, journal_path, operation_id, selector):
    """Authorize this explicit test without adding a production profile."""
    from .ups_power import NT2402PRO_CANDIDATE_PROFILE, UpsPowerBlocked, profile_matches_device
    from .ups_cancel_test import validate_test_worker_request
    from .ups_test_authorization import authorize_test_worker
    _validate_path(path)
    if selector not in ("start", "cancel"):
        raise UpsPowerBlocked("Operación de prueba del UPS no admitida.")
    try:
        authorized = authorize_test_worker(authorization_path, journal_path)
    except Exception as exc:
        raise UpsPowerBlocked("La prueba requiere autorización administrativa del servicio actual.") from exc
    if authorized is not True:
        raise UpsPowerBlocked("La prueba requiere autorización administrativa del servicio actual.")
    profile = validate_test_worker_request(authorization_path, journal_path, operation_id, selector)
    candidate_device = {**NT2402PRO_CANDIDATE_PROFILE, "path": path}
    if (not isinstance(profile, dict)
            or any(profile.get(key) != value for key, value in NT2402PRO_CANDIDATE_PROFILE.items())
            or not profile_matches_device(profile, candidate_device)):
        raise UpsPowerBlocked("La autorización de prueba no corresponde a esta unidad USB.")
    return profile


class CancellationTestTransport:
    """One manual cancellation test; never authorizes automatic UPS cutoff.

    This role has exactly two selectors, a fixed three-minute stay-off request
    and cancellation of that own journaled request. Parent and isolated worker
    independently check the authorization and operation before opening USB.
    """

    def __init__(self, path, authorization_path, journal_path, operation_id):
        self.path = _validate_path(path)
        self.authorization_path = os.fspath(authorization_path)
        self.journal_path = os.fspath(journal_path)
        self.operation_id = operation_id
        self._interruption_event = _OperationInterruption(operation_id)

    def signal_interruption(self):
        self._interruption_event.signal_interruption()

    def submit_test(self):
        return self._submit("start", reports=2)

    def cancel_test(self):
        return self._submit("cancel", reports=1)

    def _submit(self, selector, *, reports):
        _cancellation_test_profile(self.path, self.authorization_path, self.journal_path,
                                   self.operation_id, selector)
        _require_operation_not_interrupted(self.operation_id)
        key = self.path.casefold()
        with _cooldown_lock:
            if time.monotonic() < _cooldown_until.get(key, 0):
                raise TimeoutError("USB en espera de seguridad; no se envió control de prueba.")
        try:
            result = _run_bounded_worker(child_command("cancel-test-worker", self.path,
                        self.authorization_path, self.journal_path, self.operation_id, selector), key)
            if result.returncode or not result.stdout or len(result.stdout) > 2048:
                raise OSError("Prueba del UPS sin envío completo confirmado; resultado incierto.")
            data = json.loads(result.stdout)
            if (not isinstance(data, dict) or data.get("state") != "submitted"
                    or data.get("confirmation") != "unconfirmed" or type(data.get("reports_written")) is not int
                    or data["reports_written"] != reports):
                raise OSError("El proceso devolvió un resultado de prueba no válido.")
            return data
        except (OSError, ValueError, TypeError, UnicodeError):
            with _cooldown_lock:
                _cooldown_until[key] = time.monotonic() + 5
            raise


def cancel_test_worker_main(argv):
    """Fixed manual-test role; both authorization gates precede all HID APIs."""
    if len(argv) != 5 or argv[4] not in ("start", "cancel"):
        raise SystemExit("Uso interno: prueba manual aislada de cancelación del UPS.")
    try:
        path, authorization_path, journal_path, operation_id, selector = argv
        profile = _cancellation_test_profile(path, authorization_path, journal_path, operation_id, selector)
        _require_operation_not_interrupted(operation_id)
        command = b"S03R0000\r" if selector == "start" else b"C\r"
        if selector == "cancel":
            from .qs_protocol import parse_qs
            from .ups_cancel_test import validate_cancel_test_preflight

            def before_cancel():
                # Intra-worker only: no subprocess and no StatusTransport, so
                # the outer control mutex stays owned throughout QS and C.
                _require_operation_not_interrupted(operation_id)
                sample = parse_qs(_query_status_once(path))
                _require_operation_not_interrupted(operation_id)
                validate_cancel_test_preflight(authorization_path, journal_path, operation_id, sample=sample)

            result = _write_control_reports_once(path, command, profile, before_write=before_cancel,
                                                 before_each_report=lambda: _require_operation_not_interrupted(operation_id))
        else:
            result = _write_control_reports_once(path, command, profile,
                                                 before_each_report=lambda: _require_operation_not_interrupted(operation_id))
        sys.stdout.buffer.write(json.dumps(result, allow_nan=False).encode("utf-8"))
        return 0
    except Exception:
        # Never expose local file paths or journal details through the worker.
        print("Prueba manual del UPS bloqueada o resultado de envío incierto.", file=sys.stderr)
        return 1


def _battery_test_profile(path, authorization_path, journal_path, operation_id, selector):
    from .ups_power import NT2402PRO_CANDIDATE_PROFILE, UpsPowerBlocked, profile_matches_device
    from .ups_battery_test import validate_battery_test_worker_request
    from .ups_test_authorization import authorize_battery_test_worker
    _validate_path(path)
    if selector not in ("start", "cancel"):
        raise UpsPowerBlocked("Operación de prueba en batería no admitida.")
    if authorize_battery_test_worker(authorization_path, journal_path) is not True:
        raise UpsPowerBlocked("La prueba en batería requiere autorización administrativa propia.")
    profile = validate_battery_test_worker_request(authorization_path, journal_path, operation_id, selector)
    if (not isinstance(profile, dict)
            or any(profile.get(key) != value for key, value in NT2402PRO_CANDIDATE_PROFILE.items())
            or not profile_matches_device(profile, {**NT2402PRO_CANDIDATE_PROFILE, "path": path})):
        raise UpsPowerBlocked("La autorización de prueba no corresponde a esta unidad USB.")
    return profile


class BatteryTestTransport(CancellationTestTransport):
    """Distinct temporary permission and fixed commands for the battery test."""

    def cancel_after_windows_failure(self):
        return self._submit("cancel", reports=1)

    def cancel_test(self):
        raise ValueError("La prueba en batería no admite cancelación manual del UPS.")

    def _submit(self, selector, *, reports):
        _battery_test_profile(self.path, self.authorization_path, self.journal_path, self.operation_id, selector)
        _require_operation_not_interrupted(self.operation_id)
        key = self.path.casefold()
        with _cooldown_lock:
            if time.monotonic() < _cooldown_until.get(key, 0):
                raise TimeoutError("USB en espera de seguridad; no se envió control de prueba en batería.")
        try:
            result = _run_bounded_worker(child_command("battery-test-worker", self.path,
                self.authorization_path, self.journal_path, self.operation_id, selector), key)
            if result.returncode or not result.stdout or len(result.stdout) > 2048:
                raise OSError("Prueba en batería sin envío completo confirmado; resultado incierto.")
            data = json.loads(result.stdout)
            if (not isinstance(data, dict) or data.get("state") != "submitted"
                    or data.get("confirmation") != "unconfirmed" or type(data.get("reports_written")) is not int
                    or data["reports_written"] != reports):
                raise OSError("El proceso devolvió un resultado de prueba en batería no válido.")
            return data
        except (OSError, ValueError, TypeError, UnicodeError):
            with _cooldown_lock:
                _cooldown_until[key] = time.monotonic() + 5
            raise


def battery_test_worker_main(argv):
    if len(argv) != 5 or argv[4] not in ("start", "cancel"):
        raise SystemExit("Uso interno: prueba manual aislada del UPS en batería.")
    try:
        from .qs_protocol import parse_qs
        from .ups_battery_test import validate_battery_test_preflight
        path, authorization_path, journal_path, operation_id, selector = argv
        profile = _battery_test_profile(path, authorization_path, journal_path, operation_id, selector)
        _require_operation_not_interrupted(operation_id)

        def before_control():
            _require_operation_not_interrupted(operation_id)
            sample = parse_qs(_query_status_once(path))
            _require_operation_not_interrupted(operation_id)
            validate_battery_test_preflight(authorization_path, journal_path, operation_id, selector, sample=sample)

        command = b"S03R0000\r" if selector == "start" else b"C\r"
        result = _write_control_reports_once(path, command, profile, before_write=before_control,
                                             before_each_report=lambda: _require_operation_not_interrupted(operation_id))
        sys.stdout.buffer.write(json.dumps(result, allow_nan=False).encode("utf-8"))
        return 0
    except Exception:
        print("Prueba en batería bloqueada o resultado de envío incierto.", file=sys.stderr)
        return 1


def control_worker_main(argv):
    """The profile gate also protects direct invocation of this internal role."""
    from .ups_power import (OperationJournal, configured_delay_seconds, shutdown_command, validated_control_profile, UpsPowerBlocked,
                            validate_production_cancel_preflight, validate_production_start_preflight)
    if (len(argv) not in (5, 6) or argv[2] not in ("shutdown", "cancel")
            or len(argv) != (6 if argv[2] == "shutdown" else 5)):
        raise SystemExit("Uso interno: control UPS aislado con perfil validado.")
    try:
        profile = validated_control_profile(argv[1])
        _require_control_service_authorization()
        journal_path, operation_id = argv[-2:]
        _require_operation_not_interrupted(operation_id)
        initial_operation = _production_control_operation(argv[0], profile, journal_path, operation_id, argv[2])
        if argv[2] == "cancel":
            if len(argv) != 5:
                raise ValueError("Argumentos de cancelación no válidos.")
            command = b"C\r"
        else:
            if len(argv) != 6 or not argv[3].isascii() or not argv[3].isdigit():
                raise ValueError("Retardo de control no válido.")
            delay = int(argv[3])
            configured_delay_seconds(profile, initial_operation["action"], delay)
            if delay != initial_operation.get("delay_seconds"):
                raise ValueError("El retardo solicitado difiere de la operación durable propia.")
            command = shutdown_command(delay)

        control_context = {}

        def before_control():
            from .qs_protocol import parse_qs
            _require_operation_not_interrupted(operation_id)
            sample = parse_qs(_query_status_once(argv[0]))
            _require_operation_not_interrupted(operation_id)
            operation = _production_control_operation(argv[0], profile, journal_path, operation_id, argv[2])
            validate = validate_production_start_preflight if argv[2] == "shutdown" else validate_production_cancel_preflight
            validate(profile, operation, sample)
            control_context.update(operation=operation, sample=sample)
            # The named device mutex serializes this claim with every worker.
            # A crash or partial send never resumes the same transaction.
            OperationJournal(journal_path).write({**operation, "worker_claimed_for": argv[2]})
            _require_operation_not_interrupted(operation_id)

        def before_report():
            _require_operation_not_interrupted(operation_id)
            # Re-evaluate the absolute observed-sequence deadline after the
            # durable claim and between the S payload and its terminating CR.
            # The same immediate QS evidence is used; no additional query can
            # interrupt the command's two-report framing.
            validate = validate_production_start_preflight if argv[2] == "shutdown" else validate_production_cancel_preflight
            validate(profile, control_context["operation"], control_context["sample"])

        result = _write_control_once(argv[0], command, profile, before_write=before_control,
                                     before_each_report=before_report)
        sys.stdout.buffer.write(json.dumps(result, allow_nan=False).encode("utf-8"))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def battery_worker_main(argv):
    if len(argv) != 2 or argv[1] not in BATTERY_DIAGNOSTIC_QUERIES:
        raise SystemExit("Uso interno: diagnóstico de batería aislado BT/BL")
    try:
        sys.stdout.buffer.write(_query_battery_diagnostic_once(argv[0], BATTERY_DIAGNOSTIC_QUERIES[argv[1]]))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def worker_main(argv):
    if len(argv) != 1:
        raise SystemExit("Uso interno: diagnóstico QS aislado")
    try:
        if argv[0] == "--enumerate":
            payload = json.dumps(enumerate_ups(), allow_nan=False).encode("utf-8")
            if len(payload) > 65536:
                raise OSError("Demasiados metadatos HID")
            sys.stdout.buffer.write(payload)
            return 0
        sys.stdout.buffer.write(_query_status_once(argv[0]))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--qs-worker":
        raise SystemExit(worker_main(sys.argv[2:]))
    if len(sys.argv) >= 2 and sys.argv[1] == "--battery-worker":
        raise SystemExit(battery_worker_main(sys.argv[2:]))
    if len(sys.argv) >= 2 and sys.argv[1] == "--control-worker":
        raise SystemExit(control_worker_main(sys.argv[2:]))
    if len(sys.argv) >= 2 and sys.argv[1] == "--cancel-test-worker":
        raise SystemExit(cancel_test_worker_main(sys.argv[2:]))
    if len(sys.argv) >= 2 and sys.argv[1] == "--battery-test-worker":
        raise SystemExit(battery_test_worker_main(sys.argv[2:]))
    raise SystemExit("Uso interno: diagnóstico HID aislado")
