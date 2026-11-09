"""OpenBMS Monitor feed v1 (docs/exchange/openbms-feed-v1.md): the raw answers with their read times, for OpenBMS
Monitor's analysis over MQTT, and the read order that keeps the packs of a parallel set back to back."""
from __future__ import annotations

import os
import secrets
from collections.abc import Sequence
from typing import Any

from . import timeutil

VERSION = 1


def round_order(pack_count: int, sets: Sequence[Any]) -> list[list[int]]:
    """The packs of one poll round in groups read back to back. A parallel set is one group: its diagonal end packs
    (``plus``, ``minus``) first, then the rest in physical order. Packs at or above ``pack_count`` are dropped; a set
    left with fewer than two packs falls apart. Groups in the order of their lowest address."""
    taken: set[int] = set()
    groups: list[list[int]] = []
    for item in sets:
        members = [p for p in item.packs if 0 <= p < pack_count and p not in taken]
        if len(members) < 2:
            continue
        ends = [p for p in (item.plus, item.minus) if p in members]
        group = ends + [p for p in members if p not in ends]
        groups.append(group)
        taken.update(group)
    groups += [[p] for p in range(pack_count) if p not in taken]
    return sorted(groups, key=min)


class Feed:
    """State of the feed within one add-on run: ``boot`` (random per start) and the global ``seq``."""

    def __init__(self, boot: str | None = None, on: bool = False, auto_off: int | None = None) -> None:
        self.boot = boot or secrets.token_hex(8)
        self.seq = 0
        self.on = on
        self.auto_off = auto_off          # epoch ms of the automatic switch-off, None: on until turned off

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def _head(self) -> dict:
        return {"v": VERSION, "boot": self.boot, "seq": self.next_seq(), "sent_at": timeutil.now_ms()}

    def pack_message(self, address: int, raw: dict, raw47: dict | None) -> dict:
        """One pack poll: r42/r44 as read, r47 only when the parameters were read in this poll."""
        message = {**self._head(), "addr": address, **raw}
        if raw47 is not None:
            message["r47"] = raw47
        return message

    def system_message(self, raw: dict) -> dict:
        return {**self._head(), **raw}

    def meta_message(self, config: Any, packs: Sequence[Any], sets: Sequence[Any]) -> dict:
        """The retained description of the feed with the identity and parameter caches of every pack."""
        return {
            "v": VERSION,
            "on": self.on,
            "auto_off": self.auto_off,
            "boot": self.boot,
            "sent_at": timeutil.now_ms(),
            "bus": "uart1" if config.can_bus else "uart0",
            "addon": os.environ.get("ADDON_VERSION", "unknown"),
            "packs": [{"addr": p.address, **p.raw_ids, "r47": p.raw47} for p in packs],
            "sets": [{"id": s.id, "packs": list(s.packs), "wiring": s.wiring, "plus": s.plus, "minus": s.minus}
                     for s in sets],
        }
