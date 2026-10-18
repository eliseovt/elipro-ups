"""Windows IP Helper ABI and malformed-buffer cases with no real API or sockets."""
import ctypes
from dataclasses import FrozenInstanceError
import socket
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ups_monitor import windows_udp as udp


def payload(family, rows=()):
    if family == udp.AF_INET:
        encoded = [struct.pack("<III", address, socket.htons(port), pid) for address, port, pid in rows]
    else:
        encoded = [address + struct.pack("<III", 0, socket.htons(port), pid) for address, port, pid in rows]
    return struct.pack("<I", len(encoded)) + b"".join(encoded)


class FakeApi:
    def __init__(self, tables=None):
        self.tables = tables or {udp.AF_INET: payload(udp.AF_INET), udp.AF_INET6: payload(udp.AF_INET6)}
        self.calls = []

    def __call__(self, buffer, size_pointer, order, family, table_class, reserved):
        self.calls.append((buffer is None, family, order, table_class, reserved))
        size = ctypes.cast(size_pointer, ctypes.POINTER(ctypes.c_uint32)).contents
        data = self.tables[family]
        if buffer is None or size.value < len(data):
            size.value = len(data)
            return 122
        ctypes.memmove(buffer, data, len(data))
        size.value = len(data)
        return 0


class WindowsUdpTests(unittest.TestCase):
    def occupancy(self, function, port=51222):
        with patch.object(udp, "_load_udp_api", return_value=function), \
             patch.object(udp.socket, "socket", side_effect=AssertionError("No UDP sockets allowed")):
            return udp.udp_port_occupancy(port)

    def test_valid_empty_tables_both_families_confirm_absence_without_sockets(self):
        function = FakeApi()
        result = self.occupancy(function)
        self.assertEqual((result.state, result.reason), ("absent", udp.UdpPortReason.NO_ENDPOINT))
        self.assertEqual([item[1] for item in function.calls], [2, 2, 23, 23])
        self.assertTrue(all(item[2:] == (False, 1, 0) for item in function.calls))
        with self.assertRaises(FrozenInstanceError):
            result.state = "occupied"

    def test_either_family_any_address_or_pid_zero_blocks_port(self):
        for family, address in ((2, 0), (2, 0x0100007f), (2, 0x0101a8c0),
                                (23, bytes(16)), (23, bytes(15) + b"\x01")):
            with self.subTest(family=family, address=address):
                tables = {2: payload(2), 23: payload(23)}
                tables[family] = payload(family, [(address, 51222, 0)])
                result = self.occupancy(FakeApi(tables))
                self.assertEqual((result.state, result.reason, result.pids),
                                 ("occupied", udp.UdpPortReason.ENDPOINT_BOUND, (0,)))

    def test_port_byte_order_unrelated_endpoints_and_pid_bound(self):
        rows = [(0, 51223, 999), (0, 22, 888)] + [(0, 51222, pid) for pid in range(40)]
        result = self.occupancy(FakeApi({2: payload(2, rows), 23: payload(23)}))
        self.assertEqual(result.state, "occupied")
        self.assertEqual(result.pids, tuple(range(udp.MAX_REPORTED_PIDS)))
        self.assertNotIn(999, result.pids)
        self.assertEqual(self.occupancy(FakeApi({2: payload(2, [(0, 51223, 3)]), 23: payload(23)})).state, "absent")

    def test_successful_growth_retry_is_bounded_and_parses_new_buffer(self):
        stable = FakeApi({2: payload(2, [(0, 51222, 42)]), 23: payload(23)})
        calls = 0
        def growing(buffer, pointer, order, family, table_class, reserved):
            nonlocal calls
            if family == 2:
                calls += 1
                size = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32)).contents
                if calls == 1:
                    size.value = 4
                    return 122
                if calls == 2:
                    size.value = 16
                    return 122
            return stable(buffer, pointer, order, family, table_class, reserved)
        self.assertEqual(self.occupancy(growing).pids, (42,))
        self.assertEqual(calls, 3)

    def test_repeated_growth_cannot_loop_forever(self):
        calls = []
        def unstable(buffer, pointer, *args):
            calls.append(buffer is None)
            size = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32)).contents
            size.value += 16
            return 122
        result = self.occupancy(unstable)
        self.assertEqual((result.state, result.reason), ("unknown", udp.UdpPortReason.TABLE_UNSTABLE))
        self.assertEqual(len(calls), 1 + udp.MAX_TABLE_ATTEMPTS)

    def test_invalid_size_header_and_count_never_become_absence(self):
        for invalid in ("size_zero", "size_short", "size_huge", "used_zero", "used_exceeds_buffer", "count_exceeds_buffer", "null_success"):
            with self.subTest(invalid=invalid):
                def broken(buffer, pointer, *args):
                    size = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32)).contents
                    if buffer is None:
                        size.value = {"size_zero": 0, "size_short": 3, "size_huge": udp.MAX_TABLE_BYTES + 1}.get(invalid, 16)
                        return 0 if invalid == "null_success" else 122
                    if invalid == "used_zero":
                        size.value = 0
                    elif invalid == "used_exceeds_buffer":
                        size.value = 17
                    else:
                        ctypes.memmove(buffer, struct.pack("<I", 2), 4)
                    return 0
                result = self.occupancy(broken)
                self.assertEqual(result.state, "unknown")
                self.assertIn(result.reason, (udp.UdpPortReason.TABLE_LIMIT, udp.UdpPortReason.INVALID_TABLE))

    def test_one_successful_family_cannot_override_other_family_api_failure(self):
        for occupied in (False, True):
            function = FakeApi({2: payload(2, [(0, 51222, 42)] if occupied else []), 23: payload(23)})
            def failing(buffer, pointer, order, family, table_class, reserved):
                if family == 23:
                    return 5
                return function(buffer, pointer, order, family, table_class, reserved)
            result = self.occupancy(failing)
            self.assertEqual((result.state, result.reason, result.error_code), ("unknown", udp.UdpPortReason.API_ERROR, 5))
            self.assertEqual(result.family, udp.AF_INET6)

    def test_loader_and_api_exceptions_are_uncertain(self):
        with patch.object(udp, "_load_udp_api", side_effect=OSError("private path must not escape")):
            result = udp.udp_port_occupancy()
        self.assertEqual((result.state, result.reason), ("unknown", udp.UdpPortReason.API_UNAVAILABLE))
        self.assertNotIn("private", repr(result))
        for error in (OSError("failed"), ValueError("bad table"), ctypes.ArgumentError("bad pointer"), MemoryError()):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(self.occupancy(Mock(side_effect=error)).state, "unknown")

    def test_ctypes_loader_signature_is_fixed_and_cached_without_result_cache(self):
        udp._load_udp_api.cache_clear()
        self.addCleanup(udp._load_udp_api.cache_clear)
        function = Mock()
        with patch.object(udp.os, "name", "nt"), \
             patch.object(udp.ctypes, "WinDLL", return_value=SimpleNamespace(GetExtendedUdpTable=function), create=True) as load:
            self.assertIs(udp._load_udp_api(), function)
            self.assertIs(udp._load_udp_api(), function)
        load.assert_called_once_with("iphlpapi", use_last_error=True)
        self.assertEqual(function.restype, ctypes.c_uint32)
        self.assertEqual(function.argtypes, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_int32,
                                            ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32])
        self.assertEqual(ctypes.sizeof(udp._IPv4Row), 12)
        self.assertEqual(ctypes.sizeof(udp._IPv6Row), 28)
        self.assertEqual(udp._IPv4Table.rows.offset, 4)
        self.assertEqual(udp._IPv6Table.rows.offset, 4)

    def test_invalid_port_is_rejected_before_any_api_access(self):
        for port in (True, 0, -1, 65536, "51222", None):
            with self.subTest(port=port), patch.object(udp, "_load_udp_api") as load:
                with self.assertRaises(ValueError):
                    udp.udp_port_occupancy(port)
                load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
