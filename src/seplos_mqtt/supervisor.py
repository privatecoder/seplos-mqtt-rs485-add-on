"""Home Assistant Supervisor API: remove the legacy connection options once they are migrated."""
from __future__ import annotations

import json
import logging
import urllib.request

logger = logging.getLogger("SeplosBMS")

# connection options that only seed /data/monitoring.json (see monitoring/migration.py)
LEGACY_OPTIONS = (
    "rs485_remote_ip",
    "rs485_remote_port",
    "serial_interface",
    "serial_baudrate",
    "number_of_packs",
    "min_cell_voltage",
    "max_cell_voltage",
    "nominal_cell_voltage",
    "mqtt_update_interval",
    "maint_firmware",
)
TIMEOUT_S = 5


def _call(method: str, url: str, token: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310 (fixed http URL)
        return json.loads(response.read() or b"{}")


def clear_legacy_options(token: str, base: str = "http://supervisor") -> None:
    """Drop the legacy connection options from the add-on configuration.

    Every failure is logged and swallowed: polling never depends on this, and the next start tries again.
    """
    try:
        # the raw saved options: /options/config would hand back resolved "!secret" values
        reply = _call("GET", f"{base}/addons/self/info", token)
        data = reply.get("data")
        options = data.get("options") if isinstance(data, dict) else None
        if not isinstance(options, dict):
            logger.warning("Could not read the add-on options from the Supervisor")
            return
        if not any(key in options for key in LEGACY_OPTIONS):
            return
        kept = {key: value for key, value in options.items() if key not in LEGACY_OPTIONS}
        _call("POST", f"{base}/addons/self/options", token, {"options": kept})
        logger.info("Removed the legacy connection options from the add-on configuration (now in the settings app)")
    except Exception as e:  # noqa: BLE001 (URLError, timeout, bad JSON, ...: all non-fatal)
        logger.warning("Could not remove the legacy connection options: %s", e)
