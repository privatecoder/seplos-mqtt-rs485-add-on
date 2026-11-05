"""Cell types, pack assignments, balancer profiles in the monitoring document (stage 3a): model."""
from __future__ import annotations

import copy

import pytest

import api
from seplos_mqtt.monitoring.model import ConfigValidationError, MonitoringConfig, changed_fields, find_cell


def own_cell(cell_id: str = "my-cell", **changes) -> dict:
    entry = api.bundled_cells()[0]
    entry.update(id=cell_id, entry_version=1, updated="2026-10-01", **changes)
    return entry


def snapshot(cell_id: str = "my-cell", parallel: int = 1, **changes) -> dict:
    entry = own_cell(cell_id, **changes)
    return {"id": cell_id, "entry_version": entry["entry_version"], "parallel": parallel, "entry": entry}


def profile(profile_id: int = 1, name: str = "Balancer", **values) -> dict:
    base = {"start_mv": 3400, "stop_cell_mv": 3370, "trigger_mv": 5, "stop_mv": None, "when": "always", "max_a": 4}
    return {"id": profile_id, "name": name, "values": {**base, **values}}


def pack(cell=None, profile_id=None, serial="S1", mismatch=False, **extra) -> dict:
    return {"cell": cell, "balancer_profile": profile_id, "bms_serial": serial, "serial_mismatch": mismatch, **extra}


def doc(**kw) -> dict:
    return {"connection": {"transport": "tcp", "host": "10.0.0.5", "pack_count": 2}, **kw}


def test_stage_2_document_is_written_back_unchanged():
    raw = doc(parallel_sets=[], next_set_id=3, future=1)
    config = MonitoringConfig.from_dict(raw)
    assert config.cell_types == () and config.packs == {} and config.balancer_profiles == () and config.next_profile_id == 1
    assert config.to_dict() == {**raw, "connection": config.connection.to_dict()}


def test_round_trip_keeps_unknown_keys_outside_entries():
    raw = doc(
        cell_types=[own_cell()],
        packs={"0": pack(snapshot(), 1, future_pack={"a": 1}), "15": pack(None, None, None)},
        balancer_profiles=[{**profile(), "future_profile": [1]}],
        next_profile_id=4,
        future_top=True,
    )
    raw["balancer_profiles"][0]["values"]["future_value"] = "x"
    out = MonitoringConfig.from_dict(raw).to_dict()
    assert out["packs"]["0"]["future_pack"] == {"a": 1} and out["future_top"] is True
    assert out["balancer_profiles"][0]["future_profile"] == [1] and out["balancer_profiles"][0]["values"]["future_value"] == "x"
    assert out == {**raw, "connection": out["connection"]}


def test_profile_values_are_normalised():
    config = MonitoringConfig.from_dict(doc(
        balancer_profiles=[{"id": 1, "name": "  A  ", "values": {"trigger_mv": 5, "when": "charging"}}], next_profile_id=2))
    assert config.balancer_profiles[0].name == "A"
    assert config.balancer_profiles[0].values == {
        "start_mv": None, "stop_cell_mv": None, "trigger_mv": 5, "stop_mv": None, "when": "charging", "max_a": None}


P = "balancer_profiles"


@pytest.mark.parametrize(
    "raw, expected",
    [
        (doc(cell_types=[own_cell(), own_cell()]), [("cell_types[1].id", "duplicate")]),
        (doc(cell_types=[{**own_cell(), "foo": 1}]), [("cell_types[0].foo", "unknown_field")]),
        (doc(cell_types=[own_cell(voltage={"nominal_v": 3.2, "bar": 1})]), [("cell_types[0].voltage.bar", "unknown_field")]),
        (doc(cell_types=[own_cell(capacity={"nominal_ah": 0})]), [("cell_types[0].capacity.nominal_ah", "range")]),
        (doc(cell_types=[own_cell(voltage={"charge_cutoff_v": 2.0, "discharge_cutoff_v": 2.5})]),
         [("cell_types[0].voltage.charge_cutoff_v", "range"), ("cell_types[0].voltage.discharge_cutoff_v", "range")]),
        (doc(cell_types={}), [("cell_types", "type")]),
        (doc(cell_types=[5]), [("cell_types[0]", "type")]),
        (doc(packs=[]), [("packs", "type")]),
        (doc(packs={"01": pack()}), [("packs.01", "value")]),
        (doc(packs={"16": pack()}), [("packs.16", "value")]),
        (doc(packs={"x": pack()}), [("packs.x", "value")]),
        (doc(packs={"0": 5}), [("packs.0", "type")]),
        (doc(packs={"0": pack(snapshot(parallel=0))}), [("packs.0.cell.parallel", "range")]),
        (doc(packs={"0": pack(snapshot(parallel=True))}), [("packs.0.cell.parallel", "type")]),
        (doc(packs={"0": pack({**snapshot(), "foo": 1})}), [("packs.0.cell.foo", "unknown_field")]),
        (doc(packs={"0": pack({"id": "x"})}),
         [("packs.0.cell.entry_version", "required"), ("packs.0.cell.parallel", "required"), ("packs.0.cell.entry", "required")]),
        (doc(packs={"0": pack(snapshot(capacity={"nominal_ah": -1}))}), [("packs.0.cell.entry.capacity.nominal_ah", "range")]),
        (doc(packs={"0": pack(serial=5)}), [("packs.0.bms_serial", "type")]),
        (doc(packs={"0": pack(serial="")}), [("packs.0.bms_serial", "range")]),
        (doc(packs={"0": pack(mismatch="yes")}), [("packs.0.serial_mismatch", "type")]),
        (doc(packs={"0": pack(profile_id=True)}), [("packs.0.balancer_profile", "type")]),
        (doc(packs={"0": pack(profile_id=1)}), [("packs.0.balancer_profile", "value")]),
        (doc(**{P: [profile()]}), [(f"{P}[0].id", "value")]),
        (doc(**{P: [profile(), profile()]}, next_profile_id=5), [(f"{P}[1].id", "duplicate"), (f"{P}[1].name", "duplicate")]),
        (doc(**{P: [profile(name="A"), profile(2, " a ")]}, next_profile_id=5), [(f"{P}[1].name", "duplicate")]),
        (doc(**{P: [profile(name=" ")]}, next_profile_id=5), [(f"{P}[0].name", "range")]),
        (doc(**{P: [profile(name="x" * 41)]}, next_profile_id=5), [(f"{P}[0].name", "range")]),
        (doc(**{P: [profile(trigger_mv=None)]}, next_profile_id=5), [(f"{P}[0].values.trigger_mv", "required")]),
        (doc(**{P: [profile(trigger_mv=0)]}, next_profile_id=5), [(f"{P}[0].values.trigger_mv", "range")]),
        (doc(**{P: [profile(start_mv=5001)]}, next_profile_id=5), [(f"{P}[0].values.start_mv", "range")]),
        (doc(**{P: [profile(stop_mv=2.5)]}, next_profile_id=5), [(f"{P}[0].values.stop_mv", "type")]),
        (doc(**{P: [profile(stop_cell_mv=True)]}, next_profile_id=5), [(f"{P}[0].values.stop_cell_mv", "type")]),
        (doc(**{P: [profile(when="never")]}, next_profile_id=5), [(f"{P}[0].values.when", "value")]),
        (doc(**{P: [profile(max_a=-1)]}, next_profile_id=5), [(f"{P}[0].values.max_a", "range")]),
        (doc(**{P: [profile(max_a="4")]}, next_profile_id=5), [(f"{P}[0].values.max_a", "type")]),
        (doc(**{P: [{"id": 1, "name": "A"}]}, next_profile_id=5), [(f"{P}[0].values", "required")]),
        (doc(**{P: {}}), [(P, "type")]),
        (doc(next_profile_id=0), [("next_profile_id", "range")]),
    ],
)
def test_invalid_sections(raw, expected):
    with pytest.raises(ConfigValidationError) as e:
        MonitoringConfig.from_dict(raw)
    assert e.value.problems == expected


def test_profile_reference_must_exist_but_cell_reference_is_self_contained():
    config = MonitoringConfig.from_dict(doc(
        packs={"3": pack(snapshot("gone-type"), 1)}, **{P: [profile()]}, next_profile_id=2))
    assert config.packs[3].cell.id == "gone-type" and config.packs[3].balancer_profile == 1


def test_snapshot_mismatch_marks_only_that_assignment_invalid():
    wrong_id = snapshot("a-cell")
    wrong_id["entry"]["id"] = "other"
    wrong_version = snapshot("b-cell")
    wrong_version["entry"]["entry_version"] = 9
    config = MonitoringConfig.from_dict(doc(packs={
        "0": pack(wrong_id), "1": pack(wrong_version), "2": pack(snapshot("c-cell")),
    }))
    assert [config.packs[a].cell.valid for a in (0, 1, 2)] == [False, False, True]
    assert config.to_dict()["packs"]["0"]["cell"]["entry"]["id"] == "other"          # kept as stored


def test_upgrade_collision_loads_and_is_reported(monkeypatch):
    bundled = api.bundled_cells()
    api.use_bundle(monkeypatch, bundled)
    raw = doc(cell_types=[own_cell("later-bundled")], packs={"0": pack(snapshot("later-bundled"))})
    assert MonitoringConfig.from_dict(raw).cell_conflicts() == frozenset()
    api.use_bundle(monkeypatch, [*bundled, {**own_cell("later-bundled"), "entry_version": 4}])        # catalog B has the id
    config = MonitoringConfig.from_dict(raw)
    assert config.cell_conflicts() == {"later-bundled"}
    assert find_cell(config.cell_types, "later-bundled")[0] == "own"            # the own type wins, bundled is hidden
    assert find_cell((), "later-bundled")[0] == "bundled"
    assert find_cell((), "unknown") is None


def test_changed_fields():
    old = {"a": 1, "b": {"c": 1, "d": [1, 2]}, "e": True}
    new = {"a": 1, "b": {"c": 2, "d": [1, 3]}, "e": 1, "f": 0}
    assert changed_fields(old, new) == ["b.c", "b.d", "e", "f"]
    assert changed_fields(old, copy.deepcopy(old)) == []


def test_integral_float_versions_are_normalised_to_int():
    own = own_cell()
    own["entry_version"] = 2.0
    snap = snapshot()
    snap["entry"]["entry_version"], snap["entry_version"] = 2.0, 2
    config = MonitoringConfig.from_dict(doc(cell_types=[own], packs={"0": pack(snap)}))
    assert type(config.cell_types[0]["entry_version"]) is int and config.cell_types[0]["entry_version"] == 2
    assert config.packs[0].cell.valid is True and type(config.packs[0].cell.entry["entry_version"]) is int
    assert type(config.to_dict()["cell_types"][0]["entry_version"]) is int


@pytest.mark.parametrize("path", ["capacity.nominal_ah", "voltage.nominal_v"])
def test_huge_numbers_in_a_stored_document_are_a_validation_error(path):
    group, key = path.split(".")
    for raw in (
        doc(cell_types=[own_cell(**{group: {key: 10**500}})]),
        doc(packs={"0": pack(snapshot(**{group: {key: 10**500}}))}),
    ):
        with pytest.raises(ConfigValidationError) as e:
            MonitoringConfig.from_dict(raw)
        assert [k for _p, k in e.value.problems] == ["range"] and e.value.problems[0][0].endswith(path)


def test_parallel_only_needs_to_be_a_positive_integer():
    assert MonitoringConfig.from_dict(doc(packs={"0": pack(snapshot(parallel=65))})).packs[0].cell.parallel == 65
    with pytest.raises(ConfigValidationError) as e:
        MonitoringConfig.from_dict(doc(packs={"0": pack(snapshot(parallel=10_001))}))
    assert e.value.problems == [("packs.0.cell.parallel", "range")]
