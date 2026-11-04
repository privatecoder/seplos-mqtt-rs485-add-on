"""CID2 0x42 telemetry (analog values)."""
from __future__ import annotations

from .common import KELVIN_OFFSET, s16, u16

# The layout depends on the cell count M (byte 2): cell voltages at 3..2+2M, all later fields
# relative to BASE = 3 + 2M (for 16 cells BASE = 35).
CELL_OFFSET = 3
# name: (offset relative to BASE, scale, rounding, bias, signed, amount)
_FIELDS: dict[str, tuple[int, float, int | None, int, bool, int]] = {
    # BASE+0: number of temperatures (6)
    "cell_temperature":       (1, 1 / 10, 1, -KELVIN_OFFSET, False, 4),  # doc: "Cell temperature 1..4" (<= 8 cells: 3/4 copy 2)
    "ambient_temperature":    (9, 1 / 10, 1, -KELVIN_OFFSET, False, 1),  # doc: "Environment temperature"
    "components_temperature": (11, 1 / 10, 1, -KELVIN_OFFSET, False, 1), # doc: "Power temperature" (MOSFET)
    "dis_charge_current":     (13, 1 / 100, 2, 0, True, 1),              # doc: "Charge/discharge current", 0 when idle
    "total_pack_voltage":     (15, 1 / 100, 2, 0, False, 1),             # doc: "Total battery voltage"
    "residual_capacity":      (17, 1 / 100, 2, 0, False, 1),             # doc: "Residual capacity"
    # BASE+19: number of user defined values (10)
    "full_capacity":          (20, 1 / 100, 2, 0, False, 1),             # doc: "Battery capacity", learned full capacity
    "state_of_charge":        (22, 1 / 10, 1, 0, False, 1),              # doc: "SOC"
    "rated_capacity":         (24, 1 / 100, 2, 0, False, 1),             # doc: "Rated capacity", setting P58
    "charging_cycles":        (26, 1, None, 0, False, 1),                # doc: "Number of cycles"
    # BASE+28: SOH - not evaluated, the firmware always sends 1000 (100.0 %)
    "port_voltage":           (30, 1 / 100, 2, 0, False, 1),             # doc: "Port voltage"
    # The four "Reservation" fields of the doc are filled by the firmware:
    # 1: reference/offset channel of the current measurement in mA (drift compensation)
    # 2: offset corrected current in mA, only sent while idle (0 while charging/discharging)
    # 3/4: charged/discharged energy in 0.1 kWh; the firmware sends 6500 once a counter reaches 65000
    "current_offset":         (32, 1, None, 0, True, 1),
    "current_idle":           (34, 1 / 1000, 3, 0, True, 1),
    "energy_charged":         (36, 1 / 10, 1, 0, False, 1),
    "energy_discharged":      (38, 1 / 10, 1, 0, False, 1),
}


def info_length(cell_count: int) -> int:
    """Binary INFO length of the 0x42 answer."""
    return 43 + 2 * cell_count


ENERGY_KEYS = ("energy_charged", "energy_discharged")
ENERGY_FALLBACK_KWH = 650.0


def _value(raw: bytes, offset: int, scale: float, rounding: int | None, bias: int, signed: bool):
    number = s16(raw, offset) if signed else u16(raw, offset)
    value = (number + bias) * scale
    return round(value, rounding) if rounding is not None else value


def full_energy(capacity_ah: float | None, series_cells: int | None, nominal_cell_voltage: float | None) -> float | None:
    """Full energy in kWh from a capacity in Ah, the series cell count and the nominal cell voltage."""
    if capacity_ah is None or not series_cells or not nominal_cell_voltage:
        return None
    return round(capacity_ah * series_cells * nominal_cell_voltage / 1000, 2)


def decode(info: bytes, min_cell_voltage: float, max_cell_voltage: float, nominal_cell_voltage: float | None = None) -> dict:
    """Decode the 0x42 INFO into {"normal": {...}} incl. calculated values."""
    cell_count = info[2]
    base = CELL_OFFSET + 2 * cell_count
    feedback: dict = {
        "cell_voltage_setting_min": min_cell_voltage,
        "cell_voltage_setting_max": max_cell_voltage,
        "pack_voltage_setting_min": min_cell_voltage * cell_count,
        "pack_voltage_setting_max": max_cell_voltage * cell_count,
    }
    cells = [_value(info, CELL_OFFSET + 2 * i, 1 / 1000, 3, 0, False) for i in range(cell_count)]
    for i, value in enumerate(cells):
        feedback[f"voltage_cell_{i + 1}"] = value
    temps: list[float] = []
    for name, (offset, scale, rounding, bias, signed, amount) in _FIELDS.items():
        if amount > 1:
            temps = [_value(info, base + offset + 2 * i, scale, rounding, bias, signed) for i in range(amount)]
            for i, value in enumerate(temps):
                feedback[f"{name}_{i + 1}"] = value
        else:
            feedback[name] = _value(info, base + offset, scale, rounding, bias, signed)

    # 0x42 reports 0 while idle, the idle current (1 mA resolution) comes in "Reservation" 2
    current = feedback["dis_charge_current"] or feedback["current_idle"]
    feedback["current"] = current
    feedback.pop("current_idle")

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
        "highest_cell_temperature": max(temps),
        "lowest_cell_temperature": min(temps),
        "delta_cell_temperature": round(max(temps) - min(temps), 1),
        # learned full capacity x series cells x configured nominal cell voltage, kWh
        "full_energy": full_energy(feedback["full_capacity"], cell_count, nominal_cell_voltage),
    })
    return {"normal": feedback}
