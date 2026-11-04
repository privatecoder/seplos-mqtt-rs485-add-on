"""Modbus RTU (served by the master pack on the RS485 bus of the CAN port, slave ID 1)."""
from __future__ import annotations

from enum import Enum

SLAVE_ID = 0x01


class Status(Enum):
    OK = "ok"
    EXCEPTION = "exception"


def crc16(data: bytes) -> int:
    """Modbus CRC16 (poly 0xA001, init 0xFFFF)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def encode_read(function_code: int, register: int, count: int, slave: int = SLAVE_ID) -> bytes:
    request = bytes([slave, function_code, register >> 8, register & 0xFF, 0x00, count])
    return request + crc16(request).to_bytes(2, "little")


def response_length(count: int) -> int:
    return 5 + 2 * count


def parse_read(response: bytes, function_code: int, count: int, slave: int = SLAVE_ID) -> tuple[Status | None, list[int] | None]:
    """(OK, registers), (EXCEPTION, None) or (None, None) for no/invalid answer."""
    if len(response) < 5 or response[0] != slave:
        return None, None
    if response[1] == function_code | 0x80:
        frame = response[:5]
    elif response[1] == function_code and len(response) == response_length(count) and response[2] == 2 * count:
        frame = response
    else:
        return None, None
    if crc16(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
        return None, None
    if response[1] != function_code:
        return Status.EXCEPTION, None
    data = frame[3:-2]
    return Status.OK, [int.from_bytes(data[i:i + 2], "big") for i in range(0, len(data), 2)]


def parse_single_wide_count(response: bytes, function_code: int, slave: int = SLAVE_ID) -> tuple[Status | None, int | None]:
    """
    The Seplos handlers of 0x0033/0x0070(x1)/0x0071-0x0074 answer one register with a non-standard
    16-bit byte count: slave FC 00 02 HH LL CRC CRC (8 bytes).
    """
    if len(response) >= 5 and response[0] == slave and response[1] == function_code | 0x80:
        frame = response[:5]
        if crc16(frame[:-2]) == int.from_bytes(frame[-2:], "little"):
            return Status.EXCEPTION, None
        return None, None
    if len(response) != 8 or response[0] != slave or response[1] != function_code or response[2:4] != b"\x00\x02":
        return None, None
    if crc16(response[:-2]) != int.from_bytes(response[-2:], "little"):
        return None, None
    return Status.OK, int.from_bytes(response[4:6], "big")
