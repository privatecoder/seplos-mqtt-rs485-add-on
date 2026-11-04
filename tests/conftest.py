from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Defaults as the add-on's config.yaml sets them, so module-level Config is deterministic
os.environ.setdefault("NUMBER_OF_PACKS", "1")
os.environ.setdefault("MIN_CELL_VOLTAGE", "2.5")
os.environ.setdefault("MAX_CELL_VOLTAGE", "3.65")
os.environ.setdefault("MQTT_TOPIC", "seplos")
os.environ.setdefault("HA_DISCOVERY_PREFIX", "homeassistant")
os.environ.setdefault("INVERT_HA_DIS_CHARGE_MEASUREMENTS", "false")
os.environ.setdefault("MQTT_UPDATE_INTERVAL", "1")
os.environ.setdefault("LOGGING_LEVEL", "info")

GOLDEN = Path(__file__).resolve().parent / "golden"


def assert_golden(name: str, data) -> None:
    """Compare against tests/golden/<name>.json; UPDATE_GOLDEN=1 rewrites the file."""
    path = GOLDEN / f"{name}.json"
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if os.environ.get("UPDATE_GOLDEN") == "1" or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if os.environ.get("UPDATE_GOLDEN") != "1":
            pytest.fail(f"golden {name} was missing and has been created, review and rerun")
        return
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert json.loads(text) == expected, f"golden mismatch: {name}"


@pytest.fixture
def golden():
    return assert_golden
