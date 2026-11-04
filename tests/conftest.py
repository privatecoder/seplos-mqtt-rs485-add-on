from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Deterministic baseline independent of the invoking shell (add-on defaults from config.yaml)
BASE_ENV = {
    "NUMBER_OF_PACKS": "1",
    "MIN_CELL_VOLTAGE": "2.5",
    "MAX_CELL_VOLTAGE": "3.65",
    "SERIAL_INTERFACE": "/tmp/vcom0",
    "MQTT_HOST": "broker.test",
    "MQTT_PORT": "1883",
    "MQTT_USERNAME": "user",
    "MQTT_PASSWORD": "secret",
    "MQTT_TOPIC": "seplos",
    "MQTT_UPDATE_INTERVAL": "1",
    "ENABLE_HA_DISCOVERY_CONFIG": "true",
    "HA_DISCOVERY_PREFIX": "homeassistant",
    "INVERT_HA_DIS_CHARGE_MEASUREMENTS": "false",
    "LOGGING_LEVEL": "info",
}
os.environ.update(BASE_ENV)

GOLDEN = Path(__file__).resolve().parent / "golden"


def canonical(data) -> str:
    """Type-sensitive canonical JSON (True != 1, 280.0 != 280)."""
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def assert_golden(name: str, data) -> None:
    """Compare against tests/golden/<name>.json; only UPDATE_GOLDEN=1 writes."""
    path = GOLDEN / f"{name}.json"
    text = canonical(data)
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return
    if not path.exists():
        pytest.fail(f"golden {name} is missing, create it with UPDATE_GOLDEN=1 and review it")
    assert text == path.read_text(encoding="utf-8"), f"golden mismatch: {name}"


@pytest.fixture
def golden():
    return assert_golden
