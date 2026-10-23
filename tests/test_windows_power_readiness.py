"""Read-only Win32 query tests. Never suspend, hibernate or change privileges."""
import ctypes
import unittest
from unittest.mock import Mock, patch

from ups_monitor import windows_power_readiness as readiness


class WindowsReadinessTests(unittest.TestCase):
    def capabilities(self, *, s4=1, present=1, kind=2):
        value = readiness.SYSTEM_POWER_CAPABILITIES()
        value.SystemS4, value.HiberFilePresent, value.HiberFileType = s4, present, kind
        return value

    def check(self, *, capabilities=None, privilege=True, modern=True, action="hibernate"):
        value = self.capabilities() if capabilities is None else capabilities
        with patch.object(readiness.os, "name", "nt"), \
                patch.object(readiness, "_read_capabilities", return_value=value), \
                patch.object(readiness, "_hiberfile_type_supported", return_value=modern), \
                patch.object(readiness, "_has_shutdown_privilege", return_value=privilege):
            return readiness.windows_power_readiness(action)

    def test_windows_sdk_layout(self):
        self.assertEqual(ctypes.sizeof(readiness.SYSTEM_POWER_CAPABILITIES), 76)
        self.assertEqual(readiness.SYSTEM_POWER_CAPABILITIES.SystemS4.offset, 6)
        self.assertEqual(readiness.SYSTEM_POWER_CAPABILITIES.HiberFilePresent.offset, 8)
        self.assertEqual(readiness.SYSTEM_POWER_CAPABILITIES.HiberFileType.offset, 22)
        self.assertEqual(readiness.SYSTEM_POWER_CAPABILITIES.BatteryScale.offset, 32)
        self.assertEqual(ctypes.sizeof(readiness.LUID_AND_ATTRIBUTES), 12)
        self.assertEqual(readiness.TOKEN_PRIVILEGES.Privileges.offset, 4)

    def test_full_hibernation_with_permission_is_ready(self):
        self.assertEqual(self.check(), {"available": True, "reason": None})

    def test_missing_s4_missing_file_and_reduced_file_block(self):
        for options in ({"s4": 0}, {"present": 0}, {"kind": 0}, {"kind": 1}, {"kind": 3}):
            with self.subTest(options=options):
                result = self.check(capabilities=self.capabilities(**options))
                self.assertFalse(result["available"])
                self.assertTrue(result["reason"])

    def test_older_windows_does_not_interpret_reserved_hiberfile_type(self):
        self.assertTrue(self.check(capabilities=self.capabilities(kind=0), modern=False)["available"])

    def test_missing_shutdown_privilege_blocks(self):
        result = self.check(privilege=False)
        self.assertFalse(result["available"])
        self.assertIn("permiso", result["reason"])

    def test_shutdown_does_not_require_hibernation(self):
        self.assertTrue(self.check(capabilities=self.capabilities(s4=0), action="shutdown")["available"])

    def test_query_failure_and_non_windows_fail_closed(self):
        with patch.object(readiness.os, "name", "nt"), \
                patch.object(readiness, "_read_capabilities", side_effect=OSError("test")):
            self.assertFalse(readiness.windows_power_readiness()["available"])
        with patch.object(readiness.os, "name", "posix"), patch.object(readiness, "_read_capabilities") as query:
            self.assertFalse(readiness.windows_power_readiness()["available"])
            query.assert_not_called()

    def test_getpwr_queries_output_buffer_and_rejects_failure(self):
        api = Mock()
        def read(pointer):
            value = ctypes.cast(pointer, ctypes.POINTER(readiness.SYSTEM_POWER_CAPABILITIES)).contents
            value.SystemS4, value.HiberFilePresent, value.HiberFileType = 1, 1, 2
            return 1
        api.GetPwrCapabilities.side_effect = read
        with patch.object(readiness, "_power_api", return_value=api):
            self.assertEqual(readiness._read_capabilities().HiberFileType, 2)
            api.GetPwrCapabilities.side_effect = None
            api.GetPwrCapabilities.return_value = 0
            with self.assertRaises(OSError):
                readiness._read_capabilities()


class ShutdownPrivilegeTests(unittest.TestCase):
    def apis(self, *, entries=((19, 0, 0),), returned_size=None, count=None, read_ok=True):
        kernel, security = Mock(), Mock()
        kernel.GetCurrentProcess.return_value = 42
        def lookup(system, name, pointer):
            luid = ctypes.cast(pointer, ctypes.POINTER(readiness.LUID)).contents
            luid.LowPart, luid.HighPart = 19, 0
            return 1
        def open_token(process, access, pointer):
            ctypes.cast(pointer, ctypes.POINTER(readiness.HANDLE)).contents.value = 17
            return 1
        def information(token, kind, buffer, length, needed):
            size = 4 + len(entries) * ctypes.sizeof(readiness.LUID_AND_ATTRIBUTES)
            ctypes.cast(needed, ctypes.POINTER(readiness.DWORD)).contents.value = size if returned_size is None else returned_size
            if buffer is None:
                return 0
            if not read_ok:
                return 0
            readiness.DWORD.from_buffer(buffer).value = len(entries) if count is None else count
            for index, (low, high, attributes) in enumerate(entries):
                entry = readiness.LUID_AND_ATTRIBUTES.from_buffer(buffer, 4 + index * 12)
                entry.Luid.LowPart, entry.Luid.HighPart, entry.Attributes = low, high, attributes
            return 1
        security.LookupPrivilegeValueW.side_effect = lookup
        security.OpenProcessToken.side_effect = open_token
        security.GetTokenInformation.side_effect = information
        return kernel, security

    def test_present_disabled_and_enabled_privilege_are_accepted_without_mutation(self):
        for attributes in (0, 1, 2, 3):
            with self.subTest(attributes=attributes):
                kernel, security = self.apis(entries=((5, 0, 2), (19, 0, attributes)))
                with patch.object(readiness, "_token_apis", return_value=(kernel, security)):
                    self.assertTrue(readiness._has_shutdown_privilege())
                self.assertEqual(security.OpenProcessToken.call_args.args[:2], (42, 0x0008))
                security.LookupPrivilegeValueW.assert_called_once()
                self.assertEqual(security.LookupPrivilegeValueW.call_args.args[1], "SeShutdownPrivilege")
                self.assertTrue(all(call.args[1] == 3 for call in security.GetTokenInformation.call_args_list))
                security.AdjustTokenPrivileges.assert_not_called()
                kernel.CloseHandle.assert_called_once()

    def test_absent_removed_or_wrong_luid_is_rejected(self):
        for entries in ((), ((19, 0, 4),), ((18, 0, 2),), ((19, 1, 2),)):
            with self.subTest(entries=entries):
                kernel, security = self.apis(entries=entries)
                with patch.object(readiness, "_token_apis", return_value=(kernel, security)):
                    self.assertFalse(readiness._has_shutdown_privilege())
                kernel.CloseHandle.assert_called_once()

    def test_invalid_token_data_raises_and_always_closes_handle(self):
        for options in ({"returned_size": 0}, {"returned_size": 65537}, {"count": 200}, {"read_ok": False}):
            with self.subTest(options=options):
                kernel, security = self.apis(**options)
                with patch.object(readiness, "_token_apis", return_value=(kernel, security)):
                    with self.assertRaises(OSError):
                        readiness._has_shutdown_privilege()
                kernel.CloseHandle.assert_called_once()


if __name__ == "__main__":
    unittest.main()
