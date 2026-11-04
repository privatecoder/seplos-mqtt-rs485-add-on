"""Polling of one battery pack: request scheduling, caching of static data, patch level probing."""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import datetime

from .config import Config
from .decoders import alarms, identity, intrapack, parameters, telemetry
from .energy import EnergyGuard
from .framing.ascii import ExpectedLength
from .state import StateStore
from .transport import NO_ANSWER, SerialBus

logger = logging.getLogger("SeplosBMS.pack")

BatteryData = dict

MIN_CELLS, MAX_CELLS = 1, 16


def _cell_dependent(length: Callable[[int], int]) -> Callable[[bytes], int | None]:
    """Expected INFO length of 0x42/0x44, derived from the cell count in byte 2."""
    def expected(info: bytes) -> int | None:
        return length(info[2]) if len(info) > 2 and MIN_CELLS <= info[2] <= MAX_CELLS else None
    return expected


# binary INFO lengths
TELEMETRY_LEN: ExpectedLength = _cell_dependent(telemetry.info_length)
TELESIGNAL_LEN: ExpectedLength = _cell_dependent(alarms.info_length)
DEVICE_INFO_LEN = 32
SERIAL_LEN = 30
PARAMETER_LEN = 169

# the extended status (0x5A) is kept over failed requests for at most this long
STATUS_MAX_AGE_SECONDS = 60

def now_iso() -> str:
    """Local time with UTC offset, for HA timestamp sensors."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


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
        self.parameters: dict | None = None
        self.parameters_ts = 0.0
        self.extended_status: dict | None = None
        self.extended_status_ts = 0.0
        self.cell_count: int | None = None
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

    def _decode_telemetry(self, payload: bytes) -> dict:
        feedback = telemetry.decode(payload, self.config.min_cell_voltage, self.config.max_cell_voltage)
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

    def _derive_firmware_patch(self, firmware_version: str) -> str:
        """
        The firmware only reports major.minor (e.g. 16.06), so the patch level is derived from
        features that only exist since a certain patch level.
        """
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
        if time.time() - self.parameters_ts < self.PARAMETER_REFRESH_SECONDS:
            return
        self.parameters_ts = time.time()
        try:
            feedback = self._request(
                0x47, PARAMETER_LEN, lambda raw: {"normal": parameters.decode_ascii_answer(raw)}, "Parameters", info=b""
            )
        except ValueError as e:
            logger.warning("Pack%s:Invalid parameter answer: %s", self.address, e)
            feedback = None
        if feedback is not None:
            self.parameters = {**feedback["normal"], "read_at": now_iso()}
        else:
            # retry after a minute instead of waiting for the next regular refresh
            self.parameters_ts = time.time() - self.PARAMETER_REFRESH_SECONDS + 60

    # --- poll -----------------------------------------------------------------------------------

    def read(self) -> tuple[BatteryData | None, bool]:
        """
        Poll the pack. Returns (data, success); data is None if the poll failed. Change suppression
        happens when publishing.
        """
        logger.info("Pack%s:Requesting data...", self.address)
        data: BatteryData = {"telemetry": {}, "telesignalization": {}}
        try:
            self.bus.flush()

            telemetry_feedback = self._request(0x42, TELEMETRY_LEN, self._decode_telemetry, "Telemetry")
            if telemetry_feedback is None:
                return None, False
            data["telemetry"] = telemetry_feedback

            telesignal_feedback = self._request(0x44, TELESIGNAL_LEN, alarms.decode, "Telesignalization")
            if telesignal_feedback is None:
                return None, False
            data["telesignalization"] = telesignal_feedback

            self._fetch_device_info()
            if self.info:
                data["info"] = self.info

            if not self.config.multi_pack:
                extended_status = self._fetch_extended_status()
                now = time.time()
                if extended_status is not None:
                    self.extended_status = extended_status
                    self.extended_status_ts = now
                # bridge single failed requests, but don't present an old status as current
                if self.extended_status is not None:
                    if now - self.extended_status_ts <= max(STATUS_MAX_AGE_SECONDS, 3 * self.config.mqtt_update_interval):
                        data["status"] = self.extended_status
                    else:
                        data["status"] = {group: dict.fromkeys(values) for group, values in self.extended_status.items()}
                self._fetch_parameters()
                if self.parameters:
                    data["parameters"] = self.parameters

            return data, True
        except Exception as e:
            logger.error("Pack%s:Error reading serial data: %s", self.address, e)
            return None, False
