"""
Seplos ASCII frames ("SEPLOS BMS Communication Protocol V2.0"):

    ~ VER ADR CID1 CID2/RTN LENGTH INFO CHKSUM CR

All fields are hex ASCII. LENGTH = 4-bit check nibble + 12-bit length of the INFO in ASCII
characters, CHKSUM = two's complement of the sum of all characters between ~ and CHKSUM.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger("SeplosBMS.framing")

SOI = b"~"
EOI = b"\r"
VER = 0x20
CID1 = 0x46
# SOI (1) + VER/ADR/CID1/RTN/LENGTH (12) + CHKSUM (4) + EOI (1)
OVERHEAD = 18
CID2_INTRA_PACK = 0x5A


class FrameError(ValueError):
    """A received frame that is not a well-formed Seplos ASCII frame."""


@dataclass(frozen=True)
class Frame:
    address: int
    code: int          # RTN of an answer (or CID2 0x5A of intra-pack traffic)
    info: bytes        # binary INFO
    raw: bytes


def checksum(body: bytes) -> int:
    """Two's complement of the character sum, modulo 65536."""
    return (-sum(body)) & 0xFFFF


def length_field(info_ascii_len: int) -> int:
    """LENGTH field: check nibble (two's complement of the three length nibbles) + 12-bit length."""
    if not 0 <= info_ascii_len <= 0xFFF:
        raise ValueError(f"INFO too long: {info_ascii_len}")
    nibbles = (info_ascii_len & 0xF) + ((info_ascii_len >> 4) & 0xF) + ((info_ascii_len >> 8) & 0xF)
    return (((-nibbles) & 0xF) << 12) | info_ascii_len


def encode(address: int, cid2: int, info: bytes = b"\x01") -> bytes:
    """Encode a request; `info` is binary and sent as hex ASCII."""
    info_hex = info.hex().upper().encode()
    body = f"{VER:02X}{address:02X}{CID1:02X}{cid2:02X}{length_field(len(info_hex)):04X}".encode() + info_hex
    return SOI + body + f"{checksum(body):04X}".encode() + EOI


_HEX = frozenset(b"0123456789ABCDEFabcdef")


def parse(raw: bytes) -> Frame:
    """Validate the complete envelope (SOI/EOI, VER, CID1, LENGTH incl. check nibble, hex, checksum)."""
    if len(raw) < OVERHEAD or raw[:1] != SOI or raw[-1:] != EOI:
        raise FrameError("bad SOI/EOI or too short")
    if not _HEX.issuperset(raw[1:-1]) or (len(raw) - OVERHEAD) % 2:
        raise FrameError("non-hex characters or odd INFO length")
    try:
        header = bytes.fromhex(raw[1:13].decode("ascii"))
        info = bytes.fromhex(raw[13:-5].decode("ascii"))
        received_checksum = int(raw[-5:-1].decode("ascii"), 16)
    except (ValueError, UnicodeDecodeError) as e:
        raise FrameError(f"non-hex characters: {e}") from e
    if len(header) != 6:
        raise FrameError("short header")
    ver, address, cid1, code, length = header[0], header[1], header[2], header[3], int.from_bytes(header[4:6], "big")
    if ver != VER or cid1 != CID1:
        raise FrameError(f"VER/CID1 {ver:02X}/{cid1:02X}")
    if 2 * len(info) > 0xFFF or length != length_field(2 * len(info)):
        raise FrameError(f"LENGTH {length:04X} does not match {2 * len(info)} INFO characters")
    if checksum(raw[1:-5]) != received_checksum:
        raise FrameError("checksum mismatch")
    return Frame(address, code, info, raw)


# Expected binary INFO length: a fixed number, or a function of the decoded INFO
# (e.g. derived from the cell count) that returns None for an implausible INFO
ExpectedLength = int | Callable[[bytes], int | None]


def validate_response(raw: bytes, address: int, expected: ExpectedLength) -> bytes | None:
    """A normal answer (RTN 00) from `address` whose INFO has the expected length; returns the INFO."""
    try:
        frame = parse(raw)
    except FrameError as e:
        logger.debug("Invalid frame: %s", e)
        return None
    if frame.address != address:
        logger.debug("Frame address mismatch: %s", frame.address)
        return None
    if frame.code != 0x00:
        logger.debug("Frame RTN set: %02X", frame.code)
        return None
    length = expected if isinstance(expected, int) else expected(frame.info)
    if length is None or len(frame.info) != length:
        logger.debug("Frame length mismatch - expected: %s, got: %s", length, len(frame.info))
        return None
    return frame.info


def is_intra_pack(raw: bytes) -> bool:
    """Intra-pack traffic of the master polling its slaves (CID2 0x5A on RS485-1/2)."""
    return len(raw) >= OVERHEAD and raw[:1] == SOI and raw[7:9] == b"5A"


INTRA_PACK_INFO_LEN = 32


def validate_intra_pack_answer(raw: bytes, address: int) -> bytes | None:
    """A pack's own intra-pack record: 32 byte INFO, CID2 0x5A instead of a RTN."""
    try:
        frame = parse(raw)
    except FrameError:
        return None
    if frame.address == address and frame.code == CID2_INTRA_PACK and len(frame.info) == INTRA_PACK_INFO_LEN:
        return frame.info
    return None
