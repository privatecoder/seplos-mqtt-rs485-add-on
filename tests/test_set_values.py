"""Parallel sets (stage 2b): values, Home Assistant devices, main loop, status."""
from __future__ import annotations

import pytest

import api
import frames as f
from fakes import FakeBms, FakeMqtt
from seplos_mqtt import app
from seplos_mqtt.monitoring.model import ParallelSet
from seplos_mqtt.sets import KEYS, Member, set_values
from seplos_mqtt.state import StateStore
from test_characterization import _multi_pack_bms, _single_pack_bms

# ---------------------------------------------------------------- pure values


def normal(**kw):
    base = {
        "current": -10.0, "total_pack_voltage": 52.0, "state_of_charge": 50.0, "full_capacity": 100.0,
        "highest_cell_voltage": 3.3, "lowest_cell_voltage": 3.2,
        "highest_cell_temperature": 25.0, "lowest_cell_temperature": 20.0,
    }
    return {**base, **kw}


def two_packs():
    return [
        Member(0, True, 1.0, normal()),
        Member(1, True, 2.5, normal(current=-20.0, total_pack_voltage=54.0, state_of_charge=80.0, full_capacity=300.0,
                                    highest_cell_voltage=3.4, lowest_cell_voltage=3.1,
                                    highest_cell_temperature=30.0, lowest_cell_temperature=15.0)),
    ]


def test_two_pack_example():
    assert set_values(two_packs()) == {
        "current": -30.0,
        "power": -1600.0,                  # 52 * -10 + 54 * -20
        "soc": 72.5,                       # (50 * 100 + 80 * 300) / 400
        "voltage_median": 53.0,
        "voltage_range": 2.0,
        "highest_cell_voltage": 3.4, "highest_cell_voltage_pack": 1,
        "lowest_cell_voltage": 3.1, "lowest_cell_voltage_pack": 1,
        "highest_cell_temperature": 30.0, "highest_cell_temperature_pack": 1,
        "lowest_cell_temperature": 15.0, "lowest_cell_temperature_pack": 1,
        "members_reporting": 2,
        "sample_age": 2.5,
    }
    assert set(set_values(two_packs())) == set(KEYS)


def test_median_of_odd_and_even_counts():
    odd = [*two_packs(), Member(2, True, 0.0, normal(total_pack_voltage=53.5))]
    assert set_values(odd)["voltage_median"] == 53.5
    assert set_values(odd)["voltage_range"] == 2.0
    even = [*odd, Member(3, True, 0.0, normal(total_pack_voltage=60.0))]
    assert set_values(even)["voltage_median"] == 53.75 and set_values(even)["voltage_range"] == 8.0


def test_extrema_name_the_lowest_address_on_a_tie_whatever_the_order():
    a = Member(2, True, 0.0, normal())
    b = Member(5, True, 0.0, normal())
    for members in ([a, b], [b, a]):
        values = set_values(members)
        assert values["highest_cell_voltage_pack"] == values["lowest_cell_voltage_pack"] == 2
        assert values["highest_cell_temperature_pack"] == values["lowest_cell_temperature_pack"] == 2
    better = Member(5, True, 0.0, normal(highest_cell_voltage=3.31, lowest_cell_temperature=19.9))
    values = set_values([a, better])
    assert (values["highest_cell_voltage"], values["highest_cell_voltage_pack"]) == (3.31, 5)
    assert (values["lowest_cell_temperature"], values["lowest_cell_temperature_pack"]) == (19.9, 5)


def test_a_member_that_does_not_report_makes_every_aggregate_unknown():
    members = two_packs()
    members[1] = Member(1, False, None, None)
    values = set_values(members)
    assert values["members_reporting"] == 1 and values["sample_age"] == 1.0
    assert all(v is None for k, v in values.items() if k not in ("members_reporting", "sample_age"))
    nobody = set_values([Member(0, False, None, None), Member(1, False, None, None)])
    assert nobody["members_reporting"] == 0 and nobody["sample_age"] is None


@pytest.mark.parametrize("key", ["current", "total_pack_voltage", "highest_cell_voltage", "lowest_cell_temperature"])
@pytest.mark.parametrize("bad", ["x", None, True, float("nan")])
def test_a_missing_or_non_numeric_input_makes_every_aggregate_unknown(key, bad):
    members = two_packs()
    members[0] = Member(0, True, 1.0, normal(**{key: bad}))
    values = set_values(members)
    assert values["members_reporting"] == 2 and values["sample_age"] == 2.5
    assert all(v is None for k, v in values.items() if k not in ("members_reporting", "sample_age"))


@pytest.mark.parametrize("change", [{"full_capacity": 0}, {"full_capacity": None}, {"state_of_charge": None}])
def test_soc_alone_is_unknown_without_capacity_or_soc(change):
    members = two_packs()
    members[1] = Member(1, True, 2.5, {**members[1].normal, **change})
    values = set_values(members)
    assert values["soc"] is None
    assert values["current"] == -30.0 and values["power"] == -1600.0


# ---------------------------------------------------------------- discovery


def set_item(**kw):
    return ParallelSet(**{"id": 2, "name": "Left", "packs": (0, 1), "wiring": "diagonal", "plus": 0, "minus": 1, **kw})


def test_set_discovery_configs():
    disc, client = api.discovery(invert=True)
    disc.publish_set(set_item())
    records = {r["topic"]: r["payload"] for r in client.as_records()}
    assert len(records) == 15
    assert all(t.startswith("homeassistant/sensor/seplos-mqtt-set-2/") and t.endswith("/config") for t in records)
    first = client.as_records()[0]["payload"]["dev"]
    assert first == {
        "hw": "10E", "sw": "2.x / 16.x", "mdl": "BMS V14 / V16", "mf": "Seplos",
        "name": "Seplos Parallel Set Left", "ids": "seplos_bms_set_2", "via_device": "seplos_bms_pack_0",
    }
    current = records["homeassistant/sensor/seplos-mqtt-set-2/current/config"]
    assert current["uniq_id"] == current["obj_id"] == "seplos_bms_set_2_current"
    assert current["default_entity_id"] == "sensor.seplos_bms_set_2_current"
    assert current["stat_t"] == "seplos/set-2/sensors"
    assert current["avty"] == [{"t": "seplos/availability"}, {"t": "seplos/set-2/availability"}]
    assert current["avty_mode"] == "all"
    assert current["val_tpl"] == "{{ none if value_json.normal.current is none else (value_json.normal.current | float) * -1 }}"
    assert current["dev"] == first                                      # the first config carries the block
    soc = records["homeassistant/sensor/seplos-mqtt-set-2/soc/config"]
    assert soc["dev"] == {"ids": "seplos_bms_set_2"}                   # the others only the id
    assert soc["val_tpl"] == "{{ value_json.normal.soc }}" and soc["dev_cla"] == "battery"
    members = records["homeassistant/sensor/seplos-mqtt-set-2/members_reporting/config"]
    assert members["json_attr_t"] == "seplos/set-2/sensors"
    assert members["json_attr_tpl"] == "{{ value_json.get('set', {}) | tojson }}"
    assert members["ent_cat"] == "diagnostic"


def test_set_discovery_without_the_invert_option_keeps_the_sign():
    disc, client = api.discovery(invert=False)
    disc.publish_set(set_item())
    power = next(r["payload"] for r in client.as_records() if r["topic"].endswith("/power/config"))
    assert power["val_tpl"] == "{{ value_json.normal.power }}"


def test_renaming_a_set_republishes_the_device_block():
    disc, client = api.discovery()
    disc.publish_set(set_item())
    client.published.clear()
    disc.publish_set(set_item(name="Right"))
    records = client.as_records()
    assert records[0]["payload"]["dev"]["name"] == "Seplos Parallel Set Right"
    assert all(r["payload"]["dev"] == {"ids": "seplos_bms_set_2"} for r in records[1:])


def test_remove_set_clears_every_entity():
    disc, client = api.discovery()
    disc.publish_set(set_item())
    published = [r["topic"] for r in client.as_records()]
    client.published.clear()
    disc.remove_set(2)
    cleared = client.as_records()
    assert [r["topic"] for r in cleared] == published
    assert all(r["payload"] == "" and r["retain"] for r in cleared)


# ---------------------------------------------------------------- main loop


def bank() -> FakeBms:
    bms = _multi_pack_bms()
    bms.ascii[(1, 0x42)] = f.telemetry_info(adr=1, current_10ma=-1000, voltage_10mv=5299)
    return bms


def two_sets():
    return (set_item(id=1, name="Left", wiring="common_busbar", plus=None, minus=None),)


def published(client, topic):
    return [p for p in client.published if p.topic == topic]


def run(monkeypatch, bms=None, polls=4, sets=None, **kw):
    handovers = [(0, sets)] if sets is not None else None
    return api.run_main(monkeypatch, bms or bank(), number_of_packs=2, telemetry_polls=polls, sets_after_polls=handovers, **kw)


def test_set_is_published_after_one_cycle(monkeypatch):
    client, _ = run(monkeypatch, sets=two_sets())
    payload = published(client, "seplos/set-1/sensors")[-1].payload
    pack0 = published(client, "seplos/pack-0/sensors")[-1].payload["telemetry"]["normal"]
    assert payload["set"] == {"name": "Left", "packs": [0, 1], "wiring": "common_busbar", "plus": None, "minus": None}
    values = payload["normal"]
    assert values["current"] == -22.34                 # -12.34 A + -10.00 A
    assert values["power"] == -1183.1                  # 52.93 V * -12.34 A + 52.99 V * -10.00 A
    assert values["soc"] == 53.6
    assert values["voltage_median"] == 52.96 and values["voltage_range"] == 0.06
    assert values["members_reporting"] == 2
    for key in ("highest_cell_voltage", "lowest_cell_voltage", "highest_cell_temperature", "lowest_cell_temperature"):
        assert values[key] == pack0[key] and values[f"{key}_pack"] == 0          # identical packs: pack 0 wins
    assert {p.payload for p in published(client, "seplos/set-1/availability")} == {"online"}
    configs = [p for p in client.published if p.topic.startswith("homeassistant/sensor/seplos-mqtt-set-1/")]
    assert len(configs) == 15 and configs[0].payload["dev"]["via_device"] == "seplos_bms_pack_0"
    assert api.LAST_RUN["states"][0].store.get("published_sets") == [1]


def test_a_silent_pack_makes_the_aggregates_unknown(monkeypatch):
    bms = bank()
    bms.ascii[(1, 0x42)] = None
    client, _ = run(monkeypatch, bms, polls=14, sets=two_sets())
    values = published(client, "seplos/set-1/sensors")[-1].payload["normal"]
    assert values["members_reporting"] == 1
    assert all(v is None for k, v in values.items() if k not in ("members_reporting", "sample_age"))


def test_deleting_a_set_clears_its_configs_and_sets_it_offline(monkeypatch):
    client, _ = api.run_main(monkeypatch, bank(), number_of_packs=2, telemetry_polls=8,
                             sets_after_polls=[(0, two_sets()), (4, ())])
    state = api.LAST_RUN["states"][0]
    cleared = [p for p in client.published if p.topic.startswith("homeassistant/sensor/seplos-mqtt-set-1/") and p.payload == ""]
    assert len(cleared) == 15 and all(p.retain for p in cleared)
    availability = published(client, "seplos/set-1/availability")
    assert (availability[-1].payload, availability[-1].retain) == ("offline", True)
    assert state.set_inventory == set() and state.store.get("published_sets") == []
    assert state.published_sets == {} and state.set_values == {}
    assert state.publisher._latest.keys().isdisjoint({"seplos/set-1/sensors", "seplos/set-1/availability"})   # no replay


def test_renaming_a_set_republishes_its_device(monkeypatch):
    renamed = (set_item(id=1, name="Right", wiring="common_busbar", plus=None, minus=None),)
    client, _ = api.run_main(monkeypatch, bank(), number_of_packs=2, telemetry_polls=8,
                             sets_after_polls=[(0, two_sets()), (4, renamed)])
    names = [p.payload["dev"].get("name") for p in client.published
             if p.topic == "homeassistant/sensor/seplos-mqtt-set-1/current/config"]
    assert names == ["Seplos Parallel Set Left", "Seplos Parallel Set Right"]
    assert published(client, "seplos/set-1/sensors")[-1].payload["set"]["name"] == "Right"


def test_inventory_of_a_deleted_set_is_cleaned_up_at_startup(monkeypatch):
    store = StateStore(None)
    store.data["published_sets"] = [7]
    monkeypatch.setattr(app.StateStore, "default", classmethod(lambda cls, path: store))
    client, _ = run(monkeypatch)
    cleared = [p for p in client.published if p.topic.startswith("homeassistant/sensor/seplos-mqtt-set-7/")]
    assert len(cleared) == 15 and all(p.payload == "" for p in cleared)
    assert published(client, "seplos/set-7/availability")[-1].payload == "offline"
    assert store.get("published_sets") == []


def test_single_pack_mode_publishes_no_sets(monkeypatch):
    store = StateStore(None)
    store.data["published_sets"] = [3]
    monkeypatch.setattr(app.StateStore, "default", classmethod(lambda cls, path: store))
    client, _ = api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=3,
                             sets_after_polls=[(0, two_sets())])
    assert not [p for p in client.published if "set-" in p.topic and p.payload != "" and p.payload != "offline"]
    assert not [p for p in client.published if "seplos-mqtt-set-1" in p.topic]
    assert store.get("published_sets") == []
    assert api.LAST_RUN["states"][0].status["sets"][0]["reason"] == "single_pack"


def test_status_lists_sets_with_values_and_reasons(monkeypatch):
    far = set_item(id=2, name="Far", packs=(2, 3), wiring="unknown", plus=None, minus=None)
    run(monkeypatch, sets=(*two_sets(), far))
    sets = api.LAST_RUN["states"][0].status["sets"]
    assert [s["id"] for s in sets] == [1, 2]
    assert sets[0]["active"] is True and sets[0]["reason"] is None
    assert sets[0]["values"]["members_reporting"] == 2 and sets[0]["values"]["current"] == -22.34
    assert {k: sets[0][k] for k in ("name", "packs", "wiring", "plus", "minus")} == {
        "name": "Left", "packs": [0, 1], "wiring": "common_busbar", "plus": None, "minus": None}
    assert sets[1]["active"] is False and sets[1]["reason"] == "pack_not_configured" and sets[1]["values"] is None


def test_status_without_sets_has_an_empty_list(monkeypatch):
    run(monkeypatch)
    assert api.LAST_RUN["states"][0].status["sets"] == []


# ---------------------------------------------------------------- failed cleanup / failed inventory write


def bare_state(path):
    """An AppState with a publisher, discovery and a state file, and no packs (sets only)."""
    with api.mode(2):
        state = app.AppState(config=api.config())
    client = FakeMqtt()
    state.publisher = app.Publisher(client)
    state.discovery = app.Discovery(state.publisher, "seplos", "homeassistant", False)
    state.store = StateStore(str(path))
    return state, client


def test_a_failed_removal_is_retried_each_cycle_on_resync_and_after_a_restart(tmp_path):
    path = tmp_path / "state.json"
    state, client = bare_state(path)
    state.parallel_sets = (set_item(id=1),)
    app.publish_sets(state)
    assert StateStore(str(path)).get("published_sets") == [1]

    state.parallel_sets = ()
    client.fail_next = 16                                 # 15 tombstones and the retained offline
    app.publish_sets(state)
    assert client.fail_next == 0 and state.set_inventory == {1}
    assert StateStore(str(path)).get("published_sets") == [1]          # still on disk: cleanup is pending

    client.published.clear()
    client.fail_next = 3                                  # fails again, only partly
    app.publish_sets(state)
    assert state.set_inventory == {1} and StateStore(str(path)).get("published_sets") == [1]

    client.published.clear()
    app.resync(state)                                     # resync retries too
    cleared = [p for p in client.published if p.topic.startswith("homeassistant/sensor/seplos-mqtt-set-1/")]
    assert len(cleared) == 15 and all(p.payload == "" for p in cleared)
    assert published(client, "seplos/set-1/availability")[-1].payload == "offline"
    assert state.set_inventory == set() and StateStore(str(path)).get("published_sets") == []


def test_a_failed_removal_is_retried_after_a_restart(tmp_path):
    path = tmp_path / "state.json"
    state, client = bare_state(path)
    state.parallel_sets = (set_item(id=1),)
    app.publish_sets(state)
    state.parallel_sets = ()
    client.fail_next = 16
    app.publish_sets(state)

    restarted, client2 = bare_state(path)                 # the set no longer exists in the configuration
    restarted.set_inventory = set(restarted.store.get("published_sets", []))
    assert restarted.set_inventory == {1}
    app.reconcile_sets(restarted)
    assert len([p for p in client2.published if p.payload == "" and "seplos-mqtt-set-1" in p.topic]) == 15
    last = published(client2, "seplos/set-1/availability")[-1]
    assert (last.payload, last.retain) == ("offline", True)
    assert StateStore(str(path)).get("published_sets") == []


def test_a_failed_inventory_write_defers_the_publication_and_is_retried(tmp_path, monkeypatch):
    import os

    real = os.replace
    broken = {"on": True}

    def replace(src, dst):
        if broken["on"]:
            raise OSError("disk full")
        real(src, dst)

    monkeypatch.setattr("seplos_mqtt.state.os.replace", replace)
    path = tmp_path / "state.json"
    state, client = bare_state(path)
    state.parallel_sets = (set_item(id=1),)
    app.publish_sets(state)
    assert client.published == [] and state.published_sets == {}       # nothing of the set went out
    app.publish_sets(state)                                            # still failing: still deferred
    assert client.published == []

    broken["on"] = False
    app.publish_sets(state)                                            # the write is retried although membership is unchanged
    assert StateStore(str(path)).get("published_sets") == [1]
    assert len([p for p in client.published if p.topic.startswith("homeassistant/sensor/seplos-mqtt-set-1/")]) == 15
    assert published(client, "seplos/set-1/sensors")
