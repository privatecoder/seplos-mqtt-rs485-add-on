"""Computed external balancer (stage 3c): shared vectors, resets, Home Assistant entity, /api/status."""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

import api
from seplos_mqtt.discovery import Discovery
from seplos_mqtt.state import StateStore
from seplos_mqtt.status import build_status
from test_cells_model import pack, profile
from test_characterization import _single_pack_bms

VECTORS = json.loads((Path(__file__).parent / "vectors" / "balancer.json").read_text(encoding="utf-8"))
CASES = [pytest.param(case, id=f"{i}-{case['name']}") for i, case in enumerate(VECTORS["cases"])]

NOW = 1000.0
MAX_AGE = 60.0
TOPIC = "homeassistant/sensor/seplos-mqtt-pack-{}/external_balancer/config"


def sample_for(step: dict, now: float = NOW):
    """The sample of a vector step (None = missing); a stale one is older than its allowance."""
    kind = step.get("sample")
    if kind == "missing":
        return None
    if "cells_mv" in step:
        cells = step["cells_mv"]
    else:
        cells = [3300, 3300 + step.get("spread", 20)]       # a stale sample would decide if it were fresh
    values = {f"voltage_cell_{i}": mv / 1000 for i, mv in enumerate(cells, start=1)}
    values["current"] = step.get("current_a", 0)
    return api.checks_sample(values, at=now - (MAX_AGE + 1 if kind == "stale" else 0), max_age=MAX_AGE)


# --- shared vectors -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("case", CASES)
def test_vectors(case):
    state = "unknown"
    for index, step in enumerate(case["steps"]):
        state = api.balancer_step(state, case["profile"], sample_for(step), NOW)
        assert state == step["expect"], (index, step)


@pytest.mark.parametrize("case", CASES)
def test_vectors_through_the_tracker(case):
    tracker = api.balancer_tracker()
    assigned = types.SimpleNamespace(id=1, name="B", values=case["profile"])
    for index, step in enumerate(case["steps"]):
        assert tracker.update(assigned, False, sample_for(step), NOW) == step["expect"], (index, step)
        assert tracker.view(assigned, False) == step["expect"]


def test_every_deviation_is_explained_and_real():
    for case in VECTORS["cases"]:
        assert ("deviation" in case) == ("openbms" in case) or case["name"].startswith("a missing or stale"), case["name"]
        if "deviation" in case:
            assert len(case["deviation"]) > 20, case["name"]
        if "openbms" in case:
            assert case["openbms"] != [s["expect"] for s in case["steps"]], case["name"]
            assert len(case["openbms"]) == len(case["steps"])


def test_values_are_normalized_like_the_exchange_format():
    rule = api.balancer_rule
    assert rule({"trigger_mv": 10, "stop_mv": None}).stop_mv == 10
    assert rule({"trigger_mv": 10, "stop_mv": 20}).stop_mv == 10
    assert rule({"trigger_mv": 10, "stop_mv": 4}).stop_mv == 4
    assert rule({"trigger_mv": 10, "start_mv": None, "stop_cell_mv": 3300}).cell_stop_mv is None
    assert rule({"trigger_mv": 10, "start_mv": 3400}).cell_stop_mv == 3400
    assert rule({"trigger_mv": 10, "start_mv": 3400, "stop_cell_mv": 3500}).cell_stop_mv == 3400
    assert rule({"trigger_mv": 10, "start_mv": 3400, "stop_cell_mv": 3380}).cell_stop_mv == 3380


def test_float_noise_does_not_move_a_threshold():
    values = {"voltage_cell_1": 3.3, "voltage_cell_2": 3.31, "current": 1.01}
    sample = api.checks_sample(values, at=NOW, max_age=MAX_AGE)
    assert api.balancer_step("unknown", {"trigger_mv": 10, "when": "charging"}, sample, NOW) == "active"


# --- resets ---------------------------------------------------------------------------------------------------------

BAND = {"trigger_mv": 10, "stop_mv": 5, "start_mv": None, "stop_cell_mv": None, "when": "always", "max_a": None}


def fresh(spread: int, current: float = 0.0, at: float = NOW):
    return sample_for({"spread": spread, "current_a": current}, at)


def active_tracker():
    tracker = api.balancer_tracker()
    assigned = types.SimpleNamespace(id=1, name="B", values=dict(BAND))
    assert tracker.update(assigned, False, fresh(12), NOW) == "active"
    assert tracker.update(assigned, False, fresh(7), NOW) == "active"       # held in the band
    return tracker, assigned


def test_a_restart_starts_unknown():
    _tracker, assigned = active_tracker()
    assert api.balancer_tracker().update(assigned, False, fresh(7), NOW) == "unknown"


def test_a_stale_sample_resets_to_unknown_and_the_band_stays_unknown():
    tracker, assigned = active_tracker()
    assert tracker.update(assigned, False, fresh(7, at=NOW - MAX_AGE - 1), NOW) == "unknown"
    assert tracker.update(assigned, False, fresh(7), NOW) == "unknown"
    assert tracker.update(assigned, False, fresh(12), NOW) == "active"


def test_a_gap_between_two_fresh_samples_drops_the_carried_state():
    for gap in (61, 121):
        tracker, assigned = active_tracker()                                         # last evaluated sample: NOW
        cached = fresh(12)
        assert tracker.update(assigned, False, cached, NOW + 59) == "active"         # a failed poll, 59 s old: still fresh
        assert tracker.update(assigned, False, cached, NOW + 59) == "active"         # and again: no extension of continuity
        assert tracker.update(assigned, False, fresh(7, at=NOW + gap), NOW + gap) == "unknown", gap


def test_a_decisive_recovery_sample_decides_at_once():
    for spread, expected in ((12, "active"), (4, "inactive")):
        tracker, assigned = active_tracker()
        assert tracker.update(assigned, False, fresh(spread, at=NOW + 61), NOW + 61) == expected


def test_a_gap_up_to_the_allowance_keeps_the_state():
    tracker, assigned = active_tracker()
    assert tracker.update(assigned, False, fresh(7, at=NOW + MAX_AGE), NOW + MAX_AGE) == "active"      # exactly the allowance
    assert tracker.update(assigned, False, fresh(7, at=NOW + 2 * MAX_AGE + 1), NOW + 2 * MAX_AGE + 1) == "unknown"


def test_a_gap_after_a_reset_or_with_the_next_samples_continuing():
    tracker, assigned = active_tracker()
    for step in range(1, 5):                                                         # regular polls: continuity holds
        assert tracker.update(assigned, False, fresh(7, at=NOW + 30 * step), NOW + 30 * step) == "active"


def test_a_missing_sample_resets_to_unknown():
    tracker, assigned = active_tracker()
    assert tracker.update(assigned, False, None, NOW) == "unknown"
    assert tracker.update(assigned, False, fresh(7), NOW) == "unknown"


def test_a_serial_mismatch_resets_and_blocks_until_confirmed():
    tracker, assigned = active_tracker()
    assert tracker.update(assigned, True, fresh(12), NOW) == "unknown"
    assert tracker.view(assigned, True) == "unknown"
    assert tracker.update(assigned, True, fresh(2), NOW) == "unknown"              # even a deciding sample
    assert tracker.update(assigned, False, fresh(7), NOW) == "unknown"             # confirmed: the band cannot decide
    assert tracker.update(assigned, False, fresh(12), NOW) == "active"


def test_assigning_unassigning_and_reassigning_reset():
    tracker, assigned = active_tracker()
    assert tracker.update(None, False, fresh(12), NOW) is None
    assert tracker.view(None, False) is None
    assert tracker.update(assigned, False, fresh(7), NOW) == "unknown"             # assigned again after unassigning
    assert tracker.update(assigned, False, fresh(12), NOW) == "active"
    other = types.SimpleNamespace(id=2, name="B", values=dict(BAND))                # same values, another profile
    assert tracker.view(other, False) == "unknown"
    assert tracker.update(other, False, fresh(7), NOW) == "unknown"


def test_an_edit_of_the_values_resets_but_a_rename_does_not():
    tracker, assigned = active_tracker()
    renamed = types.SimpleNamespace(id=1, name="Renamed", values=dict(BAND))
    assert tracker.view(renamed, False) == "active"
    assert tracker.update(renamed, False, fresh(7), NOW) == "active"
    edited = types.SimpleNamespace(id=1, name="B", values={**BAND, "max_a": 4})     # any value, also an informative one
    assert tracker.view(edited, False) == "unknown"
    assert tracker.update(edited, False, fresh(7), NOW) == "unknown"
    assert tracker.update(types.SimpleNamespace(id=1, name="B", values={**BAND, "trigger_mv": 11}), False, fresh(12), NOW) == "active"


# --- app state: assignments, polling, status -------------------------------------------------------------------------

def make_state(monkeypatch, tmp_path, profiles=(), assignments=None, discovery=True, store=True):
    state, pack_state, client = api.availability_state(monkeypatch)
    config = api.stored_cells(assignments or {}, list(profiles))
    state.cell_packs, state.balancer_profiles = dict(config.packs), config.balancer_profiles
    if discovery:
        state.discovery = Discovery(client, "seplos", "homeassistant", False)
    if store:
        state.store = StateStore(str(tmp_path / "state.json"))
    return state, pack_state, client


def give_sample(pack_state, spread: int, current: float = 0.0, at: float = NOW) -> None:
    pack_state.pack.sample = sample_for({"spread": spread, "current_a": current}, at)


def test_a_pack_without_a_profile_has_no_balancer(monkeypatch, tmp_path):
    state, pack_state, _client = make_state(monkeypatch, tmp_path)
    give_sample(pack_state, 12)
    api.balancer_update(state, pack_state, NOW)
    assert api.balancer_report(state, pack_state) is None and state.balancers == {}
    assert build_status(state, NOW, "t")["packs"][0]["balancer"] is None


def test_polling_drives_the_state_and_status_reports_it(monkeypatch, tmp_path):
    state, pack_state, _client = make_state(
        monkeypatch, tmp_path, profiles=[profile(1, "Bal A", start_mv=None, stop_cell_mv=None, trigger_mv=10, stop_mv=5)],
        assignments={"0": pack(None, 1)})
    assert api.balancer_report(state, pack_state) == {"profile_id": 1, "profile_name": "Bal A", "state": "unknown"}   # before any poll
    give_sample(pack_state, 12)
    api.balancer_update(state, pack_state, NOW)
    assert build_status(state, NOW, "t")["packs"][0]["balancer"] == {"profile_id": 1, "profile_name": "Bal A", "state": "active"}
    give_sample(pack_state, 4)
    api.balancer_update(state, pack_state, NOW)
    assert api.balancer_report(state, pack_state)["state"] == "inactive"


def test_the_sample_ages_while_polls_fail(monkeypatch, tmp_path):
    state, pack_state, _client = make_state(
        monkeypatch, tmp_path, profiles=[profile(1, start_mv=None, stop_cell_mv=None, trigger_mv=10, stop_mv=5)], assignments={"0": pack(None, 1)})
    give_sample(pack_state, 12)
    api.balancer_update(state, pack_state, NOW)
    assert api.balancer_report(state, pack_state)["state"] == "active"
    pack_state.pack.sample = api.checks_sample(dict(pack_state.pack.sample.values), at=NOW)       # the last good sample stays
    later = NOW + state.pack_max_age(NOW) + 1
    api.balancer_update(state, pack_state, later)                                                    # no new sample came
    assert api.balancer_report(state, pack_state)["state"] == "unknown"


def test_status_is_unknown_once_the_sample_is_stale_even_if_this_pack_was_not_polled(monkeypatch, tmp_path):
    state, pack_state, _client = make_state(
        monkeypatch, tmp_path, profiles=[profile(1, start_mv=None, stop_cell_mv=None, trigger_mv=10, stop_mv=5)], assignments={"0": pack(None, 1)})
    give_sample(pack_state, 12)
    api.balancer_update(state, pack_state, NOW)
    allowance = state.pack_max_age(NOW)
    assert build_status(state, NOW + allowance - 1, "t")["packs"][0]["balancer"]["state"] == "active"
    later = NOW + allowance + 1                                       # another pack's poll refreshes availability and status
    api.publish_availability(state, pack_state, later)
    assert pack_state.availability == "offline"
    assert build_status(state, later, "t")["packs"][0]["balancer"]["state"] == "unknown"
    assert api.pack_report(state, pack_state, later)["balancer"]["state"] == "unknown"           # also the MQTT group's source


def test_a_recovery_after_failed_polls_does_not_carry_the_old_state(monkeypatch, tmp_path):
    state, pack_state, _client = make_state(
        monkeypatch, tmp_path, profiles=[profile(1, start_mv=None, stop_cell_mv=None, trigger_mv=10, stop_mv=5)], assignments={"0": pack(None, 1)})
    give_sample(pack_state, 12)
    api.balancer_update(state, pack_state, NOW)
    allowance = state.pack_max_age(NOW)
    api.balancer_update(state, pack_state, NOW + allowance - 1)                  # failed polls re-process the cached sample
    assert api.balancer_report(state, pack_state, NOW + allowance - 1)["state"] == "active"
    recovery = NOW + allowance + 1
    give_sample(pack_state, 7, at=recovery)                                       # inside the band
    api.balancer_update(state, pack_state, recovery)
    assert api.balancer_report(state, pack_state, recovery)["state"] == "unknown"
    give_sample(pack_state, 12, at=recovery + 10)
    api.balancer_update(state, pack_state, recovery + 10)
    assert api.balancer_report(state, pack_state, recovery + 10)["state"] == "active"


def test_an_assignment_change_is_unknown_at_once_in_the_status(monkeypatch, tmp_path):
    first = profile(1, "A", start_mv=None, stop_cell_mv=None, trigger_mv=10, stop_mv=5)
    state, pack_state, _client = make_state(monkeypatch, tmp_path, profiles=[first], assignments={"0": pack(None, 1)})
    give_sample(pack_state, 12)
    api.balancer_update(state, pack_state, NOW)
    assert api.balancer_report(state, pack_state)["state"] == "active"
    edited = profile(1, "A", start_mv=None, stop_cell_mv=None, trigger_mv=11, stop_mv=5)
    state.balancer_profiles = api.stored_cells({}, [edited]).balancer_profiles       # adopted at the cycle boundary
    assert api.balancer_report(state, pack_state)["state"] == "unknown"              # before the next poll
    api.balancer_update(state, pack_state, NOW)
    assert api.balancer_report(state, pack_state)["state"] == "active"
    state.cell_packs = api.stored_cells({}, [edited]).packs                          # unassigned
    api.balancer_update(state, pack_state, NOW)
    assert api.balancer_report(state, pack_state) is None and state.balancers == {}


def test_a_stored_serial_mismatch_blocks_the_state(monkeypatch, tmp_path):
    state, pack_state, _client = make_state(
        monkeypatch, tmp_path, profiles=[profile(1, start_mv=None, stop_cell_mv=None, trigger_mv=10, stop_mv=5)],
        assignments={"0": pack(None, 1, mismatch=True)})
    give_sample(pack_state, 12)
    api.balancer_update(state, pack_state, NOW)
    assert api.balancer_report(state, pack_state)["state"] == "unknown"


def test_a_defect_in_the_rule_does_not_stop_the_polling(monkeypatch, tmp_path, caplog):
    state, pack_state, _client = make_state(
        monkeypatch, tmp_path, profiles=[profile(1, start_mv=None, stop_cell_mv=None, trigger_mv=10, stop_mv=5)], assignments={"0": pack(None, 1)})
    from seplos_mqtt.cells import balancer

    def broken(*_a, **_k):
        raise RuntimeError("boom")
    monkeypatch.setattr(balancer, "step", broken)
    give_sample(pack_state, 12)
    api.balancer_update(state, pack_state, NOW)
    assert api.balancer_report(state, pack_state)["state"] == "unknown" and "Balancer state failed" in caplog.text


# --- main loop and MQTT payload -----------------------------------------------------------------------------------------

def cells_payloads(client) -> list[dict]:
    return [r.payload["cells"] for r in client.published if r.topic == "seplos/pack-0/sensors"]


def assigned_profile(profile_id: int | None, mismatch: bool = False) -> dict:
    profiles = [profile(1, "Bal"), profile(2, "Wide")]          # the document validates the reference
    return api.stored_cells({"0": pack(None, profile_id, mismatch=mismatch)}, profiles, pack_count=16).packs


def band_profile(profile_id: int, name: str = "Bal", **values) -> object:
    base = {"start_mv": None, "stop_cell_mv": None, "trigger_mv": 10, "stop_mv": 5}
    return api.stored_cells({}, [profile(profile_id, name, **{**base, **values})]).balancer_profiles[0]


def test_the_payload_has_the_state_only_for_a_pack_with_a_profile(monkeypatch):
    client, _ = api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=2)
    assert all("external_balancer" not in c["normal"] for c in cells_payloads(client))
    client, _ = api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=2,
                             cell_packs=assigned_profile(1), balancer_profiles=(band_profile(1),))
    assert cells_payloads(client)[-1]["normal"]["external_balancer"] == "active"       # the default pack: 15 mV spread


def test_reassigning_a_profile_starts_unknown_again(monkeypatch):
    second = band_profile(2, "Wide", trigger_mv=30, stop_mv=10)                          # 15 mV is inside its band
    new = assigned_profile(2)
    client, _ = api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=8,
                             cell_packs=assigned_profile(1), balancer_profiles=(band_profile(1),),
                             cells_after_polls=[(3, (new, (second,)))])
    states = [c["normal"]["external_balancer"] for c in cells_payloads(client)]
    assert states[0] == "active" and states[-1] == "unknown" and not api.LAST_RUN["execs"]


def test_a_mismatch_found_by_the_guard_makes_the_state_unknown(monkeypatch):
    client, _ = api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=2,
                             cell_packs=assigned_profile(1, mismatch=True), balancer_profiles=(band_profile(1),))
    assert cells_payloads(client)[-1]["normal"]["external_balancer"] == "unknown"


def test_the_status_of_a_running_pack_has_the_balancer(monkeypatch):
    api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=2,
                 cell_packs=assigned_profile(1), balancer_profiles=(band_profile(1, "Bal A"),))
    assert api.LAST_RUN["states"][0].status["packs"][0]["balancer"] == {"profile_id": 1, "profile_name": "Bal A", "state": "active"}


def test_the_entity_follows_assignments_in_the_main_loop(monkeypatch):
    client, _ = api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=8,
                             cell_packs=assigned_profile(1), balancer_profiles=(band_profile(1),),
                             cells_after_polls=[(3, (assigned_profile(None), ()))])
    configs = [r for r in client.published if r.topic == TOPIC.format(0)]
    assert isinstance(configs[0].payload, dict) and configs[0].retain
    assert configs[-1].payload == ""                                                      # cleared after the unassignment


# --- Home Assistant -------------------------------------------------------------------------------------------------

def test_discovery_of_the_balancer_entity():
    disc, client = api.discovery()
    disc.publish_pack(0, pack_bus=True)
    before = len(client.published)
    assert "external_balancer" not in json.dumps(client.as_records())                  # not part of a plain pack
    assert disc.publish_balancer(0)
    (record,) = client.published[before:]
    config = record.payload
    assert record.topic == TOPIC.format(0) and record.retain
    path = "value_json.cells.normal.external_balancer"
    assert config["val_tpl"] == f"{{{{ none if {path} == 'unknown' else {path} }}}}"
    assert config["dev_cla"] == "enum" and config["ops"] == ["active", "inactive"] and config["ent_cat"] == "diagnostic"
    assert config["stat_t"] == "seplos/pack-0/sensors" and config["uniq_id"] == "seplos_bms_pack_0_external_balancer"
    assert config["default_entity_id"] == "sensor.seplos_bms_pack_0_external_balancer"
    assert config["dev"]["ids"] == "seplos_bms_pack_0"
    assert "stat_cla" not in config and "unit_of_meas" not in config and "en" not in config


def test_the_template_renders_unknown_as_ha_unknown():
    jinja2 = pytest.importorskip("jinja2")
    disc, client = api.discovery()
    disc.publish_balancer(0)
    template = jinja2.Environment().from_string(client.published[-1].payload["val_tpl"])
    render = lambda state: template.render(value_json={"cells": {"normal": {"external_balancer": state}}})   # noqa: E731
    assert (render("active"), render("inactive"), render("unknown")) == ("active", "inactive", "None")        # "None" = MQTT PAYLOAD_NONE


def inventory(tmp_path) -> list:
    return StateStore(str(tmp_path / "state.json")).get("published_balancers")


def test_a_profile_publishes_the_entity_once_and_unassigning_removes_it(monkeypatch, tmp_path):
    state, _pack_state, client = make_state(monkeypatch, tmp_path, profiles=[profile(1)], assignments={"0": pack(None, 1)})
    api.reconcile_balancers(state)
    api.reconcile_balancers(state)
    assert [r.payload != "" for r in client.published if r.topic == TOPIC.format(0)] == [True]     # not again
    assert state.balancer_inventory == {0} and inventory(tmp_path) == [0]
    state.cell_packs = api.stored_cells({"0": pack(None, None)}, []).packs                            # unassigned
    api.reconcile_balancers(state)
    assert [r.payload for r in client.published if r.topic == TOPIC.format(0)][-1] == ""
    assert state.balancer_inventory == set() and inventory(tmp_path) == []


def test_a_removal_is_retried_until_the_client_accepted_it(monkeypatch, tmp_path):
    state, _pack_state, client = make_state(monkeypatch, tmp_path, profiles=[profile(1)], assignments={"0": pack(None, 1)})
    api.reconcile_balancers(state)
    state.cell_packs = {}
    client.fail_next = 2                                              # the first two removal attempts are refused
    api.reconcile_balancers(state)
    assert state.balancer_inventory == {0} and inventory(tmp_path) == [0] and state.discovery.failed
    api.reconcile_balancers(state)
    assert state.balancer_inventory == {0}
    api.reconcile_balancers(state)                                    # accepted now
    assert state.balancer_inventory == set() and inventory(tmp_path) == []
    assert [r.payload for r in client.published if r.topic == TOPIC.format(0)][-1] == ""


def test_a_restart_still_cleans_up_an_unassignment_that_never_went_out(monkeypatch, tmp_path):
    state, _pack_state, client = make_state(monkeypatch, tmp_path, profiles=[profile(1)], assignments={"0": pack(None, 1)})
    api.reconcile_balancers(state)
    state.cell_packs = {}
    client.fail_next = 5
    api.reconcile_balancers(state)
    assert inventory(tmp_path) == [0]
    # the add-on restarts: no assignment any more, the inventory comes from the state file like in main()
    again, _pack_state, client2 = make_state(monkeypatch, tmp_path)
    again.balancer_inventory = set(again.store.get("published_balancers", []))
    api.reconcile_balancers(again)
    assert [r.payload for r in client2.published if r.topic == TOPIC.format(0)] == [""]
    assert again.balancer_inventory == set() and inventory(tmp_path) == []


def test_a_pack_beyond_the_pack_count_is_cleaned_up(monkeypatch, tmp_path):
    state, _pack_state, client = make_state(monkeypatch, tmp_path)
    state.balancer_inventory = {3}
    api.reconcile_balancers(state)
    assert [r.topic for r in client.published if r.payload == ""] == [TOPIC.format(3)]
    assert state.balancer_inventory == set()


def test_a_publish_that_was_refused_is_retried(monkeypatch, tmp_path):
    state, _pack_state, client = make_state(monkeypatch, tmp_path, profiles=[profile(1)], assignments={"0": pack(None, 1)})
    client.fail_next = 1
    api.reconcile_balancers(state)
    assert state.balancer_published == set() and state.balancer_inventory == {0}
    api.reconcile_balancers(state)
    assert state.balancer_published == {0}
    assert len([r for r in client.published if r.topic == TOPIC.format(0)]) == 1


def test_a_resync_publishes_the_entity_again(monkeypatch, tmp_path):
    state, _pack_state, client = make_state(monkeypatch, tmp_path, profiles=[profile(1)], assignments={"0": pack(None, 1)})
    api.reconcile_balancers(state)
    api.reconcile_balancers(state, force=True)
    assert len([r for r in client.published if r.topic == TOPIC.format(0)]) == 2


def test_nothing_happens_without_discovery_and_the_record_stays(monkeypatch, tmp_path):
    state, _pack_state, client = make_state(monkeypatch, tmp_path, discovery=False)
    state.balancer_inventory = {0}
    api.reconcile_balancers(state)
    assert state.balancer_inventory == {0} and not client.published
