"""
Seplos ASCII frames ("SEPLOS BMS Communication Protocol V2.0"):

    ~ VER ADR CID1 CID2/RTN LENGTH INFO CHKSUM CR

All fields are hex ASCII. LENGTH = 4-bit check nibble + 12-bit length of the INFO in ASCII
characters, CHKSUM = two's complement of the sum of all characters between ~ and CHKSUM.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("SeplosBMS.framing")

SOI = b"~"
EOI = b"\r"
VER = 0x20
CID1 = 0x46
# SOI (1) + VER/ADR/CID1/RTN/LENGTH (12) + CHKSUM (4) + EOI (1)
OVERHEAD = 18
CID2_INTRA_PACK = 0x5A


def checksum(body: bytes) -> int:
    """Two's complement of the character sum, modulo 65536."""
    return (-sum(body)) & 0xFFFF


def length_field(info_ascii_len: int) -> int:
    """LENGTH field for an INFO of the given number of ASCII characters."""
    if info_ascii_len == 0:
        return 0
    lchksum = (info_ascii_len & 0xF) + ((info_ascii_len >> 4) & 0xF) + ((info_ascii_len >> 8) & 0xF)
    lchksum %= 16
    lchksum ^= 0xF
    lchksum += 1
    return (lchksum << 12) + info_ascii_len


def encode(address: int, cid2: int, info: bytes = b"01") -> bytes:
    """Encode a request; `info` is already hex ASCII."""
    body = f"{VER:02X}{address:02X}{CID1:02X}{cid2:02X}{length_field(len(info)):04X}".encode() + info
    return SOI + body + f"{checksum(body):04X}".encode() + EOI


def hex_byte(data: bytes, offset: int) -> int:
    return int(data[offset:offset + 2].decode("ascii"), 16)


def hex_word(data: bytes, offset: int) -> int:
    return int(data[offset:offset + 4].decode("ascii"), 16)


def is_hex(data: bytes) -> bool:
    try:
        bytes.fromhex(data.decode("ascii"))
        return True
    except (ValueError, UnicodeDecodeError):
        return False


def checksum_ok(raw: bytes) -> bool:
    """Character checksum of a received frame (raw incl. ~ and CR)."""
    try:
        return checksum(raw[1:-5]) == hex_word(raw, -5)
    except (ValueError, UnicodeDecodeError):
        return False


def address_of(raw: bytes) -> int | None:
    try:
        return hex_byte(raw, 3)
    except (ValueError, UnicodeDecodeError):
        return None


def rtn_of(raw: bytes) -> int | None:
    """RTN (or the CID2 0x5A echo of intra-pack answers)."""
    try:
        return hex_byte(raw, 7)
    except (ValueError, UnicodeDecodeError):
        return None


def info_ascii(raw: bytes) -> bytes:
    return raw[13:-5]


def validate_response(raw: bytes, address: int, info_len: int) -> bytes | None:
    """
    Validate a normal response (RTN 00) from `address` with exactly `info_len` INFO characters.
    Returns the binary INFO or None.
    """
    if len(raw) < info_len + OVERHEAD:
        logger.debug("Frame too short: %s", len(raw))
        return None
    adr = raw[3:5]
    info = info_ascii(raw)
    if not is_hex(adr) or hex_byte(adr, 0) != address:
        logger.debug("Frame address mismatch: %s", adr)
        return None
    if len(info) != info_len:
        logger.debug("Frame length mismatch - expected: %s, got: %s", info_len, len(info))
        return None
    if not is_hex(info):
        logger.debug("Frame includes non-hexadecimal characters: %s", info)
        return None
    if not checksum_ok(raw):
        logger.debug("Frame checksum mismatch")
        return None
    if raw[7:9] != b"00":
        logger.debug("Frame error flag (RTN) set: %s", raw[7:9])
        return None
    return bytes.fromhex(info.decode("ascii"))


def is_intra_pack(raw: bytes) -> bool:
    """Intra-pack traffic of the master polling its slaves (CID2 0x5A on RS485-1/2)."""
    return len(raw) >= OVERHEAD and raw[:1] == SOI and raw[7:9] == b"5A"


def validate_intra_pack_answer(raw: bytes, address: int) -> bytes | None:
    """A pack's own intra-pack record: 32 byte INFO, CID2 0x5A instead of a RTN."""
    try:
        if (
            len(raw) == 64 + OVERHEAD
            and raw[:1] == SOI
            and raw[7:13] == b"5AC040"
            and hex_byte(raw, 3) == address
            and checksum_ok(raw)
        ):
            return bytes.fromhex(info_ascii(raw).decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        pass
    return None
