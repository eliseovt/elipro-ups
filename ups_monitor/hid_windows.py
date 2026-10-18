"""Windows HID discovery and an explicit, isolated QS-only diagnostic transport.

SetupAPI locates the HID collection. CreateFile uses desiredAccess=0 and shared
read/write solely to inspect attributes, strings and cached report capabilities.
Enumeration sends no UPS commands. Explicit query_status sends only QS/CR and
does not control the UPS. It runs in a disposable process with a 3-second limit.

Microsoft references:
https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew
https://learn.microsoft.com/en-us/windows/win32/api/setupapi/nf-setupapi-setupdigetdeviceinterfacedetailw
https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/hidpi/ns-hidpi-_hidp_caps
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import math
import subprocess
import sys
import time
import threading
from ctypes import wintypes
from .runtime import child_command, resource_root

VENDOR_ID = 0x0665
PRODUCT_ID = 0x5161
STATUS_QUERY = b"QS\r"
_cooldown_until = {}
_cooldown_lock = threading.Lock()


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
    """Worker only: exactly one status query, without any control commands."""
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
        # Output report 0, eight payload bytes; no alternate commands/framing.
        output = ctypes.create_string_buffer(b"\x00QS\r\x00\x00\x00\x00\x00", 9)
        if not hid.HidD_SetOutputReport(handle, output, 9):
            raise ctypes.WinError(ctypes.get_last_error())
        response = bytearray()
        while time.monotonic() < deadline and len(response) < 256:
            response.extend(_read_report(kernel, handle, event, deadline))
            if b"\r" in response:
                return bytes(response[:response.index(13) + 1])
        raise TimeoutError("Respuesta QS incompleta o demasiado larga")
    finally:
        if event:
            kernel.CloseHandle(event)
        if handle:
            kernel.CloseHandle(handle)
        if acquired:
            kernel.ReleaseMutex(mutex)
        kernel.CloseHandle(mutex)


class StatusTransport:
    """QS-only transport validated against this NT-2402PRO while online.

    Direct readings have been compared with the OEM collector. Real outages and
    automatic OS shutdown have not been exercised on the physical UPS.

    HidD_SetOutputReport is synchronous; Windows cancellation may also wait for a
    driver. A child process contains both operations so a hung driver cannot block
    the caller beyond three seconds under normal Windows process cancellation.
    """

    def __init__(self, path):
        self.path = _validate_path(path)

    def query_status(self, command=STATUS_QUERY):
        if type(command) is not bytes or command != STATUS_QUERY:
            raise ValueError("Solo se permite la consulta de estado QS seguida de CR")
        key = self.path.casefold()
        with _cooldown_lock:
            if time.monotonic() < _cooldown_until.get(key, 0):
                raise TimeoutError("USB en espera de seguridad tras una respuesta fallida (5 segundos)")
        args = child_command("qs-worker", self.path)
        try:
            try:
                result = subprocess.run(args, capture_output=True, timeout=3,
                                        cwd=str(resource_root()),
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError("El diagnóstico HID excedió 3 segundos y fue terminado") from exc
            if result.returncode:
                raise OSError(result.stderr.decode("utf-8", "replace").strip()[:500] or "Falló el diagnóstico HID")
            if not result.stdout or len(result.stdout) > 256 or not result.stdout.endswith(b"\r"):
                raise OSError("El proceso devolvió una respuesta QS incompleta")
        except OSError:
            # Per-path state survives NativeHidAdapter recreating its transport.
            with _cooldown_lock:
                _cooldown_until[key] = time.monotonic() + 5
            raise
        return result.stdout


def worker_main(argv):
    if len(argv) != 1:
        raise SystemExit("Uso interno: diagnóstico QS aislado")
    try:
        sys.stdout.buffer.write(_query_status_once(argv[0]))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--qs-worker":
        raise SystemExit("Uso interno: diagnóstico QS aislado")
    raise SystemExit(worker_main(sys.argv[2:]))
