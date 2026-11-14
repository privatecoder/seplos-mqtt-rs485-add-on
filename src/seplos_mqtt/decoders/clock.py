"""CID2 0x4D: BMS real time clock (OEM on RS485-1/2, with EMU1101-EXT also on UART1)."""
from __future__ import annotations

from datetime import datetime

CID2 = 0x4D
LEN = 7


def decode(info: bytes) -> datetime:
    """7 bytes: year u16 BE, month, day, hour, minute, second. No time zone (local time by convention)."""
    return datetime(int.from_bytes(info[0:2], "big"), info[2], info[3], info[4], info[5], info[6])
