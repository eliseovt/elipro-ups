"""Read-only Windows UDP ownership snapshots; never opens a socket or USB.

GetExtendedUdpTable returns bound endpoints, not an application identity. Any
binding of the reserved collector port is conservative evidence to block USB.
Only two complete, valid IPv4/IPv6 snapshots can establish absence.

Microsoft API and layouts:
https://learn.microsoft.com/en-us/windows/win32/api/iphlpapi/nf-iphlpapi-getextendedudptable
https://learn.microsoft.com/en-us/windows/win32/api/udpmib/ns-udpmib-mib_udprow_owner_pid
https://learn.microsoft.com/en-us/windows/win32/api/udpmib/ns-udpmib-mib_udp6row_owner_pid
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
import os
import socket


AF_INET = 2
AF_INET6 = 23
UDP_TABLE_OWNER_PID = 1
ERROR_INSUFFICIENT_BUFFER = 122
MAX_TABLE_BYTES = 1024 * 1024
MAX_TABLE_ATTEMPTS = 3
MAX_REPORTED_PIDS = 16


class UdpPortReason(str, Enum):
    NO_ENDPOINT = "no_endpoint"
    ENDPOINT_BOUND = "endpoint_bound"
    API_UNAVAILABLE = "api_unavailable"
    API_ERROR = "api_error"
    INVALID_TABLE = "invalid_table"
    TABLE_LIMIT = "table_limit"
    TABLE_UNSTABLE = "table_unstable"


@dataclass(frozen=True)
class UdpPortResult:
    state: str
    reason: UdpPortReason
    pids: tuple[int, ...] = ()
    error_code: int | None = None
    family: int | None = None


class _IPv4Row(ctypes.Structure):
    _fields_ = [("address", ctypes.c_uint32), ("port", ctypes.c_uint32), ("pid", ctypes.c_uint32)]


class _IPv6Row(ctypes.Structure):
    _fields_ = [("address", ctypes.c_ubyte * 16), ("scope", ctypes.c_uint32),
                ("port", ctypes.c_uint32), ("pid", ctypes.c_uint32)]


class _IPv4Table(ctypes.Structure):
    _fields_ = [("count", ctypes.c_uint32), ("rows", _IPv4Row * 1)]


class _IPv6Table(ctypes.Structure):
    _fields_ = [("count", ctypes.c_uint32), ("rows", _IPv6Row * 1)]


class _TableError(Exception):
    def __init__(self, reason, code=None):
        self.reason, self.code = reason, code


@lru_cache(maxsize=1)
def _load_udp_api():
    if os.name != "nt":
        raise OSError("Windows UDP ownership is unavailable")
    function = ctypes.WinDLL("iphlpapi", use_last_error=True).GetExtendedUdpTable
    function.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_int32,
                         ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    function.restype = ctypes.c_uint32
    return function


def _family_pids(function, family, port):
    size = ctypes.c_uint32(0)
    result = function(None, ctypes.byref(size), False, family, UDP_TABLE_OWNER_PID, 0)
    if result != ERROR_INSUFFICIENT_BUFFER:
        raise _TableError(UdpPortReason.INVALID_TABLE if result == 0 else UdpPortReason.API_ERROR,
                          None if result == 0 else result)
    for _ in range(MAX_TABLE_ATTEMPTS):
        capacity = size.value
        if not 4 <= capacity <= MAX_TABLE_BYTES:
            raise _TableError(UdpPortReason.TABLE_LIMIT)
        buffer = ctypes.create_string_buffer(capacity)
        result = function(buffer, ctypes.byref(size), False, family, UDP_TABLE_OWNER_PID, 0)
        if result == ERROR_INSUFFICIENT_BUFFER:
            continue
        if result != 0:
            raise _TableError(UdpPortReason.API_ERROR, result)
        if not 4 <= size.value <= capacity:
            raise _TableError(UdpPortReason.INVALID_TABLE)
        row_type, table_type = (_IPv4Row, _IPv4Table) if family == AF_INET else (_IPv6Row, _IPv6Table)
        count = ctypes.c_uint32.from_buffer_copy(buffer).value
        offset, stride = table_type.rows.offset, ctypes.sizeof(row_type)
        if offset > size.value or count > (size.value - offset) // stride:
            raise _TableError(UdpPortReason.INVALID_TABLE)
        pids = set()
        for index in range(count):
            row = row_type.from_buffer_copy(buffer, offset + index * stride)
            if socket.ntohs(row.port & 0xffff) == port and len(pids) < MAX_REPORTED_PIDS:
                pids.add(row.pid)
        return pids
    raise _TableError(UdpPortReason.TABLE_UNSTABLE)


def udp_port_occupancy(port=51222):
    """Return occupied/absent/unknown, with bounded numeric diagnostics only.

    There is no negative-result cache. API counts/allocations are bounded; a
    synchronous Windows API call itself has no cancellable timeout parameter.
    """
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port debe ser un entero entre 1 y 65535")
    try:
        function = _load_udp_api()
    except (OSError, AttributeError) as exc:
        return UdpPortResult("unknown", UdpPortReason.API_UNAVAILABLE,
                             error_code=getattr(exc, "winerror", None))
    family = None
    try:
        pids = set()
        for family in (AF_INET, AF_INET6):
            pids.update(_family_pids(function, family, port))
    except _TableError as exc:
        return UdpPortResult("unknown", exc.reason, error_code=exc.code, family=family)
    except (OSError, ValueError, TypeError, ctypes.ArgumentError) as exc:
        return UdpPortResult("unknown", UdpPortReason.API_ERROR,
                             error_code=getattr(exc, "winerror", None), family=family)
    except MemoryError:
        return UdpPortResult("unknown", UdpPortReason.TABLE_LIMIT, family=family)
    if pids:
        return UdpPortResult("occupied", UdpPortReason.ENDPOINT_BOUND,
                             pids=tuple(sorted(pids))[:MAX_REPORTED_PIDS])
    return UdpPortResult("absent", UdpPortReason.NO_ENDPOINT)
