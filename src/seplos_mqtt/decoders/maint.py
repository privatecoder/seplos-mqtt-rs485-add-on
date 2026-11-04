"""
Read-only answers of the MAINT 1.5.4 / 1.6.4 custom firmware (KB mod/docs/firmware:
remote-diagnostics.md, storage-and-reset.md, openbms-monitor.md). ASCII CID2 0x64 with INFO:
03 identity (MNT05), 04 storage diagnostics, 06 persisted capacity and live energy.
"""
from __future__ import annotations

from dataclasses import dataclass

CID2 = 0x64
INFO_IDENTITY = b"\x03"
INFO_STORAGE = b"\x04"
INFO_ENERGY = b"\x06"
IDENTITY_LEN = 21      # MNT05
IDENTITY_LEN_MNT04 = 19
STORAGE_LEN = 10
ENERGY_LEN = 68

# image flags (identity byte 18)
FLAG_UART1_POWER_RTC = 0x02
FLAG_PACK_LOCK = 0x04
FLAG_POSITIVE_POLARITY = 0x08
FLAG_UART0_MARKER = 0x10
FLAG_STORAGE_DIAGNOSTICS = 0x20
FLAG_CHECKPOINT_BEFORE_RESET = 0x40
FLAG_CONFIGURABLE_HARD_STOP = 0x80
# MNT05 read-capability mask (bytes 19-20)
CAP_UART1_HISTORY = 0x0001
CAP_CALIBRATION = 0x0002
CAP_PERSISTED_SOC = 0x0004
CAP_LIVE_ENERGY = 0x0008
KNOWN_CAPS = 0x000F

RECORD_CLASS = ("erased", "valid", "invalid")
COMPARISON = ("not comparable", "identical", "different")
BOOT_CHOICE = ("fallback", "primary", "backup")


class MaintFormatError(ValueError):
    pass


@dataclass(frozen=True)
class Identity:
    info_format: int
    base_version: str          # reported OEM base, revision "?" if unknown
    patch_version: str         # e.g. "1.6.4"
    build_id: str
    image_flags: int
    capabilities: int          # 0 for MNT04

    def has_flag(self, flag: int) -> bool:
        return bool(self.image_flags & flag)

    def has_capability(self, cap: int) -> bool:
        return bool(self.capabilities & cap)

    def as_dict(self) -> dict:
        return {
            "info_format": self.info_format,
            "patch_version": self.patch_version,
            "base_version": self.base_version,
            "build_id": self.build_id,
            "image_flags": f"0x{self.image_flags:02X}",
            "capabilities": f"0x{self.capabilities:04X}",
        }


def decode_identity(info: bytes) -> Identity:
    """MNT05 (21 bytes, current releases) or MNT04 (19 bytes, no capability mask)."""
    if info[:3] != b"MNT" or (len(info), info[3:4]) not in ((IDENTITY_LEN, b"\x05"), (IDENTITY_LEN_MNT04, b"\x04")):
        raise MaintFormatError(f"unknown identity format: {info.hex()}")
    capabilities = int.from_bytes(info[19:21], "big") if info[3] == 5 else 0
    if capabilities & ~KNOWN_CAPS:
        raise MaintFormatError(f"unknown capability bits 0x{capabilities:04X}")
    revision = "?" if info[6] == 0xFF else f"{info[6]:02d}"
    return Identity(
        info_format=info[3],
        base_version=f"{info[4]}.{info[5]:02d}.{revision}",
        patch_version=".".join(str(b) for b in info[7:10]),
        build_id=info[10:18].hex(),
        image_flags=info[18],
        capabilities=capabilities,
    )


def decode_storage(info: bytes) -> dict:
    """64/04 format 01: parameter and SOC flash mirrors, 1.6.4 lock state."""
    if (len(info) != STORAGE_LEN or info[0] != 1 or any(info[i] > 2 for i in range(1, 7))
            or any(info[i] not in (0x00, 0x01, 0xFF) for i in (7, 8)) or info[9] & ~0x07):
        raise MaintFormatError(f"unknown storage diagnostics: {info.hex()}")

    def lock(value: int) -> bool | None:
        return None if value == 0xFF else bool(value)   # FF: unavailable (1.5.4) or unknown, not unlocked

    cache_valid = bool(info[9] & 0x04)
    return {
        "parameters": {"primary": RECORD_CLASS[info[1]], "backup": RECORD_CLASS[info[2]], "comparison": COMPARISON[info[3]]},
        "soc": {"primary": RECORD_CLASS[info[4]], "backup": RECORD_CLASS[info[5]], "comparison": COMPARISON[info[6]]},
        "lock_recorded_primary": lock(info[7]),
        "lock_recorded_backup": lock(info[8]),
        "lock_runtime": bool(info[9] & 0x01) if cache_valid else None,
        "lock_cache_consistent": bool(info[9] & 0x02) if cache_valid else None,
    }


def storage_problem(storage: dict) -> bool:
    """A mirror record with a broken structure/CRC. 'different' alone doesn't say which copy is newer."""
    return any(storage[group][copy] == "invalid" for group in ("parameters", "soc") for copy in ("primary", "backup"))


def decode_energy(info: bytes) -> dict:
    """64/06 format 01: live snapshot (big endian) and the two persisted SOC records (summary)."""
    if len(info) != ENERGY_LEN or info[0] != 1 or any(info[i] > 2 for i in (1, 2, 3, 5)) or info[4] & ~0x03:
        raise MaintFormatError(f"unknown energy/checkpoint answer: {info[:6].hex()}")

    def be(offset: int, size: int = 2) -> int:
        return int.from_bytes(info[offset:offset + size], "big")

    return {
        "live": {
            "energy_charged": round(be(10, 4) / 10, 1),       # kWh, full 32-bit width, no 6500 fallback
            "energy_discharged": round(be(14, 4) / 10, 1),
            "remaining_capacity": round(be(18) / 100, 2),
            "full_capacity": round(be(20) / 100, 2),
            "state_of_charge": round(be(22) / 10, 1),
        },
        "persisted": {
            "primary": RECORD_CLASS[info[1]],
            "backup": RECORD_CLASS[info[2]],
            "comparison": COMPARISON[info[3]],
            "crc_accepted": {"primary": bool(info[4] & 1), "backup": bool(info[4] & 2)},
            "boot_choice": BOOT_CHOICE[info[5]],
        },
    }


# --- stage 2: maintenance marker, Group0 settings, calibration, RTC -----------------------------

INFO_MARKER = b"\x02"
MARKER_LEN = 5
INFO_CALIBRATION_ACTIVE = b"\x05\x00"
CALIBRATION_ACTIVE_LEN = 72


def decode_marker(info: bytes) -> dict:
    """64/02: little-endian marker (FFFFFFFF inactive, 00000000 active) and effective slave count."""
    if len(info) != MARKER_LEN:
        raise MaintFormatError(f"unknown marker answer: {info.hex()}")
    marker = int.from_bytes(info[:4], "little")
    state = {0xFFFFFFFF: False, 0: True}.get(marker)     # anything else: unresolved, never "active"
    return {"maintenance_active": state, "marker": f"0x{marker:08X}", "effective_slave_count": info[4]}


def decode_group0(group0: int, ident: Identity) -> dict:
    """
    MAINT meanings of function switch BitGroup0 (OEM: only bit 1 has a function). Configuration, not
    live state: the lock state itself comes from 64/04, the recharge stop also needs Group5 bit 1.
    """
    out: dict = {
        "uart1_parameter_write_allowed": bool(group0 & 0x01),
        "uart1_maintenance_start_allowed": bool(group0 & 0x04),
        # bit 3 = 1 means "permitted" only if the image has the feature (0x02) with positive polarity (0x08)
        "uart1_power_rtc_allowed": (bool(group0 & 0x08)
                                    if ident.has_flag(FLAG_UART1_POWER_RTC) and ident.has_flag(FLAG_POSITIVE_POLARITY) else None),
    }
    if ident.has_flag(FLAG_PACK_LOCK) and ident.has_flag(FLAG_POSITIVE_POLARITY):
        out["extended_off_enabled"] = bool(group0 & 0x10)
        out["pack_lock_recorded"] = not group0 & 0x20      # bit 5 = 0: individual lock active
    if ident.has_flag(FLAG_CONFIGURABLE_HARD_STOP):
        selection = group0 & 0xC0
        if selection in (0x00, 0x40):
            out["recharge_hard_stop"], out["recharge_hard_stop_source"] = selection == 0x40, "parameter"
        else:
            out["recharge_hard_stop"], out["recharge_hard_stop_source"] = bool(ident.image_flags & 0x01), "image default"
    else:
        out["recharge_hard_stop"], out["recharge_hard_stop_source"] = bool(ident.image_flags & 0x01), "image"
    return out


def decode_calibration_active(info: bytes) -> dict:
    """64/05 page 00 (active RAM): invalid calibration bits and the converted currents A/B (mA)."""
    if len(info) != CALIBRATION_ACTIVE_LEN or info[0] != 1 or info[1] != 0:
        raise MaintFormatError(f"unknown calibration answer: {info[:2].hex()}")
    return {
        "invalid_calibration_flags": f"0x{info[2] & 0x1C:02X}",
        "current_a_ma": int.from_bytes(info[64:68], "big", signed=True),
        "current_b_ma": int.from_bytes(info[68:72], "big", signed=True),
    }
