"""Home Assistant MQTT discovery: renders the entity catalog and publishes retained configs."""
from __future__ import annotations

import json
import logging
from typing import Any

from .. import topics
from .catalog import CELL_ENTITIES, FIRMWARE_ENTITY, OBSOLETE, PACK_ENTITIES, SYSTEM, Entity

logger = logging.getLogger("SeplosBMS.discovery")

DEVICE_BASE = {
    "hw": "10C / 10E",
    "sw": "2.x / 16.x",
    "mdl": "BMS V14 / V16",
    "mf": "Seplos",
}

# HA abbreviation -> Entity attribute
_OPTIONAL = {
    "unit_of_meas": "unit",
    "dev_cla": "device_class",
    "stat_cla": "state_class",
    "sug_dsp_prc": "precision",
    "ic": "icon",
    "ent_cat": "category",
    "pl_on": "payload_on",
    "pl_off": "payload_off",
}

SYSTEM_SCOPE = "system"


class Discovery:
    def __init__(self, client, mqtt_topic: str, prefix: str, invert: bool) -> None:
        self.client = client
        self.topic = mqtt_topic
        self.prefix = prefix
        self.invert = invert
        # devices whose full device block has been sent with a config (pack number or "system")
        self._device_sent: set = set()
        # firmware version shown in the device info, per pack number and "system"
        self._sw: dict = {}
        # cell count per pack once known
        self._cells: dict[int, int] = {}
        self._system_active = False

    # --- rendering ----------------------------------------------------------------------------

    def _template(self, entity: Entity, scope) -> str:
        if entity.kind == "info":
            return f"{{{{ value_json.get('info', {{}}).get('normal', {{}}).get('{entity.key}', 'unknown') }}}}"
        if entity.kind == "heartbeat":
            return "{{ value_json.last_publish }}"
        if entity.kind == "parameters":
            return "{{ value_json.get('parameters', {}).get('read_at') }}"
        state = "binary" if entity.domain == "binary_sensor" else "normal"
        path = f"value_json.{state}.{entity.key}" if scope == SYSTEM_SCOPE else f"value_json.{entity.group}.{state}.{entity.key}"
        if entity.invert and self.invert and entity.domain == "sensor":
            return f"{{{{ ({path} | float) * -1 }}}}"
        return f"{{{{ {path} }}}}"

    def _device(self, scope) -> dict:
        full = scope not in self._device_sent
        self._device_sent.add(scope)
        if scope == SYSTEM_SCOPE:
            if not full:
                return {"ids": topics.SYSTEM_DEVICE_ID}
            return {**DEVICE_BASE, "sw": self._sw.get(scope, DEVICE_BASE["sw"]), "name": "Seplos BMS System",
                    "ids": topics.SYSTEM_DEVICE_ID, "via_device": topics.pack_device_id(0)}
        device: dict[str, Any] = {"ids": topics.pack_device_id(scope)}
        if full:
            master = "Master" if scope == 0 else "Slave"
            device = {**DEVICE_BASE, "sw": self._sw.get(scope, DEVICE_BASE["sw"]), "name": f"Seplos BMS Pack-{scope} ({master})", **device}
        if scope > 0:
            device["via_device"] = topics.pack_device_id(0)
        return device

    def render(self, entity: Entity, scope) -> dict:
        """Discovery config of `entity` for a pack number or the system device."""
        if scope == SYSTEM_SCOPE:
            object_id = f"{topics.SYSTEM_DEVICE_ID}_{entity.key}"
            state_topic = topics.system_sensors(self.topic)
            availability = topics.system_availability(self.topic)
        else:
            object_id = f"{topics.pack_device_id(scope)}_{entity.key}"
            state_topic = (topics.pack_heartbeat if entity.kind == "heartbeat" else topics.pack_sensors)(self.topic, scope)
            availability = topics.pack_availability(self.topic, scope)
        config: dict[str, Any] = {
            "name": entity.name,
            "uniq_id": object_id,
            "obj_id": object_id,
            "stat_t": state_topic,
            "val_tpl": self._template(entity, scope),
            "avty": [{"t": topics.availability(self.topic)}, {"t": availability}],
            "avty_mode": "all",
            "dev": self._device(scope),
        }
        for short, attr in _OPTIONAL.items():
            value = getattr(entity, attr)
            if value is not None:
                config[short] = value
        if entity.kind == "parameters":
            config["json_attr_t"] = topics.pack_sensors(self.topic, scope)
            config["json_attr_tpl"] = "{{ value_json.get('parameters', {}) | tojson }}"
        # newer HA versions derive the entity ID from device and entity name; default_entity_id keeps
        # the IDs stable (seplos_bms_pack_<n>_<key>)
        config["default_entity_id"] = f"{entity.domain}.{object_id}"
        return config

    # --- publishing ---------------------------------------------------------------------------

    def _node(self, scope) -> str:
        return topics.SYSTEM_NODE if scope == SYSTEM_SCOPE else topics.pack_node(scope)

    def _publish(self, domain: str, scope, key: str, config: dict | None) -> None:
        topic = topics.discovery_topic(self.prefix, domain, self._node(scope), key)
        try:
            self.client.publish(topic, json.dumps(config) if config is not None else "", retain=True, qos=1)
        except Exception as e:
            logger.error("Failed to publish discovery config %s: %s", topic, e)

    def _publish_entity(self, entity: Entity, scope) -> None:
        self._publish(entity.domain, scope, entity.key, self.render(entity, scope))

    def _remove(self, domain: str, scope, key: str) -> None:
        self._publish(domain, scope, key, None)

    def publish_pack(self, pack_no: int, pack_bus: bool) -> None:
        """
        All entities of a pack. pack_bus: connected to RS485-1/2 (single pack mode), where the
        extended status, parameters and serial number are available.
        """
        self._device_sent.discard(pack_no)
        removed: list[Entity] = []
        for entity in PACK_ENTITIES:
            for item in entity.expand():
                if item.pack_bus_only and not pack_bus:
                    removed.append(item)
                else:
                    self._publish_entity(item, pack_no)
        for item in removed:
            self._remove(item.domain, pack_no, item.key)
        # retained configs of former versions
        for domain, key in OBSOLETE:
            self._remove(domain, pack_no, key)
        logger.debug("Discovery configs published for pack %s", pack_no)

    def publish_system(self) -> None:
        """The system device (Modbus 0x1000 of the master, multi pack mode)."""
        self._device_sent.discard(SYSTEM_SCOPE)
        self._system_active = True
        for entity in SYSTEM:
            self._publish_entity(entity, SYSTEM_SCOPE)

    def remove_system(self) -> None:
        for entity in SYSTEM:
            self._remove(entity.domain, SYSTEM_SCOPE, entity.key)

    def update_cell_count(self, pack_no: int, cell_count: int) -> None:
        """Remove the per-cell entities above the pack's actual cell count (packs with < 16 cells)."""
        if self._cells.get(pack_no) == cell_count:
            return
        self._cells[pack_no] = cell_count
        for domain, key in CELL_ENTITIES:
            for i in range(cell_count + 1, 17):
                self._remove(domain, pack_no, f"{key}_{i}")

    def update_firmware(self, pack_no: int, firmware: str) -> None:
        """
        Show the real firmware in the device info. The configs are sent before the device info is
        read, so the device block is republished with one config. The system device shows the master's.
        """
        if self._sw.get(pack_no) == firmware:
            return
        self._sw[pack_no] = firmware
        self._device_sent.discard(pack_no)
        self._publish_entity(FIRMWARE_ENTITY, pack_no)
        if pack_no == 0 and self._system_active:
            self._sw[SYSTEM_SCOPE] = firmware
            self._device_sent.discard(SYSTEM_SCOPE)
            self._publish_entity(SYSTEM[0], SYSTEM_SCOPE)
