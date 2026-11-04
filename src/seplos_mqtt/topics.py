"""MQTT state topics and Home Assistant identifiers (public interface, keep stable)."""
from __future__ import annotations


def availability(base: str) -> str:
    return f"{base}/availability"


def pack_base(base: str, pack_no: int) -> str:
    return f"{base}/pack-{pack_no}"


def pack_sensors(base: str, pack_no: int) -> str:
    return f"{pack_base(base, pack_no)}/sensors"


def pack_heartbeat(base: str, pack_no: int) -> str:
    return f"{pack_base(base, pack_no)}/heartbeat"


def pack_availability(base: str, pack_no: int) -> str:
    return f"{pack_base(base, pack_no)}/availability"


def system_sensors(base: str) -> str:
    return f"{base}/system/sensors"


def system_availability(base: str) -> str:
    return f"{base}/system/availability"


def set_base(base: str, set_id: int) -> str:
    return f"{base}/set-{set_id}"


def set_sensors(base: str, set_id: int) -> str:
    return f"{set_base(base, set_id)}/sensors"


def set_availability(base: str, set_id: int) -> str:
    return f"{set_base(base, set_id)}/availability"


def set_device_id(set_id: int) -> str:
    return f"seplos_bms_set_{set_id}"


def set_node(set_id: int) -> str:
    return f"seplos-mqtt-set-{set_id}"


def pack_device_id(pack_no: int) -> str:
    return f"seplos_bms_pack_{pack_no}"


SYSTEM_DEVICE_ID = "seplos_bms_system"


def discovery_topic(prefix: str, domain: str, node: str, key: str) -> str:
    return f"{prefix}/{domain}/{node}/{key}/config"


def pack_node(pack_no: int) -> str:
    return f"seplos-mqtt-pack-{pack_no}"


SYSTEM_NODE = "seplos-mqtt-system"
