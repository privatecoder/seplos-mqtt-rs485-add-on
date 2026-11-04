"""One-time creation of the monitoring configuration from the legacy add-on options."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..config import _baudrate, _env
from .model import TCP_PTY_PATH, ConfigValidationError, MonitoringConfig
from .store import JsonStore, LoadStatus

log = logging.getLogger(__name__)


def from_legacy_env(env: Mapping[str, str]) -> MonitoringConfig:
    """Build the config from the environment variables ``run.sh`` exports.

    Malformed values raise ``ConfigError``; an invalid combination raises
    ``ConfigValidationError`` listing all problems.
    """
    connection: dict[str, Any] = {}
    remote_ip = (env.get("RS485_REMOTE_IP") or "").strip()
    if remote_ip:
        connection["transport"] = "tcp"
        connection["host"] = remote_ip
        connection["port"] = _env(env, "RS485_REMOTE_PORT", 4196, int)
    else:
        connection["transport"] = "serial"
        connection["device"] = _env(env, "SERIAL_INTERFACE", TCP_PTY_PATH, str)
    baud = _baudrate(env.get("SERIAL_BAUDRATE"))
    connection["baud"] = baud if baud else "auto"
    connection["pack_count"] = _env(env, "NUMBER_OF_PACKS", 1, int)
    connection["maint_firmware"] = _env(env, "MAINT_FIRMWARE", False, bool)
    connection["min_cell_voltage"] = _env(env, "MIN_CELL_VOLTAGE", 2.5, float)
    connection["max_cell_voltage"] = _env(env, "MAX_CELL_VOLTAGE", 3.65, float)
    connection["update_interval"] = _env(env, "MQTT_UPDATE_INTERVAL", 1, int)
    return MonitoringConfig.from_dict({"connection": connection})


def load_or_migrate(
    path: Path, env: Mapping[str, str]
) -> tuple[MonitoringConfig | None, JsonStore]:
    """Load ``path``; on a first installation create it from ``env``.

    Returns ``None`` as config for a corrupt file (or one whose content is invalid); such a
    file is never overwritten.
    """
    store = JsonStore(path)
    if store.status is LoadStatus.MISSING:
        config = from_legacy_env(env)
        store.commit(config.to_dict(), 0)
        log.info("created %s from the add-on options", path.name)
        return config, store
    if store.status is LoadStatus.CORRUPT:
        return None, store
    try:
        return MonitoringConfig.from_dict(store.data), store
    except ConfigValidationError as e:
        log.error("%s has invalid content: %s", path.name, e)
        store.mark_corrupt("invalid content")
        return None, store
