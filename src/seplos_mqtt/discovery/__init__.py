"""Home Assistant MQTT discovery: renders the entity catalog and publishes retained configs."""
from __future__ import annotations

import json
import logging
from typing import Any

from .. import topics
from .catalog import BALANCER_ENTITY, CELL_ENTITIES, OBSOLETE, PACK_ENTITIES, SET_ENTITIES, SYSTEM, Entity

logger = logging.getLogger("SeplosBMS.discovery")

# Physical pack device: product facts (generation BMS 2.0, OEM platform EMU1101, variant 1101-10E,
# hardware V16). The firmware ("sw") and serial ("sn") are per pack and added when known.
DEVICE_BASE = {
    "mf": "Seplos",
    "mdl": "BMS 2.0 / EMU1101",
    "mdl_id": "1101-10E",
    "hw": "V16",
}

# Virtual devices (bank aggregate, parallel set) are no protection boards: no hardware, firmware or
# serial. Empty strings clear what HA registered from former versions (omitted fields are kept).
VIRTUAL_BASE = {"mf": "Seplos", "hw": "", "sw": ""}
SYSTEM_MODEL = "Bank aggregate (virtual)"
SET_MODEL = "Parallel set (virtual)"

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
    "ops": "options",
}

SYSTEM_SCOPE = "system"
MAINT_CONDITIONS = frozenset({"maint", "maint_storage", "maint_uart1", "maint_lock", "maint_marker", "maint_hard_stop", "maint_rtc"})


def _is_set(scope: Any) -> bool:
    """Parallel set scopes are ``("set", id)``; packs are ints, the system device is a string."""
    return isinstance(scope, tuple)


class Discovery:
    def __init__(self, client, mqtt_topic: str, prefix: str, invert: bool, maint_enabled: bool = False) -> None:
        self.maint_enabled = maint_enabled
        self.client = client
        self.topic = mqtt_topic
        self.prefix = prefix
        self.invert = invert
        # firmware version shown in the device info, per pack number
        self._sw: dict = {}
        # battery serial (0xA2 / MAINT UART1) shown in the device info, per pack number
        self._sn: dict = {}
        # cell count per pack once known
        self._cells: dict[int, int] = {}
        # MAINT capabilities per pack (discovery conditions)
        self._caps: dict[int, set[str]] = {}
        self._system_active = False
        # names of the published parallel sets (device name)
        self._set_names: dict[int, str] = {}
        # a publish was not accepted: the app reconciles everything again later
        self.failed = False

    # --- rendering ----------------------------------------------------------------------------

    def _template(self, entity: Entity, scope) -> str:
        if entity.kind == "info":
            return f"{{{{ value_json.get('info', {{}}).get('normal', {{}}).get('{entity.key}', 'unknown') }}}}"
        if entity.kind == "heartbeat":
            return "{{ value_json.last_publish }}"
        if entity.kind == "parameters":
            return "{{ value_json.get('parameters', {}).get('read_at') }}"
        if entity.kind in ("balancer", "summary"):
            # "unknown" is no enum option: it becomes HA's unknown state (None renders as the MQTT "None" payload)
            path = f"value_json.{entity.group}.normal.{entity.key}"
            return f"{{{{ none if {path} == 'unknown' else {path} }}}}"
        state = "binary" if entity.domain == "binary_sensor" else "normal"
        flat = scope == SYSTEM_SCOPE or _is_set(scope)       # system and set payloads have no group level
        path = f"value_json.{state}.{entity.key}" if flat else f"value_json.{entity.group}.{state}.{entity.key}"
        if entity.invert and self.invert and entity.domain == "sensor":
            # keep unknown (null) values unknown instead of a template error
            return f"{{{{ none if {path} is none else ({path} | float) * -1 }}}}"
        return f"{{{{ {path} }}}}"

    def _device(self, scope, full: bool) -> dict:
        if _is_set(scope):
            device: dict[str, Any] = {"ids": topics.set_device_id(scope[1])}
            if full:
                device = {**VIRTUAL_BASE, "mdl": SET_MODEL,
                          "name": f"Seplos Parallel Set {self._set_names.get(scope[1], scope[1])}", **device,
                          "via_device": topics.pack_device_id(0)}
            return device
        if scope == SYSTEM_SCOPE:
            if not full:
                return {"ids": topics.SYSTEM_DEVICE_ID}
            return {**VIRTUAL_BASE, "mdl": SYSTEM_MODEL, "name": "Seplos BMS System",
                    "ids": topics.SYSTEM_DEVICE_ID, "via_device": topics.pack_device_id(0)}
        device: dict[str, Any] = {"ids": topics.pack_device_id(scope)}
        if full:
            master = "Master" if scope == 0 else "Slave"
            device = {**DEVICE_BASE, "sw": self._sw.get(scope, ""), "name": f"Seplos BMS Pack-{scope} ({master})", **device}
            if self._sn.get(scope):
                device["sn"] = self._sn[scope]
        if scope > 0:
            device["via_device"] = topics.pack_device_id(0)
        return device

    def render(self, entity: Entity, scope, full_device: bool = False) -> dict:
        """Discovery config of `entity` for a pack number or the system device (pure)."""
        if _is_set(scope):
            object_id = f"{topics.set_device_id(scope[1])}_{entity.key}"
            state_topic = topics.set_sensors(self.topic, scope[1])
            availability = topics.set_availability(self.topic, scope[1])
        elif scope == SYSTEM_SCOPE:
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
            "dev": self._device(scope, full_device),
        }
        for short, attr in _OPTIONAL.items():
            value = getattr(entity, attr)
            if value is not None:
                config[short] = list(value) if short == "ops" else value
        if entity.disabled:
            config["en"] = False
        if entity.kind == "parameters":
            config["json_attr_t"] = topics.pack_sensors(self.topic, scope)
            config["json_attr_tpl"] = "{{ value_json.get('parameters', {}) | tojson }}"
        elif entity.attributes:
            config["json_attr_t"] = state_topic if _is_set(scope) else topics.pack_sensors(self.topic, scope)
            config["json_attr_tpl"] = entity.attributes
        # newer HA versions derive the entity ID from device and entity name; default_entity_id keeps
        # the IDs stable (seplos_bms_pack_<n>_<key>)
        config["default_entity_id"] = f"{entity.domain}.{object_id}"
        return config

    # --- publishing ---------------------------------------------------------------------------

    def _node(self, scope) -> str:
        if _is_set(scope):
            return topics.set_node(scope[1])
        return topics.SYSTEM_NODE if scope == SYSTEM_SCOPE else topics.pack_node(scope)

    def _publish(self, domain: str, scope, key: str, config: dict | None) -> bool:
        topic = topics.discovery_topic(self.prefix, domain, self._node(scope), key)
        try:
            info = self.client.publish(topic, json.dumps(config) if config is not None else "", retain=True, qos=1)
        except Exception as e:
            logger.error("Failed to publish discovery config %s: %s", topic, e)
            self.failed = True
            return False
        # Publisher returns a bool, a Paho client an info object with rc
        ok = info if isinstance(info, bool) else getattr(info, "rc", 0) == 0
        if not ok:
            self.failed = True
        return ok

    @staticmethod
    def _anchor(scope) -> Entity:
        """The one entity whose retained config carries the full device block of a device."""
        if _is_set(scope):
            return SET_ENTITIES[0]
        return SYSTEM[0] if scope == SYSTEM_SCOPE else PACK_ENTITIES[0].expand()[0]

    def _publish_entity(self, entity: Entity, scope) -> bool:
        """Only the anchor config of a device carries the full device block, all others the ids: the
        retained configs can never disagree about firmware, serial or name, whatever order HA reads them."""
        full = entity.key == self._anchor(scope).key
        return self._publish(entity.domain, scope, entity.key, self.render(entity, scope, full))

    def _remove(self, domain: str, scope, key: str) -> None:
        self._publish(domain, scope, key, None)

    def publish_pack(self, pack_no: int, pack_bus: bool) -> None:
        """
        All entities of a pack. pack_bus: connected to RS485-1/2 (single pack mode), where the
        extended status, parameters and serial number are available.
        """
        removed: list[Entity] = []
        cells = self._cells.get(pack_no, 16)
        cell_keys = {key for _domain, key in CELL_ENTITIES}
        known = pack_no in self._caps or not self.maint_enabled
        conditions = {"always", *(["pack_bus"] if pack_bus else []), *self._caps.get(pack_no, set())}
        for entity in PACK_ENTITIES:
            for index, item in enumerate(entity.expand(), start=1):
                if not item.when & conditions and not known and item.when & MAINT_CONDITIONS:
                    continue          # MAINT detection pending: keep whatever HA has (no delete/recreate)
                if entity.key in cell_keys and index > cells or not item.when & conditions:
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
        self._system_active = True
        for entity in SYSTEM:
            self._publish_entity(entity, SYSTEM_SCOPE)

    def remove_system(self) -> None:
        for entity in SYSTEM:
            self._remove(entity.domain, SYSTEM_SCOPE, entity.key)

    def publish_set(self, item: Any) -> None:
        """All entities of a parallel set (a ``ParallelSet``); also republishes the device block, so a
        rename shows up."""
        scope = ("set", item.id)
        self._set_names[item.id] = item.name
        for entity in SET_ENTITIES:
            self._publish_entity(entity, scope)

    def remove_set(self, set_id: int) -> bool:
        """Clear every config of a set; True only if the client accepted all of them."""
        scope = ("set", set_id)
        self._set_names.pop(set_id, None)
        results = [self._publish(entity.domain, scope, entity.key, None) for entity in SET_ENTITIES]
        return all(results)

    def publish_balancer(self, pack_no: int) -> bool:
        """The computed external balancer entity of a pack with a profile; True if the client accepted it."""
        return self._publish_entity(BALANCER_ENTITY, pack_no)

    def remove_balancer(self, pack_no: int) -> bool:
        """Clear the config of a pack's balancer entity; True only if the client accepted it."""
        return self._publish(BALANCER_ENTITY.domain, pack_no, BALANCER_ENTITY.key, None)

    def set_capabilities(self, pack_no: int, caps: set[str], pack_bus: bool) -> bool:
        """
        The pack's capabilities are known (MAINT detected or confirmed absent): republish its configs with
        the matching entities. True if anything changed.
        """
        if self._caps.get(pack_no) == caps:
            return False
        self._caps[pack_no] = set(caps)
        self.publish_pack(pack_no, pack_bus)
        return True

    def update_cell_count(self, pack_no: int, cell_count: int) -> None:
        """Reconcile the per-cell entities with the pack's actual cell count (removed above it, restored up to it)."""
        previous = self._cells.get(pack_no, 16)
        if self._cells.get(pack_no) == cell_count:
            return
        self._cells[pack_no] = cell_count
        for entity in PACK_ENTITIES:
            if (entity.domain, entity.key) not in CELL_ENTITIES:
                continue
            for index, item in enumerate(entity.expand(), start=1):
                if index > cell_count:
                    self._remove(item.domain, pack_no, item.key)
                elif index > previous:
                    self._publish_entity(item, pack_no)

    def update_firmware(self, pack_no: int, firmware: str, serial: str | None = None) -> None:
        """
        Show the real firmware and the battery serial (0xA2 or MAINT UART1, never the BMS serial 0xA4)
        in the device info, trimmed but otherwise exactly as reported (placeholders included). The
        configs are sent before the device info is read, so the device block is republished when either
        becomes known or changes: in the same anchor config ``publish_pack`` uses, never in another one.
        A missing or blank serial keeps the last one.
        """
        serial = (serial or "").strip() or self._sn.get(pack_no, "")
        if self._sw.get(pack_no) == firmware and self._sn.get(pack_no, "") == serial:
            return
        self._sw[pack_no] = firmware
        if serial:
            self._sn[pack_no] = serial
        self._publish_entity(self._anchor(pack_no), pack_no)
