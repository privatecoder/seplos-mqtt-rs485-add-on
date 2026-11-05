#!/usr/bin/env python3
"""Maintainer tool: refresh the bundled cell catalog from a local OpenBMS Monitor checkout.

    python3 scripts/update_cell_catalog.py <path-to-openbms-monitor>

Copies ``data/cells/cells.json``, ``data/schema/cells.schema.json`` and ``LICENSE`` unchanged into
``src/seplos_mqtt/cells/data/`` and rewrites ``MANIFEST.json`` (source commit, sha256 of both data
files). No network access.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "src" / "seplos_mqtt" / "cells" / "data"
SOURCES = {
    "cells.json": Path("data/cells/cells.json"),
    "cells.schema.json": Path("data/schema/cells.schema.json"),
}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    source = Path(argv[1]).expanduser().resolve()
    commit = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    TARGET.mkdir(parents=True, exist_ok=True)
    digests = {}
    for name, relative in SOURCES.items():
        shutil.copyfile(source / relative, TARGET / name)
        digests[name] = hashlib.sha256((TARGET / name).read_bytes()).hexdigest()
    shutil.copyfile(source / "LICENSE", TARGET / "LICENSE")
    manifest = {"source": "openbms-monitor", "commit": commit, "files": digests}
    (TARGET / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"catalog updated to {commit}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
