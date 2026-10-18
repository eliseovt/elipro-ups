"""Process-lifetime ownership for the one Elipro monitor on this computer.

Use ``with MonitorInstanceGuard():`` in the monitor entry point, before opening
settings, USB or notifications, and keep it until those resources have stopped.
Importing this module does not acquire anything, so QS worker imports stay safe.
The Windows mutex is independent of the HTTP port and installation/data paths.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import tempfile
import threading


MUTEX_NAME = "Global\\EliproUPS.Monitor"
GUARDIAN_MUTEX_NAME = "Global\\EliproUPS.Guardian"
WAIT_OBJECT_0 = 0
WAIT_ABANDONED = 0x80
WAIT_TIMEOUT = 0x102
WAIT_FAILED = 0xFFFFFFFF

_process_lock = threading.Lock()
_process_owner = None
_guardian_process_lock = threading.Lock()
_guardian_process_owner = None


class InstanceGuardError(RuntimeError):
    """Ownership could not be verified; starting a monitor would be unsafe."""


class AlreadyRunningError(InstanceGuardError):
    """Another monitor already owns the computer-wide monitor lock."""


def _load_kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _windows_error(operation):
    return InstanceGuardError(
        f"No se pudo {operation} el bloqueo de Elipro "
        f"(error de Windows {ctypes.get_last_error()}). No se iniciará otro monitor."
    )


class _WindowsOwnership:
    def __init__(self, name=MUTEX_NAME):
        self.kernel32 = _load_kernel32()
        self.handle = self.kernel32.CreateMutexW(None, False, name)
        self.abandoned = False
        self._close_error = None
        if not self.handle:
            raise _windows_error("abrir")
        try:
            result = self.kernel32.WaitForSingleObject(self.handle, 0)
            if result == WAIT_TIMEOUT:
                raise AlreadyRunningError(
                    "Ya hay un monitor de Elipro activo en esta PC. "
                    "Ciérralo antes de iniciar otro, aunque use otro puerto o carpeta."
                )
            if result not in (WAIT_OBJECT_0, WAIT_ABANDONED):
                raise _windows_error("adquirir")
            # WAIT_ABANDONED grants ownership too. A fresh monitor must still
            # obtain fresh telemetry; no prior countdown/runtime is recovered.
            self.abandoned = result == WAIT_ABANDONED
        except BaseException:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None
            raise

    def close(self):
        if self.handle is None:
            if self._close_error:
                raise self._close_error
            return
        release_error = None
        try:
            if not self.kernel32.ReleaseMutex(self.handle):
                release_error = _windows_error("liberar")
        finally:
            closed = self.kernel32.CloseHandle(self.handle)
            self.handle = None
        if release_error:
            self._close_error = release_error
            raise release_error
        if not closed:
            self._close_error = _windows_error("cerrar")
            raise self._close_error


class _FileOwnership:
    """Non-Windows fallback for development; the shipped Windows path is above."""

    def __init__(self, lock_filename="elipro-ups-monitor.lock"):
        import fcntl

        self.abandoned = False
        self.file = None
        # Keep a stable path across checkouts and ports; do not unlink it while
        # unlocking, since replacing its inode could admit two lock owners.
        path = Path(tempfile.gettempdir()) / lock_filename
        try:
            self.file = open(path, "a+b")
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            if self.file is not None:
                self.file.close()
            raise AlreadyRunningError("Ya hay un monitor de Elipro activo en esta PC.") from exc
        except OSError as exc:
            if self.file is not None:
                self.file.close()
            raise InstanceGuardError("No se pudo verificar el bloqueo de Elipro. No se iniciará otro monitor.") from exc

    def close(self):
        # close releases flock, including when the process exits unexpectedly.
        self.file.close()


class MonitorInstanceGuard:
    """Nonblocking, non-reentrant ownership held by the calling thread.

    ``AlreadyRunningError`` means another owner exists; other
    ``InstanceGuardError`` failures also require aborting startup. Acquire and
    close on the same thread, normally the main thread surrounding serve_forever.
    ``abandoned`` reports an unclean previous exit on Windows, not stale state
    that should be restored. ``close`` is idempotent after a successful release.
    """

    def __init__(self):
        self._ownership = None
        self._thread = None
        self.abandoned = False

    def acquire(self):
        global _process_owner
        with _process_lock:
            # Windows mutexes are recursive on the same thread. This extra
            # process check prevents accidentally starting a second monitor
            # through another call to main() in that same interpreter.
            if _process_owner is not None:
                raise AlreadyRunningError("Ya hay un monitor de Elipro activo en este proceso.")
            try:
                ownership = _WindowsOwnership() if os.name == "nt" else _FileOwnership()
            except InstanceGuardError:
                raise
            except OSError as exc:
                raise InstanceGuardError("No se pudo verificar el bloqueo de Elipro. No se iniciará otro monitor.") from exc
            self._ownership = ownership
            self._thread = threading.get_ident()
            self.abandoned = ownership.abandoned
            _process_owner = self
        return self

    def close(self):
        global _process_owner
        with _process_lock:
            if self._ownership is None:
                return
            if threading.get_ident() != self._thread:
                raise InstanceGuardError("El bloqueo de Elipro debe liberarse desde el hilo que lo adquirió.")
            # Leave the process reserved on a release error: ownership is then
            # uncertain and another monitor in this interpreter must not start.
            self._ownership.close()
            self._ownership = None
            self._thread = None
            if _process_owner is self:
                _process_owner = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False


class GuardianInstanceGuard(MonitorInstanceGuard):
    """Separate ownership for the guardian; its monitor child owns the UPS lock."""

    def acquire(self):
        global _guardian_process_owner
        with _guardian_process_lock:
            if _guardian_process_owner is not None:
                raise AlreadyRunningError("Ya hay un supervisor de Elipro activo en este proceso.")
            try:
                ownership = (_WindowsOwnership(GUARDIAN_MUTEX_NAME) if os.name == "nt"
                             else _FileOwnership("elipro-ups-guardian.lock"))
            except AlreadyRunningError as exc:
                raise AlreadyRunningError("Ya hay un supervisor de Elipro activo en esta PC.") from exc
            except InstanceGuardError:
                raise
            except OSError as exc:
                raise InstanceGuardError("No se pudo verificar el bloqueo del supervisor. No se iniciará otro.") from exc
            self._ownership, self._thread = ownership, threading.get_ident()
            self.abandoned = ownership.abandoned
            _guardian_process_owner = self
        return self

    def close(self):
        global _guardian_process_owner
        with _guardian_process_lock:
            if self._ownership is None:
                return
            if threading.get_ident() != self._thread:
                raise InstanceGuardError("El bloqueo del supervisor debe liberarse desde el hilo que lo adquirió.")
            self._ownership.close()
            self._ownership = self._thread = None
            if _guardian_process_owner is self:
                _guardian_process_owner = None
