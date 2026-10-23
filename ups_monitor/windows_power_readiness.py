"""Read-only Windows readiness before scheduling loss of UPS output power.

GetPwrCapabilities and the process token are queried; no power transition,
privilege adjustment, registry write or command execution occurs here.
"""
from __future__ import annotations

import ctypes
import os
import sys


BYTE = ctypes.c_ubyte
DWORD = ctypes.c_uint32
LONG = ctypes.c_int32
HANDLE = ctypes.c_void_p


class BATTERY_REPORTING_SCALE(ctypes.Structure):
    _fields_ = [("Granularity", DWORD), ("Capacity", DWORD)]


class SYSTEM_POWER_CAPABILITIES(ctypes.Structure):
    # Windows SDK winnt.h, Windows 10+ layout. The HiberFileType region was
    # reserved before Windows 10; the overall ABI is still 76 bytes.
    # https://learn.microsoft.com/windows/win32/api/winnt/ns-winnt-system_power_capabilities
    _fields_ = [(name, BYTE) for name in (
        "PowerButtonPresent", "SleepButtonPresent", "LidPresent", "SystemS1",
        "SystemS2", "SystemS3", "SystemS4", "SystemS5", "HiberFilePresent",
        "FullWake", "VideoDimPresent", "ApmPresent", "UpsPresent", "ThermalControl",
        "ProcessorThrottle", "ProcessorMinThrottle", "ProcessorMaxThrottle",
        "FastSystemS4", "Hiberboot", "WakeAlarmPresent", "AoAc", "DiskSpinDown",
        "HiberFileType", "AoAcConnectivitySupported")]
    _fields_ += [("spare3", BYTE * 6), ("SystemBatteriesPresent", BYTE),
        ("BatteriesAreShortTerm", BYTE), ("BatteryScale", BATTERY_REPORTING_SCALE * 3)]
    _fields_ += [(name, ctypes.c_int32) for name in (
        "AcOnLineWake", "SoftLidWake", "RtcWake", "MinDeviceWakeState", "DefaultLowLatencyWake")]


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", DWORD), ("HighPart", LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]


def _power_api():
    api = ctypes.WinDLL("powrprof", use_last_error=True)
    api.GetPwrCapabilities.argtypes = [ctypes.POINTER(SYSTEM_POWER_CAPABILITIES)]
    api.GetPwrCapabilities.restype = BYTE
    return api


def _read_capabilities():
    capabilities = SYSTEM_POWER_CAPABILITIES()
    if not _power_api().GetPwrCapabilities(ctypes.byref(capabilities)):
        raise OSError("No se pudieron consultar las capacidades de energía de Windows.")
    return capabilities


def _token_apis():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.argtypes, kernel.GetCurrentProcess.restype = [], HANDLE
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [HANDLE], ctypes.c_int32
    security.OpenProcessToken.argtypes = [HANDLE, DWORD, ctypes.POINTER(HANDLE)]
    security.OpenProcessToken.restype = ctypes.c_int32
    security.LookupPrivilegeValueW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.POINTER(LUID)]
    security.LookupPrivilegeValueW.restype = ctypes.c_int32
    security.GetTokenInformation.argtypes = [HANDLE, ctypes.c_int32, HANDLE, DWORD, ctypes.POINTER(DWORD)]
    security.GetTokenInformation.restype = ctypes.c_int32
    return kernel, security


def _has_shutdown_privilege():
    kernel, security = _token_apis()
    privilege = LUID()
    if not security.LookupPrivilegeValueW(None, "SeShutdownPrivilege", ctypes.byref(privilege)):
        raise OSError("No se pudo consultar el permiso de apagado de Windows.")
    token = HANDLE()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)) or not token.value:
        raise OSError("No se pudo consultar la identidad del proceso de protección.")
    try:
        needed = DWORD()
        security.GetTokenInformation(token, 3, None, 0, ctypes.byref(needed))  # TokenPrivileges
        offset = TOKEN_PRIVILEGES.Privileges.offset
        if not offset <= needed.value <= 65536:
            raise OSError("El tamaño de los permisos de Windows no es válido.")
        allocated = needed.value
        buffer = ctypes.create_string_buffer(allocated)
        if not security.GetTokenInformation(token, 3, buffer, allocated, ctypes.byref(needed)):
            raise OSError("No se pudieron leer los permisos de Windows.")
        if not offset <= needed.value <= allocated:
            raise OSError("La lectura de permisos de Windows está incompleta.")
        count = DWORD.from_buffer(buffer).value
        entry_size = ctypes.sizeof(LUID_AND_ATTRIBUTES)
        if count > (needed.value - offset) // entry_size:
            raise OSError("La lista de permisos de Windows está incompleta.")
        for index in range(count):
            entry = LUID_AND_ATTRIBUTES.from_buffer(buffer, offset + index * entry_size)
            if (entry.Luid.LowPart == privilege.LowPart and entry.Luid.HighPart == privilege.HighPart
                    and not entry.Attributes & 0x00000004):  # SE_PRIVILEGE_REMOVED
                # A disabled but present privilege can be enabled by shutdown.exe.
                # The readiness query deliberately does not adjust the token.
                return True
        return False
    finally:
        kernel.CloseHandle(token)


def _hiberfile_type_supported():
    return sys.getwindowsversion().major >= 10


def windows_power_readiness(action="hibernate"):
    """Return readiness for the real process, without requesting any action."""
    if os.name != "nt":
        return {"available": False, "reason": "La protección real requiere Windows."}
    if action not in ("hibernate", "shutdown"):
        return {"available": False, "reason": "La acción de Windows no está admitida."}
    try:
        if action == "hibernate":
            capabilities = _read_capabilities()
            if not capabilities.SystemS4 or not capabilities.HiberFilePresent:
                return {"available": False,
                    "reason": "Windows no tiene la hibernación disponible; habilítala antes de programar el apagado del UPS."}
            if _hiberfile_type_supported() and capabilities.HiberFileType != 2:  # HIBERFILE_TYPE_FULL
                return {"available": False,
                    "reason": "Windows necesita un archivo de hibernación completo; el inicio rápido por sí solo no conserva la sesión."}
        if not _has_shutdown_privilege():
            return {"available": False,
                "reason": "El proceso de protección no tiene permiso para solicitar hibernación o apagado de Windows."}
    except Exception:
        return {"available": False,
            "reason": "No se pudo verificar que Windows esté preparado para actuar; revisa el servicio y la hibernación."}
    return {"available": True, "reason": None}
