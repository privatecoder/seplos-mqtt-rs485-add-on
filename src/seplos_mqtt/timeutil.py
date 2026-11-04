"""Wall-clock helpers (patched in tests). Ages and deadlines use time.monotonic() instead."""
from __future__ import annotations

from datetime import datetime


def local_now() -> datetime:
    """Host local time (naive), the convention the BMS clock is compared with."""
    return datetime.now()


def now_iso() -> str:
    """Local time with UTC offset, for HA timestamp sensors."""
    return datetime.now().astimezone().isoformat(timespec="seconds")
