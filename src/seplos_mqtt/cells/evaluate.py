"""One pack's datasheet report: assignment + samples -> checks, shared by the MQTT payload and ``/api/status``.

Duck-typed on the running app (``state.cell_packs``, ``state.cell_types``, ``state.pack_max_age(now)``,
``pack_state.pack``) so that ``app`` and ``status`` can both use it without importing each other.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from ..decoders import telemetry
from ..monitoring.model import find_cell
from . import balancer, checks

logger = logging.getLogger("SeplosBMS.cells")


def context(assignment: Any) -> tuple[Any, int, str | None]:
    """``(entry, parallel, blocked)`` of a stored pack assignment: no entry without a cell type, and a snapshot
    that does not match its reference or a pack whose BMS serial changed is not evaluated."""
    cell = getattr(assignment, "cell", None)
    if cell is None:
        return None, 1, None
    if not cell.valid:
        return None, cell.parallel, "invalid_snapshot"
    if assignment.serial_mismatch:
        return None, cell.parallel, "serial_mismatch"
    return cell.entry, cell.parallel, None


def nominal_voltage(assignment: Any) -> float | None:
    """Nominal cell voltage of the assigned (and trusted) entry for the full energy; None = use the connection's."""
    entry, _parallel, blocked = context(assignment)
    value = checks.entry_number(entry, "voltage", "nominal_v") if entry is not None and blocked is None else None
    return value if value else None


def full_energy(sample: Any, assignment: Any, default_voltage: float) -> float | None:
    """Pack full energy in kWh from the sample's learned Ah and series count and the nominal voltage that applies
    now (trusted assignment, else the connection's); never x parallel. Derived on demand, so an assignment or
    identity change applies to an already stored sample."""
    if sample is None:
        return None
    series = len(checks.cell_voltages(sample.values))
    return telemetry.full_energy(sample.values.get("full_capacity"), series, nominal_voltage(assignment) or default_voltage)


def update_available(assignment: Any, own_types: Any) -> bool:
    """A newer version of the assigned type exists (the snapshot stays until the user accepts it)."""
    cell = getattr(assignment, "cell", None)
    if cell is None or not cell.valid:
        return False
    found = find_cell(own_types, cell.id)
    return found is not None and found[1]["entry_version"] > cell.entry_version


def balancer_profile(state: Any, assignment: Any) -> Any:
    """The balancer profile assigned to a pack (None: none, or an id no profile has)."""
    wanted = getattr(assignment, "balancer_profile", None)
    if wanted is None:
        return None
    return next((p for p in state.balancer_profiles if p.id == wanted), None)


def balancer_update(state: Any, pack_state: Any, now: float) -> None:
    """Advance the pack's computed balancer state with its latest sample (every poll, also a failed one: the
    sample ages). A defect here must never stop the polling."""
    address = pack_state.address
    assignment = state.cell_packs.get(address)
    profile = balancer_profile(state, assignment)
    if profile is None:
        state.balancers.pop(address, None)
        return
    tracker = state.balancers.get(address)
    if tracker is None:
        tracker = state.balancers[address] = balancer.Tracker()
    sample = pack_state.pack.sample
    if sample is not None:
        sample = replace(sample, max_age=state.pack_max_age(now))
    try:
        tracker.update(profile, assignment.serial_mismatch, sample, now)
    except Exception:
        logger.exception("Pack%s:Balancer state failed", address)
        tracker.state = balancer.UNKNOWN


def balancer_report(state: Any, pack_state: Any, now: float) -> dict[str, Any] | None:
    """``{profile_id, profile_name, state}`` of the pack's balancer, None without a profile. Read-only, and
    freshness-aware with the pack's current allowance: a sample that has gone stale reads ``unknown`` even if this
    pack was not polled since (the tracker itself drops the state at its next update)."""
    assignment = state.cell_packs.get(pack_state.address)
    profile = balancer_profile(state, assignment)
    if profile is None:
        return None
    tracker = state.balancers.get(pack_state.address)
    sample = pack_state.pack.sample
    fresh = sample is not None and now - sample.at <= state.pack_max_age(now)
    value = tracker.view(profile, bool(assignment.serial_mismatch), fresh) if tracker is not None else balancer.UNKNOWN
    return {"profile_id": profile.id, "profile_name": profile.name, "state": value}


def _display_name(entry: Any) -> str:
    return " ".join(str(entry[k]) for k in ("manufacturer", "model", "variant") if entry.get(k))


def _safe(label: str, run, *args, **kwargs) -> dict[str, Any]:
    """A defect in a rule must never stop the polling: that group is simply not evaluated."""
    try:
        return run(*args, **kwargs)
    except Exception:
        logger.exception("Datasheet %s checks failed", label)
        return {"summary": checks.NOT_EVALUATED, "reason": "check_error", "checks": [], "charge_limit_a": None, "discharge_limit_a": None}


def pack_report(state: Any, pack_state: Any, now: float) -> dict[str, Any]:
    pack = pack_state.pack
    assignment = state.cell_packs.get(pack_state.address)
    entry, parallel, blocked = context(assignment)
    sample = pack.sample
    if sample is not None:
        sample = replace(sample, max_age=state.pack_max_age(now))
    parameters = pack.parameter_sample()
    series = pack.cell_count
    cell = getattr(assignment, "cell", None)
    return {
        "assignment": None if cell is None else {
            "id": cell.id, "entry_version": cell.entry_version, "parallel": cell.parallel, "valid": cell.valid,
            "serial_mismatch": assignment.serial_mismatch, "update_available": update_available(assignment, state.cell_types),
            "name": _display_name(cell.entry),
        },
        "entry": None if cell is None else cell.entry,       # shown even when the checks are blocked
        "series": series,
        "live": _safe("live", checks.live_checks, entry, parallel, sample, now, blocked=blocked),
        "parameters": _safe("parameter", checks.parameter_checks, entry, parallel, parameters, series, now, blocked=blocked),
        "sample_age_s": None if sample is None else round(max(0.0, now - sample.at), 1),
        "parameters_age_s": None if parameters is None else round(max(0.0, now - parameters.at), 1),
        "balancer": balancer_report(state, pack_state, now),
    }


def mqtt_group(report: dict[str, Any]) -> dict[str, Any]:
    """The ``cells`` group of the pack's state JSON (HA entities read ``normal``; the rest are attributes)."""
    assignment, entry, live, parameters = report["assignment"], report["entry"], report["live"], report["parameters"]
    attributes: dict[str, Any] = {}
    if assignment is not None:
        shown = entry or {}
        attributes = {
            "id": assignment["id"], "entry_version": assignment["entry_version"],
            "manufacturer": shown.get("manufacturer"), "model": shown.get("model"), "chemistry": shown.get("chemistry"),
            "nominal_v": checks.entry_number(shown, "voltage", "nominal_v"), "nominal_ah": checks.entry_number(shown, "capacity", "nominal_ah"),
            "parallel": assignment["parallel"], "series": report["series"], "update_available": assignment["update_available"],
        }
    # MQTT attributes carry key/result/limit/unit/reason without ``value``: a changing measurement must not rewrite
    # the HA state on every poll (the full values are in /api/status)
    def projected(group: dict[str, Any]) -> dict[str, Any]:
        return {"reason": group["reason"], "checks": [{k: v for k, v in c.items() if k != "value"} for c in group["checks"]]}

    normal = {
        "cell_type": assignment["name"] if assignment is not None else None,
        "datasheet_check_live": live["summary"],
        "datasheet_check_parameters": parameters["summary"],
        "datasheet_charge_current_limit": live["charge_limit_a"],
        "datasheet_discharge_current_limit": live["discharge_limit_a"],
    }
    if report["balancer"] is not None:           # only packs with a profile have the entity (and the key)
        normal["external_balancer"] = report["balancer"]["state"]
    return {
        "normal": normal,
        "cell_type": attributes,
        # no timestamps and no measured values: the attributes only change with a limit, result or reason
        "live": projected(live),
        "parameters": projected(parameters),
    }
