"""Datasheet checks (stage 3b): shared vectors, not-evaluated conditions, samples, energy, HA, status."""
from __future__ import annotations

import copy
import dataclasses
import json
import types
from pathlib import Path

import pytest

import api
import frames as f
from seplos_mqtt.cells import evaluate
from test_cells_model import pack
from test_characterization import _multi_pack_bms, _single_pack_bms

VECTORS = json.loads((Path(__file__).parent / "vectors" / "cell_checks.json").read_text(encoding="utf-8"))


def entry_of(ref):
    if not isinstance(ref, str):
        return ref
    if ref.startswith("bundled:"):
        return next(e for e in api.bundled_cells() if e["id"] == ref[8:])
    return VECTORS["entries"][ref]


def sample_values(spec: dict) -> dict:
    values = {f"voltage_cell_{i}": v for i, v in enumerate(spec["cells"], start=1)}
    values.update({f"cell_temperature_{i}": t for i, t in enumerate(spec["temps"], start=1)})
    values["current"] = spec["current"]
    if spec.get("soc") is not None:
        values["state_of_charge"] = spec["soc"]
    return values


def ids(section: str) -> list:
    return [pytest.param(case, id=f"{i}-{case.get('name', case.get('results'))}") for i, case in enumerate(VECTORS[section])]


def approx(value):
    return value if value is None else pytest.approx(value, abs=1e-6)


# --- shared vectors -----------------------------------------------------------------------------------

@pytest.mark.parametrize("case", ids("live"))
def test_live_vectors(case):
    result = api.live_checks(entry_of(case["entry"]), case["parallel"], api.checks_sample(sample_values(case["sample"])))
    by_key = {c["key"]: c for c in result["checks"]}
    assert len(by_key) == len(result["checks"]) == 6
    for key, expected in case["expect"].items():
        assert by_key[key]["result"] == expected, (key, by_key[key])
    if "summary" in case:
        assert result["summary"] == case["summary"]
    for key in ("charge_limit_a", "discharge_limit_a"):
        if key in case:
            assert result[key] == approx(case[key])


@pytest.mark.parametrize("case", ids("allowed_rate"))
def test_allowed_rate_vectors(case):
    assert api.allowed_charge_rate(entry_of(case["entry"]), case["temp"], case.get("soc")) == approx(case["expect"])


@pytest.mark.parametrize("case", ids("charge_range"))
def test_charge_range_vectors(case):
    expected = case["expect"]
    assert api.charge_range(entry_of(case["entry"])) == (None if expected is None else tuple(expected))


@pytest.mark.parametrize("case", ids("permitted_charge"))
def test_permitted_charge_vectors(case):
    assert api.permitted_charge(entry_of(case["entry"]), case["parallel"], case["temps"], case.get("soc")) == approx(case["expect"])


@pytest.mark.parametrize("case", ids("permitted_discharge"))
def test_permitted_discharge_vectors(case):
    assert api.permitted_discharge(entry_of(case["entry"]), case["parallel"], case["temps"]) == approx(case["expect"])


@pytest.mark.parametrize("case", ids("parameters"))
def test_parameter_vectors(case):
    result = api.parameter_checks(entry_of(case["entry"]), case["parallel"], api.checks_sample(case["values"]), case["series"])
    by_key = {c["key"]: c["result"] for c in result["checks"]}
    for key, expected in case["expect"].items():
        assert by_key[key] == expected, (key, result["checks"])
    if "summary" in case:
        assert result["summary"] == case["summary"]


@pytest.mark.parametrize("case", ids("summary"))
def test_summary_vectors(case):
    assert api.summarize_checks(case["results"]) == case["expect"]


def test_every_deviation_is_explained():
    for section in ("live", "allowed_rate", "charge_range", "permitted_charge", "permitted_discharge", "parameters"):
        for case in VECTORS[section]:
            assert "deviation" not in case or len(case["deviation"]) > 20, case["name"]


def test_check_shape_and_rounding():
    result = api.live_checks(entry_of("derated"), 1, api.checks_sample(sample_values(
        {"cells": [3.3], "temps": [8, 9], "current": 40, "soc": 60})))
    check = next(c for c in result["checks"] if c["key"] == "charge_current")
    assert check == {"key": "charge_current", "result": "over", "value": 40.0, "limit": 28.0, "unit": "A", "reason": "above_limit"}
    assert result["charge_limit_a"] == 28.0                       # 0.1 x 280 without float noise
    assert all(set(c) == {"key", "result", "value", "limit", "unit", "reason"} for c in result["checks"])
    json.dumps(result)


# --- not evaluated ---------------------------------------------------------------------------------------------

GOOD = {"cells": [3.3, 3.31], "temps": [22, 24, 22, 24], "current": -10, "soc": 60}


def live(entry="plain", sample=GOOD, now=0.0, **kw):
    return api.live_checks(entry_of(entry) if entry else None, 1, api.checks_sample(sample_values(sample), **kw) if sample else None, now)


def test_no_assignment_is_not_evaluated():
    for result in (live(entry=None), api.parameter_checks(None, 1, api.checks_sample({}), 16)):
        assert result["summary"] == "not_evaluated" and result["reason"] == "no_assignment" and result["checks"] == []
    assert live(entry=None)["charge_limit_a"] is None and live(entry=None)["discharge_limit_a"] is None


@pytest.mark.parametrize("blocked", ["serial_mismatch", "invalid_snapshot"])
def test_blocked_assignment_is_not_evaluated(blocked):
    live_result = api.live_checks(entry_of("plain"), 1, api.checks_sample(sample_values(GOOD)), 0.0, blocked=blocked)
    params = api.parameter_checks(entry_of("plain"), 1, api.checks_sample({"cells_in_series": 16}), 16, 0.0, blocked=blocked)
    for result in (live_result, params):
        assert result["summary"] == "not_evaluated" and result["reason"] == blocked and result["checks"] == []
    assert live_result["charge_limit_a"] is None


def test_missing_and_stale_sample():
    assert live(sample=None)["reason"] == "no_sample"
    assert live(max_age=60.0, now=60.0)["summary"] == "ok"             # exactly the allowance is still fresh
    stale = live(max_age=60.0, now=60.1)
    assert stale["reason"] == "stale_sample" and stale["summary"] == "not_evaluated" and stale["charge_limit_a"] is None


def test_missing_and_stale_parameters():
    values = {"cell_overvoltage_protection_v": 3.6}
    assert api.parameter_checks(entry_of("plain"), 1, None, 16)["reason"] == "no_parameters"
    fresh = api.parameter_checks(entry_of("plain"), 1, api.checks_sample(values, at=0.0, max_age=3 * 3600), 16, now=3 * 3600)
    assert fresh["reason"] is None and fresh["checks"]
    stale = api.parameter_checks(entry_of("plain"), 1, api.checks_sample(values, at=0.0, max_age=3 * 3600), 16, now=3 * 3600 + 1)
    assert stale["reason"] == "stale_parameters" and stale["summary"] == "not_evaluated" and stale["checks"] == []


def test_missing_sample_fields_are_not_evaluated():
    result = api.live_checks(entry_of("plain"), 1, api.checks_sample({"current": -5.0}), 0.0)
    by_key = {c["key"]: (c["result"], c["reason"]) for c in result["checks"]}
    assert by_key["cell_voltage_high"] == ("not_evaluated", "missing_sample")
    assert by_key["charge_temperature"] == ("not_evaluated", "missing_sample")
    assert by_key["charge_current"] == ("not_evaluated", "missing_sample")
    assert by_key["discharge_current"] == ("ok", None)
    assert result["summary"] == "not_evaluated" and result["charge_limit_a"] is None
    no_current = api.live_checks(entry_of("plain"), 1, api.checks_sample({"voltage_cell_1": 3.3, "cell_temperature_1": 20.0}), 0.0)
    assert {c["key"]: c["result"] for c in no_current["checks"]}["charge_current"] == "not_evaluated"


def test_non_finite_and_boolean_values_are_missing():
    values = sample_values(GOOD)
    values["voltage_cell_1"], values["current"] = float("nan"), True
    result = api.live_checks(entry_of("plain"), 1, api.checks_sample(values), 0.0)
    assert {c["key"]: c["result"] for c in result["checks"]}["charge_current"] == "not_evaluated"
    assert {c["key"]: c["result"] for c in result["checks"]}["cell_voltage_high"] == "not_evaluated"


# --- the sample of a pack --------------------------------------------------------------------------------------


def test_sample_survives_a_failed_0x44(monkeypatch):
    api.no_sleep(monkeypatch)
    with api.mode(1):
        pack_ = api.make_pack(0)
        bms = _single_pack_bms()
        data, ok = api.poll(pack_, bms)
        assert ok and pack_.sample is not None and pack_.sample.values["voltage_cell_1"] == 3.3
        first = pack_.sample
        del bms.ascii[(0, 0x44)]
        data, ok = api.poll(pack_, bms)
        assert data is None and not ok                          # nothing published, availability unchanged
        assert pack_.sample is not first and pack_.sample.values["current"] == -12.34     # the new 0x42 decode is kept
        assert pack_.sample.at >= first.at


def test_failed_0x42_keeps_the_previous_sample(monkeypatch):
    api.no_sleep(monkeypatch)
    with api.mode(1):
        pack_ = api.make_pack(0)
        bms = _single_pack_bms()
        api.poll(pack_, bms)
        first = pack_.sample
        del bms.ascii[(0, 0x42)]
        assert api.poll(pack_, bms) == (None, False)
        assert pack_.sample is first


def test_parameters_are_sampled_with_their_read_time(monkeypatch):
    api.no_sleep(monkeypatch)
    with api.mode(1):
        pack_ = api.make_pack(0)
        assert pack_.parameter_sample() is None
        api.poll(pack_, _single_pack_bms())
        sample = pack_.parameter_sample()
        assert sample.values["cells_in_series"] == 16 and sample.max_age == 3 * 3600 and sample.at > 0
    with api.mode(2):
        assert api.make_pack(0).parameter_sample() is None       # UART1 without MAINT: not readable


# --- assignments in the loop -----------------------------------------------------------------------------------

LF280K = "eve-lf280k-rev-b-2021"


def assigned(address: int = 0, parallel: int = 1, cell_id: str = LF280K, mismatch: bool = False, serial: str | None = "S1", **entry_changes):
    """A stored assignment of a bundled type (a modified copy becomes an own type)."""
    entry = next(e for e in api.bundled_cells() if e["id"] == cell_id)
    if entry_changes:
        entry = {**entry, "id": "own-type", **entry_changes}
        cell = {"id": "own-type", "entry_version": entry["entry_version"], "parallel": parallel, "entry": entry}
        return api.stored_assignments({str(address): pack(cell, None, serial, mismatch)}, cell_types=[entry], pack_count=16)
    cell = {"id": cell_id, "entry_version": entry["entry_version"], "parallel": parallel, "entry": entry}
    return api.stored_assignments({str(address): pack(cell, None, serial, mismatch)}, pack_count=16)


def last_payload(client, topic="seplos/pack-0/sensors") -> dict:
    return [r.payload for r in client.published if r.topic == topic][-1]


def test_unassigned_pack_publishes_not_evaluated(monkeypatch):
    client, _ = api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=2)
    cells = last_payload(client)["cells"]
    assert cells["normal"] == {"cell_type": None, "datasheet_check_live": "not_evaluated", "datasheet_check_parameters": "not_evaluated",
                               "datasheet_charge_current_limit": None, "datasheet_discharge_current_limit": None}
    assert cells["cell_type"] == {} and cells["live"] == {"reason": "no_assignment", "checks": []}
    assert cells["parameters"] == {"reason": "no_assignment", "checks": []}


def test_assigned_pack_publishes_checks_without_timestamps(monkeypatch):
    client, _ = api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=2, cell_packs=assigned(parallel=2))
    cells = last_payload(client)["cells"]
    assert cells["normal"]["cell_type"] == "EVE LF280K Rev B (2021)"
    assert cells["normal"]["datasheet_check_live"] == "ok"
    assert cells["normal"]["datasheet_check_parameters"] == "near"           # the rated capacity, see below
    assert cells["normal"]["datasheet_charge_current_limit"] == 560.0 and cells["normal"]["datasheet_discharge_current_limit"] == 560.0
    assert cells["cell_type"] == {
        "id": LF280K, "entry_version": 2, "manufacturer": "EVE", "model": "LF280K", "chemistry": "LFP", "nominal_v": 3.2,
        "nominal_ah": 280.0, "parallel": 2, "series": 16, "update_available": False,
    }
    keys = [c["key"] for c in cells["live"]["checks"]]
    assert keys == ["cell_voltage_high", "cell_voltage_low", "charge_temperature", "discharge_temperature", "charge_current", "discharge_current"]
    assert cells["live"]["reason"] is None
    parameter_keys = [c["key"] for c in cells["parameters"]["checks"]]
    assert "cells_in_series" in parameter_keys and "rated_capacity_ah" in parameter_keys and len(parameter_keys) == 22
    # rated capacity 280.03 Ah is 280 x 2 parallel off: a note, the rest is inside the datasheet
    rated = next(c for c in cells["parameters"]["checks"] if c["key"] == "rated_capacity_ah")
    assert rated["result"] == "near"
    assert "read_at" not in json.dumps(cells) and "generated_at" not in json.dumps(cells)


def test_the_test_bank_matches_its_datasheet_at_parallel_one(monkeypatch):
    client, _ = api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=2, cell_packs=assigned(parallel=1))
    cells = last_payload(client)["cells"]
    assert cells["normal"]["datasheet_check_parameters"] == "ok"
    assert {c["result"] for c in cells["parameters"]["checks"]} == {"ok"}
    assert {c["result"] for c in cells["live"]["checks"]} == {"ok"}


def test_serial_mismatch_and_invalid_snapshot_block_the_checks(monkeypatch):
    client, _ = api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=2,
                             cell_packs=assigned(mismatch=True))
    cells = last_payload(client)["cells"]
    assert cells["normal"]["datasheet_check_live"] == "not_evaluated" and cells["live"]["reason"] == "serial_mismatch"
    assert cells["parameters"]["reason"] == "serial_mismatch"
    assert cells["normal"]["cell_type"] == "EVE LF280K Rev B (2021)"          # still shown for re-assignment


def test_multi_pack_without_maint_has_no_parameters(monkeypatch):
    client, _ = api.run_main(monkeypatch, _multi_pack_bms(), number_of_packs=2, telemetry_polls=4, cell_packs=assigned())
    cells = last_payload(client)["cells"]
    assert cells["normal"]["datasheet_check_live"] == "ok"
    assert cells["parameters"] == {"reason": "no_parameters", "checks": []} and cells["normal"]["datasheet_check_parameters"] == "not_evaluated"


def test_update_available_for_a_bundled_and_an_own_type(monkeypatch):
    entry = next(e for e in api.bundled_cells() if e["id"] == LF280K)
    old = {**entry, "entry_version": entry["entry_version"] - 1}
    cell = {"id": LF280K, "entry_version": old["entry_version"], "parallel": 1, "entry": old}
    client, _ = api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=2,
                             cell_packs=api.stored_assignments({"0": pack(cell)}, pack_count=16))
    assert last_payload(client)["cells"]["cell_type"]["update_available"] is True


def test_a_failing_rule_does_not_stop_publishing(monkeypatch, caplog):
    def broken(*_a, **_k):
        raise RuntimeError("boom")
    monkeypatch.setattr(evaluate.checks, "live_checks", broken)
    client, _ = api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=2, cell_packs=assigned())
    cells = last_payload(client)["cells"]
    assert cells["live"]["reason"] == "check_error" and cells["normal"]["datasheet_check_live"] == "not_evaluated"
    assert cells["normal"]["datasheet_check_parameters"] == "ok"
    assert "Datasheet live checks failed" in caplog.text


# --- energy ---------------------------------------------------------------------------------------------------------

def test_pack_full_energy_uses_the_connection_voltage_without_an_assignment(monkeypatch):
    client, _ = api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=2)
    assert last_payload(client)["telemetry"]["normal"]["full_energy"] == 14.34       # 280.01 Ah x 16 x 3.2 V


def test_pack_full_energy_uses_the_assigned_nominal_voltage_and_never_the_parallel_count(monkeypatch):
    assignment = assigned(parallel=3, voltage={**next(e for e in api.bundled_cells() if e["id"] == LF280K)["voltage"], "nominal_v": 3.7})
    client, _ = api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=2, cell_packs=assignment)
    assert last_payload(client)["telemetry"]["normal"]["full_energy"] == 16.58       # 280.01 x 16 x 3.7 V, not x 3


def test_a_blocked_assignment_does_not_change_the_energy(monkeypatch):
    assignment = assigned(mismatch=True, voltage={**next(e for e in api.bundled_cells() if e["id"] == LF280K)["voltage"], "nominal_v": 3.7})
    client, _ = api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=2, cell_packs=assignment)
    assert last_payload(client)["telemetry"]["normal"]["full_energy"] == 14.34


def test_energy_follows_an_assignment_change_without_restart(monkeypatch):
    voltage = {**next(e for e in api.bundled_cells() if e["id"] == LF280K)["voltage"], "nominal_v": 3.7}
    new = assigned(voltage=voltage)
    entry = new[0].cell.entry
    client, _ = api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=8,
                             cells_after_polls=[(2, (new, ()))], cell_types_after_polls=(entry,))
    energies = [r.payload["telemetry"]["normal"]["full_energy"] for r in client.published if r.topic == "seplos/pack-0/sensors"]
    assert energies[0] == 14.34 and energies[-1] == 16.58 and not api.LAST_RUN["execs"]
    assert api.LAST_RUN["states"][0].cell_types == (entry,)


def test_system_total_is_the_sum_of_the_pack_energies(monkeypatch):
    voltage = {**next(e for e in api.bundled_cells() if e["id"] == LF280K)["voltage"], "nominal_v": 3.7}
    client, _ = api.run_main(monkeypatch, _multi_pack_bms(), number_of_packs=2, telemetry_polls=4, cell_packs=assigned(parallel=2, voltage=voltage))
    packs = {n: last_payload(client, f"seplos/pack-{n}/sensors")["telemetry"]["normal"]["full_energy"] for n in (0, 1)}
    assert packs == {0: 16.58, 1: 14.34}                                  # mixed nominal voltages
    assert last_payload(client, "seplos/system/sensors")["normal"]["total_full_energy"] == 30.92


def test_system_total_is_unknown_if_one_pack_is_unknown(monkeypatch):
    bms = _multi_pack_bms()
    del bms.ascii[(1, 0x42)]
    client, _ = api.run_main(monkeypatch, bms, number_of_packs=2, telemetry_polls=6)
    assert last_payload(client, "seplos/system/sensors")["normal"]["total_full_energy"] is None


def test_total_full_energy_needs_a_fresh_sample_of_every_pack(monkeypatch):
    clock = api.fake_monotonic(monkeypatch)
    api.no_sleep(monkeypatch)
    state, _pack_state, _client = api.availability_state(monkeypatch)
    pack_ = state.packs[0].pack
    assert api.total_full_energy(state, clock["t"]) is None                      # no sample yet
    values = {f"voltage_cell_{i}": 3.3 for i in range(1, 17)} | {"full_capacity": 280.01}
    pack_.sample = api.checks_sample(values, at=clock["t"])
    assert api.total_full_energy(state, clock["t"]) == 14.34
    assert api.total_full_energy(state, clock["t"] + state.pack_max_age(clock["t"]) + 1) is None   # stale
    pack_.sample = api.checks_sample({**values, "full_capacity": None}, at=clock["t"])
    assert api.total_full_energy(state, clock["t"]) is None


def test_mismatch_found_in_the_same_poll_uses_the_connection_voltage(monkeypatch):
    voltage = {**next(e for e in api.bundled_cells() if e["id"] == LF280K)["voltage"], "nominal_v": 3.7}
    bms = _single_pack_bms()
    bms.ascii[(0, 0xA4)] = f.serial_info("NEW")
    client, _ = api.run_main(monkeypatch, bms, number_of_packs=1, telemetry_polls=2, cell_packs=assigned(serial="OLD", voltage=voltage))
    state = api.LAST_RUN["states"][0]
    assert state.cell_packs[0].serial_mismatch is True
    first = [r.payload for r in client.published if r.topic == "seplos/pack-0/sensors"][0]
    assert first["telemetry"]["normal"]["full_energy"] == 14.34                  # 3.2 V, not 3.7 V
    assert first["cells"]["live"]["reason"] == "serial_mismatch"


def test_adopting_an_assignment_recomputes_the_total_from_the_same_sample(monkeypatch):
    client, _ = api.run_main(monkeypatch, _multi_pack_bms(), number_of_packs=2, telemetry_polls=4)
    state = api.LAST_RUN["states"][0]
    now = max(p.pack.sample.at for p in state.packs) + 1
    assert api.total_full_energy(state, now) == 28.68
    voltage = {**next(e for e in api.bundled_cells() if e["id"] == LF280K)["voltage"], "nominal_v": 3.7}
    new = assigned(voltage=voltage)
    state.request_cells(types.SimpleNamespace(packs=new, balancer_profiles=(), cell_types=(new[0].cell.entry,)))
    assert state.adopt_cells()
    assert api.total_full_energy(state, now) == 30.92                            # no new read
    state.cell_packs[0] = dataclasses.replace(state.cell_packs[0], serial_mismatch=True)
    assert api.total_full_energy(state, now) == 28.68                            # identity change, still no read


def test_mqtt_attributes_omit_measured_values_and_status_keeps_them(monkeypatch):
    api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=2, cell_packs=assigned())
    state = api.LAST_RUN["states"][0]
    pack_state = state.packs[0]
    now = pack_state.pack.sample.at + 1

    def group():
        report = api.pack_report(state, pack_state, now)
        return report, evaluate.mqtt_group(report)

    report, before = group()
    for attributes in (before["live"], before["parameters"]):
        assert attributes["checks"] and all(set(c) == {"key", "result", "limit", "unit", "reason"} for c in attributes["checks"])
    assert all("value" in c for c in report["live"]["checks"])                   # /api/status keeps them
    sample = pack_state.pack.sample
    pack_state.pack.sample = api.checks_sample({**sample.values, "current": sample.values["current"] + 0.01}, at=sample.at)
    report2, after = group()
    assert after == before and json.dumps(after) == json.dumps(before)
    assert report2["live"]["checks"] != report["live"]["checks"]                 # status changed
    # a limit, result or reason change is still published
    pack_state.pack.sample = api.checks_sample({**sample.values, "cell_temperature_1": -5.0}, at=sample.at)
    assert group()[1]["live"] != before["live"]


# --- Home Assistant -----------------------------------------------------------------------------------------------------

def cell_configs() -> dict:
    disc, client = api.discovery()
    disc.publish_pack(0, pack_bus=True)
    prefix = "homeassistant/sensor/seplos-mqtt-pack-0/"
    return {r.topic[len(prefix):-len("/config")]: r.payload for r in client.published
            if r.topic.startswith(prefix) and r.topic.split("/")[-2].startswith(("cell_type", "datasheet_"))}


def test_discovery_of_the_cell_entities():
    configs = cell_configs()
    assert sorted(configs) == ["cell_type", "datasheet_charge_current_limit", "datasheet_check_live", "datasheet_check_parameters",
                               "datasheet_discharge_current_limit"]
    sensor = "seplos/pack-0/sensors"
    cell_type = configs["cell_type"]
    assert cell_type["val_tpl"] == "{{ value_json.cells.normal.cell_type }}" and cell_type["ent_cat"] == "diagnostic"
    assert cell_type["json_attr_t"] == sensor and cell_type["json_attr_tpl"] == "{{ value_json.get('cells', {}).get('cell_type', {}) | tojson }}"
    assert cell_type["default_entity_id"] == "sensor.seplos_bms_pack_0_cell_type" and cell_type["uniq_id"] == "seplos_bms_pack_0_cell_type"
    assert cell_type["dev"]["ids"] == "seplos_bms_pack_0" or cell_type["dev"] == {"ids": "seplos_bms_pack_0"}
    for key, group in (("datasheet_check_live", "live"), ("datasheet_check_parameters", "parameters")):
        config = configs[key]
        assert config["dev_cla"] == "enum" and config["ops"] == ["ok", "near", "over", "not_evaluated"] and config["ent_cat"] == "diagnostic"
        assert "stat_cla" not in config and "unit_of_meas" not in config and "en" not in config
        assert config["json_attr_tpl"] == f"{{{{ value_json.get('cells', {{}}).get('{group}', {{}}) | tojson }}}}"
        assert config["val_tpl"] == f"{{{{ value_json.cells.normal.{key} }}}}"
    for key in ("datasheet_charge_current_limit", "datasheet_discharge_current_limit"):
        config = configs[key]
        assert config["en"] is False and config["unit_of_meas"] == "A" and config["dev_cla"] == "current"
        assert config["stat_cla"] == "measurement" and config["ent_cat"] == "diagnostic" and "json_attr_tpl" not in config
        assert config["stat_t"] == sensor and config["default_entity_id"] == f"sensor.seplos_bms_pack_0_{key}"


def test_current_limits_are_not_inverted_and_other_entities_stay_enabled():
    disc, client = api.discovery(invert=True)
    disc.publish_pack(0, pack_bus=True)
    configs = {r.topic.split("/")[-2]: r.payload for r in client.published if isinstance(r.payload, dict)}
    assert configs["datasheet_charge_current_limit"]["val_tpl"] == "{{ value_json.cells.normal.datasheet_charge_current_limit }}"
    disabled = [key for key, config in configs.items() if config.get("en") is False]
    assert disabled == ["datasheet_charge_current_limit", "datasheet_discharge_current_limit"]


# --- /api/status ----------------------------------------------------------------------------------------------------------

def test_status_has_the_assignment_both_summaries_and_the_checks(monkeypatch):
    api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=2, cell_packs=assigned(parallel=2))
    state = api.LAST_RUN["states"][0]
    cells = state.status["packs"][0]["cells"]
    assert set(cells) == {"assignment", "series", "live", "parameters", "sample_age_s", "parameters_age_s"}
    assert cells["assignment"] == {
        "id": LF280K, "entry_version": 2, "parallel": 2, "valid": True, "serial_mismatch": False, "update_available": False,
        "name": "EVE LF280K Rev B (2021)",
    }
    assert cells["series"] == 16
    assert cells["live"]["summary"] == "ok" and len(cells["live"]["checks"]) == 6 and cells["live"]["charge_limit_a"] == 560.0
    assert cells["parameters"]["summary"] == "near" and len(cells["parameters"]["checks"]) == 22
    assert cells["sample_age_s"] is not None and cells["parameters_age_s"] is not None
    json.dumps(state.status)


def test_status_of_an_unassigned_pack_before_and_after_the_first_poll(monkeypatch):
    api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=2)
    cells = api.LAST_RUN["states"][0].status["packs"][0]["cells"]
    assert cells["assignment"] is None and cells["live"]["reason"] == "no_assignment" and cells["sample_age_s"] is not None


def test_a_stale_sample_shows_in_the_report(monkeypatch):
    api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=2, cell_packs=assigned())
    state = api.LAST_RUN["states"][0]
    pack_state = state.packs[0]
    state.cycle_start = 0.0               # no running cycle stretching the allowance
    later = pack_state.pack.sample.at + state.pack_max_age(pack_state.pack.sample.at) + 5
    report = api.pack_report(state, pack_state, later)
    assert report["live"]["reason"] == "stale_sample" and report["live"]["summary"] == "not_evaluated"
    assert report["parameters"]["reason"] is None            # the parameters follow their own cadence (3 x 1 h)
    very_late = pack_state.pack.parameters_ts + 3 * 3600 + 1
    assert api.pack_report(state, pack_state, very_late)["parameters"]["reason"] == "stale_parameters"


def test_report_does_not_change_the_stored_assignment(monkeypatch):
    assignments = assigned(parallel=2)
    before = copy.deepcopy(assignments[0].cell.to_dict())
    api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=2, cell_packs=assignments)
    assert assignments[0].cell.to_dict() == before


def test_published_group_matches_the_status_block(monkeypatch):
    client, _ = api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=2, cell_packs=assigned())
    published = last_payload(client)["cells"]
    state = api.LAST_RUN["states"][0]
    report = api.pack_report(state, state.packs[0], state.packs[0].pack.sample.at + 1)
    without_values = [{k: v for k, v in c.items() if k != "value"} for c in report["live"]["checks"]]
    assert published["live"]["checks"] == without_values and published["normal"]["datasheet_check_live"] == report["live"]["summary"]
    assert f.telemetry_info()      # frames stay importable for the other tests
