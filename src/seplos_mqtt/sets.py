"""Values of a parallel set from the latest 0x42 data of its member packs (pure, no I/O)."""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any

KEYS = (
    "current", "power", "soc", "voltage_median", "voltage_range",
    "highest_cell_voltage", "highest_cell_voltage_pack", "lowest_cell_voltage", "lowest_cell_voltage_pack",
    "highest_cell_temperature", "highest_cell_temperature_pack", "lowest_cell_temperature",
    "lowest_cell_temperature_pack", "members_reporting", "sample_age",
)

# inputs every aggregate except the SOC needs from each member
_CORE = ("current", "total_pack_voltage", "highest_cell_voltage", "lowest_cell_voltage",
         "highest_cell_temperature", "lowest_cell_temperature")


@dataclass(frozen=True)
class Member:
    address: int
    reporting: bool                 # online and has data
    age_s: float | None             # age of the member's last sample
    normal: dict[str, Any] | None   # "normal" values of the member's latest 0x42 data


def _num(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _extreme(members: list[Member], key: str, highest: bool, digits: int) -> tuple[float, int]:
    """Value and DIP address of the highest/lowest member value; the lowest address wins a tie
    (``members`` are sorted by address and only a strictly better value replaces the best)."""
    best = members[0]
    for member in members[1:]:
        value, current = member.normal[key], best.normal[key]
        if value > current if highest else value < current:
            best = member
    return round(best.normal[key], digits), best.address


def set_values(members: list[Member]) -> dict[str, Any]:
    """Aggregates of the members. Unknown (None) unless every member reports with valid inputs;
    ``members_reporting`` and ``sample_age`` are always filled."""
    ordered = sorted(members, key=lambda m: m.address)
    out: dict[str, Any] = dict.fromkeys(KEYS)
    reporting = [m for m in ordered if m.reporting]
    ages = [m.age_s for m in reporting if m.age_s is not None]
    out["members_reporting"] = len(reporting)
    out["sample_age"] = round(max(ages), 1) if ages else None
    if not ordered or len(reporting) != len(ordered):
        return out
    if not all(m.normal is not None and all(_num(m.normal.get(k)) for k in _CORE) for m in ordered):
        return out

    voltages = [m.normal["total_pack_voltage"] for m in ordered]
    out["current"] = round(sum(m.normal["current"] for m in ordered), 2)
    out["power"] = round(sum(m.normal["total_pack_voltage"] * m.normal["current"] for m in ordered), 1)
    out["voltage_median"] = round(statistics.median(voltages), 2)
    out["voltage_range"] = round(max(voltages) - min(voltages), 2)
    for key, highest, digits in (
        ("highest_cell_voltage", True, 3), ("lowest_cell_voltage", False, 3),
        ("highest_cell_temperature", True, 1), ("lowest_cell_temperature", False, 1),
    ):
        out[key], out[f"{key}_pack"] = _extreme(ordered, key, highest, digits)

    if all(_num(m.normal.get("state_of_charge")) and _num(m.normal.get("full_capacity"))
           and m.normal["full_capacity"] > 0 for m in ordered):
        capacity = sum(m.normal["full_capacity"] for m in ordered)
        out["soc"] = round(sum(m.normal["state_of_charge"] * m.normal["full_capacity"] for m in ordered) / capacity, 1)
    return out
