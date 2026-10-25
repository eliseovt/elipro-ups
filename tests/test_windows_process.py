"""Browser UDP exception with fake process tables only; no Win32 or USB I/O."""
import ctypes
import unittest
from unittest.mock import Mock, patch

from ups_monitor import windows_process as process
from ups_monitor.native_adapter import ForzaProbeReason, probe_forza_monitor
from ups_monitor.windows_udp import UdpPortReason, UdpPortResult


CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OWNERS = (11300,)
OCCUPIED = UdpPortResult("occupied", UdpPortReason.ENDPOINT_BOUND, pids=OWNERS)
ABSENT = UdpPortResult("absent", UdpPortReason.NO_ENDPOINT)


class FakeKernel:
    def __init__(self, *, images=None, names=("System", "chrome.exe"), failure=None, exit_code=259):
        self.images = images or {11300: CHROME}
        self.names, self.failure, self.exit_code = names, failure, exit_code
        self.closed, self.opened, self.index = [], [], 0
        self.error = 18

    def OpenProcess(self, access, inherit, pid):
        if self.failure == "open":
            return 0
        self.opened.append((access, inherit, pid))
        return pid + 1000

    def QueryFullProcessImageNameW(self, handle, flags, image, length):
        if self.failure == "image":
            return 0
        image.value = self.images[handle - 1000]
        ctypes.cast(length, ctypes.POINTER(process.DWORD)).contents.value = len(image.value)
        return 1

    def GetExitCodeProcess(self, handle, result):
        if self.failure == "exit":
            return 0
        ctypes.cast(result, ctypes.POINTER(process.DWORD)).contents.value = self.exit_code
        return 1

    def CreateToolhelp32Snapshot(self, flags, pid):
        return ctypes.c_void_p(-1).value if self.failure == "snapshot" else 900

    def _entry(self, pointer):
        entry = ctypes.cast(pointer, ctypes.POINTER(process.PROCESSENTRY32W)).contents
        name = self.names[self.index]
        for index in range(260):
            entry.image[index] = ord(name[index]) if index < len(name) else 0

    def Process32FirstW(self, snapshot, pointer):
        if self.failure == "first" or not self.names:
            return 0
        self.index = 0
        self._entry(pointer)
        return 1

    def Process32NextW(self, snapshot, pointer):
        if self.failure == "next":
            self.error = 5
            return 0
        self.index += 1
        if self.index == len(self.names):
            return 0
        self._entry(pointer)
        return 1

    def GetLastError(self):
        return self.error

    def CloseHandle(self, handle):
        self.closed.append(handle)
        if self.failure == "close_error":
            raise OSError("Close failed")
        return 0 if self.failure == "close" else 1


class BrowserOwnerTests(unittest.TestCase):
    def confirm(self, kernel, current=OCCUPIED, pids=OWNERS):
        def recheck():
            # Every original owner handle must stay open during the fresh table.
            self.assertFalse(any(pid + 1000 in kernel.closed for pid in pids))
            return current
        with patch.object(process, "_apis", return_value=kernel), \
                patch.dict(process.os.environ, {"ProgramFiles": r"C:\Program Files",
                    "ProgramW6432": r"C:\Program Files", "ProgramFiles(x86)": r"C:\Program Files (x86)"}):
            return process.browser_only_owners(pids, recheck)

    def test_exact_installed_browser_paths_allow_with_live_handles_and_complete_snapshot(self):
        for image in (CHROME, CHROME.upper(), r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
            for current in (OCCUPIED, ABSENT):
                with self.subTest(image=image, state=current.state):
                    kernel = FakeKernel(images={11300: image})
                    self.assertTrue(self.confirm(kernel, current))
                    self.assertEqual(kernel.opened, [(0x1000, False, 11300)])
                    self.assertEqual(kernel.closed, [900, 12300])

    def test_unknown_or_lookalike_owner_is_never_exempted(self):
        for image in (r"C:\Temp\chrome.exe", r"C:\Program Files\Other\chrome.exe",
                      CHROME + ".exe", r"C:\Forza\upsMonitor.exe", r"C:\Forza\openJDK\bin\javaw.exe"):
            with self.subTest(image=image):
                kernel = FakeKernel(images={11300: image})
                self.assertFalse(self.confirm(kernel))
                self.assertEqual(kernel.closed, [12300])

    def test_any_possible_oem_process_keeps_block_even_when_only_chrome_owns_port(self):
        for name in process.OEM_PROCESS_NAMES:
            with self.subTest(name=name):
                kernel = FakeKernel(names=("System", name.upper(), "chrome.exe"))
                self.assertFalse(self.confirm(kernel))
                self.assertEqual(kernel.closed, [900, 12300])

    def test_process_exit_or_api_failure_never_confirms_absence_and_closes_resources(self):
        for failure in ("open", "image", "exit", "snapshot", "first", "next", "close", "close_error"):
            with self.subTest(failure=failure):
                kernel = FakeKernel(failure=failure)
                self.assertFalse(self.confirm(kernel))
                if failure != "open":
                    self.assertIn(12300, kernel.closed)
        self.assertFalse(self.confirm(FakeKernel(exit_code=0)))
        self.assertFalse(self.confirm(FakeKernel(names=())))

    def test_missing_unknown_truncated_or_repeated_pids_do_not_query_processes(self):
        for pids in ((), (0,), (True,), (-1,), (11300, 11300), tuple(range(1, 17)), [11300]):
            with self.subTest(pids=pids), patch.object(process, "_apis") as api:
                self.assertFalse(process.browser_only_owners(pids, Mock()))
                api.assert_not_called()

    def test_changed_owners_or_incomplete_new_udp_table_remain_blocked(self):
        for current in (UdpPortResult("occupied", UdpPortReason.ENDPOINT_BOUND, pids=(42,)),
                        UdpPortResult("occupied", UdpPortReason.ENDPOINT_BOUND, pids=(42, 11300)),
                        UdpPortResult("unknown", UdpPortReason.API_ERROR)):
            with self.subTest(current=current):
                self.assertFalse(self.confirm(FakeKernel(), current))

    def test_every_owner_must_be_a_browser_and_live(self):
        pids = (11300, 11301)
        current = UdpPortResult("occupied", UdpPortReason.ENDPOINT_BOUND, pids=pids)
        for other, allowed in ((CHROME, True), (r"C:\Temp\unknown.exe", False)):
            with self.subTest(other=other):
                kernel = FakeKernel(images={11300: CHROME, 11301: other})
                self.assertEqual(self.confirm(kernel, current, pids), allowed)
                self.assertIn(12300, kernel.closed)
                self.assertIn(12301, kernel.closed)

    def test_unavailable_process_api_keeps_port_uncertain(self):
        with patch.object(process, "_apis", side_effect=OSError("unavailable")):
            self.assertFalse(process.browser_only_owners(OWNERS, Mock()))


class BrowserProbeTests(unittest.TestCase):
    def test_confirmed_browser_exception_preserves_bounded_diagnostic_evidence(self):
        reply = Mock(state="unknown", reason=ForzaProbeReason.TIMEOUT, winerror=None, errno=None)
        with patch("ups_monitor.native_adapter.udp_port_occupancy", return_value=OCCUPIED) as table, \
                patch("ups_monitor.native_adapter._probe_forza_reply", return_value=reply), \
                patch("ups_monitor.native_adapter.browser_only_owners", return_value=True) as browser:
            result = probe_forza_monitor()
        browser.assert_called_once_with(OWNERS, table)
        self.assertEqual((result.state, result.reason, result.pids, result.reply_reason),
            ("absent", ForzaProbeReason.ENDPOINT_BROWSER_ONLY, OWNERS, ForzaProbeReason.TIMEOUT))

    def test_positive_oem_reply_cannot_be_overridden_by_browser_exception(self):
        from ups_monitor.native_adapter import ForzaProbeResult
        reply = ForzaProbeResult("active", ForzaProbeReason.RECOGNIZED_REPLY)
        with patch("ups_monitor.native_adapter.udp_port_occupancy", return_value=OCCUPIED), \
                patch("ups_monitor.native_adapter._probe_forza_reply", return_value=reply), \
                patch("ups_monitor.native_adapter.browser_only_owners") as browser:
            self.assertEqual(probe_forza_monitor().state, "active")
        browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
