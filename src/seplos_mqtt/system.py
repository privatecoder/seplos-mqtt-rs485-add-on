"""
System values read from the master via Modbus RTU FC03 0x1000 x21 (RS485 bus on the CAN port,
firmware 16.06.04+): aggregated over all packs incl. the charge/discharge limits.
"""
from __future__ import annotations

import logging

from . import timeutil
from .decoders import system as system_decoder
from .framing.modbus import UNSUPPORTED_CODES, Status
from .transport import BusBusy, SerialBus

logger = logging.getLogger("SeplosBMS.system")


FLAGS_REGISTER = 0x0074
FLAG_KEYS = ("force_charge_1", "force_charge_2")


def decode_flags(value: int) -> dict:
    """
    FC03 0x0074 (since 16.06.11), low byte like Pylontech CAN 0x35C. Force charge I/II are requested
    at SOC <= 5 % / <= 9 % (with hysteresis); charge/discharge enable duplicate the 0x1000 flags.
    """
    return {
        "force_charge_1": "ON" if value & 0x20 else "OFF",
        "force_charge_2": "ON" if value & 0x10 else "OFF",
    }


class SystemReader:
    MAX_FAILURES = 3

    def __init__(self) -> None:
        self.unsupported = False
        self.exceptions = 0
        self.flags_unsupported = False
        self.flags_failures = 0
        self.last_data: dict | None = None
        # raw registers of the latest read for the OpenBMS Monitor feed: r1000 (42 bytes), r0074 (2 bytes), each
        # {"t", "hex"} (big endian, no header/CRC) or {"t", "err"}; a block not asked is absent
        self.raw: dict[str, dict] = {}

    def read(self, bus: SerialBus) -> dict | None:
        """Read the system values; disables itself if the firmware doesn't know the block."""
        if self.unsupported:
            return None
        try:
            return self._read(bus)
        except BusBusy as e:
            logger.warning("System:%s", e)
            return None

    @staticmethod
    def _raw(bus: SerialBus, regs: list[int] | None) -> dict:
        if regs is None or bus.last_failure is not None:
            return {"t": timeutil.now_ms(), "err": bus.last_failure or "failed"}
        return {"t": bus.last_at, "hex": b"".join(r.to_bytes(2, "big") for r in regs).hex()}

    def _read(self, bus: SerialBus) -> dict | None:
        self.raw = {}
        try:
            status, regs = bus.read_modbus(0x03, system_decoder.REGISTER, system_decoder.COUNT)
        except BusBusy:
            self.raw["r1000"] = self._raw(bus, None)
            raise
        self.raw["r1000"] = self._raw(bus, regs if status is Status.OK else None)
        if status is Status.EXCEPTION:
            if regs not in UNSUPPORTED_CODES:
                logger.debug("System:Modbus exception 0x%02X (busy/device failure)", regs)
                return None
            self.exceptions += 1
            if self.exceptions >= self.MAX_FAILURES:
                self.unsupported = True
                logger.warning("System:Modbus block 0x1000 not supported (firmware < 16.06.04), disabled")
            return None
        if status is not Status.OK or regs is None:
            logger.debug("System:No valid Modbus answer")
            return None
        self.exceptions = 0
        data = system_decoder.decode(regs)
        # unknown (null) instead of leaving an old value when the flags are unavailable
        data["binary"].update(self._read_flags(bus) or dict.fromkeys(FLAG_KEYS))
        return data

    def _read_flags(self, bus: SerialBus) -> dict | None:
        """Force charge flags; disabled after repeated exceptions (firmware < 16.06.11)."""
        if self.flags_unsupported:
            return None
        try:
            status, value = bus.read_modbus_single_wide(0x03, FLAGS_REGISTER)
        except BusBusy:
            self.raw["r0074"] = self._raw(bus, None)
            raise
        self.raw["r0074"] = self._raw(bus, [value] if status is Status.OK and value is not None else None)
        if status is Status.OK and value is not None:
            self.flags_failures = 0
            return decode_flags(value)
        if status is Status.EXCEPTION and value in UNSUPPORTED_CODES:
            self.flags_failures += 1
            if self.flags_failures >= self.MAX_FAILURES:
                self.flags_unsupported = True
                logger.info("System:Modbus register 0x0074 not supported (firmware < 16.06.11), force charge flags disabled")
        return None
