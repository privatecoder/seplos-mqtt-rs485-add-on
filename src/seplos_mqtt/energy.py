"""Energy counter continuity across the 0x42 fallback value and add-on restarts."""
from __future__ import annotations

import logging
import math

from .decoders.telemetry import ENERGY_FALLBACK_KWH, ENERGY_KEYS
from .state import StateStore

logger = logging.getLogger("SeplosBMS.energy")


class EnergyGuard:
    """
    The firmware sends 6500 (650.0 kWh) once a counter reaches 65000 (6500 kWh). A 650.0 after a
    larger value is that fallback: keep the last valid value instead of publishing a false reset.
    The last value survives restarts via the state store; without any history a 650.0 is ambiguous
    and published as unknown (None) until the counter moves.
    """

    def __init__(self, pack_address: int, store: StateStore) -> None:
        self.pack_address = pack_address
        self.store = store
        self.overflow_logged = False

    def _key(self, counter: str) -> str:
        return f"pack{self.pack_address}.{counter}"

    def _last(self, counter: str) -> float | None:
        value = self.store.get(self._key(counter))
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
            return None    # no (valid) history
        return float(value)

    def apply(self, normal: dict) -> None:
        for counter in ENERGY_KEYS:
            value = normal[counter]
            last = self._last(counter)
            if value == ENERGY_FALLBACK_KWH:
                if last is None:
                    value = None
                elif last > ENERGY_FALLBACK_KWH:
                    if not self.overflow_logged:
                        logger.warning("Pack%s:Energy counter exceeded 6500 kWh, the BMS no longer reports it", self.pack_address)
                        self.overflow_logged = True
                    value = last
            if value is not None:
                self.store.set(self._key(counter), value)
            normal[counter] = value
