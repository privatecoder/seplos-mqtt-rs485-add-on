"""Runtime configuration from the environment variables that run.sh exports."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any

from .monitoring.model import TCP_PTY_PATH

if TYPE_CHECKING:
    from .monitoring.model import MonitoringConfig


class ConfigError(ValueError):
    pass


def _env(env: Mapping[str, str], name: str, default: Any, cast: type) -> Any:
    value = env.get(name)
    if value is None or value == "":
        return default
    if cast is bool:
        text = str(value).strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off"):
            return False
        raise ConfigError(f"{name}={value!r} is not a boolean")
    try:
        return cast(value)
    except (ValueError, TypeError) as e:
        raise ConfigError(f"{name}={value!r} is not a valid {cast.__name__}") from e


def _baudrate(value: str | None) -> int:
    """'auto'/empty -> 0 (by mode), else 9600 or 19200."""
    if value is None or str(value).strip().lower() in ("", "auto", "0"):
        return 0
    try:
        baud = int(value)
    except ValueError:
        baud = -1
    if baud not in (9600, 19200):
        raise ConfigError(f"SERIAL_BAUDRATE={value!r} (auto, 9600 or 19200)")
    return baud


@dataclass(frozen=True)
class Config:
    # BMS
    min_cell_voltage: float = 2.5
    max_cell_voltage: float = 3.65
    # nominal cell voltage (manual): only for the calculated full energy
    nominal_cell_voltage: float = 3.2
    number_of_packs: int = 1
    # Serial
    serial_interface: str = "/tmp/vcom0"
    # 0 = by mode (single pack 19200, multi pack 9600); firmware 16.04.x uses 9600 on RS485-1/2 too
    serial_baudrate: int = 0
    # RS485-to-Ethernet gateway bridged to serial_interface by socat (empty = local serial device)
    remote_host: str = ""
    remote_port: int = 4196
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
    # read the extra data of the MAINT 1.5.4 / 1.6.4 custom firmware (read-only)
    maint_firmware: bool = False
    # Logging
    logging_level: str = "INFO"
    # persistent state (energy counters); empty = /data/seplos_state.json if writable
    state_file: str = ""

    @property
    def multi_pack(self) -> bool:
        """
        Multiple packs are read via the RS485 bus on the CAN port (UART1, 9600 baud),
        a single pack via RS485-1/2 (UART0, 19200 baud).
        """
        return self.number_of_packs > 1

    @property
    def baudrate(self) -> int:
        if self.serial_baudrate:
            return self.serial_baudrate
        return 9600 if self.multi_pack else 19200

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        d = cls()
        remote_host = (env.get("RS485_REMOTE_IP") or "").strip()
        return cls(
            remote_host=remote_host,
            remote_port=_env(env, "RS485_REMOTE_PORT", d.remote_port, int) if remote_host else d.remote_port,
            min_cell_voltage=_env(env, "MIN_CELL_VOLTAGE", d.min_cell_voltage, float),
            max_cell_voltage=_env(env, "MAX_CELL_VOLTAGE", d.max_cell_voltage, float),
            nominal_cell_voltage=_env(env, "NOMINAL_CELL_VOLTAGE", d.nominal_cell_voltage, float),
            number_of_packs=_env(env, "NUMBER_OF_PACKS", d.number_of_packs, int),
            serial_interface=_env(env, "SERIAL_INTERFACE", d.serial_interface, str),
            serial_baudrate=_baudrate(env.get("SERIAL_BAUDRATE")),
            mqtt_host=_env(env, "MQTT_HOST", d.mqtt_host, str),
            mqtt_port=_env(env, "MQTT_PORT", d.mqtt_port, int),
            mqtt_username=_env(env, "MQTT_USERNAME", d.mqtt_username, str),
            mqtt_password=_env(env, "MQTT_PASSWORD", d.mqtt_password, str),
            mqtt_topic=_env(env, "MQTT_TOPIC", d.mqtt_topic, str),
            mqtt_update_interval=_env(env, "MQTT_UPDATE_INTERVAL", d.mqtt_update_interval, int),
            enable_ha_discovery=_env(env, "ENABLE_HA_DISCOVERY_CONFIG", d.enable_ha_discovery, bool),
            ha_discovery_prefix=_env(env, "HA_DISCOVERY_PREFIX", d.ha_discovery_prefix, str),
            invert_ha_dis_charge_measurements=_env(env, "INVERT_HA_DIS_CHARGE_MEASUREMENTS", d.invert_ha_dis_charge_measurements, bool),
            maint_firmware=_env(env, "MAINT_FIRMWARE", d.maint_firmware, bool),
            logging_level=_env(env, "LOGGING_LEVEL", "info", str).upper(),
            state_file=_env(env, "STATE_FILE", d.state_file, str),
        )

    @classmethod
    def from_monitoring(
        cls, mon: MonitoringConfig, env: Mapping[str, str] | None = None
    ) -> Config:
        """Connection settings from the monitoring document, everything else like ``from_env``."""
        env = os.environ if env is None else env
        d = cls()
        conn = mon.connection
        return cls(
            remote_host=conn.host if conn.transport == "tcp" else "",
            remote_port=conn.port if conn.transport == "tcp" else d.remote_port,
            min_cell_voltage=conn.min_cell_voltage,
            max_cell_voltage=conn.max_cell_voltage,
            number_of_packs=conn.pack_count,
            serial_interface=conn.device if conn.transport == "serial" else TCP_PTY_PATH,
            serial_baudrate=0 if conn.baud == "auto" else int(conn.baud),
            mqtt_host=_env(env, "MQTT_HOST", d.mqtt_host, str),
            mqtt_port=_env(env, "MQTT_PORT", d.mqtt_port, int),
            mqtt_username=_env(env, "MQTT_USERNAME", d.mqtt_username, str),
            mqtt_password=_env(env, "MQTT_PASSWORD", d.mqtt_password, str),
            mqtt_topic=_env(env, "MQTT_TOPIC", d.mqtt_topic, str),
            mqtt_update_interval=conn.update_interval,
            enable_ha_discovery=_env(env, "ENABLE_HA_DISCOVERY_CONFIG", d.enable_ha_discovery, bool),
            ha_discovery_prefix=_env(env, "HA_DISCOVERY_PREFIX", d.ha_discovery_prefix, str),
            invert_ha_dis_charge_measurements=_env(env, "INVERT_HA_DIS_CHARGE_MEASUREMENTS", d.invert_ha_dis_charge_measurements, bool),
            maint_firmware=conn.maint_firmware,
            logging_level=_env(env, "LOGGING_LEVEL", "info", str).upper(),
            state_file=_env(env, "STATE_FILE", d.state_file, str),
        )

    def __post_init__(self) -> None:
        problems = []
        if not 1 <= self.number_of_packs <= 16:
            problems.append(f"number_of_packs {self.number_of_packs} (1-16)")
        if not 0 < self.min_cell_voltage < self.max_cell_voltage <= 5:
            problems.append(f"cell voltage range {self.min_cell_voltage}-{self.max_cell_voltage} V")
        if not 0 < self.nominal_cell_voltage <= 5:
            problems.append(f"nominal cell voltage {self.nominal_cell_voltage} V")
        if not 1 <= self.mqtt_port <= 65535:
            problems.append(f"mqtt_port {self.mqtt_port}")
        if self.mqtt_update_interval < 0:
            problems.append(f"mqtt_update_interval {self.mqtt_update_interval}")
        for name in ("mqtt_topic", "ha_discovery_prefix"):
            value = getattr(self, name)
            if not value or any(c in value for c in "+#") or value.endswith("/"):
                problems.append(f"{name} {value!r} (no wildcards, no trailing slash)")
        if problems:
            raise ConfigError("; ".join(problems))

    def loggable(self) -> dict[str, Any]:
        """All settings except secrets, for the debug log."""
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "mqtt_password"}
