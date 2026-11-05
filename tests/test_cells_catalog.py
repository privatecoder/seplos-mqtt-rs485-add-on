"""Cell catalog (stage 3a): bundled files, the two validation layers."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

import api

DATA = Path(__file__).resolve().parent.parent / "src" / "seplos_mqtt" / "cells" / "data"


def _entry(derating: bool = False) -> dict:
    cells = api.bundled_cells()
    return next(e for e in cells if ("charge_derating" in e) == derating)


# ---------------------------------------------------------------- bundle


def test_manifest_digests():
    manifest = json.loads((DATA / "MANIFEST.json").read_text())
    assert manifest["source"] == "openbms-monitor" and len(manifest["commit"]) == 40
    assert set(manifest["files"]) == {"cells.json", "cells.schema.json"}
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((DATA / name).read_bytes()).hexdigest() == digest, name
    assert "MIT" in (DATA / "LICENSE").read_text()


def test_every_bundled_entry_is_valid_in_both_layers():
    cells = api.bundled_cells()
    assert len(cells) > 20 and len({c["id"] for c in cells}) == len(cells)
    for cell in cells:
        assert api.cell_structural_problems(cell) == [], cell["id"]
        assert api.cell_semantic_problems(cell) == [], cell["id"]


# ---------------------------------------------------------------- structural


def _set(path: list, value):
    def mutate(entry):
        node = entry
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
    return mutate


def _drop(path: list):
    def mutate(entry):
        node = entry
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]
    return mutate


_PLAIN = [
    *(_drop([key]) for key in ("schema_version", "id", "entry_version", "updated", "manufacturer", "model", "chemistry", "datasheet")),
    _drop(["datasheet", "url"]), _drop(["datasheet", "title"]),
    _set(["foo"], 1), _set(["voltage", "foo"], 1), _set(["datasheet", "foo"], 1), _set(["provenance", "x", "foo"], 1),
    _set(["schema_version"], 2), _set(["schema_version"], True), _set(["schema_version"], "1"),
    _set(["id"], "Bad Id"), _set(["id"], "-x"), _set(["id"], ""), _set(["id"], 5), _set(["id"], None),
    _set(["entry_version"], 0), _set(["entry_version"], True), _set(["entry_version"], 1.5), _set(["entry_version"], "2"),
    _set(["updated"], "2026-13-01"), _set(["updated"], "2026-1-1"), _set(["updated"], "yesterday"), _set(["updated"], 20261009),
    _set(["chemistry"], "AGM"), _set(["form_factor"], "square"), _set(["manufacturer"], None), _set(["model"], 7),
    _set(["datasheet", "url"], "not a uri"), _set(["datasheet", "url"], ""), _set(["datasheet", "url"], "/relative"),
    _set(["datasheet", "url"], "https://ex.example/a b"), _set(["datasheet", "url"], "https://exämple.example/"),
    _set(["datasheet", "url"], "https://example.org/a%zz"), _set(["datasheet", "url"], "https://example.org/a?b=1#c"),
    *(_set(["datasheet", "url"], url) for url in (
        "http://[bad]", "http://a:bad/", "http://a/#a#b", "http://a/[]", "http://[::1]:8080/x", "http://[::1", "http://[v1.a]/",
        "http://[fe80::1%25eth0]/", "mailto:a@b.example", "urn:isbn:0451450523", "http:", "http://user:pw@host:80/p?q=1#f",
        "http://a/%", "http://a b", "1http://a", "http://a:/", "//a/b", "http://a/?q[]", "http://a/?q=/?", "http://a/#/?",
        "http://a@b@c/", "http://[1:2:3:4:5:6:7:8:9]/", "http://a:99999999/", "file:///etc/x", "x:/a//b", "x:a:b",
    )),
    _set(["id"], "my-cell\n"),
    _set(["datasheet", "source_type"], "blog"), _set(["datasheet", "sha256"], "abc"), _set(["datasheet", "sha256"], "A" * 64),
    _set(["capacity", "nominal_ah"], "280"), _set(["capacity", "nominal_ah"], True), _set(["capacity", "nominal_ah"], 0),
    _set(["capacity", "nominal_ah"], -1), _set(["capacity", "nominal_ah"], None), _set(["capacity"], []),
    _set(["capacity", "test_condition"], {"rate": {"value": 0.5, "basis": "X"}}),
    _set(["capacity", "test_condition"], {"rate": {"value": -1, "basis": "C"}}),
    _set(["capacity", "test_condition"], {"rate": {"basis": "C"}, "temperature_c": 25}),
    _set(["capacity", "test_condition"], {"temperature_c": 25}),
    _set(["current", "max_continuous_charge_a"], -1), _set(["current", "max_continuous_charge_a"], "1"),
    _set(["current", "standard_charge"], {"value": 0.5}), _set(["current", "pulse_charge"], {"value": 1, "basis": "C"}),
    _set(["voltage", "nominal_v"], True), _set(["voltage", "storage_v"], "3.3"), _set(["temperature", "charge_min_c"], None),
    _set(["provenance", "x"], {"verified": "yes"}), _set(["provenance", "x"], {}), _set(["provenance", "x"], 1),
    _set(["provenance", "x"], {"verified": True, "page": 1.5}), _set(["provenance", "x"], {"verified": True, "page": 2.0}),
    _set(["provenance", "x"], {"verified": True, "source": "nope"}), _set(["provenance", "x"], {"verified": True, "source": "https://a.example/x"}),
    _set(["notes"], 3), _set(["variant"], None),
]
_DERATING = [
    _set(["charge_derating", "basis"], "X"), _set(["charge_derating", "interpolation"], "cubic"),
    _set(["charge_derating", "interpolation"], "linear"), _set(["charge_derating", "points"], []),
    _set(["charge_derating", "points"], {}), _set(["charge_derating", "foo"], 1), _drop(["charge_derating", "points"]),
    _drop(["charge_derating", "basis"]), _set(["charge_derating", "points", 0, "foo"], 1),
    _set(["charge_derating", "points", 0, "value"], -1), _drop(["charge_derating", "points", 0, "value"]),
    _set(["charge_derating", "points", 0, "temp_c"], 10), _set(["charge_derating", "points", 0, "temp_min_c"], 1),
    _set(["charge_derating", "points", 0, "temp_max_c"], 1), _set(["charge_derating", "points", 0, "soc_min_pct"], 101),
    _set(["charge_derating", "points", 0, "soc_max_pct"], -1), _set(["charge_derating", "points", 0, "soc_min_pct"], 50),
    _set(["charge_derating", "points", 0], {"value": 1}),
    _set(["charge_derating", "points", 0], {"value": 1, "temp_c": 5, "temp_min_c": 0}),
    _set(["charge_derating", "points", 0], {"value": 1, "temp_min_c": 0, "temp_max_c": 10}),
    _set(["charge_derating", "points", 0], {"value": 1, "temp_max_c": 10}),
    _set(["charge_derating", "points", 0], {"value": 1, "temp_c": "5"}),
    _set(["charge_derating", "points", 0], 5),
]


@pytest.mark.parametrize("derating", [False, True])
def test_structural_layer_agrees_with_jsonschema(derating):
    jsonschema = pytest.importorskip("jsonschema", reason="jsonschema is a dev-only dependency of this test")
    pytest.importorskip("rfc3987", reason="the uri format check of jsonschema needs rfc3987")
    schema = api.cell_schema()
    validator = jsonschema.Draft202012Validator(
        {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": "#/$defs/cell"},
        format_checker=jsonschema.FormatChecker(),
    )
    base = _entry(derating)
    assert validator.is_valid(base)
    mutations = [*_PLAIN, *(_DERATING if derating else [])]
    rejected = 0
    for index, mutate in enumerate(mutations):
        entry = copy.deepcopy(base)
        try:
            mutate(entry)
        except (KeyError, IndexError, TypeError):
            continue                                    # the mutation does not apply to this entry
        expected = validator.is_valid(entry)
        assert (api.cell_structural_problems(entry) == []) is expected, (index, json.dumps(entry)[:200])
        rejected += not expected
    assert rejected > 30


def test_structural_problem_vocabulary():
    entry = _entry()
    entry["foo"] = 1
    entry["voltage"]["nominal_v"] = "3.2"
    entry["capacity"]["nominal_ah"] = 0
    entry["updated"] = "2026-1-1"
    entry["chemistry"] = "AGM"
    entry["provenance"]["voltage.nominal_v"] = {"note": "x"}
    del entry["model"]
    entry["schema_version"] = None
    assert sorted(api.cell_problems(entry, "cell_types[0]")) == sorted([
        ("cell_types[0].foo", "unknown_field"),
        ("cell_types[0].voltage.nominal_v", "type"),
        ("cell_types[0].capacity.nominal_ah", "range"),
        ("cell_types[0].updated", "format"),
        ("cell_types[0].chemistry", "value"),
        ('cell_types[0].provenance["voltage.nominal_v"].verified', "required"),
        ("cell_types[0].model", "required"),
        ("cell_types[0].schema_version", "type"),
    ])


def test_id_with_a_trailing_newline_is_accepted_by_the_schema_but_rejected_by_policy():
    entry = _entry()
    entry["id"] = "my-cell\n"
    assert api.cell_structural_problems(entry) == []                  # the schema pattern lets it through (search semantics)
    assert api.cell_semantic_problems(entry) == [("id", "format")]
    assert api.cell_problems(entry) == [("id", "format")]


@pytest.mark.parametrize("path", ["capacity.nominal_ah", "voltage.nominal_v", "current.max_continuous_charge_a"])
@pytest.mark.parametrize("value", [10**500, -(10**500), 10**9 + 1, float("inf"), float("nan")])
def test_huge_or_non_finite_numbers_are_a_range_problem(path, value):
    entry = _entry()
    group, key = path.split(".")
    entry.setdefault(group, {})[key] = value
    problems = api.cell_problems(entry)
    assert (path, "range") in problems or api.cell_structural_problems(entry) != []      # negatives may fail the schema first
    if value > 0 or value != value:
        assert problems == [(path, "range")]


def test_a_number_at_the_bound_is_fine():
    entry = _entry()
    entry["capacity"]["nominal_ah"] = 10**9
    assert api.cell_problems(entry) == []


def test_derating_point_needs_exactly_one_temperature_form():
    entry = _entry(derating=True)
    entry["charge_derating"]["points"][0] = {"value": 1, "temp_c": 5, "temp_max_c": 10}
    assert api.cell_structural_problems(entry) == [("charge_derating.points[0]", "value")]


def test_unsupported_schema_keyword_fails_loudly(monkeypatch):
    schema = copy.deepcopy(api.cell_schema())
    schema["$defs"]["cell"]["properties"]["id"]["maxLength"] = 10
    import dataclasses

    from seplos_mqtt.cells import catalog
    monkeypatch.setattr(catalog, "_CATALOG", dataclasses.replace(catalog.current(), schema=schema))
    with pytest.raises(ValueError, match="maxLength"):
        api.cell_structural_problems(_entry())


# ---------------------------------------------------------------- semantic (on its own)


def _full() -> dict:
    entry = _entry(derating=True)
    entry["voltage"].update(nominal_v=3.2, charge_cutoff_v=3.65, discharge_cutoff_v=2.5)
    entry["temperature"].update(charge_min_c=0, charge_max_c=55, discharge_min_c=-20, discharge_max_c=55,
                                storage_min_c=-10, storage_max_c=30)
    return entry


def test_semantic_accepts_the_reference_entry():
    assert api.cell_semantic_problems(_full()) == []


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda e: e["voltage"].update(nominal_v=0), [("voltage.nominal_v", "range")]),
        (lambda e: e["voltage"].update(nominal_v=-3.2), [("voltage.nominal_v", "range")]),
        (lambda e: e["voltage"].update(charge_cutoff_v=2.5), [("voltage.charge_cutoff_v", "range"), ("voltage.discharge_cutoff_v", "range")]),
        (lambda e: e["voltage"].update(charge_cutoff_v=2.0), [("voltage.charge_cutoff_v", "range"), ("voltage.discharge_cutoff_v", "range")]),
        (lambda e: e["temperature"].update(charge_min_c=55), [("temperature.charge_min_c", "range"), ("temperature.charge_max_c", "range")]),
        (lambda e: e["temperature"].update(discharge_min_c=60), [("temperature.discharge_min_c", "range"), ("temperature.discharge_max_c", "range")]),
        (lambda e: e["temperature"].update(storage_min_c=31), [("temperature.storage_min_c", "range"), ("temperature.storage_max_c", "range")]),
        (lambda e: e["capacity"].update(minimum_ah=float("inf")), [("capacity.minimum_ah", "range")]),
        (lambda e: e["capacity"].update(nominal_energy_wh=float("nan")), [("capacity.nominal_energy_wh", "range")]),
        (lambda e: e["charge_derating"]["points"].append({"value": 1, "temp_min_c": 20, "temp_max_c": 10}),
         [("charge_derating.points[{n}].temp_min_c", "range"), ("charge_derating.points[{n}].temp_max_c", "range")]),
        (lambda e: e["charge_derating"]["points"].append({"value": 1, "temp_c": 5, "soc_min_pct": 80, "soc_max_pct": 20}),
         [("charge_derating.points[{n}].soc_min_pct", "range"), ("charge_derating.points[{n}].soc_max_pct", "range")]),
        (lambda e: e["charge_derating"].update(interpolation="linear"), [("charge_derating.interpolation", "value")]),
    ],
)
def test_semantic_problems(mutate, expected):
    entry = _full()
    n = len(entry["charge_derating"]["points"])
    mutate(entry)
    assert api.cell_semantic_problems(entry) == [(p.replace("{n}", str(n)), k) for p, k in expected]


def test_linear_interpolation_with_provenance_is_fine():
    entry = _full()
    entry["charge_derating"]["interpolation"] = "linear"
    entry["provenance"]["charge_derating.interpolation"] = {"verified": True, "note": "datasheet p. 3 allows interpolation"}
    assert api.cell_problems(entry) == []


def test_equal_band_bounds_are_allowed_but_equal_limits_are_not():
    entry = _full()
    entry["charge_derating"]["points"].append({"value": 1, "temp_min_c": 10, "temp_max_c": 10})
    assert api.cell_semantic_problems(entry) == []


def test_semantic_runs_only_on_structurally_valid_entries():
    entry = _full()
    entry["voltage"]["nominal_v"] = 0
    entry["foo"] = 1
    assert api.cell_problems(entry) == [("foo", "unknown_field")]
