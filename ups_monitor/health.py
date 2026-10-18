"""Small in-memory health counters; collecting them never reads UPS or SQLite."""
from __future__ import annotations

import os
import ctypes
from ctypes import wintypes
import threading
import time

from .core import iso_utc


class ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


_MEMORY_UNINITIALIZED = object()
_memory_reader = _MEMORY_UNINITIALIZED
_memory_reader_lock = threading.Lock()


def _make_memory_reader():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    api = ctypes.WinDLL("psapi", use_last_error=True)
    api.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD]
    api.GetProcessMemoryInfo.restype = wintypes.BOOL

    def read():
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if api.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return int(counters.WorkingSetSize)
        return None

    return read


def working_set_bytes():
    """Return this process's resident memory when Windows exposes it."""
    if os.name != "nt":
        return None
    global _memory_reader
    try:
        with _memory_reader_lock:
            if _memory_reader is _MEMORY_UNINITIALIZED:
                try:
                    _memory_reader = _make_memory_reader()
                except (OSError, AttributeError, ValueError):
                    _memory_reader = None
            reader = _memory_reader
        return reader() if reader is not None else None
    except (OSError, AttributeError, ValueError):
        pass
    return None


class MonitorHealth:
    def __init__(self, *, clock=None):
        self.clock = clock or time.monotonic
        self.lock = threading.RLock()
        self.started_at = iso_utc()
        self.started_monotonic = self.clock()
        self.last_success = None
        self.last_success_monotonic = None
        self.read_started_monotonic = None
        self.last_read_finished_monotonic = None
        self.read_successes = self.read_failures = self.consecutive_failures = 0
        self.last_read_duration_ms = None
        self.last_error_at = self.last_error_kind = None
        self.last_supervisor_check = None
        self.recoveries = self.reader_restarts = 0
        self.needs_recovery = False
        self.cpu_baseline = (self.clock(), time.process_time())
        self.cpu_percent = None

    def begin_read(self):
        with self.lock:
            self.read_started_monotonic = self.clock()

    def finish_read(self, success, *, timestamp=None, error_kind=None):
        with self.lock:
            now = self.clock()
            if self.read_started_monotonic is not None:
                self.last_read_duration_ms = round(max(0, now - self.read_started_monotonic) * 1000, 2)
            self.read_started_monotonic = None
            self.last_read_finished_monotonic = now
            if success:
                self.read_successes += 1
                self.consecutive_failures = 0
                self.last_success, self.last_success_monotonic = timestamp, now
                if self.needs_recovery:
                    self.recoveries += 1
                self.needs_recovery = False
            else:
                self.read_failures += 1
                self.consecutive_failures += 1
                self.problem(error_kind or "telemetry")

    def problem(self, kind):
        with self.lock:
            self.needs_recovery = True
            self.last_error_at, self.last_error_kind = iso_utc(), kind

    def check(self):
        with self.lock:
            self.last_supervisor_check = iso_utc()

    def reset_progress_after_resume(self):
        """Sleeping time is not reader execution time; retain real failure totals."""
        with self.lock:
            self.read_started_monotonic = None
            self.last_read_finished_monotonic = self.clock()

    def abandon_read(self):
        with self.lock:
            self.read_started_monotonic = None

    def read_stalled(self, seconds):
        with self.lock:
            return self.read_started_monotonic is not None and self.clock() - self.read_started_monotonic > seconds

    def progress_stalled(self, seconds):
        with self.lock:
            previous = self.last_read_finished_monotonic
            return self.clock() - (previous if previous is not None else self.started_monotonic) > seconds

    def snapshot(self):
        with self.lock:
            now, cpu = self.clock(), time.process_time()
            elapsed = now - self.cpu_baseline[0]
            if elapsed >= 1:
                self.cpu_percent = round(max(0, cpu - self.cpu_baseline[1]) / elapsed * 100, 2)
                self.cpu_baseline = now, cpu
            return {
                "metrics_version": 1,
                "started_at": self.started_at,
                "uptime_seconds": round(max(0, now - self.started_monotonic), 1),
                "last_success": self.last_success,
                "read_age_seconds": round(max(0, now - self.last_success_monotonic), 1)
                    if self.last_success_monotonic is not None else None,
                "read_successes": self.read_successes, "read_failures": self.read_failures,
                "consecutive_failures": self.consecutive_failures,
                "last_read_duration_ms": self.last_read_duration_ms,
                "last_error_at": self.last_error_at, "last_error_kind": self.last_error_kind,
                "read_in_progress": self.read_started_monotonic is not None,
                "read_in_progress_seconds": round(max(0, now - self.read_started_monotonic), 1)
                    if self.read_started_monotonic is not None else None,
                "reader_progress_age_seconds": round(max(0, now - (self.last_read_finished_monotonic
                    if self.last_read_finished_monotonic is not None else self.started_monotonic)), 1),
                "last_supervisor_check": self.last_supervisor_check,
                "recoveries": self.recoveries, "reader_restarts": self.reader_restarts,
                "process": {"pid": os.getpid(), "cpu_seconds": round(cpu, 3),
                            "cpu_percent": self.cpu_percent, "memory_bytes": working_set_bytes(),
                            "threads": threading.active_count()},
            }
