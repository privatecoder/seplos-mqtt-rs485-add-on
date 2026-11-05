"""Computed state of an external active balancer: nothing is read from the device, its activity follows from the
cell voltages and the pack current with the manual settings of its profile (rules of
``docs/exchange/seplos-bank-settings-v1.md``, ``balancers[]``; the same state machine as OpenBMS Monitor's
``balancer.rs``, ``Tracker::step``).

``step`` is the pure rule; ``Tracker`` carries the state of one pack and resets it whenever the basis changes.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .checks import Sample, _num, cell_voltages

ACTIVE = "active"
INACTIVE = "inactive"
UNKNOWN = "unknown"
STATES = (ACTIVE, INACTIVE, UNKNOWN)

# |current| (mA) of the ``when`` conditions: charging above +1 A, discharging below -1 A
CHARGE_MA = 1000


@dataclass(frozen=True)
class Rule:
    """The settings of a profile with the hysteresis rules applied (mV)."""

    start_mv: float | None       # the highest cell must be at least this to start
    cell_stop_mv: float | None   # highest cell below which it stops (None: no cell condition)
    trigger_mv: float            # spread from which it starts
    stop_mv: float               # spread below which it stops (<= trigger)
    when: str

    @classmethod
    def from_values(cls, values: Mapping[str, Any]) -> Rule:
        trigger = values["trigger_mv"]
        stop = values.get("stop_mv")
        start = values.get("start_mv")
        cell_stop = values.get("stop_cell_mv")
        return cls(
            start_mv=start,
            # ignored without a start; null or above the start: the start itself (no cell hysteresis)
            cell_stop_mv=None if start is None else start if cell_stop is None else min(cell_stop, start),
            trigger_mv=trigger,
            stop_mv=trigger if stop is None else min(stop, trigger),
            when=values.get("when", "always"),
        )

    def when_holds(self, milliamps: int) -> bool:
        if self.when == "charging":
            return milliamps > CHARGE_MA
        if self.when == "not-discharging":
            return milliamps >= -CHARGE_MA
        return True


def measurement(sample: Sample | None, now: float) -> tuple[int, int, int] | None:
    """``(highest cell mV, lowest cell mV, current mA)`` of a fresh sample; None when the sample is missing or
    stale, or has no cells or no current (indecisive). Rounded to whole mV / mA, so that a threshold hit exactly
    is not lost to float noise."""
    if sample is None or sample.stale(now):
        return None
    cells = cell_voltages(sample.values)
    current = _num(sample.values.get("current"))
    if not cells or current is None:
        return None
    millivolts = [round(v * 1000) for v in cells]
    return max(millivolts), min(millivolts), round(current * 1000)


def step(state: str, profile_values: Mapping[str, Any], sample: Sample | None, now: float) -> str:
    """The next state from the state before and the latest sample.

    Start: spread >= trigger, highest cell >= start (if any) and ``when`` holds. Stop: spread < stop, or (with a
    start) the highest cell < the cell stop, or ``when`` fails. Otherwise the state is kept - ``unknown`` stays
    ``unknown`` until a sample decides. A missing, stale or incomplete sample is never decisive: ``unknown``."""
    measured = measurement(sample, now)
    if measured is None:
        return UNKNOWN
    highest, lowest, milliamps = measured
    rule = Rule.from_values(profile_values)
    if not rule.when_holds(milliamps) or (rule.cell_stop_mv is not None and highest < rule.cell_stop_mv):
        return INACTIVE
    spread = highest - lowest
    if spread >= rule.trigger_mv and (rule.start_mv is None or highest >= rule.start_mv):
        return ACTIVE
    if spread < rule.stop_mv:
        return INACTIVE
    return state


class Tracker:
    """The state of one pack's balancer. A new profile, an edit of its values, an identity mismatch and a stale
    sample all drop it back to ``unknown`` (a restart does too: nothing is persisted). It also remembers the receive
    time of the last sample it evaluated: a new sample received more than its freshness allowance after that one
    means the observation was interrupted, so the carried state is dropped before the new sample is evaluated.
    Evaluating the same cached sample again (a failed poll) never extends that continuity."""

    def __init__(self) -> None:
        self.profile_id: int | None = None
        self.values: dict[str, Any] | None = None
        self.state = UNKNOWN
        self.seen_at: float | None = None       # receive time of the last evaluated sample

    def matches(self, profile: Any) -> bool:
        return profile is not None and self.profile_id == profile.id and self.values == dict(profile.values)

    def update(self, profile: Any, blocked: bool, sample: Sample | None, now: float) -> str | None:
        """Advance with the latest sample; None without a profile."""
        if profile is None:
            self.profile_id, self.values, self.state, self.seen_at = None, None, UNKNOWN, None
            return None
        if not self.matches(profile):
            self.profile_id, self.values, self.state, self.seen_at = profile.id, dict(profile.values), UNKNOWN, None
        if blocked:
            self.state, self.seen_at = UNKNOWN, None
            return self.state
        if sample is None or sample.stale(now):
            self.state, self.seen_at = UNKNOWN, None
            return self.state
        if sample.at != self.seen_at:             # a new measurement, not the cached one again
            if self.seen_at is not None and sample.max_age is not None and sample.at - self.seen_at > sample.max_age:
                self.state = UNKNOWN              # continuity lost between the two measurements
            self.seen_at = sample.at
        self.state = step(self.state, self.values, sample, now)
        return self.state

    def view(self, profile: Any, blocked: bool, fresh: bool = True) -> str | None:
        """What is known now, without advancing: the carried state only while it belongs to the profile that is
        assigned at this moment (an assignment or edit not yet seen by a poll reads ``unknown``) and the pack's
        sample is still fresh (``fresh``: judged by the caller with the pack's current allowance)."""
        if profile is None:
            return None
        return self.state if self.matches(profile) and not blocked and fresh else UNKNOWN
