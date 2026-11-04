"""
System values read from the master via Modbus RTU FC03 0x1000 x21 (RS485 bus on the CAN port,
firmware 16.06.04+): aggregated over all packs incl. the charge/discharge limits.
"""
from __future__ import annotations

import logging

from .decoders import system as system_decoder
from .framing.modbus import Status
from .transport import SerialBus

logger = logging.getLogger("SeplosBMS.system")


class SystemReader:
    MAX_FAILURES = 3

    def __init__(self) -> None:
        self.unsupported = False
        self.exceptions = 0
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
        return system_decoder.decode(regs)
