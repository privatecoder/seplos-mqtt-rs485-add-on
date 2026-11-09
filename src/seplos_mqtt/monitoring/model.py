"""Typed, strictly validated monitoring configuration document."""

from __future__ import annotations

import copy
import ipaddress
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..cells import catalog
from ..cells import validate as cell_validate

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
        "bus",
        "bus_quiet_ms",
    }
)


BUSES = ("uart0", "uart1")
MIN_BUS_QUIET_MS = 200
MAX_BUS_QUIET_MS = 2000
DEFAULT_BUS_QUIET_MS = 1000


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
    # "uart0" = RS485-1/2 (19200 Bd), "uart1" = RS485 pins of the CAN socket (9600 Bd); several packs need uart1
    bus: str = "uart0"
    # quiet on the bus before each request (the packs end a frame after about 100 ms of silence)
    bus_quiet_ms: int = 1000
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

        # several packs are always read on the CAN socket: the choice exists only for a single pack
        several = _is_int(pack_count) and pack_count > 1
        bus = "uart1" if several else "uart0"
        if "bus" in data:
            if data["bus"] not in BUSES or not isinstance(data["bus"], str):
                bad("bus", "value")
            elif not several:
                bus = data["bus"]

        bus_quiet_ms = DEFAULT_BUS_QUIET_MS
        if "bus_quiet_ms" in data:
            bus_quiet_ms = data["bus_quiet_ms"]
            if not _is_int(bus_quiet_ms):
                bad("bus_quiet_ms", "type")
            elif not MIN_BUS_QUIET_MS <= bus_quiet_ms <= MAX_BUS_QUIET_MS:
                bad("bus_quiet_ms", "range")

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
            bus=bus,
            bus_quiet_ms=bus_quiet_ms,
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
            bus=self.bus,
            bus_quiet_ms=self.bus_quiet_ms,
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


_CELL_KEYS = frozenset({"id", "entry_version", "parallel", "entry"})
_PACK_KEYS = frozenset({"cell", "balancer_profile", "bms_serial", "serial_mismatch"})
_PROFILE_KEYS = frozenset({"id", "name", "values"})
MAX_PARALLEL = 10_000          # only a guard against absurd input; the contract says "an integer >= 1"
MAX_SERIAL = 64
MAX_PROFILE_NAME = 40
PROFILE_WHEN = ("always", "charging", "not-discharging")
_MV_KEYS = ("start_mv", "stop_cell_mv", "trigger_mv", "stop_mv")


def join_path(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


@dataclass(frozen=True)
class CellAssignment:
    """A pack's cell type: the reference (id, version, parallel) and the server-made snapshot of the
    entry. ``valid`` is False when the snapshot does not match the reference (checks are then not
    evaluated and the assignment is shown for re-assignment)."""

    id: str
    entry_version: int
    parallel: int
    entry: Mapping[str, Any]
    valid: bool = True

    @classmethod
    def from_dict(cls, data: Any, path: str) -> CellAssignment:
        if not isinstance(data, Mapping):
            raise ConfigValidationError([(path, "type")])
        problems: list[Problem] = []

        def bad(key: str, kind: str) -> None:
            problems.append((join_path(path, key), kind))

        for key in data:
            if key not in _CELL_KEYS:
                bad(str(key), "unknown_field")
        for key in ("id", "entry_version", "parallel", "entry"):
            if key not in data:
                bad(key, "required")
        cell_id, version, parallel = data.get("id"), data.get("entry_version"), data.get("parallel")
        if "id" in data and (not isinstance(cell_id, str) or not cell_id):
            bad("id", "type")
        if "entry_version" in data:
            if not _is_int(version):
                bad("entry_version", "type")
            elif version < 1:
                bad("entry_version", "range")
        if "parallel" in data:
            if not _is_int(parallel):
                bad("parallel", "type")
            elif not 1 <= parallel <= MAX_PARALLEL:
                bad("parallel", "range")
        entry = data.get("entry")
        if "entry" in data:
            problems.extend(cell_validate.entry_problems(entry, join_path(path, "entry")))
        if problems:
            raise ConfigValidationError(problems)
        entry = cell_validate.normalized(entry)
        valid = entry["id"] == cell_id and entry["entry_version"] == version
        return cls(cell_id, version, parallel, entry, valid)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "entry_version": self.entry_version,
            "parallel": self.parallel,
            "entry": copy.deepcopy(dict(self.entry)),
        }


@dataclass(frozen=True)
class PackAssignment:
    """What is stored for one DIP address (also for addresses at or above ``pack_count``)."""

    address: int
    cell: CellAssignment | None = None
    balancer_profile: int | None = None
    bms_serial: str | None = None
    serial_mismatch: bool = False
    _extra: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, address: int, data: Any, path: str) -> PackAssignment:
        if not isinstance(data, Mapping):
            raise ConfigValidationError([(path, "type")])
        problems: list[Problem] = []

        def bad(key: str, kind: str) -> None:
            problems.append((join_path(path, key), kind))

        cell = None
        if data.get("cell") is not None:
            try:
                cell = CellAssignment.from_dict(data["cell"], join_path(path, "cell"))
            except ConfigValidationError as e:
                problems.extend(e.problems)
        profile = data.get("balancer_profile")
        if profile is not None:
            if not _is_int(profile):
                bad("balancer_profile", "type")
            elif profile < 1:
                bad("balancer_profile", "range")
        serial = data.get("bms_serial")
        if serial is not None:
            if not isinstance(serial, str):
                bad("bms_serial", "type")
            elif not 1 <= len(serial) <= MAX_SERIAL:
                bad("bms_serial", "range")
        mismatch = data.get("serial_mismatch", False)
        if not isinstance(mismatch, bool):
            bad("serial_mismatch", "type")
        if problems:
            raise ConfigValidationError(problems)
        return cls(
            address, cell, profile, serial, mismatch,
            {k: copy.deepcopy(v) for k, v in data.items() if k not in _PACK_KEYS},
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = copy.deepcopy(dict(self._extra))
        out.update(
            cell=self.cell.to_dict() if self.cell is not None else None,
            balancer_profile=self.balancer_profile,
            bms_serial=self.bms_serial,
            serial_mismatch=self.serial_mismatch,
        )
        return out


def _mv_problem(value: Any) -> str | None:
    if not _is_int(value):
        return "type"
    return None if 1 <= value <= 5000 else "range"


@dataclass(frozen=True)
class BalancerProfile:
    """Values of one kind of external active balancer (rules of ``docs/exchange``). Twelve equal units
    share one profile."""

    id: int
    name: str
    values: Mapping[str, Any]
    _extra: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any, path: str) -> BalancerProfile:
        if not isinstance(data, Mapping):
            raise ConfigValidationError([(path, "type")])
        problems: list[Problem] = []

        def bad(key: str, kind: str) -> None:
            problems.append((join_path(path, key), kind))

        profile_id = data.get("id")
        if "id" not in data:
            bad("id", "required")
        elif not _is_int(profile_id):
            bad("id", "type")
        elif profile_id < 1:
            bad("id", "range")

        name = ""
        if "name" not in data:
            bad("name", "required")
        elif not isinstance(data["name"], str):
            bad("name", "type")
        else:
            name = data["name"].strip()
            if not 1 <= len(name) <= MAX_PROFILE_NAME:
                bad("name", "range")

        values: dict[str, Any] = {}
        raw = data.get("values")
        if "values" not in data:
            bad("values", "required")
        elif not isinstance(raw, Mapping):
            bad("values", "type")
        else:
            def vbad(key: str, kind: str) -> None:
                bad(f"values.{key}", kind)

            for key in _MV_KEYS:
                value = raw.get(key)
                if value is None:
                    if key == "trigger_mv":
                        vbad(key, "required")
                    values[key] = None
                    continue
                kind = _mv_problem(value)
                if kind is not None:
                    vbad(key, kind)
                values[key] = value
            when = raw.get("when")
            if "when" not in raw:
                vbad("when", "required")
            elif not isinstance(when, str) or when not in PROFILE_WHEN:
                vbad("when", "value")
            values["when"] = when
            max_a = raw.get("max_a")
            if max_a is not None:
                if not _is_number(max_a):
                    vbad("max_a", "type")
                elif isinstance(max_a, float) and not math.isfinite(max_a) or not 0 <= max_a <= 1_000_000:
                    vbad("max_a", "range")
            values["max_a"] = max_a
            values.update({k: copy.deepcopy(v) for k, v in raw.items() if k not in values})

        if problems:
            raise ConfigValidationError(problems)
        return cls(
            id=profile_id, name=name, values=values,
            _extra={k: copy.deepcopy(v) for k, v in data.items() if k not in _PROFILE_KEYS},
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = copy.deepcopy(dict(self._extra))
        out.update(id=self.id, name=self.name, values=copy.deepcopy(dict(self.values)))
        return out


def find_cell(own: Iterable[Mapping[str, Any]], cell_id: str) -> tuple[str, Mapping[str, Any]] | None:
    """``(source, entry)`` of a cell type id: an own type wins over a bundled one with the same id
    (an upgrade collision keeps the own type usable and hides the bundled entry)."""
    for entry in own:
        if isinstance(entry, Mapping) and entry.get("id") == cell_id:
            return "own", entry
    bundled = catalog.current().get(cell_id)
    return ("bundled", bundled) if bundled is not None else None


def changed_fields(old: Any, new: Any, path: str = "") -> list[str]:
    """Paths of the values that differ between two entries (lists count as one value)."""
    if isinstance(old, Mapping) and isinstance(new, Mapping):
        out: list[str] = []
        for key in [*old, *(k for k in new if k not in old)]:
            out.extend(changed_fields(old.get(key), new.get(key), f"{path}.{key}" if path else key))
        return out
    return [] if old == new and type(old) is type(new) else [path]


@dataclass(frozen=True)
class MonitoringConfig:
    connection: ConnectionSettings
    parallel_sets: tuple[ParallelSet, ...] = ()
    # the id of the next new set; ids are never reused
    next_set_id: int = 1
    # own cell types (full entries of the cell database schema)
    cell_types: tuple[Mapping[str, Any], ...] = ()
    packs: Mapping[int, PackAssignment] = field(default_factory=dict)
    balancer_profiles: tuple[BalancerProfile, ...] = ()
    next_profile_id: int = 1
    # unknown top-level keys (sections of later versions) survive load and save untouched
    _extra: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def cell_conflicts(self) -> frozenset[str]:
        """Own types whose id a (newer) bundle also has: they stay usable, the user renames them."""
        return frozenset(e["id"] for e in self.cell_types if catalog.current().get(e["id"]) is not None)

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

        own: list[Mapping[str, Any]] = []
        raw_types = data.get("cell_types", [])
        if not isinstance(raw_types, list):
            problems.append(("cell_types", "type"))
        else:
            seen_types: set[Any] = set()
            for i, raw in enumerate(raw_types):
                path = f"cell_types[{i}]"
                found = cell_validate.entry_problems(raw, path)
                problems.extend(found)
                if found:
                    continue
                if raw["id"] in seen_types:
                    problems.append((f"{path}.id", "duplicate"))
                seen_types.add(raw["id"])
                own.append(cell_validate.normalized(raw))

        next_profile_id = 1
        profile_next_ok = True
        if "next_profile_id" in data:
            next_profile_id = data["next_profile_id"]
            if not _is_int(next_profile_id):
                problems.append(("next_profile_id", "type"))
                profile_next_ok = False
            elif next_profile_id < 1:
                problems.append(("next_profile_id", "range"))
                profile_next_ok = False

        profiles: list[BalancerProfile] = []
        raw_profiles = data.get("balancer_profiles", [])
        if not isinstance(raw_profiles, list):
            problems.append(("balancer_profiles", "type"))
        else:
            profile_ids: set[int] = set()
            profile_names: set[str] = set()
            for i, raw in enumerate(raw_profiles):
                path = f"balancer_profiles[{i}]"
                try:
                    profile = BalancerProfile.from_dict(raw, path)
                except ConfigValidationError as e:
                    problems.extend(e.problems)
                    continue
                profiles.append(profile)
                if profile.id in profile_ids:
                    problems.append((f"{path}.id", "duplicate"))
                elif profile_next_ok and profile.id >= next_profile_id:
                    problems.append((f"{path}.id", "value"))
                profile_ids.add(profile.id)
                if profile.name.casefold() in profile_names:
                    problems.append((f"{path}.name", "duplicate"))
                profile_names.add(profile.name.casefold())

        packs: dict[int, PackAssignment] = {}
        raw_packs = data.get("packs", {})
        if not isinstance(raw_packs, Mapping):
            problems.append(("packs", "type"))
        else:
            for key, raw in raw_packs.items():
                path = f"packs.{key}"
                if not (isinstance(key, str) and key.isascii() and key.isdigit() and str(int(key)) == key
                        and int(key) <= 15):
                    problems.append((path, "value"))
                    continue
                try:
                    item = PackAssignment.from_dict(int(key), raw, path)
                except ConfigValidationError as e:
                    problems.extend(e.problems)
                    continue
                packs[item.address] = item
                if item.balancer_profile is not None and item.balancer_profile not in {p.id for p in profiles}:
                    problems.append((f"{path}.balancer_profile", "value"))

        if problems or connection is None:
            raise ConfigValidationError(problems)
        known = {
            "connection", "parallel_sets", "next_set_id", "cell_types", "packs", "balancer_profiles",
            "next_profile_id",
        }
        extra = {k: copy.deepcopy(v) for k, v in data.items() if k not in known}
        return cls(
            connection=connection, parallel_sets=tuple(sets), next_set_id=next_set_id,
            cell_types=tuple(own), packs=packs, balancer_profiles=tuple(profiles),
            next_profile_id=next_profile_id, _extra=extra,
        )

    def to_dict(self) -> dict[str, Any]:
        out = copy.deepcopy(dict(self._extra))
        out["connection"] = self.connection.to_dict()
        # a stage-1 document stays as it is until the first set is saved
        if self.parallel_sets or self.next_set_id != 1:
            out["parallel_sets"] = [s.to_dict() for s in self.parallel_sets]
            out["next_set_id"] = self.next_set_id
        # the cell sections are optional in the same way
        if self.cell_types:
            out["cell_types"] = [copy.deepcopy(dict(e)) for e in self.cell_types]
        if self.packs:
            out["packs"] = {str(a): self.packs[a].to_dict() for a in sorted(self.packs)}
        if self.balancer_profiles or self.next_profile_id != 1:
            out["balancer_profiles"] = [p.to_dict() for p in self.balancer_profiles]
            out["next_profile_id"] = self.next_profile_id
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
