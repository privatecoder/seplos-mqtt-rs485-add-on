"""Polling of one battery pack: request scheduling, caching of static data, patch level probing."""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable

from .config import Config
from .decoders import alarms, identity, intrapack, parameters, telemetry
from .transport import SerialBus

logger = logging.getLogger("SeplosBMS.pack")

BatteryData = dict

# INFO length in ASCII characters (16 cells)
TELEMETRY_INFO_LEN = 150
TELESIGNAL_INFO_LEN = 98
DEVICE_INFO_LEN = 64
SERIAL_INFO_LEN = 60
PARAMETER_INFO_LEN = 338

# A pause between consecutive requests is mandatory or the responses get corrupted
REQUEST_GAP_SECONDS = 1


def request_gap() -> None:
    time.sleep(REQUEST_GAP_SECONDS)


class BatteryPack:
    """Reads one pack (DIP address) via the shared bus."""

    # Give up on the (static) device info after this many polls without success
    INFO_MAX_ATTEMPTS = 3
    PARAMETER_REFRESH_SECONDS = 3600

    def __init__(self, address: int, bus: SerialBus, config: Config) -> None:
        self.address = address
        self.bus = bus
        self.config = config
        self.last_status: BatteryData | None = None
        self.info: dict | None = None
        self.info_attempts = 0
        self.parameters: dict | None = None
        self.parameters_ts = 0.0
        self.extended_status: dict | None = None
        self.energy = telemetry.EnergyGuard(address)

    # --- request helpers ----------------------------------------------------------------------

    def _request(self, cid2: int, info_len: int, decoder: Callable[[bytes], dict], label: str, info: bytes = b"01") -> dict | None:
        payload = self.bus.request(self.address, cid2, info_len, label, info)
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
        return feedback

    # --- static device info -------------------------------------------------------------------

    def _fetch_device_info(self) -> None:
        """
        Fetch static device info once: device name, firmware version, active CAN protocol (0x51)
        and serial number (0xA2). Failures don't affect the regular poll; gives up after
        INFO_MAX_ATTEMPTS polls.
        """
        if self.info is not None or self.info_attempts >= self.INFO_MAX_ATTEMPTS:
            return
        self.info_attempts += 1

        requests = [(0x51, DEVICE_INFO_LEN, identity.decode_device_info, "DeviceInfo")]
        # 0xA2 is only served on the RS485-1/2 port (UART0); the RS485 bus on the CAN port
        # (UART1) only knows 0x42/0x44/0x4F/0x51/0x60-0x63
        if not self.config.multi_pack:
            requests.append((0xA2, SERIAL_INFO_LEN, identity.decode_serial_number, "SerialNumber"))

        info: dict = {"normal": {}}
        for cid2, info_len, decoder, label in requests:
            request_gap()
            feedback = self._request(cid2, info_len, decoder, label, info=b"")
            if feedback is None:
                if self.info_attempts >= self.INFO_MAX_ATTEMPTS:
                    logger.warning("Pack%s:Giving up on device info", self.address)
                return
            info["normal"].update(feedback["normal"])

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
        No answer only counts if the pack answers a 0x51 right afterwards.
        """
        silent_rounds = 0
        for _ in range(3):
            request_gap()
            if self.bus.request_rtn(self.address, 0xAA) == 0x04:
                return "16.06.03-16.06.12"
            request_gap()
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

        request_gap()
        if self.bus.request_rtn(0x02, 0x60) == 0x00:
            # Modbus answers can get lost on the shared bus, so retry and cross-check
            for _ in range(3):
                request_gap()
                result = self.bus.probe_modbus(0x03, 0x001F, 9)
                logger.debug("Pack%s:Patch probe Modbus 0x001F x9: %s", self.address, result)
                if result is True:
                    return "16.06.13+"
                if result is False:
                    return "16.06.12"
                request_gap()
                result = self.bus.probe_modbus(0x03, 0x9000, 21)
                logger.debug("Pack%s:Patch probe Modbus 0x9000 x21: %s", self.address, result)
                if result is True:
                    return "16.06.12"
                if result is False:
                    return "16.06.13+"
            return "16.06.12+"

        request_gap()
        if self.bus.request_rtn(0x12, 0x60) == 0x00:
            request_gap()
            result = self.bus.probe_modbus(0x04, 0x0000, 21)
            if result is True:
                return "16.06.11"
            if result is False:
                return "16.06.04-16.06.05"
            return "16.06.04-16.06.11"

        request_gap()
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
        request_gap()
        feedback = self._request(
            0x47, PARAMETER_INFO_LEN, lambda raw: {"normal": parameters.decode_ascii_answer(raw)}, "Parameters", info=b""
        )
        if feedback is not None:
            self.parameters = feedback["normal"]
        else:
            # retry after a minute instead of waiting for the next regular refresh
            self.parameters_ts = time.time() - self.PARAMETER_REFRESH_SECONDS + 60

    # --- poll -----------------------------------------------------------------------------------

    def read(self) -> tuple[BatteryData | None, bool]:
        """
        Poll the pack. Returns (data, success): data is None if nothing changed since the last
        poll or the poll failed.
        """
        logger.info("Pack%s:Requesting data...", self.address)
        data: BatteryData = {"telemetry": {}, "telesignalization": {}}
        try:
            self.bus.flush()

            telemetry_feedback = self._request(0x42, TELEMETRY_INFO_LEN, self._decode_telemetry, "Telemetry")
            if telemetry_feedback is None:
                return None, False
            data["telemetry"] = telemetry_feedback

            request_gap()
            telesignal_feedback = self._request(0x44, TELESIGNAL_INFO_LEN, alarms.decode, "Telesignalization")
            if telesignal_feedback is None:
                return None, False
            data["telesignalization"] = telesignal_feedback

            self._fetch_device_info()
            if self.info:
                data["info"] = self.info

            if not self.config.multi_pack:
                request_gap()
                extended_status = self._fetch_extended_status()
                if extended_status is not None:
                    self.extended_status = extended_status
                # keep the last valid status so the HA templates don't break on a single failed request
                if self.extended_status is not None:
                    data["status"] = self.extended_status
                self._fetch_parameters()
                if self.parameters:
                    data["parameters"] = self.parameters

            if self.last_status is None or self.last_status != data:
                self.last_status = data
                return data, True
            return None, True
        except Exception as e:
            logger.error("Pack%s:Error reading serial data: %s", self.address, e)
            return None, False
