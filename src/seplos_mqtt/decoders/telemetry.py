"""CID2 0x42 telemetry (analog values)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .common import KELVIN_OFFSET, s16, u16

logger = logging.getLogger("SeplosBMS.telemetry")

# Byte offsets in the binary INFO for 16 cells (DATA FLAG, address, cell count, 16 cells, temperature count …)
# name: (offset, scale, rounding, bias, signed, amount)
_FIELDS: dict[str, tuple[int, float, int | None, int, bool, int]] = {
    "voltage_cell":           (3,  1 / 1000, 3, 0, False, 16),           # doc: "Voltage of cell 1..M"
    "cell_temperature":       (36, 1 / 10, 1, -KELVIN_OFFSET, False, 4),  # doc: "Cell temperature 1..4"
    "ambient_temperature":    (44, 1 / 10, 1, -KELVIN_OFFSET, False, 1),  # doc: "Environment temperature"
    "components_temperature": (46, 1 / 10, 1, -KELVIN_OFFSET, False, 1),  # doc: "Power temperature" (MOSFET)
    "dis_charge_current":     (48, 1 / 100, 2, 0, True, 1),               # doc: "Charge/discharge current", 0 when idle
    "total_pack_voltage":     (50, 1 / 100, 2, 0, False, 1),              # doc: "Total battery voltage"
    "residual_capacity":      (52, 1 / 100, 2, 0, False, 1),              # doc: "Residual capacity"
    "full_capacity":          (55, 1 / 100, 1, 0, False, 1),              # doc: "Battery capacity", learned full capacity
    "state_of_charge":        (57, 1 / 10, 1, 0, False, 1),               # doc: "SOC"
    "rated_capacity":         (59, 1 / 100, 1, 0, False, 1),              # doc: "Rated capacity", setting P58
    "charging_cycles":        (61, 1, None, 0, False, 1),                 # doc: "Number of cycles"
    # offset 63: SOH - not evaluated, the firmware always sends 1000 (100.0 %)
    "port_voltage":           (65, 1 / 100, 2, 0, False, 1),              # doc: "Port voltage"
    # The four "Reservation" fields of the doc are filled by the firmware:
    # 1: reference/offset channel of the current measurement in mA (drift compensation)
    # 2: offset corrected current in mA, only sent while idle (0 while charging/discharging)
    # 3/4: charged/discharged energy in 0.1 kWh; the firmware sends 6500 once a counter reaches 65000
    "current_offset":         (67, 1, None, 0, True, 1),
    "current_idle":           (69, 1 / 1000, 3, 0, True, 1),
    "energy_charged":         (71, 1 / 10, 1, 0, False, 1),
    "energy_discharged":      (73, 1 / 10, 1, 0, False, 1),
}

ENERGY_KEYS = ("energy_charged", "energy_discharged")
ENERGY_FALLBACK_KWH = 650.0


def _value(raw: bytes, offset: int, scale: float, rounding: int | None, bias: int, signed: bool):
    number = s16(raw, offset) if signed else u16(raw, offset)
    value = (number + bias) * scale
    return round(value, rounding) if rounding is not None else value


def decode(info: bytes, min_cell_voltage: float, max_cell_voltage: float) -> dict:
    """Decode the 0x42 INFO into {"normal": {...}} incl. calculated values."""
    number_of_cells = info[2]
    feedback: dict = {
        "cell_voltage_setting_min": min_cell_voltage,
        "cell_voltage_setting_max": max_cell_voltage,
        "pack_voltage_setting_min": min_cell_voltage * number_of_cells,
        "pack_voltage_setting_max": max_cell_voltage * number_of_cells,
    }
    lists: dict[str, list] = {}
    for name, (offset, scale, rounding, bias, signed, amount) in _FIELDS.items():
        if amount > 1:
            count = number_of_cells if name == "voltage_cell" else amount
            values = [_value(info, offset + 2 * i, scale, rounding, bias, signed) for i in range(count)]
            lists[name] = values
            for i, value in enumerate(values):
                feedback[f"{name}_{i + 1}"] = value
        else:
            feedback[name] = _value(info, offset, scale, rounding, bias, signed)

    # 0x42 reports 0 while idle, the idle current (1 mA resolution) comes in "Reservation" 2
    current = feedback["dis_charge_current"] or feedback["current_idle"]
    feedback["current"] = current
    feedback.pop("current_idle")

    cells = lists["voltage_cell"]
    temps = lists["cell_temperature"]
    lowest_idx, lowest = min(enumerate(cells), key=lambda x: x[1])
    highest_idx, highest = max(enumerate(cells), key=lambda x: x[1])
    feedback.update({
        # uses the combined current, so the power isn't 0 while idle
        "dis_charge_power": round((current or 0) * feedback["total_pack_voltage"], 2),
        "average_cell_voltage": round(sum(cells) / len(cells), 3),
        "lowest_cell": lowest_idx + 1,      # 1-based for display
        "lowest_cell_voltage": lowest,
        "highest_cell": highest_idx + 1,
        "highest_cell_voltage": highest,
        "delta_cell_voltage": round(highest - lowest, 3),
        "delta_cell_temperature": round(max(temps) - min(temps), 1),
    })
    return {"normal": feedback}


@dataclass
class EnergyGuard:
    """
    The firmware sends 6500 (650.0 kWh) once a counter reaches 65000 (6500 kWh).
    Keep the last valid value instead of publishing a false reset.
    """
    pack_address: int
    last: dict[str, float] = field(default_factory=dict)
    overflow_logged: bool = False

    def apply(self, normal: dict) -> None:
        for key in ENERGY_KEYS:
            value = normal[key]
            last = self.last.get(key)
            if value == ENERGY_FALLBACK_KWH and last is not None and last > ENERGY_FALLBACK_KWH:
                if not self.overflow_logged:
                    logger.warning("Pack%s:Energy counter exceeded 6500 kWh, the BMS no longer reports it", self.pack_address)
                    self.overflow_logged = True
                value = last
            self.last[key] = value
            normal[key] = value
