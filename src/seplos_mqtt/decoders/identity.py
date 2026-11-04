"""Device identity: CID2 0x51 device info, 0xA2 serial number."""
from __future__ import annotations

from .common import text


def decode_device_info(raw: bytes) -> dict:
    """0x51, 32 bytes: device name (10), firmware major/minor (2), active CAN inverter protocol (20)."""
    return {
        "normal": {
            "device_name": text(raw[0:10]),
            "firmware_version": f"{raw[10]}.{raw[11]:02d}",
            "can_protocol": text(raw[12:32]),
        }
    }


def decode_serial_number(raw: bytes) -> dict:
    """0xA2, 30 bytes ASCII."""
    return {"normal": {"serial_number": text(raw[0:30])}}


def device_firmware(info: dict) -> str:
    """Firmware for the HA device info: the derived patch level if determined, else major.minor."""
    normal = info.get("normal", {})
    version = normal.get("firmware_version") or "unknown"
    patch = normal.get("firmware_patch") or ""
    if patch.startswith(version) and " " not in patch:
        return patch
    return version
