"""Status snapshot for the settings app: a plain, JSON-ready dict built by the main loop thread.

The web thread only reads the finished dict (one attribute assignment hands it over), so it never
touches the bus, the packs or any mutable polling state.
"""
from __future__ import annotations

from typing import Any

from .cells import evaluate as cell_report
from .monitoring.model import activity

# fields of the last pack data (decoder keys, canonical values: the HA inversion is not applied)
_PACK_VALUES = ("state_of_charge", "total_pack_voltage", "current", "highest_cell_voltage", "lowest_cell_voltage", "rated_capacity")
_SYSTEM_VALUES = ("system_soc", "system_voltage", "system_current", "system_power", "total_capacity", "total_full_energy", "system_state")


def _normal(data: dict | None, group: str | None = None) -> dict:
    """The ``normal`` values of a group of the pack data (or of the system data when group is None)."""
    if not isinstance(data, dict):
        return {}
    section = data if group is None else data.get(group)
    normal = section.get("normal") if isinstance(section, dict) else None
    return normal if isinstance(normal, dict) else {}


def _maint(pack: Any) -> str:
    if pack.maint is None:
        return "disabled"
    # unknown / unsupported (the MAINT query was inconclusive) count as not yet determined
    return pack.maint.state if pack.maint.state in ("present", "absent") else "pending"


def _maint_version(pack: Any) -> str | None:
    """The MAINT patch version (e.g. "1.6.4") once a MAINT board was identified."""
    maint = pack.maint
    if maint is None or maint.state != "present" or maint.identity is None:
        return None
    return maint.identity.patch_version


def bms_serial(pack_state: Any) -> str | None:
    """The BMS serial (0xA4 string 2, trimmed) once it was read: RS485-1/2 or MAINT UART1; else None.
    The battery serial (0xA2) is never used for identity."""
    pack = pack_state.pack
    info = _normal(pack.info) if pack.info else {}
    value = _normal(pack_state.last_data, "info").get("bms_serial_number") or info.get("bms_serial_number")
    value = value.strip() if isinstance(value, str) else ""
    return value or None


def cells_pending(state: Any, address: int) -> bool:
    """A saved assignment of this pack the main loop has not adopted yet (adopted at the cycle boundary)."""
    pending = getattr(state, "pending_cells", None)
    if pending is None or pending is getattr(state, "cells_adopted", None):
        return False
    return pending[0].get(address) != state.adopted_packs.get(address)


def _cells_status(state: Any, pack_state: Any, now_mono: float) -> dict[str, Any]:
    """Datasheet assignment, both check summaries and the checks of one pack. ``pending`` is always False in
    the snapshot: a save arrives between two snapshots, so ``with_pending`` overlays it at response time."""
    report = cell_report.pack_report(state, pack_state, now_mono)
    out = {key: report[key] for key in ("assignment", "series", "live", "parameters", "sample_age_s", "parameters_age_s")}
    out["pending"] = False
    return out


def with_pending(state: Any, status: dict[str, Any] | None) -> dict[str, Any] | None:
    """The status snapshot as served: for a pack whose saved assignment the main loop has not adopted yet,
    ``pending`` is set and a missing assignment is reported as ``pending`` instead of ``no_assignment``.
    Plain reference reads only; the snapshot itself is not modified."""
    if status is None or not any(cells_pending(state, p["address"]) for p in status.get("packs", ())):
        return status
    packs = []
    for pack in status["packs"]:
        if cells_pending(state, pack["address"]):
            cells = {**pack["cells"], "pending": True}
            for key in ("live", "parameters"):
                if cells[key].get("reason") == "no_assignment":
                    cells[key] = {**cells[key], "reason": "pending"}
            pack = {**pack, "cells": cells}
        packs.append(pack)
    return {**status, "packs": packs}


def _pack_status(state: Any, pack_state: Any, now_mono: float) -> dict[str, Any]:
    pack = pack_state.pack
    info = _normal(pack.info) if pack.info else {}
    values = _normal(pack_state.last_data, "telemetry")
    age = None if pack_state.last_success is None else round(max(0.0, now_mono - pack_state.last_success), 1)
    return {
        "address": pack_state.address,
        "availability": pack_state.availability,
        "last_success_age_s": age,
        "device_name": info.get("device_name"),
        "firmware_version": info.get("firmware_version"),
        "firmware_patch": info.get("firmware_patch"),
        "cell_count": pack.cell_count,
        "bms_serial": bms_serial(pack_state),
        "maint": _maint(pack),
        "maint_version": _maint_version(pack),
        **{key: values.get(key) for key in _PACK_VALUES},
        "cells": _cells_status(state, pack_state, now_mono),
        "balancer": cell_report.balancer_report(state, pack_state, now_mono),
    }


def build_status(state: Any, now_mono: float, now_wall: str) -> dict[str, Any]:
    """Snapshot of the running add-on. Missing values are None."""
    multi = state.config.can_bus
    if state.bridge is None:
        bridge = "none"
    else:
        bridge = "alive" if state.bridge.alive() else "dead"
    system = None
    if multi and state.system_data is not None:
        values = _normal(state.system_data)
        system = {key: values.get(key) for key in _SYSTEM_VALUES}
    flags = activity(state.parallel_sets, state.config.number_of_packs)
    sets = [
        {
            "id": item.id, "name": item.name, "packs": list(item.packs), "wiring": item.wiring,
            "plus": item.plus, "minus": item.minus, "active": active, "reason": reason,
            "values": state.set_values.get(item.id) if active else None,
        }
        for item, (active, reason) in zip(state.parallel_sets, flags, strict=True)
    ]
    return {
        "generated_at": now_wall,
        "mode": "multi" if state.config.multi_pack else "single",
        "mqtt_connected": bool(state.mqtt is not None and state.mqtt.is_connected()),
        "bridge": bridge,
        "connection_error": getattr(state, "connection_error", None),
        "last_cycle_s": round(state.last_cycle, 1) if state.last_cycle else None,
        "packs": [_pack_status(state, p, now_mono) for p in state.packs],
        "system": system,
        "sets": sets,
    }
