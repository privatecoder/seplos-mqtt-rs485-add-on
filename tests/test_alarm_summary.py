"""Aggregate pack alarm (``alarm_summary``) and the datasheet limits on the ``cell_type`` attributes."""
from __future__ import annotations

import json
import re

import pytest

import api
import frames as f
from test_cell_checks import LF280K, assigned, last_payload
from test_characterization import _single_pack_bms

rules = api.alarm_rules()


def decoded(**kwargs) -> dict:
    return api.decode_telesignal(f.telesignal_info(**kwargs))


def summary(**kwargs) -> dict:
    return api.alarm_summary(decoded(**kwargs))


def base_key(key: str) -> str:
    return re.sub(r"_\d+$", "", key) if key not in rules.severity else key


# --- the mapping table -----------------------------------------------------------------------------------------

def test_the_table_covers_every_decoded_key_exactly_once():
    result = decoded()
    produced = {base_key(k) for k in (*result["normal"], *result["binary"])}
    expected = {bf.key for bf in rules.bit_fields} | {"cell_voltage_alarm", *rules.byte_alarms} \
        | {"any_cell_voltage_alarm", "any_cell_temperature_alarm", "system_status"}
    maint = {"storage_problem", "pack_lock_active", "maintenance_mode"}
    assert produced == expected
    assert set(rules.severity) == expected | maint                   # nothing missing, nothing stale


def test_two_bit_fields_are_tiered_and_everything_else_has_a_fixed_class_or_is_excluded():
    for field in rules.bit_fields:
        rule = rules.severity[field.key]
        assert (rule == rules.tiered) == (field.mode in rules.two_bit_modes), field.key
        if rule != rules.tiered:
            assert rule in (None, rules.alarm, rules.protection, rules.fault), field.key
    assert rules.severities == ("ok", "alarm", "protection", "fault")


def test_fault_bits_are_faults_whatever_their_mode():
    for field in rules.bit_fields:
        if field.mode == "fault_normal":
            assert rules.severity[field.key] == rules.fault, field.key


def test_operating_states_never_contribute():
    result = summary(on_off=0x0F, balancing=(0xFF, 0xFF), system_status=0x02,
                     events_1_6=(0, 0, 0, 0x40, 0, 0x02), event7=0x30)
    assert result == {"normal": {"bms_alarm": "ok"}, "attributes": {"active": [], "count": 0}}


# --- severities -----------------------------------------------------------------------------------------------

def test_ok_without_any_condition():
    assert summary() == {"normal": {"bms_alarm": "ok"}, "attributes": {"active": [], "count": 0}}


@pytest.mark.parametrize("kwargs, expected, key, state", [
    ({"cell_alarms": [0, 0, 1] + [0] * 13}, "alarm", "cell_voltage_alarm_3", "Alarm (low)"),
    ({"temp_alarms": (0, 2, 0, 0)}, "alarm", "cell_temperature_alarm_2", "Alarm (high)"),
    ({"ambient_alarm": 1}, "alarm", "ambient_temperature_alarm", "Alarm (low)"),
    ({"current_alarm": 3}, "alarm", "dis_charging_current_alarm", "Alarm (other)"),
    ({"events_1_6": (0, 0x01, 0, 0, 0, 0)}, "alarm", "cell_overvoltage", "Alarm"),
    ({"events_1_6": (0, 0x03, 0, 0, 0, 0)}, "protection", "cell_overvoltage", "Protection"),
    ({"events_1_6": (0, 0, 0, 0, 0x10, 0)}, "protection", "transient_overcurrent", "Protection"),
    ({"events_1_6": (0, 0, 0, 0, 0x50, 0)}, "protection", "transient_overcurrent", "Lockout"),
    ({"events_1_6": (0, 0, 0, 0, 0x20, 0)}, "protection", "output_short_circuit", "Protection"),
    ({"events_1_6": (0, 0, 0, 0, 0, 0x01)}, "protection", "charging_high_voltage_protection", "Protection"),
    ({"events_1_6": (0, 0, 0, 0, 0, 0x10)}, "protection", "cell_low_voltage_forbidden_charging", "Protection"),
    ({"events_1_6": (0, 0, 0, 0, 0, 0x04)}, "alarm", "soc_low", "Alarm"),
    ({"events_1_6": (0, 0, 0, 0, 0, 0x0C)}, "protection", "soc_low", "Protection"),
    ({"event8": 0x04}, "alarm", "no_calibration_of_voltage", "Warning"),
    ({"event8": 0x20}, "alarm", "rtc_time_invalid", "Warning"),
    ({"events_1_6": (0x01, 0, 0, 0, 0, 0)}, "fault", "voltage_sensing_failure", "Fault"),
    ({"events_1_6": (0, 0, 0, 0x80, 0, 0)}, "fault", "severe_fault", "Fault"),
    ({"events_1_6": (0, 0, 0, 0, 0, 0x40)}, "fault", "output_connection_failure", "Fault"),
    ({"events_1_6": (0x10, 0, 0, 0, 0, 0)}, "fault", "cell_voltage_difference_fault", "Fault"),
    ({"event8": 0x01}, "fault", "eep_storage_failure", "Fault"),
    ({"event8": 0x02}, "fault", "rtc_clock_failure", "Fault"),
])
def test_single_conditions(kwargs, expected, key, state):
    result = summary(**kwargs)
    assert result["normal"]["bms_alarm"] == expected
    assert result["attributes"] == {"active": [{"key": key, "state": state}], "count": 1}


def test_the_worst_severity_wins_and_the_conditions_are_sorted_by_key():
    result = summary(cell_alarms=[2] + [0] * 15, events_1_6=(0x20, 0x03, 0, 0, 0, 0))
    assert result["normal"]["bms_alarm"] == "fault"
    assert result["attributes"]["active"] == [
        {"key": "cell_overvoltage", "state": "Protection"},
        {"key": "cell_voltage_alarm_1", "state": "Alarm (high)"},
        {"key": "charging_switch_failure", "state": "Fault"},
    ]
    assert result["attributes"]["count"] == 3
    protection = summary(cell_alarms=[2] + [0] * 15, events_1_6=(0, 0x03, 0, 0, 0, 0))
    assert protection["normal"]["bms_alarm"] == "protection"
    assert summary(cell_alarms=[2] + [0] * 15)["normal"]["bms_alarm"] == "alarm"


def test_maint_storage_problem_is_a_fault_but_the_pack_lock_is_not():
    base = decoded()
    assert api.alarm_summary(base, {"storage_problem": "ON"}) == {
        "normal": {"bms_alarm": "fault"}, "attributes": {"active": [{"key": "storage_problem", "state": "ON"}], "count": 1}}
    for quiet in ({"storage_problem": "OFF"}, {"storage_problem": None}, {"pack_lock_active": "ON", "maintenance_mode": "ON"}, None, {}):
        assert api.alarm_summary(base, quiet)["normal"]["bms_alarm"] == "ok"


# --- unknown --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("missing", [None, {}, {"normal": {}, "binary": {}}, {"normal": {"system_status": "Standby"}}, "x"])
def test_without_valid_alarm_data_the_state_is_unknown_never_ok(missing):
    assert api.alarm_summary(missing, {"storage_problem": "ON"}) == {
        "normal": {"bms_alarm": "unknown"}, "attributes": {"active": [], "count": 0}}


def test_the_ha_entity_renders_unknown_as_none_and_offers_only_the_four_severities():
    config = api.alarm_entity_config(0)
    assert config["dev_cla"] == "enum" and config["ops"] == ["ok", "alarm", "protection", "fault"]
    assert config["val_tpl"] == "{{ none if value_json.alarm_summary.normal.bms_alarm == 'unknown' else value_json.alarm_summary.normal.bms_alarm }}"
    assert config["json_attr_tpl"] == "{{ value_json.get('alarm_summary', {}).get('attributes', {}) | tojson }}"
    assert "ent_cat" not in config                                       # shown on the device page, not a diagnostic


# --- in the published payload -----------------------------------------------------------------------------------

def test_the_poll_publishes_the_summary_and_it_is_stable_across_unchanged_and_measurement_only_polls(monkeypatch):
    api.no_sleep(monkeypatch)
    with api.mode(1):
        pack = api.make_pack(0)
        bms = _single_pack_bms()
        first, ok = api.poll(pack, bms)
        assert ok and first["alarm_summary"] == {"normal": {"bms_alarm": "ok"}, "attributes": {"active": [], "count": 0}}
        second, _ = api.poll(pack, bms)
        assert json.dumps(first["alarm_summary"]) == json.dumps(second["alarm_summary"])
        bms.ascii[(0, 0x42)] = f.telemetry_info(cycles=99)               # a changed measurement
        third, _ = api.poll(pack, bms)
        assert third["telemetry"] != first["telemetry"] and third["alarm_summary"] == first["alarm_summary"]
        bms.ascii[(0, 0x44)] = f.telesignal_info(events_1_6=(0, 0x03, 0, 0, 0, 0))
        fourth, _ = api.poll(pack, bms)
        assert fourth["alarm_summary"]["normal"]["bms_alarm"] == "protection"
        assert fourth["alarm_summary"]["attributes"]["active"] == [{"key": "cell_overvoltage", "state": "Protection"}]


def test_a_failed_alarm_read_publishes_nothing_so_the_state_is_never_a_stale_ok(monkeypatch):
    api.no_sleep(monkeypatch)
    with api.mode(1):
        pack = api.make_pack(0)
        bms = _single_pack_bms()
        assert api.poll(pack, bms)[1]
        bms.ascii[(0, 0x44)] = None
        data, ok = api.poll(pack, bms)
        assert data is None and not ok


# --- datasheet limits -------------------------------------------------------------------------------------------

LIMITS_X1 = {
    "cell_voltage_min_v": 2.5, "cell_voltage_max_v": 3.65, "cell_voltage_nominal_v": 3.2,
    "charge_temperature_min_c": 0.0, "charge_temperature_max_c": 55.0,
    "discharge_temperature_min_c": -20.0, "discharge_temperature_max_c": 55.0,
    "charge_current_max_a": 280.0, "discharge_current_max_a": 280.0,
}


def test_limits_of_the_lf280k_at_parallel_one_and_two(monkeypatch):
    for parallel, current in ((1, 280.0), (2, 560.0)):
        client, _ = api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=2,
                                 cell_packs=assigned(parallel=parallel))
        attributes = last_payload(client)["cells"]["cell_type"]
        assert attributes["id"] == LF280K and attributes["parallel"] == parallel
        assert attributes["limits"] == {**LIMITS_X1, "charge_current_max_a": current, "discharge_current_max_a": current}


def test_limits_are_null_without_a_trusted_assignment(monkeypatch):
    client, _ = api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=2)
    assert last_payload(client)["cells"]["cell_type"] == {"limits": None}
    client, _ = api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=2, cell_packs=assigned(mismatch=True))
    attributes = last_payload(client)["cells"]["cell_type"]
    assert attributes["id"] == LF280K and attributes["limits"] is None      # identity not trusted


def test_a_type_without_a_current_or_temperature_field_leaves_just_that_limit_null():
    entry = next(e for e in api.bundled_cells() if e["id"] == LF280K)
    stripped = {**entry, "current": {k: v for k, v in entry["current"].items() if k != "max_continuous_discharge_a"}}
    limits = api.cell_limits(stripped, 3)
    assert limits["discharge_current_max_a"] is None and limits["charge_current_max_a"] == 840.0
    assert limits["cell_voltage_nominal_v"] == 3.2
    assert api.cell_limits(None, 1) is None


def test_the_charge_temperature_limits_are_the_effective_derating_range_the_checks_use():
    entry = next(e for e in api.bundled_cells() if e["id"] == "eve-lf105-pbri-rev-d")
    # the plain fields say 0..65 °C, the derating table only allows charging from 10 to 55 °C
    assert (entry["temperature"]["charge_min_c"], entry["temperature"]["charge_max_c"]) == (0, 65)
    limits = api.cell_limits(entry, 1)
    assert (limits["charge_temperature_min_c"], limits["charge_temperature_max_c"]) == (10, 55)
    assert (limits["discharge_temperature_min_c"], limits["discharge_temperature_max_c"]) == (
        entry["temperature"]["discharge_min_c"], entry["temperature"]["discharge_max_c"])
    # no range at all: null, not a guess
    bare = {**entry, "temperature": {k: v for k, v in entry["temperature"].items() if not k.startswith("charge_")}}
    bare.pop("charge_derating", None)
    limits = api.cell_limits(bare, 1)
    assert limits["charge_temperature_min_c"] is None and limits["charge_temperature_max_c"] is None
