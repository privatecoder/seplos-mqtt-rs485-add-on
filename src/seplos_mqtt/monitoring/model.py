"""Typed, strictly validated monitoring configuration document."""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# the pty that run.sh creates with socat for a TCP RS485 gateway
TCP_PTY_PATH = "/tmp/vcom0"

Problem = tuple[str, str]  # (json_path, key), e.g. ("connection.port", "range")


class ConfigValidationError(ValueError):
    """The document is invalid; ``problems`` lists every ``(json_path, key)`` found."""

    def __init__(self, problems: list[Problem]) -> None:
        self.problems = problems
        super().__init__("; ".join(f"{path}: {key}" for path, key in problems))


def _is_int(value: Any) -> bool:
    return type(value) is int


def _is_number(value: Any) -> bool:
    return type(value) in (int, float)


def _voltage_problem(value: Any) -> str | None:
    """``type`` / ``range`` for one cell voltage limit, ``None`` if valid (checks ints before any
    float conversion, so huge JSON integers cannot overflow)."""
    if not _is_number(value):
        return "type"
    if _is_int(value):
        if abs(value) > 1000:
            return "range"
    elif not math.isfinite(value):
        return "range"
    return None if 0 < value <= 5 else "range"


_CONNECTION_KEYS = frozenset(
    {
        "transport",
        "host",
        "port",
        "device",
        "baud",
        "pack_count",
        "maint_firmware",
        "min_cell_voltage",
        "max_cell_voltage",
        "nominal_cell_voltage",
        "update_interval",
    }
)


@dataclass(frozen=True)
class ConnectionSettings:
    transport: str  # "tcp" | "serial"
    host: str = ""  # tcp
    port: int = 4196  # tcp
    device: str = ""  # serial, absolute path
    baud: str | int = "auto"  # "auto" | 9600 | 19200
    pack_count: int = 1
    maint_firmware: bool = False
    min_cell_voltage: float = 2.5
    max_cell_voltage: float = 3.65
    nominal_cell_voltage: float = 3.2
    update_interval: int = 1
    # unknown keys (settings of later versions) survive load and save untouched
    _extra: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any, path: str = "connection") -> ConnectionSettings:
        if not isinstance(data, Mapping):
            raise ConfigValidationError([(path, "type")])
        problems: list[Problem] = []

        def bad(key: str, kind: str) -> None:
            problems.append((f"{path}.{key}", kind))

        transport = data.get("transport")
        if "transport" not in data:
            bad("transport", "required")
        elif transport not in ("tcp", "serial") or not isinstance(transport, str):
            bad("transport", "value")

        host = ""
        port = 4196
        device = ""
        if transport == "tcp":
            if "host" not in data:
                bad("host", "required")
            else:
                host = data["host"]
                if not isinstance(host, str):
                    bad("host", "type")
                elif not host or any(c.isspace() for c in host):
                    bad("host", "format")
            if "port" in data:
                port = data["port"]
                if not _is_int(port):
                    bad("port", "type")
                elif not 1 <= port <= 65535:
                    bad("port", "range")
        elif transport == "serial":
            if "device" not in data:
                bad("device", "required")
            else:
                device = data["device"]
                if not isinstance(device, str):
                    bad("device", "type")
                elif not device.startswith("/"):
                    bad("device", "format")

        baud: str | int = "auto"
        if "baud" in data:
            baud = data["baud"]
            if not (_is_int(baud) or isinstance(baud, str)):
                bad("baud", "type")
            elif baud not in ("auto", 9600, 19200):
                bad("baud", "value")

        pack_count = 1
        if "pack_count" in data:
            pack_count = data["pack_count"]
            if not _is_int(pack_count):
                bad("pack_count", "type")
            elif not 1 <= pack_count <= 16:
                bad("pack_count", "range")

        maint_firmware = False
        if "maint_firmware" in data:
            maint_firmware = data["maint_firmware"]
            if not isinstance(maint_firmware, bool):
                bad("maint_firmware", "type")

        volts: dict[str, float] = {"min_cell_voltage": 2.5, "max_cell_voltage": 3.65}
        volts_ok = True
        for key in volts:
            if key in data:
                kind = _voltage_problem(data[key])
                if kind is not None:
                    bad(key, kind)
                    volts_ok = False
                else:
                    volts[key] = float(data[key])
        if volts_ok and not volts["min_cell_voltage"] < volts["max_cell_voltage"]:
            bad("min_cell_voltage", "range")
            bad("max_cell_voltage", "range")

        # independent of min/max: no ordering check
        nominal = 3.2
        if "nominal_cell_voltage" in data:
            kind = _voltage_problem(data["nominal_cell_voltage"])
            if kind is not None:
                bad("nominal_cell_voltage", kind)
            else:
                nominal = float(data["nominal_cell_voltage"])

        update_interval = 1
        if "update_interval" in data:
            update_interval = data["update_interval"]
            if not _is_int(update_interval):
                bad("update_interval", "type")
            elif update_interval < 0:
                bad("update_interval", "range")

        if problems:
            raise ConfigValidationError(problems)
        return cls(
            transport=transport,
            host=host,
            port=port,
            device=device,
            baud=baud,
            pack_count=pack_count,
            maint_firmware=maint_firmware,
            min_cell_voltage=volts["min_cell_voltage"],
            max_cell_voltage=volts["max_cell_voltage"],
            nominal_cell_voltage=nominal,
            update_interval=update_interval,
            _extra={k: copy.deepcopy(v) for k, v in data.items() if k not in _CONNECTION_KEYS},
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = copy.deepcopy(dict(self._extra))
        out["transport"] = self.transport
        if self.transport == "tcp":
            out["host"] = self.host
            out["port"] = self.port
        else:
            out["device"] = self.device
        out.update(
            baud=self.baud,
            pack_count=self.pack_count,
            maint_firmware=self.maint_firmware,
            min_cell_voltage=self.min_cell_voltage,
            max_cell_voltage=self.max_cell_voltage,
            nominal_cell_voltage=self.nominal_cell_voltage,
            update_interval=self.update_interval,
        )
        return out


@dataclass(frozen=True)
class MonitoringConfig:
    connection: ConnectionSettings
    # unknown top-level keys (sections of later versions) survive load and save untouched
    _extra: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any) -> MonitoringConfig:
        if not isinstance(data, Mapping):
            raise ConfigValidationError([("", "type")])
        problems: list[Problem] = []
        connection = None
        if "connection" not in data:
            problems.append(("connection", "required"))
        else:
            try:
                connection = ConnectionSettings.from_dict(data["connection"])
            except ConfigValidationError as e:
                problems.extend(e.problems)
        if problems or connection is None:
            raise ConfigValidationError(problems)
        extra = {k: copy.deepcopy(v) for k, v in data.items() if k != "connection"}
        return cls(connection=connection, _extra=extra)

    def to_dict(self) -> dict[str, Any]:
        out = copy.deepcopy(dict(self._extra))
        out["connection"] = self.connection.to_dict()
        return out
