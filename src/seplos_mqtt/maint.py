"""
Optional read-only support for the EMU1101-EXT patched firmware (Light from 1.5.4, Full from 1.6.4), per board.

Detection only via ASCII 64/03 (never from 0x51 or a version string). Each read is gated by the
board's own identity: storage diagnostics by image flag 0x20, the full-width live energy by
capability bit 3; the UART1 binary parameter/serial reads exist in both current profiles.
Failures of these optional reads never fail the regular poll. Nothing here writes to the BMS.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from . import timeutil
from .decoders import identity as identity_decoder
from .decoders import maint as md
from .decoders import parameters as parameter_decoder
from .framing import binary as bf
from .transport import NO_ANSWER, SerialBus

logger = logging.getLogger("SeplosBMS.maint")

DETECT_ATTEMPTS = 3
# RTN 04 unknown command, RTN 05 unknown subcommand: the board has no EMU1101-EXT identity read
UNSUPPORTED_RTN = (0x04, 0x05)
ENERGY_INTERVAL = 60
ENERGY_MAX_AGE = 600
STORAGE_INTERVAL = 600
LOCK_INTERVAL = 60            # 64/04 on the Full line: carries the runtime lock state
LOCK_PARAMETER_INTERVAL = 300  # TEMP(maint-1.6): no 64/04, the lock state comes from BitGroup0 of 0x47
MARKER_INTERVAL = 60
CALIBRATION_INTERVAL = 600
PARAMETER_INTERVAL = 3600
RETRY_INTERVAL = 60

# binary UART1 commands
BIN_PARAMETERS, BIN_BATTERY_SERIAL, BIN_BMS_SERIAL = 0x47, 0xA2, 0xA4
PARAMETER_BLOCK_LEN = 168
SERIAL_LEN = 30

# discovery conditions (see discovery.catalog.Entity.when)
CAP_MAINT = "maint"
CAP_STORAGE = "maint_storage"
CAP_UART1 = "maint_uart1"
CAP_LOCK = "maint_lock"
CAP_MARKER = "maint_marker"
CAP_HARD_STOP = "maint_hard_stop"
CAP_RTC = "maint_rtc"


@dataclass
class Timed:
    value: object = None
    ts: float = 0.0          # monotonic time of the last success
    next_due: float = 0.0
    read_at: str | None = None

    def fresh(self, now: float, max_age: float) -> bool:
        return self.value is not None and now - self.ts <= max_age


@dataclass
class MaintSupport:
    address: int
    can_bus: bool
    state: str = "pending"            # pending | present | absent | unknown | unsupported
    attempts: int = 0
    identity: md.Identity | None = None
    energy: Timed = field(default_factory=Timed)
    storage: Timed = field(default_factory=Timed)
    parameters: Timed = field(default_factory=Timed)
    serials: dict = field(default_factory=dict)
    serials_done: bool = False
    serial_attempts: int = 0
    marker: Timed = field(default_factory=Timed)
    calibration: Timed = field(default_factory=Timed)
    group0: int | None = None           # BitGroup0 from the latest parameter read (ASCII or binary 47)
    # answers as received, for the OpenBMS Monitor feed: maint (64/03), a2/a4 (binary serial DATA), r47 (the binary
    # parameter block as the 169-byte ASCII layout: address byte + 168 data bytes); {"t": epoch ms, "hex"}
    raw: dict = field(default_factory=dict)

    # --- detection -------------------------------------------------------------------------

    def detect(self, bus: SerialBus) -> None:
        if self.state != "pending":
            return
        self.attempts += 1
        answer = bus.query(self.address, md.CID2, md.INFO_IDENTITY)
        if answer == NO_ANSWER or answer is None:
            if self.attempts >= DETECT_ATTEMPTS:
                self.state = "unknown"
                logger.info("Pack%s:No answer to the EMU1101-EXT identity request, EMU1101-EXT reads disabled", self.address)
            return
        if answer.code in UNSUPPORTED_RTN and not answer.info:
            # OEM firmware: unknown command / subcommand
            self.state = "absent"
            logger.info("Pack%s:No EMU1101-EXT firmware (identity request answered with RTN %02X)", self.address, answer.code)
            return
        if answer.code != 0x00:
            # checksum/device errors (RTN 02, 08, ...) or a rejection that isn't the exact empty form
            # say nothing about the firmware: try again
            logger.debug("Pack%s:EMU1101-EXT identity request: RTN %02X", self.address, answer.code)
            if self.attempts >= DETECT_ATTEMPTS:
                self.state = "unknown"
            return
        self.raw["maint"] = {"t": bus.last_at, "hex": answer.info.hex()}
        try:
            self.identity = md.decode_identity(answer.info)
        except md.MaintFormatError as e:
            # a patched board we don't support (older build, unknown format) is not an OEM board either:
            # no reads, but nothing resolved
            self.state = "unsupported"
            logger.warning("Pack%s:Unsupported patch firmware identity, EMU1101-EXT reads disabled: %s", self.address, e)
            return
        self.state = "present"
        logger.info("Pack%s:%s on OEM %s (build %s, flags 0x%02X, capabilities 0x%04X)", self.address,
                    self.identity.name, self.identity.base_version, self.identity.build_id,
                    self.identity.image_flags, self.identity.capabilities)

    @property
    def capabilities(self) -> set[str]:
        """Conditions for the discovery catalog."""
        if self.identity is None:
            return set()
        caps = {CAP_MAINT}
        if self.identity.has_flag(md.FLAG_STORAGE_DIAGNOSTICS):
            caps.add(CAP_STORAGE)
        if self.can_bus:
            caps.add(CAP_UART1)
            if self.identity.has_flag(md.FLAG_UART1_POWER_RTC):
                caps.add(CAP_RTC)
        # lock state: from the 64/04 runtime cache, or (TEMP(maint-1.6): no flag 0x20) from BitGroup0 of 0x47
        if self.identity.has_flag(md.FLAG_PACK_LOCK) and (self.identity.has_flag(md.FLAG_STORAGE_DIAGNOSTICS) or self.can_bus):
            caps.add(CAP_LOCK)
        if self._marker_readable:
            caps.add(CAP_MARKER)
        caps.add(CAP_HARD_STOP)
        return caps

    @property
    def _lock_from_group0(self) -> bool:
        """TEMP(maint-1.6): pack lock without 64/04 (only the first MAINT 1.6 has flag 0x04 but not 0x20)."""
        return (self.identity is not None and self.identity.has_flag(md.FLAG_PACK_LOCK)
                and not self.identity.has_flag(md.FLAG_STORAGE_DIAGNOSTICS))

    @property
    def _parameter_interval(self) -> float:
        return LOCK_PARAMETER_INTERVAL if self._lock_from_group0 else PARAMETER_INTERVAL

    @property
    def parameter_interval(self) -> float:
        return self._parameter_interval

    @property
    def _marker_readable(self) -> bool:
        """64/02: on UART1 only the master answers, on UART0 every pack with image flag 0x10."""
        if self.identity is None:
            return False
        return self.address == 0 if self.can_bus else self.identity.has_flag(md.FLAG_UART0_MARKER)

    # --- periodic reads ----------------------------------------------------------------------

    def _ascii(self, bus: SerialBus, info: bytes, length: int) -> bytes | None:
        answer = bus.query(self.address, md.CID2, info)
        if isinstance(answer, int) or answer is None or answer.code != 0x00 or len(answer.info) != length:
            return None
        return answer.info

    def _due(self, item: Timed, now: float, interval: float, read) -> None:
        if now < item.next_due:
            return
        try:
            value = read()
        except (md.MaintFormatError, bf.BinaryFrameError, ValueError) as e:
            logger.warning("Pack%s:Invalid EMU1101-EXT answer: %s", self.address, e)
            value = None
        if value is not None:
            item.value, item.ts, item.next_due, item.read_at = value, now, now + interval, timeutil.now_iso()
        else:
            item.next_due = now + min(interval, RETRY_INTERVAL)

    def poll(self, bus: SerialBus) -> None:
        if self.identity is None:
            return
        now = time.monotonic()
        if self.identity.capabilities & (md.CAP_LIVE_ENERGY | md.CAP_PERSISTED_SOC):
            self._due(self.energy, now, ENERGY_INTERVAL,
                      lambda: self._decode(bus, md.INFO_ENERGY, md.ENERGY_LEN, md.decode_energy))
        if self.identity.has_flag(md.FLAG_STORAGE_DIAGNOSTICS):
            interval = LOCK_INTERVAL if self.identity.has_flag(md.FLAG_PACK_LOCK) else STORAGE_INTERVAL
            self._due(self.storage, now, interval,
                      lambda: self._decode(bus, md.INFO_STORAGE, md.STORAGE_LEN, md.decode_storage))
        if self._marker_readable:
            self._due(self.marker, now, MARKER_INTERVAL,
                      lambda: self._decode(bus, md.INFO_MARKER, md.MARKER_LEN, md.decode_marker))
        if self.identity.has_capability(md.CAP_CALIBRATION):
            self._due(self.calibration, now, CALIBRATION_INTERVAL,
                      lambda: self._decode(bus, md.INFO_CALIBRATION_ACTIVE, md.CALIBRATION_ACTIVE_LEN, md.decode_calibration_active))
        if self.can_bus:
            self._due(self.parameters, now, self._parameter_interval, lambda: self._read_parameters(bus))
            if not self.serials_done:
                self._read_serials(bus)

    def _decode(self, bus: SerialBus, info: bytes, length: int, decoder):
        raw = self._ascii(bus, info, length)
        return decoder(raw) if raw is not None else None

    def _read_parameters(self, bus: SerialBus) -> dict | None:
        data = bus.request_binary(self.address, BIN_PARAMETERS)
        if data is None or len(data) != PARAMETER_BLOCK_LEN:
            return None
        decoded = parameter_decoder.decode_block(data)
        self.raw["r47"] = {"t": bus.last_at, "hex": (bytes([self.address]) + data).hex()}
        return decoded

    def _read_serials(self, bus: SerialBus) -> None:
        self.serial_attempts += 1
        if self.serial_attempts > DETECT_ATTEMPTS:
            self.serials_done = True
            return
        for command, key in ((BIN_BATTERY_SERIAL, "serial_number"), (BIN_BMS_SERIAL, "bms_serial_number")):
            if key in self.serials:
                continue
            try:
                data = bus.request_binary(self.address, command)
            except bf.BinaryRejected as e:
                logger.info("Pack%s:Serial number 0x%02X rejected: %s", self.address, command, e)
                data = b""
            if data is None:
                return            # retried with the next poll
            if len(data) == SERIAL_LEN:
                self.raw["a2" if command == BIN_BATTERY_SERIAL else "a4"] = {"t": bus.last_at, "hex": data.hex()}
            self.serials[key] = identity_decoder.decode_serial_number(data, key)["normal"][key] if len(data) == SERIAL_LEN else None
        self.serials_done = True

    # --- results ------------------------------------------------------------------------------

    def live_energy(self) -> dict | None:
        """The full-width counters if fresh; a stale value is not presented as current."""
        if self.identity is None or not self.identity.has_capability(md.CAP_LIVE_ENERGY):
            return None
        now = time.monotonic()
        if self.energy.fresh(now, ENERGY_MAX_AGE):
            live = self.energy.value["live"]
            return {"energy_charged": live["energy_charged"], "energy_discharged": live["energy_discharged"]}
        return {"energy_charged": None, "energy_discharged": None}

    def payload(self) -> dict | None:
        """The `maint` group of the pack payload."""
        if self.identity is None:
            return None
        now = time.monotonic()
        normal: dict = {"maint_version": self.identity.name}
        binary: dict = {}
        attributes: dict = {"identity": self.identity.as_dict()}
        if self.energy.value is not None and self.identity.has_capability(md.CAP_LIVE_ENERGY):
            attributes["live_snapshot"] = {**self.energy.value["live"], "read_at": self.energy.read_at}
        if self.energy.value is not None:
            if self.identity.has_capability(md.CAP_PERSISTED_SOC):
                attributes["checkpoint"] = {**self.energy.value["persisted"], "read_at": self.energy.read_at}
        if self.identity.has_flag(md.FLAG_STORAGE_DIAGNOSTICS):
            fresh = self.storage.fresh(now, 3 * STORAGE_INTERVAL)
            binary["storage_problem"] = ("ON" if md.storage_problem(self.storage.value) else "OFF") if fresh else None
            if self.storage.value is not None:
                attributes["storage"] = {**self.storage.value, "read_at": self.storage.read_at}
            if CAP_LOCK in self.capabilities:
                lock_fresh = self.storage.fresh(now, 3 * LOCK_INTERVAL)
                runtime = self.storage.value.get("lock_runtime") if lock_fresh else None
                binary["pack_lock_active"] = None if runtime is None else ("ON" if runtime else "OFF")
        elif self._lock_from_group0 and CAP_LOCK in self.capabilities:     # TEMP(maint-1.6)
            fresh = self.parameters.fresh(now, 3 * LOCK_PARAMETER_INTERVAL) and self.group0 is not None
            binary["pack_lock_active"] = ("OFF" if self.group0 & 0x20 else "ON") if fresh else None
        if self._marker_readable:
            marker = self.marker.value if self.marker.fresh(now, 3 * MARKER_INTERVAL) else None
            active = marker["maintenance_active"] if marker else None
            binary["maintenance_mode"] = None if active is None else ("ON" if active else "OFF")
            normal["effective_slave_count"] = marker["effective_slave_count"] if marker else None
        if self.calibration.value is not None:
            attributes["calibration"] = {**self.calibration.value, "read_at": self.calibration.read_at}
        if self.group0 is not None:
            settings = md.decode_group0(self.group0, self.identity)
            attributes["settings"] = settings
            normal["recharge_stop_mode"] = "hard stop" if settings["recharge_hard_stop"] else "soft stop"
        else:
            normal["recharge_stop_mode"] = None
        return {"normal": normal, "binary": binary, "attributes": attributes}

    def uart1_parameters(self) -> dict | None:
        return self.parameters.value if self.can_bus else None
