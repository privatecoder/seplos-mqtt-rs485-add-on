"""
System values read from the master via Modbus RTU FC03 0x1000 x21 (RS485 bus on the CAN port,
firmware 16.06.04+): aggregated over all packs incl. the charge/discharge limits.
"""
from __future__ import annotations

import logging

from .decoders import system as system_decoder
from .framing.modbus import Status
from .pack import request_gap
from .transport import SerialBus

logger = logging.getLogger("SeplosBMS.system")


FLAGS_REGISTER = 0x0074


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

    def read(self, bus: SerialBus) -> dict | None:
        """Read the system values; disables itself if the firmware doesn't know the block."""
        if self.unsupported:
            return None
        status, regs = bus.read_modbus(0x03, system_decoder.REGISTER, system_decoder.COUNT)
        if status is Status.EXCEPTION:
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
        flags = self._read_flags(bus)
        if flags is not None:
            data["binary"].update(flags)
        return data

    def _read_flags(self, bus: SerialBus) -> dict | None:
        """Force charge flags; disabled after repeated exceptions (firmware < 16.06.11)."""
        if self.flags_unsupported:
            return None
        request_gap()
        status, value = bus.read_modbus_single_wide(0x03, FLAGS_REGISTER)
        if status is Status.OK and value is not None:
            self.flags_failures = 0
            return decode_flags(value)
        if status is Status.EXCEPTION:
            self.flags_failures += 1
            if self.flags_failures >= self.MAX_FAILURES:
                self.flags_unsupported = True
                logger.info("System:Modbus register 0x0074 not supported (firmware < 16.06.11), force charge flags disabled")
        return None
