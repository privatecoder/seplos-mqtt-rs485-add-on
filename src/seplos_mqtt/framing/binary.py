"""
Binary frames of the EMU1101-EXT firmware on UART1 (RS485 pair in the CAN socket):

    55 ADR CMD LEN DATA[LEN] CRC_HI CRC_LO AA

CRC-16/XMODEM (poly 0x1021, init 0) over ADR..DATA, big endian. The payload may contain 55/AA/CR,
so frames are read by LEN, never by delimiter. A rejection replaces CMD by an error code with LEN 0.
"""
from __future__ import annotations

import binascii

SOF = 0x55
EOF = 0xAA
OVERHEAD = 7
MAX_DATA = 223

ERRORS = {
    0x81: "unknown or unsupported command",
    0x83: "incorrect parameter section markers",
    0x87: "wrong DATA length for command",
    0xE2: "parameter validation rejected the block",
    0xE3: "UART1 write denied or protected Group0 bit changed",
}


class BinaryFrameError(ValueError):
    pass


class BinaryRejected(BinaryFrameError):
    def __init__(self, code: int) -> None:
        super().__init__(f"{ERRORS.get(code, 'rejected')} (0x{code:02X})")
        self.code = code


def crc(body: bytes) -> int:
    return binascii.crc_hqx(body, 0)


def encode(address: int, command: int, data: bytes = b"") -> bytes:
    if not 0 <= address <= 21 or len(data) > MAX_DATA:
        raise ValueError("invalid address or data length")
    body = bytes([address, command, len(data)]) + data
    return bytes([SOF]) + body + crc(body).to_bytes(2, "big") + bytes([EOF])


def parse(raw: bytes, address: int, command: int) -> bytes:
    """Validate a complete answer; returns DATA or raises (BinaryRejected for a valid rejection)."""
    if len(raw) < OVERHEAD or raw[0] != SOF or raw[-1] != EOF or len(raw) != raw[3] + OVERHEAD:
        raise BinaryFrameError("invalid frame markers/length")
    if crc(raw[1:-3]) != int.from_bytes(raw[-3:-1], "big"):
        raise BinaryFrameError("CRC mismatch")
    if raw[1] != address:
        raise BinaryFrameError(f"answer from address {raw[1]}")
    if raw[2] != command:
        if raw[2] in ERRORS and raw[3] == 0:
            raise BinaryRejected(raw[2])
        raise BinaryFrameError(f"unexpected command 0x{raw[2]:02X}")
    return bytes(raw[4:-3])
