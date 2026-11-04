"""Parallel sets (stage 2a): model, API, live apply in the main loop."""
from __future__ import annotations

import json
import logging

import pytest

import api
from seplos_mqtt import app
from seplos_mqtt.monitoring.model import (
    ConfigValidationError,
    MonitoringConfig,
    ParallelSet,
    set_activity,
)
from seplos_mqtt.monitoring.store import JsonStore
from seplos_mqtt.web.server import WebApp
from test_characterization import _multi_pack_bms
from test_web import LOCAL, USER, csrf_of, make_store, put, run_with

# ---------------------------------------------------------------- model


def _tcp(**kw):
    return {"transport": "tcp", "host": "10.0.0.5", **kw}


def _set(**kw):
    return {"id": 1, "name": "Left", "packs": [0, 1], "wiring": "unknown", **kw}


def _doc(*sets, next_set_id=10, pack_count=4, **kw):
    return {"connection": _tcp(pack_count=pack_count), "parallel_sets": list(sets), "next_set_id": next_set_id, **kw}


def test_valid_sets():
    diagonal = _set(wiring="diagonal", plus=0, minus=1)
    busbar = _set(id=2, name="Right", packs=[2, 3, 4], wiring="common_busbar", plus=None, minus=None)
    unknown = _set(id=3, name="Other", packs=[5, 6])
    config = MonitoringConfig.from_dict(_doc(diagonal, busbar, unknown))
    assert [s.id for s in config.parallel_sets] == [1, 2, 3]
    assert config.parallel_sets[0].packs == (0, 1) and config.parallel_sets[0].plus == 0
    assert config.parallel_sets[1].plus is None and config.parallel_sets[2].minus is None
    assert config.next_set_id == 10
    assert config.to_dict()["parallel_sets"][0] == diagonal


def test_name_is_trimmed():
    config = MonitoringConfig.from_dict(_doc(_set(name="  Left  ")))
    assert config.parallel_sets[0].name == "Left"


def test_stage_1_document_loads_and_is_written_back_unchanged():
    raw = {"connection": _tcp(), "future": {"x": 1}}
    config = MonitoringConfig.from_dict(raw)
    assert config.parallel_sets == () and config.next_set_id == 1
    assert config.to_dict() == {"connection": config.connection.to_dict(), "future": {"x": 1}}


def test_empty_sets_with_a_used_counter_are_kept():
    config = MonitoringConfig.from_dict(_doc(next_set_id=4))
    assert config.to_dict()["parallel_sets"] == [] and config.to_dict()["next_set_id"] == 4


def test_unknown_keys_round_trip():
    raw = _doc(_set(future={"a": [1]}), future_top=True)
    out = MonitoringConfig.from_dict(raw).to_dict()
    assert out["parallel_sets"][0]["future"] == {"a": [1]}
    assert out["future_top"] is True


S = "parallel_sets"


@pytest.mark.parametrize(
    "doc, expected",
    [
        (_doc(_set(packs=[0])), [(f"{S}[0].packs", "range")]),
        (_doc(_set(packs=[0, 16])), [(f"{S}[0].packs[1]", "range")]),
        (_doc(_set(packs=[0, -1])), [(f"{S}[0].packs[1]", "range")]),
        (_doc(_set(packs=list(range(17)))), [(f"{S}[0].packs[16]", "range"), (f"{S}[0].packs", "range")]),
        (_doc(_set(packs=[0, 0])), [(f"{S}[0].packs[1]", "duplicate")]),
        (_doc(_set(packs="01")), [(f"{S}[0].packs", "type")]),
        (_doc(_set(packs=[0, True])), [(f"{S}[0].packs[1]", "type")]),
        (_doc(_set(), _set(id=2, name="Right", packs=[1, 2])), [(f"{S}[1].packs[0]", "duplicate")]),
        (_doc(_set(wiring="diagonal")), [(f"{S}[0].plus", "required"), (f"{S}[0].minus", "required")]),
        (_doc(_set(wiring="diagonal", plus=0)), [(f"{S}[0].minus", "required")]),
        (
            _doc(_set(wiring="diagonal", plus=1, minus=1)),
            [(f"{S}[0].plus", "value"), (f"{S}[0].minus", "value")],
        ),
        (_doc(_set(wiring="common_busbar", plus=5)), [(f"{S}[0].plus", "value")]),
        (_doc(_set(minus=7)), [(f"{S}[0].minus", "value")]),
        (_doc(_set(plus=True)), [(f"{S}[0].plus", "type")]),
        (_doc(_set(plus="0")), [(f"{S}[0].plus", "type")]),
        (_doc(_set(wiring="star")), [(f"{S}[0].wiring", "value")]),
        (_doc(_set(name="   ")), [(f"{S}[0].name", "range")]),
        (_doc(_set(name="x" * 41)), [(f"{S}[0].name", "range")]),
        (_doc(_set(name=5)), [(f"{S}[0].name", "type")]),
        (
            _doc(_set(name="Left"), _set(id=2, name=" LEFT ", packs=[2, 3])),
            [(f"{S}[1].name", "duplicate")],
        ),
        (_doc(_set(id=True)), [(f"{S}[0].id", "type")]),
        (_doc(_set(id=0)), [(f"{S}[0].id", "range")]),
        (_doc(_set(id=10)), [(f"{S}[0].id", "value")]),
        (_doc(_set(id=3), next_set_id=3), [(f"{S}[0].id", "value")]),
        (_doc(_set(), _set(name="Right", packs=[2, 3])), [(f"{S}[1].id", "duplicate")]),
        (_doc(_set(), next_set_id=0), [("next_set_id", "range")]),
        (_doc(_set(), next_set_id=True), [("next_set_id", "type")]),
        ({**_doc(), "parallel_sets": {}}, [(S, "type")]),
        (_doc(5), [(f"{S}[0]", "type")]),
        (_doc({"id": 1}), [(f"{S}[0].name", "required"), (f"{S}[0].packs", "required"), (f"{S}[0].wiring", "required")]),
    ],
)
def test_invalid_sets(doc, expected):
    with pytest.raises(ConfigValidationError) as e:
        MonitoringConfig.from_dict(doc)
    assert e.value.problems == expected


def test_id_without_next_set_id_is_invalid():
    with pytest.raises(ConfigValidationError) as e:
        MonitoringConfig.from_dict({"connection": _tcp(), "parallel_sets": [_set()]})
    assert e.value.problems == [(f"{S}[0].id", "value")]


def test_set_activity():
    sets = (_set(), _set(id=2, name="Right", packs=[2, 3]), _set(id=3, name="Far", packs=[4, 9]))
    two = set_activity(MonitoringConfig.from_dict(_doc(*sets, pack_count=2)))
    assert two == [(True, None), (False, "pack_not_configured"), (False, "pack_not_configured")]
    four = set_activity(MonitoringConfig.from_dict(_doc(*sets, pack_count=4)))
    assert four == [(True, None), (True, None), (False, "pack_not_configured")]
    single = set_activity(MonitoringConfig.from_dict(_doc(*sets, pack_count=1)))
    assert single == [(False, "single_pack")] * 3
    assert set_activity(MonitoringConfig.from_dict({"connection": _tcp()})) == []


# ---------------------------------------------------------------- API


def sets_store(tmp_path, sets=(), next_set_id=None, pack_count=4):
    store = make_store(tmp_path)
    doc = store.data
    doc["connection"] = {**doc["connection"], "pack_count": pack_count}
    if sets or next_set_id:
        doc["parallel_sets"] = list(sets)
        doc["next_set_id"] = next_set_id or max([s["id"] for s in sets], default=0) + 1
    store.commit(doc, store.revision)
    return store


async def put_sets(client, body, revision, *, csrf=True, user=True):
    headers = dict(USER) if user else {}
    if csrf:
        headers["X-CSRF-Token"] = await csrf_of(client)
    headers["If-Match"] = str(revision)
    headers["Content-Type"] = "application/json"
    return await client.put("/api/sets", data=json.dumps(body), headers=headers)


async def error_of(res):
    return (await res.json())["error"]


def test_get_sets_shape(tmp_path):
    store = sets_store(tmp_path, [_set(), _set(id=2, name="Far", packs=[2, 9])])
    app_ = WebApp(LOCAL, store)

    async def scenario(client):
        res = await client.get("/api/sets")
        body = await res.json()
        assert res.status == 200
        assert body["revision"] == store.revision and body["pack_count"] == 4
        assert body["sets"][0] == {**_set(plus=None, minus=None), "active": True, "reason": None}
        assert body["sets"][1]["active"] is False and body["sets"][1]["reason"] == "pack_not_configured"

    run_with(app_, scenario)


def test_get_sets_of_a_stage_1_store_is_empty(tmp_path):
    app_ = WebApp(LOCAL, make_store(tmp_path))

    async def scenario(client):
        body = await (await client.get("/api/sets")).json()
        assert body["sets"] == [] and body["pack_count"] == 1

    run_with(app_, scenario)


def test_get_sets_missing_and_corrupt_store(tmp_path):
    missing = WebApp(LOCAL, JsonStore(tmp_path / "none.json"))

    async def scenario_missing(client):
        res = await client.get("/api/sets")
        assert (res.status, (await error_of(res))["key"]) == (409, "store_missing")

    run_with(missing, scenario_missing)

    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    corrupt = WebApp(LOCAL, JsonStore(tmp_path / "bad.json"))

    async def scenario_corrupt(client):
        res = await client.get("/api/sets")
        assert (res.status, (await error_of(res))["key"]) == (409, "store_corrupt")
        res = await put_sets(client, {"sets": []}, 0)
        assert (res.status, (await error_of(res))["key"]) == (409, "store_corrupt")

    run_with(corrupt, scenario_corrupt)


def test_put_sets_on_a_missing_store_is_refused(tmp_path):
    app_ = WebApp(LOCAL, JsonStore(tmp_path / "none.json"))

    async def scenario(client):
        res = await put_sets(client, {"sets": []}, 0)
        assert (res.status, (await error_of(res))["key"]) == (409, "store_missing")

    run_with(app_, scenario)


def new(name, packs, **kw):
    return {"name": name, "packs": packs, "wiring": "unknown", **kw}


def test_create_rename_delete_and_ids_are_never_reused(tmp_path):
    store = sets_store(tmp_path)
    saved, restarts = [], []
    app_ = WebApp(LOCAL, store, on_connection_saved=restarts.append, on_sets_saved=saved.append)

    async def scenario(client):
        rev = store.revision
        res = await put_sets(client, {"sets": [new("Left", [0, 1]), new("Right", [2, 3])]}, rev)
        body = await res.json()
        assert res.status == 200 and body["revision"] == rev + 1
        assert [s["id"] for s in body["sets"]] == [1, 2]
        assert json.loads((tmp_path / "monitoring.json").read_text())["data"]["next_set_id"] == 3
        assert await (await client.get("/api/sets")).json() == body

        # rename keeps the id; active/reason of the GET view may be sent back and are ignored
        renamed = [{**body["sets"][0], "name": "Links"}, body["sets"][1]]
        res = await put_sets(client, {"sets": renamed}, body["revision"])
        body = await res.json()
        assert res.status == 200 and [(s["id"], s["name"]) for s in body["sets"]] == [(1, "Links"), (2, "Right")]

        # delete set 1, then create: the new set gets 3, never 1 again
        res = await put_sets(client, {"sets": [body["sets"][1]]}, body["revision"])
        body = await res.json()
        assert [s["id"] for s in body["sets"]] == [2]
        res = await put_sets(client, {"sets": [body["sets"][0], new("Again", [0, 1])]}, body["revision"])
        body = await res.json()
        assert res.status == 200 and [s["id"] for s in body["sets"]] == [2, 3]

    run_with(app_, scenario)
    disk = json.loads((tmp_path / "monitoring.json").read_text())["data"]
    assert disk["next_set_id"] == 4
    assert "active" not in disk["parallel_sets"][0] and "reason" not in disk["parallel_sets"][0]
    assert len(saved) == 4 and all(isinstance(c, MonitoringConfig) for c in saved)
    assert [s.id for s in saved[-1].parallel_sets] == [2, 3]
    assert restarts == []                              # a set change never restarts anything


def test_unknown_id_is_rejected(tmp_path):
    store = sets_store(tmp_path, [_set()])
    saved = []
    app_ = WebApp(LOCAL, store, on_sets_saved=saved.append)

    async def scenario(client):
        res = await put_sets(client, {"sets": [_set(id=99)]}, store.revision)
        err = await error_of(res)
        assert (res.status, err["key"]) == (422, "invalid_fields")
        assert err["params"]["fields"] == [{"path": "parallel_sets[0].id", "key": "value"}]
        for bad in (True, [1], "1"):                   # not an int: the model's type problem
            res = await put_sets(client, {"sets": [_set(id=bad)]}, store.revision)
            assert (await error_of(res))["params"]["fields"] == [{"path": "parallel_sets[0].id", "key": "type"}]

    run_with(app_, scenario)
    assert saved == []


def test_422_paths(tmp_path):
    store = sets_store(tmp_path)
    app_ = WebApp(LOCAL, store)

    async def scenario(client):
        body = {"sets": [new("Left", [0, 1]), new("left", [1, 2], wiring="diagonal", plus=9)]}
        res = await put_sets(client, body, store.revision)
        assert res.status == 422
        fields = {(f["path"], f["key"]) for f in (await error_of(res))["params"]["fields"]}
        assert fields == {("parallel_sets[1].plus", "value"), ("parallel_sets[1].minus", "required")}
        # cross-set checks run once every set is valid on its own
        body = {"sets": [new("Left", [0, 1]), new("left", [1, 2])]}
        res = await put_sets(client, body, store.revision)
        fields = {(f["path"], f["key"]) for f in (await error_of(res))["params"]["fields"]}
        assert fields == {("parallel_sets[1].name", "duplicate"), ("parallel_sets[1].packs[0]", "duplicate")}
        res = await put_sets(client, {"sets": [5]}, store.revision)
        assert (await error_of(res))["params"]["fields"] == [{"path": "parallel_sets[0]", "key": "type"}]

    run_with(app_, scenario)


def test_body_shape_errors(tmp_path):
    store = sets_store(tmp_path)
    app_ = WebApp(LOCAL, store)

    async def scenario(client):
        res = await put_sets(client, {"sets": [], "connection": {}}, store.revision)
        err = await error_of(res)
        assert (res.status, err["key"], err["params"]["fields"]) == (400, "unknown_field", ["connection"])
        res = await put_sets(client, {}, store.revision)
        err = await error_of(res)
        assert (res.status, err["key"], err["params"]["fields"]) == (400, "missing_field", ["sets"])
        res = await put_sets(client, {"sets": {}}, store.revision)
        assert (res.status, (await error_of(res))["key"]) == (400, "invalid_request")
        res = await put_sets(client, [], store.revision)
        assert (res.status, (await error_of(res))["key"]) == (400, "invalid_request")

    run_with(app_, scenario)


def test_put_gate(tmp_path):
    store = sets_store(tmp_path)
    saved = []
    app_ = WebApp(LOCAL, store, on_sets_saved=saved.append)

    async def scenario(client):
        res = await put_sets(client, {"sets": []}, store.revision + 5)
        err = await error_of(res)
        assert (res.status, err["key"], err["params"]["current"]) == (412, "revision_stale", store.revision)
        res = await put_sets(client, {"sets": []}, store.revision, csrf=False)
        assert (res.status, (await error_of(res))["key"]) == (403, "csrf_invalid")
        res = await put_sets(client, {"sets": []}, store.revision, user=False, csrf=False)       # read-only (no user)
        assert (res.status, (await error_of(res))["key"]) == (403, "identity_required")
        headers = {**USER, "X-CSRF-Token": await csrf_of(client), "Content-Type": "application/json"}
        res = await client.put("/api/sets", data=json.dumps({"sets": []}), headers=headers)
        assert (res.status, (await error_of(res))["key"]) == (428, "revision_required")

    run_with(app_, scenario)
    assert saved == []


def test_connection_and_sets_saves_keep_each_other(tmp_path):
    store = sets_store(tmp_path, [_set(future={"k": 1})])
    saved_sets, saved_conn = [], []
    app_ = WebApp(LOCAL, store, on_connection_saved=saved_conn.append, on_sets_saved=saved_sets.append)

    async def scenario(client):
        # connection save: the stored sets (and the unknown key of a set) stay as they are
        res = await put(client, {"connection": _tcp(pack_count=2, future_conn=1)}, revision=str(store.revision))
        assert res.status == 200
        rev = (await res.json())["revision"]
        disk = json.loads((tmp_path / "monitoring.json").read_text())["data"]
        assert disk["parallel_sets"] == [_set(plus=None, minus=None, future={"k": 1})]
        assert disk["next_set_id"] == 2
        sets = await (await client.get("/api/sets")).json()
        assert sets["pack_count"] == 2 and sets["sets"][0]["active"] is True

        # sets save: the connection (with its unknown key) stays as it is
        res = await put_sets(client, {"sets": [new("Left", [0, 3], id=1)]}, rev)
        assert res.status == 200
        sets = await res.json()
        assert sets["sets"][0]["active"] is False and sets["sets"][0]["reason"] == "pack_not_configured"
        conn = (await (await client.get("/api/connection")).json())["connection"]
        assert conn["pack_count"] == 2 and conn["future_conn"] == 1

    run_with(app_, scenario)
    assert len(saved_conn) == 1 and len(saved_sets) == 1
    assert saved_conn[0].parallel_sets[0]._extra == {"future": {"k": 1}}
    assert saved_sets[0].connection.pack_count == 2


def test_raising_sets_callback_still_returns_200(tmp_path, caplog):
    store = sets_store(tmp_path)

    def boom(_config):
        raise RuntimeError("nope")

    app_ = WebApp(LOCAL, store, on_sets_saved=boom)

    async def scenario(client):
        res = await put_sets(client, {"sets": [new("Left", [0, 1])]}, store.revision)
        assert res.status == 200

    with caplog.at_level(logging.ERROR, logger="SeplosBMS.web"):
        run_with(app_, scenario)
    assert "sets saved callback failed" in caplog.text


# ---------------------------------------------------------------- live apply


def _sets(*ids):
    return tuple(
        ParallelSet(id=i, name=f"Set {i}", packs=(0, 1), wiring="unknown") for i in ids
    )


def test_state_adopts_pending_sets_only_when_asked(caplog):
    state = app.AppState(config=api.config())
    assert state.parallel_sets == ()
    one = _sets(1)
    state.request_sets(MonitoringConfig.from_dict(_doc(*(s.to_dict() for s in one))))
    assert state.parallel_sets == ()                          # the web thread never changes the active ones
    with caplog.at_level(logging.INFO, logger="SeplosBMS"):
        state.adopt_sets()
        state.adopt_sets()
    assert state.parallel_sets == one
    assert caplog.text.count("Parallel sets updated: 1 set(s)") == 1
    state.request_sets(MonitoringConfig.from_dict({"connection": _tcp(), "next_set_id": 2}))   # all deleted
    state.adopt_sets()
    assert state.parallel_sets == ()


def test_main_loop_adopts_saved_sets_at_the_cycle_boundary(monkeypatch, caplog):
    sets = _sets(1)
    with caplog.at_level(logging.INFO, logger="SeplosBMS"):
        api.run_main(monkeypatch, _multi_pack_bms(), number_of_packs=2, telemetry_polls=8, sets_after_polls=[(2, sets)])
    state = api.LAST_RUN["states"][0]
    assert state.parallel_sets == sets
    assert caplog.text.count("Parallel sets updated: 1 set(s)") == 1


def test_main_loop_without_saved_sets_keeps_none(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger="SeplosBMS"):
        api.run_main(monkeypatch, _multi_pack_bms(), number_of_packs=2, telemetry_polls=6)
    assert api.LAST_RUN["states"][0].parallel_sets == ()
    assert "Parallel sets updated" not in caplog.text


def test_load_sets_at_startup(tmp_path):
    store = sets_store(tmp_path, [_set(), _set(id=2, name="Right", packs=[2, 3])])
    assert [s.id for s in app.load_sets(store)] == [1, 2]
    assert app.load_sets(JsonStore(tmp_path / "none.json")) == ()
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert app.load_sets(JsonStore(tmp_path / "bad.json")) == ()
