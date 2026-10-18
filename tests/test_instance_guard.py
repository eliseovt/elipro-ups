import ctypes
from ctypes import wintypes
import threading
import unittest
from unittest.mock import Mock, patch

from ups_monitor import instance_guard
from ups_monitor.instance_guard import (
    AlreadyRunningError, InstanceGuardError, MonitorInstanceGuard,
    MUTEX_NAME, WAIT_ABANDONED, WAIT_FAILED, WAIT_OBJECT_0, WAIT_TIMEOUT,
)


class MonitorInstanceGuardTests(unittest.TestCase):
    def setUp(self):
        self.kernel32 = Mock()
        # Above the 32-bit range to catch code that converts handles to DWORD.
        self.handle = 0x123456789AB
        self.kernel32.CreateMutexW.return_value = self.handle
        self.kernel32.WaitForSingleObject.return_value = WAIT_OBJECT_0
        self.kernel32.ReleaseMutex.return_value = True
        self.kernel32.CloseHandle.return_value = True
        self.real_loader = instance_guard._load_kernel32
        self.platform = patch.object(instance_guard.os, "name", "nt")
        self.load = patch.object(instance_guard, "_load_kernel32", return_value=self.kernel32)
        self.last_error = patch.object(ctypes, "get_last_error", return_value=5, create=True)
        self.platform.start()
        self.load.start()
        self.last_error.start()
        self.addCleanup(self.platform.stop)
        self.addCleanup(self.load.stop)
        self.addCleanup(self.last_error.stop)
        self.addCleanup(self.reset_process_owner)

    @staticmethod
    def reset_process_owner():
        # Failed releases intentionally reserve the interpreter until exit.
        instance_guard._process_owner = None

    def test_context_holds_one_global_mutex_until_resources_have_stopped(self):
        with MonitorInstanceGuard() as guard:
            self.assertFalse(guard.abandoned)
            self.kernel32.CreateMutexW.assert_called_once_with(None, False, MUTEX_NAME)
            self.assertEqual(MUTEX_NAME, "Global\\EliproUPS.Monitor")
            self.kernel32.WaitForSingleObject.assert_called_once_with(self.handle, 0)
            self.kernel32.ReleaseMutex.assert_not_called()
            self.kernel32.CloseHandle.assert_not_called()
        self.kernel32.ReleaseMutex.assert_called_once_with(self.handle)
        self.kernel32.CloseHandle.assert_called_once_with(self.handle)
        guard.close()
        self.assertEqual(self.kernel32.CloseHandle.call_count, 1)

    def test_constructing_guard_does_not_acquire_or_touch_windows(self):
        MonitorInstanceGuard()
        self.kernel32.CreateMutexW.assert_not_called()
        self.kernel32.WaitForSingleObject.assert_not_called()
        self.assertIsNone(instance_guard._process_owner)

    def test_second_owner_in_same_interpreter_cannot_exploit_recursive_windows_mutex(self):
        with MonitorInstanceGuard() as guard:
            for contender in (guard, MonitorInstanceGuard()):
                with self.assertRaises(AlreadyRunningError):
                    contender.acquire()
            self.kernel32.CreateMutexW.assert_called_once()
        with MonitorInstanceGuard():
            pass
        self.assertEqual(self.kernel32.CreateMutexW.call_count, 2)

    def test_other_process_owner_refused_without_release_or_waiting(self):
        self.kernel32.WaitForSingleObject.return_value = WAIT_TIMEOUT
        with self.assertRaisesRegex(AlreadyRunningError, "otro puerto o carpeta"):
            MonitorInstanceGuard().acquire()
        self.kernel32.WaitForSingleObject.assert_called_once_with(self.handle, 0)
        self.kernel32.ReleaseMutex.assert_not_called()
        self.kernel32.CloseHandle.assert_called_once_with(self.handle)
        self.assertIsNone(instance_guard._process_owner)

    def test_abandoned_mutex_grants_ownership_and_is_released_normally(self):
        self.kernel32.WaitForSingleObject.return_value = WAIT_ABANDONED
        with MonitorInstanceGuard() as guard:
            self.assertTrue(guard.abandoned)
            self.kernel32.CloseHandle.assert_not_called()
        self.kernel32.ReleaseMutex.assert_called_once_with(self.handle)
        self.kernel32.CloseHandle.assert_called_once_with(self.handle)

    def test_create_permission_error_aborts_without_wait_or_close_invalid_handle(self):
        self.kernel32.CreateMutexW.return_value = None
        with self.assertRaisesRegex(InstanceGuardError, "Windows 5"):
            MonitorInstanceGuard().acquire()
        self.kernel32.WaitForSingleObject.assert_not_called()
        self.kernel32.ReleaseMutex.assert_not_called()
        self.kernel32.CloseHandle.assert_not_called()
        self.assertIsNone(instance_guard._process_owner)

    def test_wait_failure_and_unknown_results_fail_closed_and_close_handle(self):
        for result in (WAIT_FAILED, 7):
            with self.subTest(result=result):
                self.kernel32.reset_mock()
                self.kernel32.WaitForSingleObject.return_value = result
                with self.assertRaises(InstanceGuardError):
                    MonitorInstanceGuard().acquire()
                self.kernel32.CloseHandle.assert_called_once_with(self.handle)
                self.kernel32.ReleaseMutex.assert_not_called()
                self.assertIsNone(instance_guard._process_owner)

    def test_context_body_error_still_releases_and_closes(self):
        with self.assertRaisesRegex(ValueError, "startup failed"):
            with MonitorInstanceGuard():
                raise ValueError("startup failed")
        self.kernel32.ReleaseMutex.assert_called_once_with(self.handle)
        self.kernel32.CloseHandle.assert_called_once_with(self.handle)

    def test_release_failure_still_closes_handle_and_reserves_process(self):
        guard = MonitorInstanceGuard().acquire()
        self.kernel32.ReleaseMutex.return_value = False
        with self.assertRaisesRegex(InstanceGuardError, "liberar"):
            guard.close()
        self.kernel32.CloseHandle.assert_called_once_with(self.handle)
        with self.assertRaises(InstanceGuardError):
            guard.close()
        self.kernel32.CloseHandle.assert_called_once_with(self.handle)
        with self.assertRaises(AlreadyRunningError):
            MonitorInstanceGuard().acquire()

    def test_close_failure_is_reported_and_does_not_admit_another_monitor(self):
        guard = MonitorInstanceGuard().acquire()
        self.kernel32.CloseHandle.return_value = False
        with self.assertRaisesRegex(InstanceGuardError, "cerrar"):
            guard.close()
        self.kernel32.ReleaseMutex.assert_called_once_with(self.handle)
        with self.assertRaises(AlreadyRunningError):
            MonitorInstanceGuard().acquire()

    def test_other_thread_cannot_release_owner_and_guard_remains_held(self):
        guard = MonitorInstanceGuard().acquire()
        errors = []

        def close_from_other_thread():
            try:
                guard.close()
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=close_from_other_thread)
        worker.start()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], InstanceGuardError)
        self.kernel32.ReleaseMutex.assert_not_called()
        self.kernel32.CloseHandle.assert_not_called()
        guard.close()
        self.kernel32.ReleaseMutex.assert_called_once_with(self.handle)

    def test_loader_os_error_also_aborts_before_any_monitor_resources(self):
        with patch.object(instance_guard, "_load_kernel32", side_effect=OSError("denied")):
            with self.assertRaises(InstanceGuardError):
                MonitorInstanceGuard().acquire()
        self.assertIsNone(instance_guard._process_owner)

    def test_win32_bindings_use_pointer_width_handles_and_do_not_inherit_them(self):
        library = Mock()
        with patch.object(ctypes, "WinDLL", return_value=library, create=True) as load:
            # Call the real loader rather than the fixture's replacement.
            self.real_loader()
        load.assert_called_once_with("kernel32", use_last_error=True)
        self.assertEqual(library.CreateMutexW.restype, wintypes.HANDLE)
        self.assertEqual(library.CreateMutexW.argtypes, [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR])
        self.assertEqual(library.WaitForSingleObject.argtypes, [wintypes.HANDLE, wintypes.DWORD])
        self.assertEqual(library.ReleaseMutex.argtypes, [wintypes.HANDLE])
        self.assertEqual(library.CloseHandle.argtypes, [wintypes.HANDLE])


if __name__ == "__main__":
    unittest.main()
