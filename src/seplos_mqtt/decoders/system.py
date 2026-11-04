"""Modbus FC03 0x1000 x21: system aggregate of the master (RS485 bus of the CAN port)."""
from __future__ import annotations

from .common import signed16

REGISTER = 0x1000
COUNT = 21
SYSTEM_STATES = {0x01: "Discharging", 0x02: "Charging", 0x10: "Idle", 0x20: "Off"}


def _pack(value: int) -> int | None:
    """Pack numbers are 1-based in the firmware, the add-on counts from 0 (master)."""
    return value - 1 if value > 0 else None


def decode(regs: list[int]) -> dict:
    s = signed16
    voltage = round(regs[4] / 10, 1)
    current = round(s(regs[5]) / 10, 1)
    return {
        "normal": {
            "system_state": SYSTEM_STATES.get(regs[0], f"0x{regs[0]:02X}"),
            "system_voltage": voltage,
            "system_current": current,
            # calculated from the master's system voltage and current (not reported by the BMS)
            "system_power": round(voltage * current, 1),
            "system_soc": regs[6],
            "highest_cell_temperature": round(s(regs[8]) / 10, 1),
            "highest_cell_temperature_pack": _pack(regs[9]),
            "lowest_cell_temperature": round(s(regs[10]) / 10, 1),
            "lowest_cell_temperature_pack": _pack(regs[11]),
            "highest_cell_voltage": round(regs[12] / 1000, 3),
            "highest_cell_voltage_pack": _pack(regs[13]),
            "lowest_cell_voltage": round(regs[14] / 1000, 3),
            "lowest_cell_voltage_pack": _pack(regs[15]),
            "charge_voltage_limit": round(regs[16] / 10, 1),
            "charge_current_limit": round(regs[17] / 10, 1),
            "discharge_current_limit": round(regs[18] / 10, 1),
            "discharge_voltage_limit": round(regs[19] / 10, 1),
            "total_capacity": regs[20],
            # high byte: alarm event 5 (current), low byte: alarm event 2 (voltage), OR of all packs
            "alarm_word_current_voltage": f"0x{regs[2]:04X}",
            # low byte: alarm event 3 (cell temperature), high byte: ambient/power temperature,
            # heating, remaining capacity alarm, cell difference fault, pack communication fault
            "alarm_word_temperature_misc": f"0x{regs[3]:04X}",
        },
        "binary": {
            "charge_allowed": "ON" if regs[1] & 0x02 else "OFF",
            "discharge_allowed": "ON" if regs[1] & 0x01 else "OFF",
            "pack_communication_fault": "ON" if regs[3] & 0x8000 else "OFF",
        },
    }
