"""Validation of cell database entries in two layers, without new runtime dependencies.

1. *structural*: equivalent to the vendored JSON schema (``data/cells.schema.json``). It interprets
   that file directly, so a schema update needs no code change; a keyword this interpreter does not
   know makes loading fail loudly instead of being ignored. A dev-only test compares it with
   ``jsonschema``.
2. *semantic*: what a schema cannot say (finite numbers, ordered limits, ...). Only run on
   structurally valid entries.

Problems are ``(json_path, key)`` with the vocabulary of the monitoring model: ``required``, ``type``,
``range``, ``format``, ``value``, ``unknown_field``.
"""

from __future__ import annotations

import copy
import datetime
import ipaddress
import math
import re
from collections.abc import Callable
from typing import Any

from . import catalog

Problem = tuple[str, str]

_SUPPORTED = frozenset(
    {
        "$schema", "$id", "$defs", "$ref", "title", "description", "type", "items", "required", "properties",
        "additionalProperties", "enum", "const", "pattern", "minimum", "maximum", "exclusiveMinimum", "minItems",
        "format", "oneOf", "anyOf", "not",
    }
)
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# RFC 3986 URI grammar (what the ``rfc3987`` "URI" rule checks), composed from its components
_PCT = r"%[0-9A-Fa-f]{2}"
_UNRESERVED_SUB = r"[A-Za-z0-9\-._~!$&'()*+,;=]"
_PCHAR = rf"(?:{_UNRESERVED_SUB}|{_PCT}|[:@])"
_SEGMENT_NZ = rf"(?:{_PCHAR})+"
_PATH_ABEMPTY = rf"(?:/(?:{_PCHAR})*)*"
_PATH_ABSOLUTE = rf"/(?:{_SEGMENT_NZ}(?:/(?:{_PCHAR})*)*)?"
_PATH_ROOTLESS = rf"{_SEGMENT_NZ}(?:/(?:{_PCHAR})*)*"
_AUTHORITY = (
    rf"(?:(?:{_UNRESERVED_SUB}|{_PCT}|:)*@)?"
    rf"(?:\[(?P<literal>[^\]]*)\]|(?:{_UNRESERVED_SUB}|{_PCT})*)(?::[0-9]*)?"
)
_URI = re.compile(
    rf"[A-Za-z][A-Za-z0-9+.\-]*:(?://{_AUTHORITY}{_PATH_ABEMPTY}|{_PATH_ABSOLUTE}|{_PATH_ROOTLESS}|)"
    rf"(?:\?(?:{_PCHAR}|[/?])*)?(?:#(?:{_PCHAR}|[/?])*)?"
)
_IPV_FUTURE = re.compile(r"v[0-9A-Fa-f]+\.(?:[A-Za-z0-9\-._~!$&'()*+,;=]|:)+")
# a datasheet value beyond this is a typing error, and it keeps every comparison away from float overflow
MAX_NUMBER = 1e9


def _join(path: str, key: str) -> str:
    """``a.b`` for plain names, ``a["voltage.nominal_v"]`` for keys that are not identifiers."""
    if _IDENT.fullmatch(key):
        return f"{path}.{key}" if path else key
    return f'{path}["{key}"]'


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) or (isinstance(value, float) and value.is_integer())


_TYPES: dict[str, Callable[[Any], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "number": _is_number,
    "integer": _is_integer,
    "boolean": lambda v: isinstance(v, bool),
}


def _same(a: Any, b: Any) -> bool:
    """JSON equality: ``True`` is not ``1``."""
    return isinstance(a, bool) == isinstance(b, bool) and a == b


def _format_ok(name: str, value: str) -> bool:
    if name == "date":
        if not _DATE.fullmatch(value):
            return False
        try:
            datetime.date.fromisoformat(value)
        except ValueError:
            return False
        return True
    if name == "uri":
        match = _URI.fullmatch(value)
        if match is None:
            return False
        literal = match.group("literal")
        if literal is None:
            return True
        if _IPV_FUTURE.fullmatch(literal):
            return True
        try:
            ipaddress.IPv6Address(literal)
        except ValueError:
            return False
        return "%" not in literal            # no zone id in a URI
    raise ValueError(f"unsupported format {name!r}")


def _assert_supported(node: Any, where: str = "#") -> None:
    if not isinstance(node, dict):
        return
    for key in node:
        if key not in _SUPPORTED:
            raise ValueError(f"cell schema keyword {key!r} at {where} is not supported by the validator")
    for name, sub in node.get("properties", {}).items():
        _assert_supported(sub, f"{where}/properties/{name}")
    for name, sub in node.get("$defs", {}).items():
        _assert_supported(sub, f"{where}/$defs/{name}")
    for key in ("items", "additionalProperties", "not"):
        _assert_supported(node.get(key), f"{where}/{key}")
    for key in ("oneOf", "anyOf"):
        for i, sub in enumerate(node.get(key, [])):
            _assert_supported(sub, f"{where}/{key}/{i}")


def _cell_schema() -> tuple[dict[str, Any], dict[str, Any]]:
    """``(schema document, schema of one entry)`` of the bundled catalog."""
    schema = catalog.current().schema
    _assert_supported(schema)
    return schema, schema["$defs"]["cell"]


class _Walker:
    def __init__(self, root: dict[str, Any]) -> None:
        self.root = root

    def resolve(self, schema: dict[str, Any]) -> dict[str, Any]:
        ref = schema.get("$ref")
        if ref is None:
            return schema
        node: Any = self.root
        for part in ref.removeprefix("#/").split("/"):
            node = node[part]
        return node

    def matches(self, value: Any, schema: dict[str, Any]) -> bool:
        scratch: list[Problem] = []
        self.check(value, schema, "", scratch)
        return not scratch

    def check(self, value: Any, schema: dict[str, Any], path: str, out: list[Problem]) -> None:
        schema = self.resolve(schema)
        if value is None:                       # null is never allowed
            out.append((path, "type"))
            return
        kind = schema.get("type")
        if kind is not None and not _TYPES[kind](value):
            out.append((path, "type"))
            return
        if "const" in schema and not _same(value, schema["const"]):
            out.append((path, "value"))
        if "enum" in schema and not any(_same(value, option) for option in schema["enum"]):
            out.append((path, "value"))
        if isinstance(value, str):
            if "pattern" in schema and not re.search(schema["pattern"], value):      # JSON Schema: search semantics
                out.append((path, "format"))
            if "format" in schema and not _format_ok(schema["format"], value):
                out.append((path, "format"))
        if _is_number(value):
            if "minimum" in schema and value < schema["minimum"]:
                out.append((path, "range"))
            if "maximum" in schema and value > schema["maximum"]:
                out.append((path, "range"))
            if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
                out.append((path, "range"))
        if isinstance(value, dict):
            self.check_object(value, schema, path, out)
        if isinstance(value, list):
            if "minItems" in schema and len(value) < schema["minItems"]:
                out.append((path, "range"))
            if "items" in schema:
                for i, item in enumerate(value):
                    self.check(item, schema["items"], f"{path}[{i}]", out)
        if "oneOf" in schema and sum(self.matches(value, sub) for sub in schema["oneOf"]) != 1:
            out.append((path, "value"))
        if "anyOf" in schema and not any(self.matches(value, sub) for sub in schema["anyOf"]):
            out.append((path, "value"))
        if "not" in schema and self.matches(value, schema["not"]):
            out.append((path, "value"))

    def check_object(self, value: dict[str, Any], schema: dict[str, Any], path: str, out: list[Problem]) -> None:
        for key in schema.get("required", []):
            if key not in value:
                out.append((_join(path, key), "required"))
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                self.check(item, properties[key], _join(path, key), out)
            elif extra is False:
                out.append((_join(path, key), "unknown_field"))
            elif isinstance(extra, dict):
                self.check(item, extra, _join(path, key), out)


def structural_problems(entry: Any, path: str = "") -> list[Problem]:
    """Problems of one entry against the vendored schema (equivalent to ``jsonschema``)."""
    schema, cell = _cell_schema()
    out: list[Problem] = []
    _Walker(schema).check(entry, cell, path, out)
    return out


def _numbers(value: Any, path: str):
    """Every number of an entry with its path."""
    if _is_number(value):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _numbers(item, _join(path, key))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _numbers(item, f"{path}[{i}]")


def semantic_problems(entry: dict[str, Any], path: str = "") -> list[Problem]:
    """What a schema cannot express; ``entry`` must be structurally valid."""
    out: list[Problem] = []

    # no float conversion of an arbitrary integer: its magnitude is compared as it is
    for where, number in _numbers(entry, path):
        if isinstance(number, float) and not math.isfinite(number) or abs(number) > MAX_NUMBER:
            out.append((where, "range"))
    if out:
        return out                                  # the other checks assume sane numbers

    pattern = _cell_schema()[1]["properties"]["id"]["pattern"]
    if not re.fullmatch(pattern, entry["id"]):      # the schema pattern lets a final newline through
        out.append((_join(path, "id"), "format"))

    def section(name: str) -> dict[str, Any]:
        value = entry.get(name)
        return value if isinstance(value, dict) else {}

    def positive(group: str, key: str) -> None:
        value = section(group).get(key)
        if value is not None and math.isfinite(value) and not value > 0:      # non-finite: reported above
            out.append((_join(_join(path, group), key), "range"))

    def ordered(group: str, low: str, high: str) -> None:
        values = section(group)
        if low in values and high in values:
            if values[low] >= values[high]:
                out.append((_join(_join(path, group), low), "range"))
                out.append((_join(_join(path, group), high), "range"))

    positive("capacity", "nominal_ah")
    positive("capacity", "minimum_ah")
    positive("capacity", "nominal_energy_wh")
    positive("voltage", "nominal_v")
    voltage = section("voltage")
    if "charge_cutoff_v" in voltage and "discharge_cutoff_v" in voltage and not (
        voltage["charge_cutoff_v"] > voltage["discharge_cutoff_v"]
    ):
        out.append((_join(_join(path, "voltage"), "charge_cutoff_v"), "range"))
        out.append((_join(_join(path, "voltage"), "discharge_cutoff_v"), "range"))
    ordered("temperature", "charge_min_c", "charge_max_c")
    ordered("temperature", "discharge_min_c", "discharge_max_c")
    ordered("temperature", "storage_min_c", "storage_max_c")

    derating = section("charge_derating")
    base = _join(path, "charge_derating")
    for i, point in enumerate(derating.get("points", [])):
        where = f"{base}.points[{i}]"
        if "temp_min_c" in point and "temp_max_c" in point and point["temp_min_c"] > point["temp_max_c"]:
            out.append((f"{where}.temp_min_c", "range"))
            out.append((f"{where}.temp_max_c", "range"))
        if "soc_min_pct" in point and "soc_max_pct" in point and point["soc_min_pct"] > point["soc_max_pct"]:
            out.append((f"{where}.soc_min_pct", "range"))
            out.append((f"{where}.soc_max_pct", "range"))
    if derating.get("interpolation") == "linear" and "charge_derating.interpolation" not in section("provenance"):
        out.append((f"{base}.interpolation", "value"))
    return out


def normalized(entry: dict[str, Any]) -> dict[str, Any]:
    """A copy of a structurally valid entry whose ``schema_version`` and ``entry_version`` are ``int``
    (the schema accepts ``2.0`` as an integer)."""
    out = copy.deepcopy(entry)
    for key in ("schema_version", "entry_version"):
        if isinstance(out.get(key), float):
            out[key] = int(out[key])
    return out


def entry_problems(entry: Any, path: str = "") -> list[Problem]:
    """Structural problems, else the semantic ones."""
    problems = structural_problems(entry, path)
    return problems or semantic_problems(entry, path)
