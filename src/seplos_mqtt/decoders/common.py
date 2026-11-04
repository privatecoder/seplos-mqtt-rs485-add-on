"""Small helpers shared by the decoders."""
from __future__ import annotations

KELVIN_OFFSET = 2731  # temperatures travel as 0.1 K


def u16(raw: bytes, offset: int) -> int:
    return int.from_bytes(raw[offset:offset + 2], "big")


def s16(raw: bytes, offset: int) -> int:
    return int.from_bytes(raw[offset:offset + 2], "big", signed=True)


def signed16(value: int) -> int:
    return value - 0x10000 if value & 0x8000 else value


def celsius(raw_kelvin_tenths: int) -> float:
    return round((raw_kelvin_tenths - KELVIN_OFFSET) / 10, 1)


def on_off(value: int | bool) -> str:
    return "ON" if value else "OFF"


def text(raw: bytes) -> str:
    """Fixed-width ASCII field, padded with NUL or spaces."""
    return raw.decode("ascii", errors="replace").strip("\x00 ")
