"""Original, strict decoder for the observed binary QS/T telemetry protocol.

This module contains no USB operations and cannot send UPS control commands.
The field layout and arithmetic are documented by the upstream NUT sources:
https://github.com/networkupstools/nut/blob/master/drivers/nutdrv_qx_voltronic-qs-hex.c
https://github.com/networkupstools/nut/blob/master/drivers/nutdrv_qx_blazer-common.c
The implementation here is independently written from that protocol description.
Only the 11-field T variant observed in this UPS's existing logs is accepted.
"""
from __future__ import annotations

from datetime import datetime, timezone


class QsProtocolError(ValueError):
    """A response cannot be trusted as a complete QS/T measurement."""


_FIELD_WIDTHS = (2, 1, 2, 1, 1, 2, 3, 1, 1, 1, 1)
_ESCAPES = {0: 0x0D, 1: 0x11, 2: 0x13, 3: 0x0A, 4: 0x20}
MAX_FRAME_BYTES = 64


def _field_bytes(encoded: bytes) -> bytes:
    decoded = bytearray()
    index = 0
    while index < len(encoded):
        value = encoded[index]
        if value == 0x28 and index + 1 < len(encoded) and encoded[index + 1] in _ESCAPES:
            index += 1
            value = _ESCAPES[encoded[index]]
        elif value in (0x0D, 0x0A, 0x11, 0x13):
            raise QsProtocolError("La respuesta QS contiene un byte reservado sin escape.")
        decoded.append(value)
        index += 1
    return bytes(decoded)


def _bounded(name: str, value: float, lower: float, upper: float) -> float:
    if not lower <= value <= upper:
        raise QsProtocolError(f"{name} fuera del rango físico admitido.")
    return value


def parse_qs(frame: bytes) -> dict:
    """Return a standard sample after validating one complete binary QS/T frame.

    The caller supplies bytes through the terminating CR, with transport padding
    removed. NUL inside a field is data. Spaces delimit encoded fields, before
    unescaping. A literal 0x28 is an escape only before a recognized escape code.
    The timestamp records reception/decoding time, not device uptime.
    Test, fault and UPS shutdown states retain metrics but inhibit OS protection.
    """
    received_at = datetime.now(timezone.utc).isoformat()
    if not isinstance(frame, bytes):
        raise QsProtocolError("La respuesta QS debe ser bytes.")
    if not 1 < len(frame) <= MAX_FRAME_BYTES or frame[:1] != b"#" or frame[-1:] != b"\r":
        raise QsProtocolError("Trama QS incompleta, demasiado grande o con delimitadores inválidos.")
    encoded_fields = frame[1:-1].split(b" ")
    if len(encoded_fields) != len(_FIELD_WIDTHS):
        raise QsProtocolError("La respuesta QS/T debe tener once campos.")
    fields = tuple(_field_bytes(field) for field in encoded_fields)
    if tuple(map(len, fields)) != _FIELD_WIDTHS:
        raise QsProtocolError("Anchuras de campos QS/T inválidas.")
    values = tuple(int.from_bytes(field, "big") for field in fields)
    input_raw, input_scale, output_raw, output_scale, load, divisor, dividend, battery_raw, battery_scale, flags, ratings = values
    if divisor == 0:
        raise QsProtocolError("Divisor de frecuencia QS igual a cero.")

    input_voltage = _bounded("Voltaje de entrada", (input_raw * input_scale // 51) / 256, 0, 400)
    output_voltage = _bounded("Voltaje de salida", (output_raw * output_scale // 51) / 256, 0, 400)
    frequency = _bounded("Frecuencia", dividend / divisor, 40, 70)
    battery_voltage = _bounded("Voltaje de batería", battery_raw * battery_scale / 510, 0, 100)
    if battery_voltage == 0:
        raise QsProtocolError("Voltaje de batería QS no verificable.")
    _bounded("Carga", load, 0, 200)

    utility_failed = bool(flags & 0x80)
    fault = bool(flags & 0x10)
    testing = bool(flags & 0x04)
    shutdown = bool(flags & 0x02)
    inhibited = fault or testing or shutdown
    if input_voltage == 0 and not (utility_failed or inhibited):
        raise QsProtocolError("Entrada nula incompatible con el estado de red QS.")
    if output_voltage == 0 and not inhibited:
        raise QsProtocolError("Salida nula incompatible con el estado operativo QS.")
    if fault:
        mode = "Fault mode"
    elif shutdown:
        mode = "Shutdown mode"
    elif testing:
        mode = "Battery test mode"
    elif utility_failed:
        mode = "Battery mode"
    elif flags & 0x20:
        mode = "AVR mode"
    else:
        mode = "Line mode"

    return {
        "timestamp": received_at,
        "input_voltage": round(input_voltage, 1),
        "output_voltage": round(output_voltage, 1),
        "frequency_hz": round(frequency, 1),
        "load_percent": load,
        "battery_percent": None,
        "battery_voltage": round(battery_voltage, 2),
        "runtime_seconds": None,
        "temperature_c": None,
        "on_battery": None if inhibited else utility_failed,
        "low_battery": bool(flags & 0x40),
        "fault": fault,
        "estimated_watts": None,
        "watts_is_estimate": False,
        "raw_status": mode,
        "raw_flags": {"status_hex": f"{flags:02X}", "ratings_hex": f"{ratings:02X}"},
    }
