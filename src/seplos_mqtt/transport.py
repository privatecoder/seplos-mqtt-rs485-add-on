"""
The serial bus: the single owner of the serial port. Every request (Seplos ASCII, Modbus RTU,
later EMU1101-EXT binary frames and gated commands) runs as one transaction under one lock, and every
transmission waits for a quiet gap after the last bus activity. Without that gap the BMS answers
get corrupted, so callers never sleep on their own.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Protocol

from . import timeutil
from .decoders import intrapack
from .framing import ascii as af
from .framing import binary as bf
from .framing import modbus

logger = logging.getLogger("SeplosBMS.bus")

# request_rtn(): nothing received at all (distinct from an invalid answer)
NO_ANSWER = -1

# minimum continuous quiet time on the bus (no received byte) before a transmission
QUIET_GAP_SECONDS = 1.0
# give up establishing quiet after this long (busy bus, foreign traffic): the request is not sent
MAX_QUIET_WAIT_SECONDS = 10.0
# upper bound of quiet-wait rounds (each round sleeps once), independent of the clock
MAX_QUIET_ROUNDS = 50


class BusBusy(RuntimeError):
    """No quiet gap could be established; nothing was transmitted."""


class SerialPort(Protocol):
    def write(self, data: bytes) -> int | None: ...
    def read(self, size: int = 1) -> bytes: ...
    def read_until(self, expected: bytes = b"\n", size: int | None = None) -> bytes: ...
    def flushInput(self) -> None: ...  # noqa: N802 - pyserial name
    def flushOutput(self) -> None: ...  # noqa: N802
    def close(self) -> None: ...


class SerialBus:
    FRAME_READ_RETRIES = 5

    def __init__(
        self,
        port: SerialPort,
        gap: float = QUIET_GAP_SECONDS,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.port = port
        self.gap = gap
        # looked up at call time, so a patched time module is honored
        self._clock = clock or (lambda: time.monotonic())
        self._sleep = sleep or (lambda seconds: time.sleep(seconds))
        self._lock = threading.Lock()
        self._last_activity: float | None = None
        self._skipped = 0
        # the last request: epoch ms right after its valid answer, and why it failed
        # ("busy": nothing sent, "failed": no valid answer, "unsupported": the firmware refused it; None: ok)
        self.last_at: int | None = None
        self.last_failure: str | None = None

    def set_gap(self, seconds: float) -> None:
        """The quiet before each request from the next request on (OpenBMS Monitor feed: 0.2 s)."""
        self.gap = seconds

    def _answered(self) -> None:
        self.last_at, self.last_failure = timeutil.now_ms(), None

    def _failed(self, reason: str) -> None:
        self.last_failure = reason

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
        with self._lock:
            self.port.close()

    def flush(self) -> None:
        """Discard buffered input; counts as bus activity, so the next request waits a full quiet gap."""
        with self._lock:
            self.port.flushOutput()
            self.port.flushInput()
            self._mark_activity()

    # --- timing ---------------------------------------------------------------------------

    def _pending(self) -> int:
        try:
            return int(getattr(self.port, "in_waiting", 0) or 0)
        except Exception:
            return 0

    def _wait_quiet(self) -> None:
        """
        Wait until no byte was received for `gap` seconds, measured from the last received byte:
        bytes arriving meanwhile (noise, late answers, foreign traffic) are discarded and restart the
        timer. Raises BusBusy (nothing sent) if that can't be established in time.
        """
        start = self._clock()
        if self._last_activity is None:
            self._last_activity = start          # also establish quiet before the very first request
        for _ in range(MAX_QUIET_ROUNDS):
            remaining = self.gap - (self._clock() - self._last_activity)
            if remaining > 0:
                self._sleep(remaining)
            pending = self._pending()
            if not pending:
                return
            self.port.read(pending)
            self._mark_activity()
            if self._clock() - start > MAX_QUIET_WAIT_SECONDS:
                break
        raise BusBusy("no quiet gap on the bus")

    def _mark_activity(self) -> None:
        self._last_activity = self._clock()

    def _send_tracked(self, data: bytes, reset_input: bool = False) -> None:
        """``_send`` for the requests that report ``last_failure``."""
        try:
            self._send(data, reset_input)
        except BusBusy:
            self._failed("busy")
            raise

    def _send(self, data: bytes, reset_input: bool = False) -> None:
        # _wait_quiet() returns only with an empty input buffer (everything received was drained and
        # restarted the gap), so no separate input reset is needed; `reset_input` is kept for callers
        self._wait_quiet()
        try:
            self.port.write(data)
        except Exception:
            self._mark_activity()            # a partial write may have reached the bus
            raise

    def _receive_frame(self, max_skipped: int = 5) -> bytes:
        try:
            return self._read_frame(max_skipped)
        finally:
            self._mark_activity()

    # --- Seplos ASCII ---------------------------------------------------------------------

    def _read_frame(self, max_skipped: int) -> bytes:
        """Read the next frame, logging and skipping interleaved intra-pack frames."""
        self._skipped = 0
        raw = self.port.read_until(af.EOI)
        for _ in range(max_skipped):
            if not self._skip_intra_pack(raw):
                break
            self._skipped += 1
            raw = self.port.read_until(af.EOI)
        return raw

    @staticmethod
    def _skip_intra_pack(raw: bytes) -> bool:
        """Master polling its slaves (CID2 0x5A on RS485-1/2): debug-log and skip."""
        if not af.is_intra_pack(raw):
            return False
        if logger.isEnabledFor(logging.DEBUG):
            try:
                frame = af.parse(raw)
                if not frame.info:
                    logger.debug("Intra-pack: master requests pack %s", frame.address)
                elif len(frame.info) == af.INTRA_PACK_INFO_LEN:
                    logger.debug("Intra-pack: pack %s answers %s", frame.address, json.dumps(intrapack.decode_record(frame.info)))
                else:
                    logger.debug("Intra-pack: unexpected frame %s", raw)
            except af.FrameError:
                logger.debug("Intra-pack: invalid frame %s", raw)
        return True

    def request(
        self, address: int, cid2: int, expected: af.ExpectedLength, label: str, info: bytes = b"\x01", retries: int | None = None
    ) -> bytes | None:
        """Request a normal answer whose binary INFO has the expected length; returns that INFO.

        `retries` limits the attempts (None: FRAME_READ_RETRIES). A caller that sets it handles the
        failure itself (e.g. probing addresses), so it is only debug-logged then.
        """
        attempts = self.FRAME_READ_RETRIES if retries is None else retries
        command = af.encode(address, cid2, info)
        logger.debug("Pack%s:%s_command: %s", address, label, command)
        with self._lock:
            for attempt in range(attempts):
                self._send_tracked(command)
                payload = af.validate_response(self._receive_frame(), address, expected)
                if payload is not None:
                    self._answered()
                    return payload
                logger.debug("Pack%s:%s attempt %s: no valid answer", address, label, attempt + 1)
        self._failed("failed")
        if retries is None:
            logger.error("Pack%s:Failed to read %s after %s retries", address, label.lower(), attempts)
        else:
            logger.debug("Pack%s:No valid %s answer", address, label.lower())
        return None

    def request_rtn(self, address: int, cid2: int) -> int | None:
        """
        Send a command without INFO and return the RTN of the answer: NO_ANSWER if nothing at all
        was received, None for an invalid answer.
        """
        with self._lock:
            self._send(af.encode(address, cid2, b""), reset_input=True)
            raw = self._receive_frame()
            skipped = self._skipped
        if not raw:
            # silence only if nothing at all was received (other traffic makes it inconclusive)
            return NO_ANSWER if not skipped else None
        try:
            frame = af.parse(raw)
        except af.FrameError:
            return None
        return frame.code if frame.address == address else None

    def query(self, address: int, cid2: int, info: bytes, attempts: int = 2) -> af.Frame | int | None:
        """
        Optional request (e.g. an EMU1101-EXT extension): the answer frame whatever its RTN, NO_ANSWER if the
        pack stayed silent, None for invalid answers. Fewer retries than regular polls.
        """
        result: af.Frame | int | None = None
        command = af.encode(address, cid2, info)
        with self._lock:
            for _ in range(attempts):
                self._send_tracked(command, reset_input=True)
                raw = self._receive_frame()
                if raw == command:             # gateway echo of the request
                    raw = self._receive_frame()
                if not raw:
                    result = NO_ANSWER
                    continue
                try:
                    frame = af.parse(raw)
                except af.FrameError:
                    result = None
                    continue
                if frame.address == address:
                    self._answered()
                    return frame
        self._failed("failed")
        return result

    def request_binary(self, address: int, command: int, data: bytes = b"") -> bytes | None:
        """
        EMU1101-EXT binary frame on UART1; returns DATA, None if there's no valid answer. Raises
        BinaryRejected for a valid rejection. A gateway echo of the request is skipped.
        """
        request = bf.encode(address, command, data)
        with self._lock:
            self._send_tracked(request, reset_input=True)
            self._failed("failed")
            try:
                for _ in range(2):
                    header = self.port.read(4)
                    if len(header) < 4 or header[0] != bf.SOF or header[3] > bf.MAX_DATA:
                        return None
                    raw = header + self.port.read(header[3] + 3)
                    if raw == request:
                        continue
                    try:
                        answer = bf.parse(raw, address, command)
                    except bf.BinaryRejected:
                        self._failed("unsupported")
                        raise
                    except bf.BinaryFrameError as e:
                        logger.debug("Pack%s:Binary 0x%02X: %s", address, command, e)
                        return None
                    self._answered()
                    return answer
                return None
            finally:
                self._mark_activity()

    def request_intra_pack(self, address: int) -> bytes | None:
        """The pack's own intra-pack record (CID2 0x5A), only served on RS485-1/2 (UART0)."""
        command = af.encode(address, af.CID2_INTRA_PACK, b"")
        with self._lock:
            for _ in range(self.FRAME_READ_RETRIES):
                self._send(command, reset_input=True)
                payload = af.validate_intra_pack_answer(self._receive_frame(max_skipped=0), address)
                if payload is not None:
                    return payload
        logger.debug("Pack%s:No valid intra-pack (0x5A) answer", address)
        return None

    # --- Modbus RTU -----------------------------------------------------------------------

    def _modbus_exchange(self, request: bytes, response_length: int) -> bytes:
        with self._lock:
            self._send_tracked(request, reset_input=True)
            try:
                return self.port.read(response_length)
            finally:
                self._mark_activity()

    def read_modbus(self, function_code: int, register: int, count: int) -> tuple[modbus.Status | None, list[int] | None]:
        response = self._modbus_exchange(modbus.encode_read(function_code, register, count), modbus.response_length(count))
        return self._modbus_result(modbus.parse_read(response, function_code, count))

    def read_modbus_single_wide(self, function_code: int, register: int) -> tuple[modbus.Status | None, int | None]:
        """One register from a handler with the non-standard 16-bit byte count (8 byte answer)."""
        response = self._modbus_exchange(modbus.encode_read(function_code, register, 1), 8)
        return self._modbus_result(modbus.parse_single_wide_count(response, function_code))

    def _modbus_result(self, result):
        status, value = result
        if status is modbus.Status.OK:
            self._answered()
        elif status is modbus.Status.EXCEPTION and value in modbus.UNSUPPORTED_CODES:
            self._failed("unsupported")
        else:
            self._failed("failed")
        return result

    def probe_modbus(self, function_code: int, register: int, count: int) -> bool | None:
        """True for a valid response, False for an exception response, None for no/invalid answer."""
        status, value = self.read_modbus(function_code, register, count)
        if status is modbus.Status.EXCEPTION:
            # only "illegal function/address" says the feature is missing; busy/device failure is inconclusive
            return False if value in modbus.UNSUPPORTED_CODES else None
        return True if status is modbus.Status.OK else None
