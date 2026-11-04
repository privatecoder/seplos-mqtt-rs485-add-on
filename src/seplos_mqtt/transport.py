"""
The serial bus. All requests to the BMS (Seplos ASCII and Modbus RTU) go through one SerialBus,
so later additions (MAINT binary frames, a gated command path) share the same owner.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Protocol

from .decoders import intrapack
from .framing import ascii as af
from .framing import modbus

logger = logging.getLogger("SeplosBMS.bus")

# request_rtn(): nothing received at all (distinct from an invalid answer)
NO_ANSWER = -1


class SerialPort(Protocol):
    def write(self, data: bytes) -> int | None: ...
    def read(self, size: int = 1) -> bytes: ...
    def read_until(self, expected: bytes = b"\n", size: int | None = None) -> bytes: ...
    def flushInput(self) -> None: ...  # noqa: N802 - pyserial name
    def flushOutput(self) -> None: ...  # noqa: N802
    def close(self) -> None: ...


class SerialBus:
    FRAME_READ_RETRIES = 5
    INTRA_PACK_RETRY_DELAY = 0.2

    def __init__(self, port: SerialPort) -> None:
        self.port = port

    @property
    def is_open(self) -> bool:
        if hasattr(self.port, "is_open"):
            return bool(self.port.is_open)
        if hasattr(self.port, "isOpen"):
            try:
                return bool(self.port.isOpen())
            except Exception:
                return False
        return False

    def close(self) -> None:
        self.port.close()

    def flush(self) -> None:
        self.port.flushOutput()
        self.port.flushInput()

    # --- Seplos ASCII ---------------------------------------------------------------------

    def read_frame(self, max_skipped: int = 5) -> bytes:
        """Read the next frame, logging and skipping interleaved intra-pack frames."""
        raw = self.port.read_until(af.EOI)
        for _ in range(max_skipped):
            if not self._skip_intra_pack(raw):
                break
            raw = self.port.read_until(af.EOI)
        return raw

    @staticmethod
    def _skip_intra_pack(raw: bytes) -> bool:
        """Master polling its slaves (CID2 0x5A on RS485-1/2): debug-log and skip."""
        try:
            if not af.is_intra_pack(raw):
                return False
            address = int(raw[3:5], 16)
            if logger.isEnabledFor(logging.DEBUG):
                if raw[9:13] == b"0000":
                    logger.debug("Intra-pack: master requests pack %s", address)
                elif raw[9:13] == b"C040" and len(raw) == 82:
                    decoded = intrapack.decode_record(bytes.fromhex(af.info_ascii(raw).decode("ascii")))
                    logger.debug("Intra-pack: pack %s answers %s", address, json.dumps(decoded))
                else:
                    logger.debug("Intra-pack: unexpected frame %s", raw)
            return True
        except (UnicodeDecodeError, ValueError):
            return False

    def request(self, address: int, cid2: int, expected: af.ExpectedLength, label: str, info: bytes = b"01") -> bytes | None:
        """Request a normal answer whose INFO has the expected binary length; returns the binary INFO."""
        command = af.encode(address, cid2, info)
        logger.debug("Pack%s:%s_command: %s", address, label, command)
        for attempt in range(self.FRAME_READ_RETRIES):
            self.port.write(command)
            raw = self.read_frame()
            if len(raw) < af.min_frame_length(expected):
                logger.debug("Pack%s:%s attempt %s: insufficient length", address, label, attempt + 1)
                continue
            payload = af.validate_response(raw, address, expected)
            if payload is not None:
                return payload
            logger.debug("Pack%s:%s attempt %s: validation failed", address, label, attempt + 1)
        logger.error("Pack%s:Failed to read %s after %s retries", address, label.lower(), self.FRAME_READ_RETRIES)
        return None

    def request_rtn(self, address: int, cid2: int) -> int | None:
        """
        Send a command without INFO and return the RTN of the answer: NO_ANSWER if nothing at all
        was received, None for an invalid answer.
        """
        self.port.flushInput()
        self.port.write(af.encode(address, cid2, b""))
        raw = self.read_frame()
        if not raw:
            return NO_ANSWER
        if len(raw) < af.OVERHEAD or raw[:1] != af.SOI:
            return None
        if af.address_of(raw) != address or not af.checksum_ok(raw):
            return None
        return af.rtn_of(raw)

    def request_intra_pack(self, address: int) -> bytes | None:
        """The pack's own intra-pack record (CID2 0x5A), only served on RS485-1/2 (UART0)."""
        command = af.encode(address, af.CID2_INTRA_PACK, b"")
        for _ in range(self.FRAME_READ_RETRIES):
            self.port.flushInput()
            self.port.write(command)
            payload = af.validate_intra_pack_answer(self.port.read_until(af.EOI), address)
            if payload is not None:
                return payload
            time.sleep(self.INTRA_PACK_RETRY_DELAY)
        logger.debug("Pack%s:No valid intra-pack (0x5A) answer", address)
        return None

    # --- Modbus RTU -----------------------------------------------------------------------

    def read_modbus(self, function_code: int, register: int, count: int) -> tuple[modbus.Status | None, list[int] | None]:
        self.port.flushInput()
        self.port.write(modbus.encode_read(function_code, register, count))
        return modbus.parse_read(self.port.read(modbus.response_length(count)), function_code, count)

    def read_modbus_single_wide(self, function_code: int, register: int) -> tuple[modbus.Status | None, int | None]:
        """One register from a handler with the non-standard 16-bit byte count (8 byte answer)."""
        self.port.flushInput()
        self.port.write(modbus.encode_read(function_code, register, 1))
        return modbus.parse_single_wide_count(self.port.read(8), function_code)

    def probe_modbus(self, function_code: int, register: int, count: int) -> bool | None:
        """True for a valid response, False for an exception response, None for no/invalid answer."""
        status, _ = self.read_modbus(function_code, register, count)
        if status is None:
            return None
        return status is modbus.Status.OK
