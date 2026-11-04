"""Typed, strictly validated monitoring configuration document."""

from __future__ import annotations

import copy
import ipaddress
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# the pty that run.sh creates with socat for a TCP RS485 gateway
TCP_PTY_PATH = "/tmp/vcom0"

_HOST_RE = re.compile(r"^[A-Za-z0-9.:\-\[\]]{1,253}$")


def valid_gateway_host(host: str) -> bool:
    """A host name or IP literal the socat bridge accepts (shared with ``bridge.SocatBridge``)."""
    if not _HOST_RE.fullmatch(host):          # fullmatch: "$" would accept a trailing newline
        return False
    if ":" not in host:
        return "[" not in host and "]" not in host
    inner = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        ipaddress.IPv6Address(inner)
    except ValueError:
        return False
    return True

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
                elif not valid_gateway_host(host):
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


_SET_KEYS = frozenset({"id", "name", "packs", "wiring", "plus", "minus"})
WIRINGS = ("diagonal", "common_busbar", "unknown")
MAX_SET_NAME = 40


@dataclass(frozen=True)
class ParallelSet:
    """Packs wired in parallel. The packs are kept in physical order; ``plus``/``minus`` are the packs
    carrying the battery cables of a diagonal wiring (``None``: not known)."""

    id: int
    name: str
    packs: tuple[int, ...]
    wiring: str = "unknown"  # "diagonal" | "common_busbar" | "unknown"
    plus: int | None = None
    minus: int | None = None
    # unknown keys (settings of later versions) survive load and save untouched
    _extra: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any, path: str = "parallel_sets[0]") -> ParallelSet:
        if not isinstance(data, Mapping):
            raise ConfigValidationError([(path, "type")])
        problems: list[Problem] = []

        def bad(key: str, kind: str) -> None:
            problems.append((f"{path}.{key}", kind))

        set_id = data.get("id")
        if "id" not in data:
            bad("id", "required")
        elif not _is_int(set_id):
            bad("id", "type")
        elif set_id < 1:
            bad("id", "range")

        name = ""
        if "name" not in data:
            bad("name", "required")
        elif not isinstance(data["name"], str):
            bad("name", "type")
        else:
            name = data["name"].strip()
            if not 1 <= len(name) <= MAX_SET_NAME:
                bad("name", "range")

        packs: list[int] = []
        packs_ok = False
        if "packs" not in data:
            bad("packs", "required")
        elif not isinstance(data["packs"], list):
            bad("packs", "type")
        else:
            packs_ok = True
            for j, address in enumerate(data["packs"]):
                if not _is_int(address):
                    bad(f"packs[{j}]", "type")
                    packs_ok = False
                elif not 0 <= address <= 15:
                    bad(f"packs[{j}]", "range")
                    packs_ok = False
                elif address in packs:
                    bad(f"packs[{j}]", "duplicate")
                    packs_ok = False
                else:
                    packs.append(address)
            if not 2 <= len(data["packs"]) <= 16:
                bad("packs", "range")
                packs_ok = False

        wiring = "unknown"
        wiring_ok = False
        if "wiring" not in data:
            bad("wiring", "required")
        elif data["wiring"] not in WIRINGS or not isinstance(data["wiring"], str):
            bad("wiring", "value")
        else:
            wiring = data["wiring"]
            wiring_ok = True

        taps: dict[str, int | None] = {"plus": None, "minus": None}
        taps_ok = True
        for key in taps:
            value = data.get(key)
            if value is None:
                continue
            if not _is_int(value):
                bad(key, "type")
                taps_ok = False
            elif packs_ok and value not in packs:
                bad(key, "value")
                taps_ok = False
            else:
                taps[key] = value
        if wiring == "diagonal" and wiring_ok:
            for key in taps:
                if data.get(key) is None:
                    bad(key, "required")
                    taps_ok = False
            if taps_ok and taps["plus"] == taps["minus"]:
                bad("plus", "value")
                bad("minus", "value")

        if problems:
            raise ConfigValidationError(problems)
        return cls(
            id=set_id,
            name=name,
            packs=tuple(packs),
            wiring=wiring,
            plus=taps["plus"],
            minus=taps["minus"],
            _extra={k: copy.deepcopy(v) for k, v in data.items() if k not in _SET_KEYS},
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = copy.deepcopy(dict(self._extra))
        out.update(
            id=self.id,
            name=self.name,
            packs=list(self.packs),
            wiring=self.wiring,
            plus=self.plus,
            minus=self.minus,
        )
        return out


@dataclass(frozen=True)
class MonitoringConfig:
    connection: ConnectionSettings
    parallel_sets: tuple[ParallelSet, ...] = ()
    # the id of the next new set; ids are never reused
    next_set_id: int = 1
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

        next_set_id = 1
        next_ok = True
        if "next_set_id" in data:
            next_set_id = data["next_set_id"]
            if not _is_int(next_set_id):
                problems.append(("next_set_id", "type"))
                next_ok = False
            elif next_set_id < 1:
                problems.append(("next_set_id", "range"))
                next_ok = False

        sets: list[ParallelSet] = []
        raw_sets = data.get("parallel_sets", [])
        if not isinstance(raw_sets, list):
            problems.append(("parallel_sets", "type"))
        else:
            seen_ids: set[int] = set()
            seen_names: set[str] = set()
            seen_packs: set[int] = set()
            for i, raw in enumerate(raw_sets):
                path = f"parallel_sets[{i}]"
                try:
                    item = ParallelSet.from_dict(raw, path)
                except ConfigValidationError as e:
                    problems.extend(e.problems)
                    continue
                sets.append(item)
                if item.id in seen_ids:
                    problems.append((f"{path}.id", "duplicate"))
                elif next_ok and item.id >= next_set_id:
                    problems.append((f"{path}.id", "value"))
                seen_ids.add(item.id)
                if item.name.casefold() in seen_names:
                    problems.append((f"{path}.name", "duplicate"))
                seen_names.add(item.name.casefold())
                for j, address in enumerate(item.packs):
                    if address in seen_packs:
                        problems.append((f"{path}.packs[{j}]", "duplicate"))
                seen_packs.update(item.packs)

        if problems or connection is None:
            raise ConfigValidationError(problems)
        known = {"connection", "parallel_sets", "next_set_id"}
        extra = {k: copy.deepcopy(v) for k, v in data.items() if k not in known}
        return cls(connection=connection, parallel_sets=tuple(sets), next_set_id=next_set_id, _extra=extra)

    def to_dict(self) -> dict[str, Any]:
        out = copy.deepcopy(dict(self._extra))
        out["connection"] = self.connection.to_dict()
        # a stage-1 document stays as it is until the first set is saved
        if self.parallel_sets or self.next_set_id != 1:
            out["parallel_sets"] = [s.to_dict() for s in self.parallel_sets]
            out["next_set_id"] = self.next_set_id
        return out


def set_activity(config: MonitoringConfig) -> list[tuple[bool, str | None]]:
    """``(active, reason)`` per stored set: a set works only in multi pack mode and when all its
    packs are configured. Inactive sets stay stored (not a validation error)."""
    return activity(config.parallel_sets, config.connection.pack_count)


def activity(sets: tuple[ParallelSet, ...], count: int) -> list[tuple[bool, str | None]]:
    """``set_activity`` for a set list and the configured pack count."""
    out: list[tuple[bool, str | None]] = []
    for item in sets:
        if count == 1:
            out.append((False, "single_pack"))
        elif any(address >= count for address in item.packs):
            out.append((False, "pack_not_configured"))
        else:
            out.append((True, None))
    return out
