"""Runtime configuration from the environment variables that run.sh exports."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Any


def _env(env: Mapping[str, str], name: str, default: Any, cast: type) -> Any:
    value = env.get(name)
    if value is None or value == "":
        return default
    try:
        if cast is bool:
            return str(value).lower() in ("true", "1", "yes", "on")
        return cast(value)
    except (ValueError, TypeError):
        return default


@dataclass(frozen=True)
class Config:
    # BMS
    min_cell_voltage: float = 2.5
    max_cell_voltage: float = 3.65
    number_of_packs: int = 1
    # Serial
    serial_interface: str = "/tmp/vcom0"
    # MQTT
    mqtt_host: str = "192.168.1.100"
    mqtt_port: int = 1883
    mqtt_username: str = "seplos-mqtt"
    mqtt_password: str = field(default="", repr=False)
    mqtt_topic: str = "seplos"
    mqtt_update_interval: int = 0
    # Home Assistant discovery
    enable_ha_discovery: bool = True
    ha_discovery_prefix: str = "homeassistant"
    invert_ha_dis_charge_measurements: bool = True
    # Logging
    logging_level: str = "INFO"

    @property
    def multi_pack(self) -> bool:
        """
        Multiple packs are read via the RS485 bus on the CAN port (UART1, 9600 baud),
        a single pack via RS485-1/2 (UART0, 19200 baud).
        """
        return self.number_of_packs > 1

    @property
    def baudrate(self) -> int:
        return 9600 if self.multi_pack else 19200

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        d = cls()
        return cls(
            min_cell_voltage=_env(env, "MIN_CELL_VOLTAGE", d.min_cell_voltage, float),
            max_cell_voltage=_env(env, "MAX_CELL_VOLTAGE", d.max_cell_voltage, float),
            number_of_packs=_env(env, "NUMBER_OF_PACKS", d.number_of_packs, int),
            serial_interface=_env(env, "SERIAL_INTERFACE", d.serial_interface, str),
            mqtt_host=_env(env, "MQTT_HOST", d.mqtt_host, str),
            mqtt_port=_env(env, "MQTT_PORT", d.mqtt_port, int),
            mqtt_username=_env(env, "MQTT_USERNAME", d.mqtt_username, str),
            mqtt_password=_env(env, "MQTT_PASSWORD", d.mqtt_password, str),
            mqtt_topic=_env(env, "MQTT_TOPIC", d.mqtt_topic, str),
            mqtt_update_interval=_env(env, "MQTT_UPDATE_INTERVAL", d.mqtt_update_interval, int),
            enable_ha_discovery=_env(env, "ENABLE_HA_DISCOVERY_CONFIG", d.enable_ha_discovery, bool),
            ha_discovery_prefix=_env(env, "HA_DISCOVERY_PREFIX", d.ha_discovery_prefix, str),
            invert_ha_dis_charge_measurements=_env(env, "INVERT_HA_DIS_CHARGE_MEASUREMENTS", d.invert_ha_dis_charge_measurements, bool),
            logging_level=_env(env, "LOGGING_LEVEL", "info", str).upper(),
        )

    def loggable(self) -> dict[str, Any]:
        """All settings except secrets, for the debug log."""
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "mqtt_password"}
