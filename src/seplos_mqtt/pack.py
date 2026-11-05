"""Polling of one battery pack: request scheduling, caching of static data, patch level probing."""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable

from . import timeutil
from .cells.checks import Sample
from .config import Config
from .decoders import alarms, clock, identity, intrapack, parameters, telemetry
from .energy import EnergyGuard
from .framing.ascii import ExpectedLength
from .maint import MaintSupport
from .state import StateStore
from .transport import NO_ANSWER, SerialBus

logger = logging.getLogger("SeplosBMS.pack")

BatteryData = dict

# OEM parameter P65 allows 7-16 cells
MIN_CELLS, MAX_CELLS = 7, 16


def _cell_dependent(length: Callable[[int], int], bytes_per_cell: int, markers: dict[int, int]) -> Callable[[bytes], int | None]:
    """
    Expected INFO length of 0x42/0x44 from the cell count in byte 2; None (invalid) for an implausible
    count or wrong count markers (offsets relative to BASE = 3 + bytes_per_cell * M).
    """
    def expected(info: bytes) -> int | None:
        if len(info) < 3 or not MIN_CELLS <= info[2] <= MAX_CELLS:
            return None
        total, base = length(info[2]), 3 + bytes_per_cell * info[2]
        if len(info) != total or any(info[base + offset] != value for offset, value in markers.items()):
            return None
        return total
    return expected


# binary INFO lengths; markers: 0x42 temperature count 6 and custom count 10, 0x44 temperature count 6 and bit alarm count 20
TELEMETRY_LEN: ExpectedLength = _cell_dependent(telemetry.info_length, 2, {0: 6, 19: 10})
TELESIGNAL_LEN: ExpectedLength = _cell_dependent(alarms.info_length, 1, {0: 6, 9: 20})
DEVICE_INFO_LEN = 32
SERIAL_LEN = 30
PARAMETER_LEN = 169

# the extended status (0x5A) is kept over failed requests for at most this long
STATUS_MAX_AGE_SECONDS = 60
# all extended status keys with unknown values
UNKNOWN_STATUS = {group: dict.fromkeys(values) for group, values in intrapack.decode_extended_status(bytes(32)).items()}



class BatteryPack:
    """Reads one pack (DIP address) via the shared bus."""

    # Give up on the (static) device info after this many polls without success
    INFO_MAX_ATTEMPTS = 3
    PARAMETER_REFRESH_SECONDS = 3600

    def __init__(self, address: int, bus: SerialBus, config: Config, store: StateStore | None = None) -> None:
        self.address = address
        self.bus = bus
        self.config = config
        self.info: dict | None = None
        self.info_attempts = 0
        self.info_revision = 0          # incremented when the cached device info changes
        self.parameters: dict | None = None
        self.parameters_ts = 0.0         # monotonic time of the last successful 0x47 read
        self.parameters_next = 0.0       # monotonic deadline of the next 0x47 read (0: due now)
        # latest valid 0x42 decode with its receive time, kept even when the following 0x44 fails (datasheet
        # checks and energy; what is published is unaffected)
        self.sample: Sample | None = None
        self.extended_status: dict | None = None
        self.extended_status_ts = 0.0
        self.cell_count: int | None = None
        # monotonic time of the last successful core read (0x42 + 0x44), for the health check
        self.core_success = 0.0
        self.maint = MaintSupport(address, config.multi_pack) if config.maint_firmware else None
        self._uart1_parameters_ts = 0.0
        self.clock_value: dict | None = None
        self.clock_ts = 0.0
        self.clock_next = 0.0
        self._uart1_parameters_read_at: str | None = None
        self.energy = EnergyGuard(address, store if store is not None else StateStore(None))

    # --- request helpers ----------------------------------------------------------------------

    def _request(self, cid2: int, expected: ExpectedLength, decoder: Callable[[bytes], dict], label: str,
                 info: bytes = b"\x01") -> dict | None:
        payload = self.bus.request(self.address, cid2, expected, label, info)
        if payload is None:
            return None
        feedback = decoder(payload)
        logger.info("Pack%s:%s received", self.address, label)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Pack%s:%s: %s", self.address, label, json.dumps(feedback, indent=2))
        return feedback

    def _decode_telesignal(self, payload: bytes) -> dict:
        if payload[2] != self.cell_count:
            raise ValueError(f"0x44 reports {payload[2]} cells, 0x42 {self.cell_count}")
        return alarms.decode(payload)

    def _decode_telemetry(self, payload: bytes) -> dict:
        # full_energy here uses the connection's nominal voltage; the app derives the final value from the
        # sample and the trusted assignment (cells.evaluate.full_energy)
        feedback = telemetry.decode(payload, self.config.min_cell_voltage, self.config.max_cell_voltage, self.config.nominal_cell_voltage)
        self.energy.apply(feedback["normal"])
        self.cell_count = payload[2]
        return feedback

    # --- static device info -------------------------------------------------------------------

    def _fetch_device_info(self) -> None:
        """
        Fetch static device info once: device name, firmware version, active CAN protocol (0x51)
        and on RS485-1/2 the battery (0xA2) and BMS (0xA4) serial numbers. Failures don't affect the
        regular poll; gives up after INFO_MAX_ATTEMPTS polls. A missing serial number doesn't
        discard the device info.
        """
        if self.info is not None or self.info_attempts >= self.INFO_MAX_ATTEMPTS:
            return
        self.info_attempts += 1

        feedback = self._request(0x51, DEVICE_INFO_LEN, identity.decode_device_info, "DeviceInfo", info=b"")
        if feedback is None:
            if self.info_attempts >= self.INFO_MAX_ATTEMPTS:
                logger.warning("Pack%s:Giving up on device info", self.address)
            return
        info: dict = {"normal": dict(feedback["normal"])}

        # 0xA2/0xA4 are only served on the RS485-1/2 port (UART0); the RS485 bus on the CAN port
        # (UART1) only knows 0x42/0x44/0x4F/0x51/0x60-0x63
        if not self.config.multi_pack:
            for cid2, key, label in ((0xA2, "serial_number", "SerialNumber"), (0xA4, "bms_serial_number", "BmsSerialNumber")):
                serial = self._request(cid2, SERIAL_LEN, lambda raw, k=key: identity.decode_serial_number(raw, k), label, info=b"")
                if serial is not None:
                    info["normal"].update(serial["normal"])

        info["normal"]["firmware_patch"] = self._derive_firmware_patch(info["normal"]["firmware_version"])
        self.info = info
        self.info_revision += 1

    def _derive_firmware_patch(self, firmware_version: str) -> str:
        """
        The firmware only reports major.minor (e.g. 16.06), so the patch level is derived from
        features that only exist since a certain patch level.
        """
        if self.maint is not None and self.maint.identity is not None:
            # reported by the MAINT image itself, no probing needed
            return self.maint.identity.base_version
        if firmware_version == "16.04":
            return "16.04.x"
        if firmware_version != "16.06":
            return f"{firmware_version} (unknown)"
        if self.config.multi_pack:
            return self._derive_patch_can_port()
        return self._derive_patch_pack_bus()

    def _derive_patch_pack_bus(self) -> str:
        """
        RS485-1/2 port (UART0, 19200 baud): CID2 0xAA exists since 16.06.13. Older versions
        answer it with RTN 0x04, 16.06.13+ doesn't answer (0xAA tunnels a request to the device
        at the RS232/binary port; if one is attached, a late reply with 6 bytes may follow,
        which the regular frame validation discards).
        Silence only counts if the pack answers a 0x51 right afterwards; any other answer (another
        RTN, a corrupt frame) makes the result unknown instead of guessing.
        """
        silent_rounds = 0
        for _ in range(3):
            rtn = self.bus.request_rtn(self.address, 0xAA)
            if rtn == 0x04:
                return "16.06.03-16.06.12"
            if rtn != NO_ANSWER:
                logger.debug("Pack%s:Patch probe 0xAA: unexpected answer %s", self.address, rtn)
                return "16.06 (unknown)"
            if self.bus.request_rtn(self.address, 0x51) == 0x00:
                silent_rounds += 1
        return "16.06.13+" if silent_rounds == 3 else "16.06 (unknown)"

    def _derive_patch_can_port(self) -> str:
        """
        RS485 bus on the CAN port (UART1, 9600 baud). Only the master pack (address 0) serves
        the Pylontech system commands and Modbus RTU there, so slaves can't be classified.
        - Pylontech 0x60 at ADR 0x02: accepted since 16.06.12 (before only ADR 0x12),
          0x60 itself exists since 16.06.04, 0x61 since 16.04
        - Modbus FC03 0x001F x9: since 16.06.13; Modbus FC04 0x0000 x21: since 16.06.11
        - Modbus FC03 0x9000: 16.06.13 only accepts 26 registers, before only 21 (fallback check)
        """
        if self.address != 0:
            return "16.06 (unknown, only derivable for the master)"

        if self.bus.request_rtn(0x02, 0x60) == 0x00:
            # Modbus answers can get lost on the shared bus, so retry and cross-check
            for _ in range(3):
                result = self.bus.probe_modbus(0x03, 0x001F, 9)
                logger.debug("Pack%s:Patch probe Modbus 0x001F x9: %s", self.address, result)
                if result is True:
                    return "16.06.13+"
                if result is False:
                    return "16.06.12"
                result = self.bus.probe_modbus(0x03, 0x9000, 21)
                logger.debug("Pack%s:Patch probe Modbus 0x9000 x21: %s", self.address, result)
                if result is True:
                    return "16.06.12"
                if result is False:
                    return "16.06.13+"
            return "16.06.12+"

        if self.bus.request_rtn(0x12, 0x60) == 0x00:
            result = self.bus.probe_modbus(0x04, 0x0000, 21)
            if result is True:
                return "16.06.11"
            if result is False:
                return "16.06.04-16.06.05"
            return "16.06.04-16.06.11"

        if self.bus.request_rtn(0x12, 0x61) == 0x00:
            return "16.06.03"
        return "16.06 (unknown)"

    # --- RS485-1/2 only: extended status (0x5A) and parameters (0x47) ---------------------------

    def _fetch_extended_status(self) -> dict | None:
        payload = self.bus.request_intra_pack(self.address)
        return intrapack.decode_extended_status(payload) if payload is not None else None

    def _fetch_parameters(self) -> None:
        """Fetch the parameters (CID2 0x47) at startup and then every PARAMETER_REFRESH_SECONDS."""
        now = time.monotonic()
        if now < self.parameters_next:
            return
        self.parameters_next = now + self.PARAMETER_REFRESH_SECONDS
        try:
            feedback = self._request(
                0x47, PARAMETER_LEN, lambda raw: {"normal": parameters.decode_ascii_answer(raw)}, "Parameters", info=b""
            )
        except ValueError as e:
            logger.warning("Pack%s:Invalid parameter answer: %s", self.address, e)
            feedback = None
        if feedback is not None:
            self.parameters = {**feedback["normal"], "read_at": timeutil.now_iso()}
            self.parameters_ts = time.monotonic()
        else:
            # retry after a minute instead of waiting for the next regular refresh
            self.parameters_next = time.monotonic() + 60

    # --- MAINT custom firmware (optional, read-only) ----------------------------------------------

    def _guarded(self, step) -> None:
        """Optional MAINT reads never fail the regular poll."""
        try:
            step(self.bus)
        except Exception as e:
            logger.warning("Pack%s:MAINT read failed: %s", self.address, e)

    def _energy_source_key(self) -> str:
        return f"pack{self.address}.energy_source"

    def _merge_maint(self, data: BatteryData) -> None:
        maint = self.maint
        store = self.energy.store
        if maint.state == "absent" and store.get(self._energy_source_key()):
            store.set_durable(self._energy_source_key(), None)   # confirmed OEM now (e.g. replaced pack)
        if maint.identity is None:
            if store.get(self._energy_source_key()) == "maint":
                # the totals came from the wide MAINT counters before: never fall back to the narrow
                # 0x42 values on the same entities while the identity is unknown
                data["telemetry"]["normal"].update(energy_charged=None, energy_discharged=None)
            return
        if self.info is not None and self.info["normal"].get("firmware_patch") != maint.identity.base_version:
            self.info["normal"]["firmware_patch"] = maint.identity.base_version
            self.info_revision += 1
        if self.parameters is not None and not self.config.multi_pack:
            maint.group0 = int(self.parameters["function_switches"][:2], 16)
        elif maint.parameters.value is not None:
            maint.group0 = int(maint.parameters.value["function_switches"][:2], 16)
        payload = maint.payload()
        if payload is None:
            return
        data["maint"] = payload
        energy = maint.live_energy()
        if energy is not None:
            # the source choice must be on disk before the first wide value is published
            if store.set_durable(self._energy_source_key(), "maint"):
                # full-width counters replace the 0x42 values with their 6500 kWh fallback
                data["telemetry"]["normal"].update(energy)
            else:
                logger.warning("Pack%s:Could not persist the MAINT energy source, energy unknown until it is", self.address)
                data["telemetry"]["normal"].update(energy_charged=None, energy_discharged=None)
        if self.config.multi_pack:
            if maint.parameters.value is not None:
                if maint.parameters.ts != self._uart1_parameters_ts:
                    self._uart1_parameters_ts = maint.parameters.ts
                    self._uart1_parameters_read_at = timeutil.now_iso()
                data["parameters"] = {**parameters.public_view(maint.parameters.value), "read_at": self._uart1_parameters_read_at}
            if maint.serials and "info" in data:
                data["info"] = {"normal": {**data["info"]["normal"], **{k: v for k, v in maint.serials.items() if v}}}

    # --- BMS clock (0x4D): OEM on RS485-1/2, with MAINT also on UART1 --------------------------------

    CLOCK_INTERVAL = 600

    def _clock_readable(self) -> bool:
        if not self.config.multi_pack:
            return True
        return self.maint is not None and "maint_rtc" in self.maint.capabilities

    def _read_clock(self) -> None:
        now = time.monotonic()
        if not self._clock_readable() or now < self.clock_next:
            return
        answer = self.bus.query(self.address, clock.CID2, b"")
        if not isinstance(answer, int) and answer is not None and answer.code == 0x00 and len(answer.info) == clock.LEN:
            try:
                bms_time = clock.decode(answer.info)
            except ValueError as e:
                logger.debug("Pack%s:Invalid clock answer: %s", self.address, e)
            else:
                host = timeutil.local_now()
                self.clock_value = {"bms_time": bms_time.isoformat(), "clock_offset": round((bms_time - host).total_seconds())}
                self.clock_ts, self.clock_next = now, now + self.CLOCK_INTERVAL
                return
        self.clock_next = now + 60

    def _clock_payload(self) -> dict | None:
        if not self._clock_readable():
            return None
        fresh = self.clock_value is not None and time.monotonic() - self.clock_ts <= 3 * self.CLOCK_INTERVAL
        return {"normal": self.clock_value if fresh else {"bms_time": None, "clock_offset": None}}

    def parameter_sample(self) -> Sample | None:
        """The readable parameters with their read time: 0x47 on RS485-1/2, MAINT binary 47 on UART1. Stale after
        two missed refreshes of the cadence."""
        if not self.config.multi_pack:
            if self.parameters is None:
                return None
            return Sample(self.parameters, self.parameters_ts, 3 * self.PARAMETER_REFRESH_SECONDS)
        maint = self.maint
        if maint is None or maint.parameters.value is None:
            return None
        return Sample(maint.parameters.value, maint.parameters.ts, 3 * maint.parameter_interval)

    @property
    def capabilities(self) -> set[str]:
        return self.maint.capabilities if self.maint is not None else set()

    @property
    def capabilities_resolved(self) -> bool:
        """MAINT detected or confirmed absent; pending/silent leaves the discovery catalog untouched."""
        return self.maint is not None and self.maint.state in ("present", "absent")

    # --- poll -----------------------------------------------------------------------------------

    def read(self) -> tuple[BatteryData | None, bool]:
        """
        Poll the pack. Returns (data, success); data is None if the poll failed. Change suppression
        happens when publishing.
        """
        logger.info("Pack%s:Requesting data...", self.address)
        data: BatteryData = {"telemetry": {}, "telesignalization": {}}
        try:
            telemetry_feedback = self._request(0x42, TELEMETRY_LEN, self._decode_telemetry, "Telemetry")
            if telemetry_feedback is None:
                return None, False
            data["telemetry"] = telemetry_feedback
            self.sample = Sample(dict(telemetry_feedback["normal"]), time.monotonic())

            telesignal_feedback = self._request(0x44, TELESIGNAL_LEN, self._decode_telesignal, "Telesignalization")
            if telesignal_feedback is None:
                return None, False
            data["telesignalization"] = telesignal_feedback
            self.core_success = time.monotonic()

            if self.maint is not None:
                self._guarded(self.maint.detect)
            self._fetch_device_info()
            if self.info:
                data["info"] = self.info
            if self.maint is not None:
                self._guarded(self.maint.poll)
                self._merge_maint(data)
            self._guarded(lambda _bus: self._read_clock())
            clock_payload = self._clock_payload()
            if clock_payload is not None:
                data["clock"] = clock_payload

            if not self.config.multi_pack:
                extended_status = self._fetch_extended_status()
                now = time.monotonic()
                if extended_status is not None:
                    self.extended_status = extended_status
                    self.extended_status_ts = now
                # bridge single failed requests, but never present an old (or a previous run's) status as current
                fresh = now - self.extended_status_ts <= max(STATUS_MAX_AGE_SECONDS, 3 * self.config.mqtt_update_interval)
                data["status"] = self.extended_status if self.extended_status is not None and fresh else UNKNOWN_STATUS
                self._fetch_parameters()
                if self.parameters:
                    data["parameters"] = parameters.public_view(self.parameters)

            return data, True
        except Exception as e:
            logger.error("Pack%s:Error reading serial data: %s", self.address, e)
            return None, False
