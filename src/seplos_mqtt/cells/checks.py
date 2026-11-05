"""Datasheet checks of one pack: pure functions, no I/O, no clock.

The rules and margins are those of OpenBMS Monitor (``rules.ts``, ``packCheck.ts``, ``paramCheck.ts``), shared as
test vectors (``tests/vectors/cell_checks.json``). Deviations (documented in the design): every cell probe is
evaluated, an unknown SOC means the worst SOC row, a missing basis or sample is ``not_evaluated`` instead of
silently skipped.

Inputs: the assigned entry snapshot (``None`` = no assignment), ``parallel``, a ``Sample`` of the latest valid
0x42 decode (cell voltages, the four cell probes, current with charging positive, SOC) and the parameters. A
check is ``{key, result, value, limit, unit, reason}``, result ``ok | near | over | not_evaluated``.
"""
from __future__ import annotations

import functools
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

OK, NEAR, OVER, NOT_EVALUATED = "ok", "near", "over", "not_evaluated"

# margins (OpenBMS NEAR / FLOW_A / CHARGE_AT_ZERO_A)
NEAR_CELL_HIGH_V = 0.05
NEAR_CELL_LOW_V = 0.2
NEAR_TEMP_C = 5.0
NEAR_CURRENT = 0.8                  # near above 80 % of a current limit
CHARGING_A = 0.2                    # charging: current >= +0.2 A (also: any charge at a 0 A limit is a violation)
DISCHARGING_A = -0.5                # discharging: current < -0.5 A, in between the pack is at rest
CHARGE_TOLERANCE_A = 0.5            # over a charge limit only beyond limit + 0.5 A
RATED_TOLERANCE = 0.02
EPS = 1e-9                          # decimal values (3.65 - 0.05) must not decide an equality


def _gt(a: float, b: float) -> bool:
    return a > b + EPS


def _ge(a: float, b: float) -> bool:
    return a >= b - EPS


def _lt(a: float, b: float) -> bool:
    return a < b - EPS


def _le(a: float, b: float) -> bool:
    return a <= b + EPS


@dataclass(frozen=True)
class Sample:
    """Decoded values with their monotonic receive time. ``max_age`` (seconds) is set by the caller; older than
    that is stale."""

    values: Mapping[str, Any]
    at: float
    max_age: float | None = None

    def stale(self, now: float) -> bool:
        return self.max_age is not None and now - self.at > self.max_age


# --- sample access -----------------------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def cell_voltages(values: Mapping[str, Any]) -> list[float]:
    out: list[float] = []
    for index in range(1, 33):
        number = _num(values.get(f"voltage_cell_{index}"))
        if number is None:
            break
        out.append(number)
    return out


def cell_temperatures(values: Mapping[str, Any]) -> list[float]:
    numbers = (_num(values.get(f"cell_temperature_{index}")) for index in range(1, 5))
    return [t for t in numbers if t is not None]


def entry_number(entry: Mapping[str, Any], group: str, key: str) -> float | None:
    section = entry.get(group)
    return _num(section.get(key)) if isinstance(section, Mapping) else None


# --- derating table (OpenBMS rules.ts) ------------------------------------------------------------------------

# an axis is (point, min, max): a point row matches by its neighbours, a band row by its inclusive bounds
_Axis = tuple[float | None, float | None, float | None]


def _temp_axis(p: Mapping[str, Any]) -> _Axis:
    return p.get("temp_c"), p.get("temp_min_c"), p.get("temp_max_c")


def _soc_axis(p: Mapping[str, Any]) -> _Axis:
    low, high = p.get("soc_min_pct"), p.get("soc_max_pct")
    if low is not None and low == high:
        return low, None, None
    return None, 0 if low is None else low, 100 if high is None else high      # no SOC bounds: every SOC


def _govern(items: Sequence[Mapping[str, Any]], x: float, axis) -> list[Mapping[str, Any]]:
    """Rows that govern x: every band containing x (bounds inclusive, a missing bound is open) and the nearest
    point at or below and the nearest at or above x. Outside the range of the points there are no neighbours."""
    out = []
    for item in items:
        point, low, high = axis(item)
        if point is None and (-math.inf if low is None else low) <= x <= (math.inf if high is None else high):
            out.append(item)
    points = [item for item in items if axis(item)[0] is not None]
    if points:
        values = sorted({axis(item)[0] for item in points})
        below = max((v for v in values if v <= x), default=None)
        above = min((v for v in values if v >= x), default=None)
        if below is not None and above is not None:
            out.extend(item for item in points if axis(item)[0] in (below, above))
    return out


def _by_soc(rows: Sequence[Mapping[str, Any]], soc: float | None) -> list[Mapping[str, Any]]:
    return list(rows) if soc is None else _govern(rows, soc, _soc_axis)       # unknown SOC: all rows (worst case)


def _min0(values: Sequence[float]) -> float:
    return min(values) if values else 0.0


def _linear(points: Sequence[Mapping[str, Any]], temp: float, soc: float | None) -> float:
    """Only for ``interpolation: linear``: bands are constant, temperature points are interpolated per SOC row
    group, SOC still follows the lower rule."""
    rows = _by_soc(points, soc)
    values = [
        p["value"] for p in rows
        if p.get("temp_c") is None
        and (-math.inf if p.get("temp_min_c") is None else p["temp_min_c"]) <= temp
        <= (math.inf if p.get("temp_max_c") is None else p["temp_max_c"])
    ]
    groups: dict[tuple, list[Mapping[str, Any]]] = {}
    for p in rows:
        if p.get("temp_c") is not None:
            groups.setdefault((p.get("soc_min_pct"), p.get("soc_max_pct")), []).append(p)
    for group in groups.values():
        def at(t: float, group=group) -> float:
            return _min0([p["value"] for p in group if p["temp_c"] == t])

        temps = sorted({p["temp_c"] for p in group})
        low = max((t for t in temps if t <= temp), default=None)
        high = min((t for t in temps if t >= temp), default=None)
        if low is None or high is None:
            continue
        values.append(at(low) if low == high else at(low) + (at(high) - at(low)) * (temp - low) / (high - low))
    return _min0(values)


def _rate(derating: Mapping[str, Any], temp: float, soc: float | None) -> float:
    """Unknown SOC is never more permissive than any possible SOC: the lowest rate over all SOC regions 0..100,
    where a region the table does not cover at this temperature counts as 0 (deliberate deviation from OpenBMS,
    which filters by temperature first and so drops uncovered regions)."""
    points = derating.get("points", [])
    if soc is None:
        bounds = [0, 100, *(p[k] for p in points for k in ("soc_min_pct", "soc_max_pct") if p.get(k) is not None)]
        return min(_rate(derating, temp, s) for s in _probes(bounds, 0, 100))
    if derating.get("interpolation") == "linear":
        return _linear(points, temp, soc)
    return _min0([p["value"] for p in _by_soc(_govern(points, temp, _temp_axis), soc)])


def allowed_charge_rate(entry: Mapping[str, Any], temp: float, soc: float | None = None) -> float | None:
    """Allowed continuous charge rate (in the table's basis) at a cell temperature and SOC: between two points
    and on shared band bounds the lower value, outside the table 0, unknown SOC = lowest over all SOC rows.
    None without a derating table."""
    derating = entry.get("charge_derating")
    return _rate(derating, temp, soc) if isinstance(derating, Mapping) else None


def _probes(bounds: Sequence[float], low: float, high: float) -> list[float]:
    b = sorted({x for x in bounds if low <= x <= high})
    return b + [(b[i - 1] + b[i]) / 2 for i in range(1, len(b))]


def _crossings(derating: Mapping[str, Any]) -> list[float]:
    """A linear table is positive on a whole open segment if one end is: probe just inside each segment."""
    if derating.get("interpolation") != "linear":
        return []
    temps = sorted({p["temp_c"] for p in derating.get("points", []) if p.get("temp_c") is not None})
    out: list[float] = []
    for i in range(1, len(temps)):
        step = temps[i] - temps[i - 1]
        out += [temps[i - 1] + step * 0.001, temps[i] - step * 0.001]
    return out


@functools.lru_cache(maxsize=64)
def _table_range(derating_json: str) -> tuple[float, float] | None:
    """Outer temperature bounds of the intervals in which the table allows charging (> 0) at some SOC. Under
    the lower rule the value is constant between two bounds, so evaluating every bound and the midpoints
    between them is exact."""
    derating = json.loads(derating_json)
    points = derating.get("points", [])
    bounds_all = [p[k] for p in points for k in ("temp_c", "temp_min_c", "temp_max_c") if p.get(k) is not None]
    if not bounds_all:
        return None
    t_low, t_high = min(bounds_all), max(bounds_all)
    bounds = set(bounds_all)
    temps = sorted({t_low - 1, *_probes(bounds_all, t_low, t_high), *_crossings(derating), t_high + 1})
    socs = _probes([0, 100, *(p[k] for p in points for k in ("soc_min_pct", "soc_max_pct") if p.get(k) is not None)], 0, 100)
    positive = [any(_rate(derating, t, s) > 0 for s in socs) for t in temps]
    starts = [i for i, p in enumerate(positive) if p and (i == 0 or not positive[i - 1])]
    ends = [i for i, p in enumerate(positive) if p and (i + 1 == len(positive) or not positive[i + 1])]
    if not starts:
        return None
    first, last = starts[0], ends[-1]
    # an interval that starts or ends between two bounds excludes the neighbouring bound: it begins/ends there
    starts_inside = temps[first] not in bounds and temps[first] != t_low - 1
    ends_inside = temps[last] not in bounds and temps[last] != t_high + 1
    low = temps[first - 1] if starts_inside else t_low if temps[first] == t_low - 1 else temps[first]
    high = temps[last + 1] if ends_inside else t_high if temps[last] == t_high + 1 else temps[last]
    return low, high


def charge_range(entry: Mapping[str, Any]) -> tuple[float, float] | None:
    """Where charging is allowed at all: the outer bounds of the derating table, else the charge temperatures."""
    derating = entry.get("charge_derating")
    if isinstance(derating, Mapping):
        found = _table_range(json.dumps(derating, sort_keys=True))
        if found is not None:
            return found
    low, high = entry_number(entry, "temperature", "charge_min_c"), entry_number(entry, "temperature", "charge_max_c")
    return None if low is None or high is None else (low, high)


# --- currents of the pack ----------------------------------------------------------------------------------


def to_amps(entry: Mapping[str, Any], value: float, basis: str) -> float | None:
    """Amperes of one cell for a rate: C x nominal Ah, P x (nominal Wh, else Ah x V) / nominal V. Never the
    learned capacity."""
    ah = entry_number(entry, "capacity", "nominal_ah")
    volts = entry_number(entry, "voltage", "nominal_v")
    if basis == "C":
        return None if ah is None else value * ah
    wh = entry_number(entry, "capacity", "nominal_energy_wh")
    if wh is None and ah is not None and volts is not None:
        wh = ah * volts
    return None if wh is None or volts is None else value * wh / volts


def permitted_charge_a(entry: Mapping[str, Any], parallel: int, temps: Sequence[float], soc: float | None) -> float | None:
    """Charge current of the pack the datasheet allows at these cell temperatures; None without a basis.

    With a derating table: the table value at every probe (the lowest wins), capped by
    ``max_continuous_charge_a``. Without: ``max_continuous_charge_a`` inside the charge temperature range, 0
    outside. Times ``parallel``."""
    if not temps:
        return None
    cap = entry_number(entry, "current", "max_continuous_charge_a")
    derating = entry.get("charge_derating")
    if isinstance(derating, Mapping):
        amps = []
        for temp in temps:
            value = to_amps(entry, _rate(derating, temp, soc), derating.get("basis", "C"))
            if value is None:
                return None
            amps.append(value)
        limit = min(amps) if cap is None else min(min(amps), cap)
        return limit * parallel
    low, high = entry_number(entry, "temperature", "charge_min_c"), entry_number(entry, "temperature", "charge_max_c")
    if cap is None or low is None or high is None:
        return None
    return cap * parallel if _ge(min(temps), low) and _le(max(temps), high) else 0.0


def permitted_discharge_a(entry: Mapping[str, Any], parallel: int, temps: Sequence[float]) -> float | None:
    """Advisory discharge budget: ``max_continuous_discharge_a`` x parallel inside the discharge temperature
    range, 0 outside, None without a basis."""
    cap = entry_number(entry, "current", "max_continuous_discharge_a")
    low, high = entry_number(entry, "temperature", "discharge_min_c"), entry_number(entry, "temperature", "discharge_max_c")
    if not temps or cap is None or low is None or high is None:
        return None
    return cap * parallel if _ge(min(temps), low) and _le(max(temps), high) else 0.0


# --- results ---------------------------------------------------------------------------------------------------


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _check(key: str, result: str, value: float | None = None, limit: float | None = None, unit: str | None = None,
           reason: str | None = None) -> dict[str, Any]:
    return {"key": key, "result": result, "value": _round(value), "limit": _round(limit), "unit": unit, "reason": reason}


def _not_evaluated(key: str, unit: str | None, reason: str) -> dict[str, Any]:
    return _check(key, NOT_EVALUATED, unit=unit, reason=reason)


def _graded(key: str, over: bool, near: bool, value: float, limit: float, unit: str, over_reason: str,
            near_reason: str) -> dict[str, Any]:
    if over:
        return _check(key, OVER, value, limit, unit, over_reason)
    if near:
        return _check(key, NEAR, value, limit, unit, near_reason)
    return _check(key, OK, value, limit, unit)


def summarize(checks: Sequence[Mapping[str, Any]]) -> str:
    """Worst result; ``over`` always wins, a ``not_evaluated`` check makes an otherwise ok summary
    ``not_evaluated`` (no checks at all as well)."""
    results = {c["result"] for c in checks}
    for worst in (OVER, NEAR, NOT_EVALUATED):
        if worst in results:
            return worst
    return OK if results else NOT_EVALUATED


def _unavailable(entry, blocked: str | None, reading: Sample | None, now: float, missing: str) -> str | None:
    if blocked is not None:
        return blocked
    if entry is None:
        return "no_assignment"
    if reading is None:
        return f"no_{missing}"
    if reading.stale(now):
        return f"stale_{missing}"
    return None


# --- live checks ---------------------------------------------------------------------------------------------


def live_checks(entry: Mapping[str, Any] | None, parallel: int, sample: Sample | None, now: float, *,
                blocked: str | None = None) -> dict[str, Any]:
    """The pack's live values against the datasheet. ``blocked`` (``serial_mismatch``, ``invalid_snapshot``)
    makes everything not evaluated. Returns ``summary``, ``reason`` (why nothing was evaluated), ``checks`` and
    the advisory ``charge_limit_a`` / ``discharge_limit_a`` (None while not evaluated)."""
    reason = _unavailable(entry, blocked, sample, now, "sample")
    if reason is not None:
        return {"summary": NOT_EVALUATED, "reason": reason, "checks": [], "charge_limit_a": None, "discharge_limit_a": None}
    values = sample.values
    cells, temps = cell_voltages(values), cell_temperatures(values)
    current, soc = _num(values.get("current")), _num(values.get("state_of_charge"))
    charging = current is not None and _ge(current, CHARGING_A)
    discharging = current is not None and _lt(current, DISCHARGING_A)
    checks = []

    charge_cutoff = entry_number(entry, "voltage", "charge_cutoff_v")
    if not cells:
        checks.append(_not_evaluated("cell_voltage_high", "V", "missing_sample"))
    elif charge_cutoff is None:
        checks.append(_not_evaluated("cell_voltage_high", "V", "missing_limit"))
    else:
        top = max(cells)
        checks.append(_graded("cell_voltage_high", _gt(top, charge_cutoff), _ge(top, charge_cutoff - NEAR_CELL_HIGH_V),
                              top, charge_cutoff, "V", "above_charge_cutoff", "near_charge_cutoff"))
    discharge_cutoff = entry_number(entry, "voltage", "discharge_cutoff_v")
    if not cells:
        checks.append(_not_evaluated("cell_voltage_low", "V", "missing_sample"))
    elif discharge_cutoff is None:
        checks.append(_not_evaluated("cell_voltage_low", "V", "missing_limit"))
    else:
        bottom = min(cells)
        checks.append(_graded("cell_voltage_low", _lt(bottom, discharge_cutoff), _le(bottom, discharge_cutoff + NEAR_CELL_LOW_V),
                              bottom, discharge_cutoff, "V", "below_discharge_cutoff", "near_discharge_cutoff"))

    discharge_range = (entry_number(entry, "temperature", "discharge_min_c"), entry_number(entry, "temperature", "discharge_max_c"))
    checks.append(_temperature_check("charge_temperature", temps, charge_range(entry), charging))
    checks.append(_temperature_check(
        "discharge_temperature", temps, None if None in discharge_range else discharge_range, discharging))

    limit = permitted_charge_a(entry, parallel, temps, soc)
    if current is None or not temps:
        checks.append(_not_evaluated("charge_current", "A", "missing_sample"))
    elif limit is None:
        checks.append(_not_evaluated("charge_current", "A", "missing_limit"))
    else:
        flow = max(0.0, current)
        if limit == 0:      # nothing may flow in: a real charge current (two steps of the 0.1 A resolution) is a violation
            over, reason = _ge(flow, CHARGING_A), "charge_not_allowed"
        else:
            over, reason = _gt(flow, limit + CHARGE_TOLERANCE_A), "above_limit"
        checks.append(_graded("charge_current", over, _gt(flow, limit * NEAR_CURRENT), flow, limit, "A", reason,
                              "charge_not_allowed" if limit == 0 else "near_limit"))
    cap = entry_number(entry, "current", "max_continuous_discharge_a")
    if current is None:
        checks.append(_not_evaluated("discharge_current", "A", "missing_sample"))
    elif cap is None:
        checks.append(_not_evaluated("discharge_current", "A", "missing_limit"))
    else:
        flow, maximum = max(0.0, -current), cap * parallel      # static: a cold pack is reported by the temperature check
        checks.append(_graded("discharge_current", _gt(flow, maximum), _gt(flow, maximum * NEAR_CURRENT), flow, maximum,
                              "A", "above_limit", "near_limit"))
    return {
        "summary": summarize(checks), "reason": None, "checks": checks,
        "charge_limit_a": _round(limit), "discharge_limit_a": _round(permitted_discharge_a(entry, parallel, temps)),
    }


def _temperature_check(key: str, temps: Sequence[float], bounds: tuple[float, float] | None, flowing: bool) -> dict[str, Any]:
    """Outside the range is a violation only while current flows in that direction; at rest (or the other way)
    it means "would not be allowed now" and counts as near."""
    if not temps:
        return _not_evaluated(key, "°C", "missing_sample")
    if bounds is None:
        return _not_evaluated(key, "°C", "missing_limit")
    low_bound, high_bound = bounds
    coldest, warmest = min(temps), max(temps)
    cold_side = coldest - low_bound < high_bound - warmest
    outside = _lt(coldest, low_bound) or _gt(warmest, high_bound)
    near = outside or _lt(coldest, low_bound + NEAR_TEMP_C) or _gt(warmest, high_bound - NEAR_TEMP_C)
    return _graded(key, outside and flowing, near, coldest if cold_side else warmest, low_bound if cold_side else high_bound,
                   "°C", "outside_range", "outside_range" if outside else "near_range_edge")


# --- parameter checks ----------------------------------------------------------------------------------------

# (parameter key, kind, unit, datasheet limit). Only alarm and protection thresholds: recovery values lie inside
# them by design. ``maxabs``: by magnitude (the BMS keeps discharge currents negative).
_PARAMETERS = (
    ("cell_high_voltage_alarm_v", "max", "V", "cell_high"),
    ("cell_overvoltage_protection_v", "max", "V", "cell_high"),
    ("cell_low_voltage_alarm_v", "min", "V", "cell_low"),
    ("cell_undervoltage_protection_v", "min", "V", "cell_low"),
    ("pack_high_voltage_alarm_v", "max", "V", "pack_high"),
    ("pack_overvoltage_protection_v", "max", "V", "pack_high"),
    ("pack_low_voltage_alarm_v", "min", "V", "pack_low"),
    ("pack_undervoltage_protection_v", "min", "V", "pack_low"),
    ("charging_high_temperature_alarm_c", "max", "°C", "charge_hot"),
    ("charging_over_temperature_protection_c", "max", "°C", "charge_hot"),
    ("charging_low_temperature_alarm_c", "min", "°C", "charge_cold"),
    ("charging_under_temperature_protection_c", "min", "°C", "charge_cold"),
    ("discharging_high_temperature_alarm_c", "max", "°C", "discharge_hot"),
    ("discharging_over_temperature_protection_c", "max", "°C", "discharge_hot"),
    ("discharging_low_temperature_alarm_c", "min", "°C", "discharge_cold"),
    ("discharging_under_temperature_protection_c", "min", "°C", "discharge_cold"),
    ("charging_overcurrent_alarm_a", "max", "A", "charge_current"),
    ("charging_overcurrent_protection_a", "max", "A", "charge_current"),
    ("discharging_overcurrent_alarm_a", "maxabs", "A", "discharge_current"),
    ("discharging_overcurrent_protection_a", "maxabs", "A", "discharge_current"),
)


def _parameter_limits(entry: Mapping[str, Any], parallel: int, series: int | None) -> dict[str, float | None]:
    charge_cutoff = entry_number(entry, "voltage", "charge_cutoff_v")
    discharge_cutoff = entry_number(entry, "voltage", "discharge_cutoff_v")
    bounds = charge_range(entry)
    discharge_min, discharge_max = entry_number(entry, "temperature", "discharge_min_c"), entry_number(entry, "temperature", "discharge_max_c")
    charge_a, discharge_a = entry_number(entry, "current", "max_continuous_charge_a"), entry_number(entry, "current", "max_continuous_discharge_a")
    return {
        "cell_high": charge_cutoff,
        "cell_low": discharge_cutoff,
        "pack_high": None if charge_cutoff is None or not series else round(charge_cutoff * series, 2),
        "pack_low": None if discharge_cutoff is None or not series else round(discharge_cutoff * series, 2),
        "charge_hot": None if bounds is None else bounds[1],
        "charge_cold": None if bounds is None else bounds[0],
        "discharge_hot": discharge_max,
        "discharge_cold": discharge_min,
        "charge_current": None if charge_a is None else charge_a * parallel,
        "discharge_current": None if discharge_a is None else discharge_a * parallel,
    }


def parameter_checks(entry: Mapping[str, Any] | None, parallel: int, parameters: Sample | None, series: int | None,
                     now: float, *, blocked: str | None = None) -> dict[str, Any]:
    """The BMS settings against the datasheet (only where parameters are readable). ``series`` is the measured
    series count: pack voltage limits need it, P65 differing from it is a configuration warning (near), a rated
    capacity beyond 2 % of ``nominal_ah`` x ``parallel`` is near (informational)."""
    reason = _unavailable(entry, blocked, parameters, now, "parameters")
    if reason is not None:
        return {"summary": NOT_EVALUATED, "reason": reason, "checks": []}
    values = parameters.values
    limits = _parameter_limits(entry, parallel, series)
    checks = []
    for key, kind, unit, source in _PARAMETERS:
        value, limit = _num(values.get(key)), limits[source]
        if value is None:
            checks.append(_not_evaluated(key, unit, "missing_value"))
        elif limit is None:
            checks.append(_not_evaluated(key, unit, "no_series" if source.startswith("pack_") and not series else "missing_limit"))
        else:
            magnitude = abs(value) if kind == "maxabs" else value
            over = _lt(magnitude, limit) if kind == "min" else _gt(magnitude, limit)
            why = ("below_limit" if kind == "min" else "above_limit") if over else None
            checks.append(_check(key, OVER if over else OK, value, limit, unit, why))
    rated, nominal = _num(values.get("rated_capacity_ah")), entry_number(entry, "capacity", "nominal_ah")
    if rated is None:
        checks.append(_not_evaluated("rated_capacity_ah", "Ah", "missing_value"))
    elif nominal is None:
        checks.append(_not_evaluated("rated_capacity_ah", "Ah", "missing_limit"))
    else:
        expected = nominal * parallel
        checks.append(_graded("rated_capacity_ah", False, not _le(abs(rated - expected), expected * RATED_TOLERANCE), rated,
                              expected, "Ah", "", "rated_capacity_mismatch"))
    configured = _num(values.get("cells_in_series"))
    if configured is None:
        checks.append(_not_evaluated("cells_in_series", None, "missing_value"))
    elif not series:
        checks.append(_not_evaluated("cells_in_series", None, "no_series"))
    else:
        checks.append(_graded("cells_in_series", False, configured != series, configured, series, None, "", "series_mismatch"))
    return {"summary": summarize(checks), "reason": None, "checks": checks}
