"""CID2 0x5A intra-pack record (master <-> slaves on RS485-1/2), 32 byte INFO."""
from __future__ import annotations

from .common import on_off, s16, u16


def decode_record(raw: bytes) -> dict:
    """10 x u16 measurements followed by 10 status bytes (bytes 0/1: DATA FLAG 00, constant 01)."""
    return {
        "highest_cell_voltage": u16(raw, 2) / 1000,
        "lowest_cell_voltage": u16(raw, 4) / 1000,
        "highest_cell_temperature": round((u16(raw, 6) - 2731) / 10, 1),
        "lowest_cell_temperature": round((u16(raw, 8) - 2731) / 10, 1),
        "current": s16(raw, 10) / 100,
        "pack_voltage": u16(raw, 12) / 100,
        "residual_capacity": u16(raw, 14) / 100,
        "full_capacity": u16(raw, 16) / 100,
        "state_of_charge": u16(raw, 18) / 10,
        "port_voltage": u16(raw, 20) / 100,
        "alarm_events_1_6": raw[22:28].hex(" ").upper(),
        "switch_status": f"{raw[28]:02X}",
        "alarm_event_7": f"{raw[29]:02X}",   # bit0 balancing active, bit1 balancing blocked/not needed
        "alarm_event_8": f"{raw[30]:02X}",
        "system_status": f"{raw[31]:02X}",
    }


def decode_extended_status(raw: bytes) -> dict:
    """The values of the intra-pack record that aren't available via 0x42/0x44."""
    record = decode_record(raw)
    alarm_event_7 = raw[29]
    alarm_event_8 = raw[30]
    return {
        "normal": {
            # not zeroed in idle like the current of 0x42
            "dis_charge_current_unfiltered": record["current"],
            "highest_cell_temperature": record["highest_cell_temperature"],
            "lowest_cell_temperature": record["lowest_cell_temperature"],
        },
        "binary": {
            "balancing_active": on_off(alarm_event_7 & 0x01),
            "balancing_blocked": on_off(alarm_event_7 & 0x02),          # blocked or not needed
            "balancing_time_limit": on_off(alarm_event_7 & 0x04),
            "balancing_temperature_blocked": on_off(alarm_event_7 & 0x08),
            "charge_forbidden": on_off(alarm_event_8 & 0x40),           # drives the charge MOSFET
        },
    }
