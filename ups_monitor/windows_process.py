"""Read-only process identity checks for an unrelated Chrome UDP binding.

An occupied port normally remains uncertain. This narrow exception requires
live handles to every owner, exact installed Chrome image paths, a complete
process snapshot without a possible OEM collector, and a fresh UDP snapshot.
It does not claim atomic exclusion of another reader starting afterwards.
"""
from __future__ import annotations

import ctypes
import ntpath
import os

from .windows_udp import MAX_REPORTED_PIDS


DWORD = ctypes.c_uint32
HANDLE = ctypes.c_void_p
STILL_ACTIVE, ERROR_NO_MORE_FILES = 259, 18
MAX_PROCESSES = 8192
OEM_PROCESS_NAMES = {"upsmonitor.exe", "forza.exe", "upstray.exe", "java.exe", "javaw.exe"}


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("size", DWORD), ("usage", DWORD), ("pid", DWORD),
                ("heap", ctypes.c_size_t), ("module", DWORD), ("threads", DWORD),
                ("parent_pid", DWORD), ("priority", ctypes.c_int32), ("flags", DWORD),
                ("image", ctypes.c_uint16 * 260)]


def _apis():
    if os.name != "nt":
        raise OSError("Windows process identity is unavailable")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "OpenProcess": ([DWORD, ctypes.c_int, DWORD], HANDLE),
        "QueryFullProcessImageNameW": ([HANDLE, DWORD, ctypes.c_wchar_p, ctypes.POINTER(DWORD)], ctypes.c_int),
        "GetExitCodeProcess": ([HANDLE, ctypes.POINTER(DWORD)], ctypes.c_int),
        "CreateToolhelp32Snapshot": ([DWORD, DWORD], HANDLE),
        "Process32FirstW": ([HANDLE, ctypes.POINTER(PROCESSENTRY32W)], ctypes.c_int),
        "Process32NextW": ([HANDLE, ctypes.POINTER(PROCESSENTRY32W)], ctypes.c_int),
        "GetLastError": ([], DWORD),
        "CloseHandle": ([HANDLE], ctypes.c_int),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return kernel


def _chrome_paths():
    paths = set()
    for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(variable, "")
        drive, tail = ntpath.splitdrive(base)
        if len(drive) == 2 and drive[1] == ":" and tail.startswith("\\"):
            paths.add(ntpath.normcase(ntpath.normpath(ntpath.join(base, "Google", "Chrome", "Application", "chrome.exe"))))
    return paths


def _is_alive(kernel, handle):
    code = DWORD()
    return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == STILL_ACTIVE)


def _close(kernel, handle):
    try:
        return bool(kernel.CloseHandle(handle))
    except (OSError, AttributeError, TypeError, ValueError, ctypes.ArgumentError):
        return False


def _no_oem_processes(kernel):
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return False
    safe = False
    try:
        entry = PROCESSENTRY32W()
        entry.size = ctypes.sizeof(entry)
        present = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        if present:
            for _ in range(MAX_PROCESSES):
                text = bytes(entry.image).decode("utf-16-le")
                if "\0" not in text:
                    break
                name = text.split("\0", 1)[0].lower()
                if not name or name in OEM_PROCESS_NAMES:
                    break
                if not kernel.Process32NextW(snapshot, ctypes.byref(entry)):
                    safe = kernel.GetLastError() == ERROR_NO_MORE_FILES
                    break
    finally:
        if not _close(kernel, snapshot):
            safe = False
    return safe


def browser_only_owners(pids, recheck):
    """Confirm only installed Chrome owns the port; uncertainty returns False.

    ``recheck`` rereads both UDP families while original process handles stay
    open. At the cap the PID list could be truncated, so it is never exempted.
    Paths are used only for comparison and never returned in diagnostics.
    """
    if (not isinstance(pids, tuple) or not 0 < len(pids) < MAX_REPORTED_PIDS
            or any(type(pid) is not int or not 0 < pid <= 0xFFFFFFFF for pid in pids)
            or len(set(pids)) != len(pids)):
        return False
    handles, kernel, allowed = [], None, False
    try:
        kernel = _apis()
        expected = _chrome_paths()
        if not expected:
            return False
        for pid in pids:
            handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            handles.append(handle)
            image, length = ctypes.create_unicode_buffer(32768), DWORD(32768)
            if (not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length))
                    or not 0 < length.value < 32768 or len(image.value) != length.value
                    or ntpath.normcase(ntpath.normpath(image.value)) not in expected):
                return False
        # Neither UDP timeout nor a known browser alone is sufficient. Require
        # the same owners (or a now empty port) and no possible OEM process.
        current = recheck()
        same_owners = current.state == "occupied" and current.pids == pids
        empty_port = current.state == "absent" and not current.pids
        allowed = ((same_owners or empty_port) and _no_oem_processes(kernel)
                   and all(_is_alive(kernel, handle) for handle in handles))
    except (OSError, AttributeError, TypeError, ValueError, ctypes.ArgumentError):
        allowed = False
    finally:
        if kernel is not None:
            for handle in handles:
                if not _close(kernel, handle):
                    allowed = False
    return allowed
