"""The bundled cell catalog: a vendored copy of the OpenBMS Monitor cell database and its schema.

The files in ``data/`` are copied unchanged by ``scripts/update_cell_catalog.py``; ``MANIFEST.json``
records the source commit and the sha256 of both files. Nothing is downloaded at build or run time.
Entries are shared and read-only: copy before storing or changing one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent / "data"
FILES = ("cells.json", "cells.schema.json")


@dataclass(frozen=True)
class Catalog:
    entries: tuple[dict[str, Any], ...]
    schema: dict[str, Any]
    manifest: dict[str, Any]

    def get(self, cell_id: str) -> dict[str, Any] | None:
        for entry in self.entries:
            if entry.get("id") == cell_id:
                return entry
        return None


def _read(name: str) -> Any:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


_CATALOG: Catalog | None = None       # tests replace it to simulate another bundle


def current() -> Catalog:
    """The bundled catalog (read once)."""
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = Catalog(
            entries=tuple(_read("cells.json")),
            schema=_read("cells.schema.json"),
            manifest=_read("MANIFEST.json"),
        )
    return _CATALOG
