"""Cell types, pack assignments, balancer profiles (stage 3a): API and live apply."""
from __future__ import annotations

import copy
import json
import logging
import types

import pytest

import api
from seplos_mqtt import app
from seplos_mqtt.monitoring.model import MonitoringConfig, PackAssignment
from seplos_mqtt.monitoring.store import JsonStore
from seplos_mqtt.web.server import WebApp
from test_cells_model import own_cell, pack, profile, snapshot
from test_characterization import _multi_pack_bms
from test_web import LOCAL, USER, csrf_of, make_store, run_with

DAY = "2026-11-04"          # api.no_sleep fixes the clock


def cells_store(tmp_path, pack_count=2, **sections):
    store = make_store(tmp_path)
    data = store.data
    data["connection"] = {**data["connection"], "pack_count": pack_count}
    data.update(copy.deepcopy(sections))
    store.commit(data, store.revision)
    return store


def disk(tmp_path) -> dict:
    return json.loads((tmp_path / "monitoring.json").read_text())["data"]


async def call(client, method, path, body, revision, *, csrf=True):
    headers = {**USER, "Content-Type": "application/json", "If-Match": str(revision)}
    if csrf:
        headers["X-CSRF-Token"] = await csrf_of(client)
    return await client.request(method, path, data=json.dumps(body), headers=headers)


async def error_of(res):
    return (await res.json())["error"]


def make_app(store, serials=None, **callbacks):
    """A WebApp whose status snapshot reports the given BMS serials {address: serial}."""
    status = {"packs": [{"address": a, "cell_count": 16, "bms_serial": s, "rated_capacity": 280.0 if a == 0 else None}
                        for a, s in (serials or {}).items()]}
    return WebApp(LOCAL, store, status_provider=lambda: status, **callbacks), status


def bundled_id() -> str:
    return api.bundled_cells()[0]["id"]


# ---------------------------------------------------------------- cells


def test_get_cells_lists_bundled_and_own(tmp_path):
    store = cells_store(tmp_path, cell_types=[own_cell()])
    app_, _ = make_app(store)

    async def scenario(client):
        body = await (await client.get("/api/cells")).json()
        assert body["revision"] == store.revision and body["manifest"]["source"] == "openbms-monitor"
        bundled = [c for c in body["cells"] if c["source"] == "bundled"]
        assert len(bundled) == len(api.bundled_cells()) and bundled[0]["conflict"] is None
        assert [c["entry"]["id"] for c in body["cells"] if c["source"] == "own"] == ["my-cell"]

    run_with(app_, scenario)


def test_put_cells_create_edit_delete(tmp_path, monkeypatch):
    api.no_sleep(monkeypatch)
    store = cells_store(tmp_path)
    saved, restarts = [], []
    app_, _ = make_app(store, on_cells_saved=saved.append, on_connection_saved=restarts.append)

    async def scenario(client):
        res = await call(client, "PUT", "/api/cells", {"cells": [own_cell()]}, store.revision)
        body = await res.json()
        assert res.status == 200 and body["cells"][-1]["entry"]["entry_version"] == 1
        # unchanged content: the stored version and date stay, whatever the client sends
        res = await call(client, "PUT", "/api/cells", {"cells": [{**own_cell(), "entry_version": 9, "updated": "2000-01-01"}]},
                         body["revision"])
        body = await res.json()
        assert (body["cells"][-1]["entry"]["entry_version"], body["cells"][-1]["entry"]["updated"]) == (1, "2026-10-01")
        # an edit raises the version and sets the date
        res = await call(client, "PUT", "/api/cells", {"cells": [own_cell(manufacturer="Other")]}, body["revision"])
        body = await res.json()
        assert (body["cells"][-1]["entry"]["entry_version"], body["cells"][-1]["entry"]["updated"]) == (2, DAY)
        res = await call(client, "PUT", "/api/cells", {"cells": []}, body["revision"])
        assert res.status == 200 and [c for c in (await res.json())["cells"] if c["source"] == "own"] == []

    run_with(app_, scenario)
    assert len(saved) == 4 and all(isinstance(c, MonitoringConfig) for c in saved) and restarts == []
    assert "cell_types" not in disk(tmp_path) or disk(tmp_path)["cell_types"] == []


def test_put_cells_errors(tmp_path):
    store = cells_store(tmp_path)
    app_, _ = make_app(store)

    async def scenario(client):
        async def paths(entries):
            res = await call(client, "PUT", "/api/cells", {"cells": entries}, store.revision)
            err = await error_of(res)
            assert (res.status, err["key"]) == (422, "invalid_fields")
            return [(f["path"], f["key"]) for f in err["params"]["fields"]]

        assert await paths([own_cell(bundled_id())]) == [("cell_types[0].id", "duplicate")]
        assert await paths([own_cell(), own_cell()]) == [("cell_types[1].id", "duplicate")]
        assert await paths([{**own_cell(), "foo": 1}]) == [("cell_types[0].foo", "unknown_field")]
        assert await paths([own_cell(capacity={"nominal_ah": 0})]) == [("cell_types[0].capacity.nominal_ah", "range")]
        assert await paths([5]) == [("cell_types[0]", "type")]
        res = await call(client, "PUT", "/api/cells", {"cells": {}}, store.revision)
        assert (res.status, (await error_of(res))["key"]) == (400, "invalid_request")
        res = await call(client, "PUT", "/api/cells", {"packs": []}, store.revision)
        assert (res.status, (await error_of(res))["key"]) == (400, "unknown_field")

    run_with(app_, scenario)


def test_the_shared_gate_applies_to_the_new_routes(tmp_path):
    store = cells_store(tmp_path)
    app_, _ = make_app(store)

    async def scenario(client):
        for method, path, body in (
            ("PUT", "/api/cells", {"cells": []}), ("POST", "/api/cells/rename", {"from": "a", "to": "b"}),
            ("PUT", "/api/packs", {"packs": {}}), ("POST", "/api/packs/accept-update", {"addresses": []}),
            ("POST", "/api/packs/confirm-identity", {"address": 0}), ("PUT", "/api/balancer-profiles", {"profiles": []}),
        ):
            res = await call(client, method, path, body, store.revision, csrf=False)
            assert (res.status, (await error_of(res))["key"]) == (403, "csrf_invalid"), path
            if path in ("/api/cells/rename", "/api/packs/confirm-identity"):
                continue                                   # their bodies name things that do not exist: 422 comes first
            res = await call(client, method, path, body, store.revision + 3)
            assert (res.status, (await error_of(res))["key"]) == (412, "revision_stale"), path

    run_with(app_, scenario)


def test_delete_of_an_assigned_own_type_is_refused_with_addresses(tmp_path):
    packs = {"1": pack(snapshot()), "7": pack(snapshot()), "0": pack(snapshot("other"))}       # 7 is above pack_count
    store = cells_store(tmp_path, cell_types=[own_cell(), own_cell("other"), own_cell("free")], packs=packs)
    saved = []
    app_, _ = make_app(store, on_cells_saved=saved.append)

    async def scenario(client):
        res = await call(client, "PUT", "/api/cells", {"cells": [own_cell("other")]}, store.revision)
        err = await error_of(res)
        assert (res.status, err["key"]) == (409, "in_use")
        assert err["params"] == {"kind": "cell_type", "items": [{"id": "my-cell", "addresses": [1, 7]}]}
        res = await call(client, "PUT", "/api/cells", {"cells": [own_cell("my-cell"), own_cell("other")]}, store.revision)
        assert res.status == 200                              # an unassigned type can go
        assert [c["id"] for c in disk(tmp_path)["cell_types"]] == ["my-cell", "other"]

    run_with(app_, scenario)
    assert len(saved) == 1


# ---------------------------------------------------------------- upgrade collision and rename


def test_upgrade_collision_then_rename(tmp_path, monkeypatch):
    api.no_sleep(monkeypatch)
    catalog_a = api.bundled_cells()
    api.use_bundle(monkeypatch, catalog_a)
    store = cells_store(tmp_path, pack_count=2)
    app_, _ = make_app(store, {0: "S0", 1: "S1"})

    async def scenario(client):
        res = await call(client, "PUT", "/api/cells", {"cells": [own_cell("upgrade-me", capacity={"nominal_ah": 100})]},
                         store.revision)
        rev = (await res.json())["revision"]
        # packs 0 and 5 (inactive: pack_count is 2) use the own type
        res = await call(client, "PUT", "/api/packs", {"packs": {
            "0": {"cell": {"id": "upgrade-me", "parallel": 2}, "balancer_profile": None},
            "5": {"cell": {"id": "upgrade-me", "parallel": 1}, "balancer_profile": None}}}, rev)
        rev = (await res.json())["revision"]

        # catalog B brings an entry with the same id: loading works, the own type is reported as a conflict
        api.use_bundle(monkeypatch, [*catalog_a, {**own_cell("upgrade-me"), "entry_version": 7, "manufacturer": "Bundled"}])
        body = await (await client.get("/api/cells")).json()
        sources = [(c["source"], c["conflict"]) for c in body["cells"] if c["entry"]["id"] == "upgrade-me"]
        assert sources == [("own", "bundled_id")]                                  # the bundled entry is hidden
        packs = {p["address"]: p for p in (await (await client.get("/api/packs")).json())["packs"]}
        assert packs[0]["cell"]["update_available"] is False and packs[0]["cell"]["source"] == "own"
        res = await call(client, "PUT", "/api/cells", {"cells": [own_cell("upgrade-me", capacity={"nominal_ah": 100})]}, rev)
        assert res.status == 200                                                   # a conflicting type may stay as it is
        rev = (await res.json())["revision"]

        for bad, key in (("upgrade-me", "duplicate"), (catalog_a[0]["id"], "duplicate"), ("Bad Id", "format"), (5, "type")):
            res = await call(client, "POST", "/api/cells/rename", {"from": "upgrade-me", "to": bad}, rev)
            assert [(f["path"], f["key"]) for f in (await error_of(res))["params"]["fields"]] == [("to", key)]
        res = await call(client, "POST", "/api/cells/rename", {"from": catalog_a[0]["id"], "to": "x-y"}, rev)
        assert [(f["path"], f["key"]) for f in (await error_of(res))["params"]["fields"]] == [("from", "value")]

        res = await call(client, "POST", "/api/cells/rename", {"from": "upgrade-me", "to": "upgrade-me-custom"}, rev)
        body = await res.json()
        assert res.status == 200
        listed = {(c["source"], c["conflict"], c["entry"]["id"]) for c in body["cells"] if "upgrade" in c["entry"]["id"]}
        assert listed == {("bundled", None, "upgrade-me"), ("own", None, "upgrade-me-custom")}

    run_with(app_, scenario)
    data = disk(tmp_path)
    assert [c["id"] for c in data["cell_types"]] == ["upgrade-me-custom"]
    for address, parallel in (("0", 2), ("5", 1)):                  # references and snapshots follow, also the inactive address
        cell = data["packs"][address]["cell"]
        assert (cell["id"], cell["entry"]["id"], cell["parallel"]) == ("upgrade-me-custom", "upgrade-me-custom", parallel)
        assert (cell["entry_version"], cell["entry"]["entry_version"], cell["entry"]["capacity"]) == (1, 1, {"nominal_ah": 100})


# ---------------------------------------------------------------- packs


def test_get_packs_shape(tmp_path):
    store = cells_store(tmp_path, packs={"0": pack(snapshot("a-cell", 2), serial="S0"), "4": pack(None, None, None)})
    app_, _ = make_app(store, {0: "S0", 1: "X1"})

    async def scenario(client):
        body = await (await client.get("/api/packs")).json()
        assert body["pack_count"] == 2 and [p["address"] for p in body["packs"]] == [0, 1, 4]
        first, second, far = body["packs"]
        assert first["configured"] and first["series_count"] == 16 and first["live_bms_serial"] == "S0"
        assert (first["identity"], first["cell"]["parallel"], first["cell"]["valid"]) == ("verified", 2, True)
        assert first["cell"]["update_available"] is False and first["cell"]["changed"] == []
        assert (second["cell"], second["identity"], second["live_bms_serial"]) == (None, None, "X1")
        assert not far["configured"] and far["series_count"] is None and far["identity"] is None
        assert (first["rated_capacity_ah"], second["rated_capacity_ah"], far["rated_capacity_ah"]) == (280.0, None, None)

    run_with(app_, scenario)


def test_server_rejection_maps_to_a_form_field_of_the_ui(tmp_path):
    """A value the client accepts (a number) but the server rejects: the UI must find the field by the server's path."""
    import shutil
    import subprocess

    from test_web import STATIC_DIR

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    store = cells_store(tmp_path)
    app_ = WebApp(LOCAL, store)
    answer = {}

    async def scenario(client):
        entry = {**own_cell(), "capacity": {"nominal_ah": -1}}
        res = await call(client, "PUT", "/api/cells", {"cells": [entry]}, store.revision)
        answer["status"], answer["body"] = res.status, await res.json()

    run_with(app_, scenario)
    assert answer["status"] == 422 and answer["body"]["error"]["key"] == "invalid_fields"
    fields = answer["body"]["error"]["params"]["fields"]
    script = (
        f"import {{ splitProblemPath }} from {(STATIC_DIR / 'cellui.js').as_uri()!r};"
        f"import {{ FIELDS }} from {(STATIC_DIR / 'cells.js').as_uri()!r};"
        f"const fields = {json.dumps(fields)};"
        "const mapped = fields.map((f) => splitProblemPath(f.path));"
        "const known = mapped.filter((m) => FIELDS.some((f) => f.path === m.field));"
        "if (known.length === 0 || known[0].index !== 0 || known[0].field !== 'capacity.nominal_ah') {"
        " console.log(JSON.stringify(fields), JSON.stringify(mapped)); process.exit(1); }"
    )
    done = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stdout + done.stderr


def test_get_packs_before_the_first_status_and_identity_states(tmp_path):
    packs = {"0": pack(snapshot(), serial=None), "1": pack(snapshot(), serial="S", mismatch=True), "2": pack(None, None, "S")}
    store = cells_store(tmp_path, pack_count=3, packs=packs)
    app_ = WebApp(LOCAL, store)

    async def scenario(client):
        body = await (await client.get("/api/packs")).json()
        assert [p["identity"] for p in body["packs"]] == ["not_verifiable", "mismatch", None]
        assert all(p["series_count"] is None and p["live_bms_serial"] is None and p["rated_capacity_ah"] is None for p in body["packs"])

    run_with(app_, scenario)


def test_put_packs_patch_semantics_and_serials(tmp_path):
    store = cells_store(tmp_path, pack_count=3, packs={"2": pack(snapshot("kept"), 1, "S2", future_pack=1)},
                        **{"balancer_profiles": [profile()], "next_profile_id": 2})
    saved = []
    app_, _ = make_app(store, {0: "S0", 1: None}, on_cells_saved=saved.append)
    bid = bundled_id()

    async def scenario(client):
        res = await call(client, "PUT", "/api/packs", {"packs": {
            "0": {"cell": {"id": bid, "parallel": 3}, "balancer_profile": 1},
            "1": {"cell": None, "balancer_profile": 1}}}, store.revision)
        body = await res.json()
        assert res.status == 200
        by = {p["address"]: p for p in body["packs"]}
        assert (by[0]["cell"]["id"], by[0]["cell"]["parallel"], by[0]["bms_serial"]) == (bid, 3, "S0")
        assert by[0]["cell"]["entry"] == api.bundled_cells()[0] and by[0]["cell"]["entry_version"] == api.bundled_cells()[0]["entry_version"]
        assert (by[1]["cell"], by[1]["balancer_profile"], by[1]["bms_serial"], by[1]["identity"]) == (None, 1, None, "not_verifiable")
        assert by[2]["cell"]["id"] == "kept"                                    # not listed: unchanged
        rev = body["revision"]
        # null clears; an entry that ends up empty goes away, one with unknown keys stays
        res = await call(client, "PUT", "/api/packs", {"packs": {
            "0": {"cell": None, "balancer_profile": None}, "2": {"cell": None, "balancer_profile": None}}}, rev)
        assert res.status == 200

    run_with(app_, scenario)
    stored = disk(tmp_path)["packs"]
    assert "0" not in stored and stored["1"]["balancer_profile"] == 1
    assert stored["2"] == {"cell": None, "balancer_profile": None, "bms_serial": None, "serial_mismatch": False, "future_pack": 1}
    assert len(saved) == 2


def test_put_packs_errors(tmp_path):
    store = cells_store(tmp_path, **{"balancer_profiles": [profile()], "next_profile_id": 2})
    app_, _ = make_app(store)
    good = {"cell": {"id": bundled_id(), "parallel": 1}, "balancer_profile": None}

    async def scenario(client):
        async def paths(packs):
            res = await call(client, "PUT", "/api/packs", {"packs": packs}, store.revision)
            err = await error_of(res)
            assert (res.status, err["key"]) == (422, "invalid_fields")
            return [(f["path"], f["key"]) for f in err["params"]["fields"]]

        assert await paths({"01": good}) == [("packs.01", "value")]
        assert await paths({"16": good}) == [("packs.16", "value")]
        assert await paths({"0": 5}) == [("packs.0", "type")]
        assert await paths({"0": {"cell": None}}) == [("packs.0.balancer_profile", "required")]
        assert await paths({"0": {**good, "bms_serial": "x"}}) == [("packs.0.bms_serial", "unknown_field")]
        assert await paths({"0": {"cell": {"id": "nope", "parallel": 1}, "balancer_profile": None}}) == [("packs.0.cell.id", "value")]
        assert await paths({"0": {"cell": {"id": bundled_id()}, "balancer_profile": None}}) == [("packs.0.cell.parallel", "required")]
        assert await paths({"0": {"cell": {"id": bundled_id(), "parallel": 1, "entry": {}}, "balancer_profile": None}}) == [
            ("packs.0.cell.entry", "unknown_field")]
        assert await paths({"0": {"cell": {"id": bundled_id(), "parallel": 0}, "balancer_profile": None}}) == [("packs.0.cell.parallel", "range")]
        assert await paths({"0": {**good, "balancer_profile": 9}}) == [("packs.0.balancer_profile", "value")]
        assert await paths({"0": {**good, "balancer_profile": "1"}}) == [("packs.0.balancer_profile", "type")]
        res = await call(client, "PUT", "/api/packs", {"packs": []}, store.revision)
        assert (res.status, (await error_of(res))["key"]) == (400, "invalid_request")

    run_with(app_, scenario)


def test_same_id_keeps_the_snapshot_for_bundled_and_own_types(tmp_path, monkeypatch):
    api.no_sleep(monkeypatch)
    cells = api.bundled_cells()
    api.use_bundle(monkeypatch, cells)
    bid = cells[0]["id"]
    store = cells_store(tmp_path, cell_types=[own_cell()])
    app_, _ = make_app(store, {0: "S0", 1: "S1"})

    async def scenario(client):
        res = await call(client, "PUT", "/api/packs", {"packs": {
            "0": {"cell": {"id": bid, "parallel": 1}, "balancer_profile": None},
            "1": {"cell": {"id": "my-cell", "parallel": 1}, "balancer_profile": None}}}, store.revision)
        rev = (await res.json())["revision"]
        # the bundle moves on (version 3, other capacity), the own type is edited (version 2)
        newer = {**cells[0], "entry_version": 3, "capacity": {"nominal_ah": 111}}
        api.use_bundle(monkeypatch, [newer, *cells[1:]])
        res = await call(client, "PUT", "/api/cells", {"cells": [own_cell(capacity={"nominal_ah": 222})]}, rev)
        rev = (await res.json())["revision"]

        # only `parallel` (and the profile) change: the snapshots stay
        res = await call(client, "PUT", "/api/packs", {"packs": {
            "0": {"cell": {"id": bid, "parallel": 2}, "balancer_profile": None},
            "1": {"cell": {"id": "my-cell", "parallel": 4}, "balancer_profile": None}}}, rev)
        body = await res.json()
        by = {p["address"]: p["cell"] for p in body["packs"]}
        assert (by[0]["parallel"], by[0]["entry_version"], by[0]["entry"]["capacity"]) == (2, cells[0]["entry_version"], cells[0]["capacity"])
        assert (by[1]["parallel"], by[1]["entry_version"], by[1]["entry"]["capacity"]) == (4, 1, cells[0]["capacity"])
        assert by[0]["update_available"] and "capacity.nominal_ah" in by[0]["changed"] and "entry_version" in by[0]["changed"]
        assert by[1]["update_available"] and sorted(by[1]["changed"]) == ["capacity.nominal_ah", "entry_version", "updated"]
        rev = body["revision"]

        # nothing to accept for an unknown address or a bad type
        res = await call(client, "POST", "/api/packs/accept-update", {"addresses": [5, "x"]}, rev)
        assert [(f["path"], f["key"]) for f in (await error_of(res))["params"]["fields"]] == [
            ("addresses[0]", "value"), ("addresses[1]", "type")]

    run_with(app_, scenario)


def test_accept_update_replaces_the_snapshot_and_keeps_parallel(tmp_path, monkeypatch):
    api.no_sleep(monkeypatch)
    cells = api.bundled_cells()
    api.use_bundle(monkeypatch, cells)
    bid = cells[0]["id"]
    store = cells_store(tmp_path, packs={"0": pack({"id": bid, "entry_version": cells[0]["entry_version"], "parallel": 2,
                                                  "entry": copy.deepcopy(cells[0])}),
                                         "1": pack(snapshot("kept"))})
    app_, _ = make_app(store)
    saved = []
    app_._on_cells_saved = saved.append

    async def scenario(client):
        newer = {**cells[0], "entry_version": cells[0]["entry_version"] + 1, "model": "New model"}
        api.use_bundle(monkeypatch, [newer, *cells[1:]])
        res = await call(client, "POST", "/api/packs/accept-update", {"addresses": [1]}, store.revision)
        assert res.status == 422                                   # pack 1 has no update (own type, unchanged)
        res = await call(client, "POST", "/api/packs/accept-update", {"addresses": [0]}, store.revision)
        body = await res.json()
        cell = body["packs"][0]["cell"]
        assert res.status == 200 and cell["entry"]["model"] == "New model" and cell["parallel"] == 2
        assert cell["entry_version"] == newer["entry_version"] and cell["update_available"] is False

    run_with(app_, scenario)
    assert len(saved) == 1


def test_changing_the_cell_id_makes_a_new_snapshot_and_takes_the_serial(tmp_path):
    other = api.bundled_cells()[1]["id"]
    store = cells_store(tmp_path, packs={"0": pack(snapshot("a-cell"), serial="OLD", mismatch=True)})
    app_, _ = make_app(store, {0: "NEW"})

    async def scenario(client):
        # the same id keeps serial and mismatch, a new id is a re-assignment
        res = await call(client, "PUT", "/api/packs", {"packs": {"0": {"cell": {"id": "a-cell", "parallel": 2}, "balancer_profile": None}}},
                         store.revision)
        body = await res.json()
        first = body["packs"][0]
        assert res.status == 200                                    # the valid snapshot is kept, a-cell need not be in the catalog
        assert (first["bms_serial"], first["serial_mismatch"], first["cell"]["parallel"]) == ("OLD", True, 2)
        res = await call(client, "PUT", "/api/packs", {"packs": {"0": {"cell": {"id": other, "parallel": 1}, "balancer_profile": None}}},
                         body["revision"])
        body = await res.json()
        first = body["packs"][0]
        assert (first["cell"]["id"], first["bms_serial"], first["serial_mismatch"], first["identity"]) == (other, "NEW", False, "verified")

    run_with(app_, scenario)


def test_profile_change_alone_keeps_serial_and_mismatch(tmp_path):
    bid = bundled_id()
    entry = api.bundled_cells()[0]
    ref = {"id": bid, "entry_version": entry["entry_version"], "parallel": 1, "entry": entry}
    store = cells_store(tmp_path, packs={"0": pack(ref, serial="OLD", mismatch=True)},
                        **{"balancer_profiles": [profile()], "next_profile_id": 2})
    app_, _ = make_app(store, {0: "NEW"})

    async def scenario(client):
        res = await call(client, "PUT", "/api/packs", {"packs": {"0": {"cell": {"id": bid, "parallel": 1}, "balancer_profile": 1}}},
                         store.revision)
        first = (await res.json())["packs"][0]
        assert (first["bms_serial"], first["serial_mismatch"], first["balancer_profile"]) == ("OLD", True, 1)

    run_with(app_, scenario)


def test_confirm_identity(tmp_path):
    store = cells_store(tmp_path, packs={"0": pack(snapshot(), serial="OLD", mismatch=True), "1": pack(None, None, None)})
    app_, status = make_app(store, {0: None, 1: "S1"})
    saved = []
    app_._on_cells_saved = saved.append

    async def scenario(client):
        res = await call(client, "POST", "/api/packs/confirm-identity", {"address": 0}, store.revision)
        assert (res.status, (await error_of(res))["key"]) == (409, "serial_unavailable")      # unreadable serial
        res = await call(client, "POST", "/api/packs/confirm-identity", {"address": 1}, store.revision)
        assert [(f["path"], f["key"]) for f in (await error_of(res))["params"]["fields"]] == [("address", "value")]
        res = await call(client, "POST", "/api/packs/confirm-identity", {"address": "0"}, store.revision)
        assert [(f["path"], f["key"]) for f in (await error_of(res))["params"]["fields"]] == [("address", "type")]
        res = await call(client, "POST", "/api/packs/confirm-identity", {"address": 0, "x": 1}, store.revision)
        assert (res.status, (await error_of(res))["key"]) == (400, "unknown_field")
        status["packs"][0]["bms_serial"] = "NEW"
        res = await call(client, "POST", "/api/packs/confirm-identity", {"address": 0}, store.revision)
        first = (await res.json())["packs"][0]
        assert res.status == 200 and (first["bms_serial"], first["serial_mismatch"], first["identity"]) == ("NEW", False, "verified")

    run_with(app_, scenario)
    assert len(saved) == 1


# ---------------------------------------------------------------- balancer profiles


def test_profiles_create_edit_delete_and_ids_are_never_reused(tmp_path):
    store = cells_store(tmp_path)
    saved, restarts = [], []
    app_, _ = make_app(store, on_cells_saved=saved.append, on_connection_saved=restarts.append)

    def new(name, **values):
        return {"name": name, "values": profile(**values)["values"]}

    async def scenario(client):
        res = await call(client, "PUT", "/api/balancer-profiles", {"profiles": [new("A"), new("B")]}, store.revision)
        body = await res.json()
        assert res.status == 200 and [p["id"] for p in body["profiles"]] == [1, 2]
        assert await (await client.get("/api/balancer-profiles")).json() == body
        edited = [{**body["profiles"][1], "name": "B2"}]
        res = await call(client, "PUT", "/api/balancer-profiles", {"profiles": edited}, body["revision"])
        body = await res.json()
        assert [(p["id"], p["name"]) for p in body["profiles"]] == [(2, "B2")]
        res = await call(client, "PUT", "/api/balancer-profiles", {"profiles": [*body["profiles"], new("C")]}, body["revision"])
        assert [p["id"] for p in (await res.json())["profiles"]] == [2, 3]            # 1 is never reused

    run_with(app_, scenario)
    assert disk(tmp_path)["next_profile_id"] == 4 and restarts == [] and len(saved) == 3


def test_profile_errors(tmp_path):
    store = cells_store(tmp_path, **{"balancer_profiles": [profile()], "next_profile_id": 2})
    app_, _ = make_app(store)
    P = "balancer_profiles"

    async def scenario(client):
        async def paths(profiles):
            res = await call(client, "PUT", "/api/balancer-profiles", {"profiles": profiles}, store.revision)
            err = await error_of(res)
            assert (res.status, err["key"]) == (422, "invalid_fields")
            return [(f["path"], f["key"]) for f in err["params"]["fields"]]

        assert await paths([profile(9)]) == [(f"{P}[0].id", "value")]                 # an id that is not stored
        assert await paths([profile(1), {"name": "b", "values": profile()["values"]}, {"name": "B", "values": profile()["values"]}]) == [
            (f"{P}[2].name", "duplicate")]
        assert await paths([{"name": "x", "values": profile(trigger_mv=0)["values"]}]) == [(f"{P}[0].values.trigger_mv", "range")]
        assert await paths([{"name": "x", "values": profile(when="x")["values"]}]) == [(f"{P}[0].values.when", "value")]
        assert await paths([5]) == [(f"{P}[0]", "type")]

    run_with(app_, scenario)


def test_deleting_an_assigned_profile_is_refused_with_addresses(tmp_path):
    packs = {"0": pack(None, 1), "9": pack(None, 1), "1": pack(None, 2)}
    store = cells_store(tmp_path, packs=packs, **{"balancer_profiles": [profile(1, "A"), profile(2, "B"), profile(3, "C")],
                                                  "next_profile_id": 4})
    app_, _ = make_app(store)

    async def scenario(client):
        res = await call(client, "PUT", "/api/balancer-profiles", {"profiles": [profile(2, "B")]}, store.revision)
        err = await error_of(res)
        assert (res.status, err["key"]) == (409, "in_use")
        assert err["params"] == {"kind": "balancer_profile", "items": [{"id": 1, "addresses": [0, 9]}]}
        res = await call(client, "PUT", "/api/balancer-profiles", {"profiles": [profile(1, "A"), profile(2, "B")]}, store.revision)
        assert res.status == 200                                    # the unassigned profile 3 can go

    run_with(app_, scenario)


# ---------------------------------------------------------------- live apply and identity guard


def _guard(tmp_path, stored_serial="S1", mismatch=False, assigned=True):
    cell = snapshot() if assigned else None
    store = cells_store(tmp_path, packs={"0": pack(cell, None, stored_serial, mismatch)})
    state = app.AppState(config=api.config())
    state.monitoring_store = store
    state.cell_packs = dict(MonitoringConfig.from_dict(store.snapshot()[3]).packs)
    state.adopted_packs = dict(state.cell_packs)
    pack_state = types.SimpleNamespace(address=0, pack=types.SimpleNamespace(info=None), last_data=None)

    def read(serial):
        pack_state.last_data = {"info": {"normal": {"bms_serial_number": serial}}}
        api.check_identity(state, pack_state)
    return store, state, read


def test_identity_guard_transitions(tmp_path, caplog):
    store, state, read = _guard(tmp_path)
    revision = store.revision
    read("S1")
    read(" S1 ")                                                    # trimmed
    assert store.revision == revision and not state.cell_packs[0].serial_mismatch
    with caplog.at_level(logging.WARNING, logger="SeplosBMS"):
        read("OTHER")
    assert state.cell_packs[0].serial_mismatch and store.revision == revision + 1
    assert store.snapshot()[3]["packs"]["0"]["serial_mismatch"] is True
    assert store.snapshot()[3]["packs"]["0"]["bms_serial"] == "S1"            # the stored serial is never replaced here
    assert "differs from the stored one" in caplog.text
    read("OTHER")
    read("OTHER")
    assert store.revision == revision + 1                           # one write per change
    for unreadable in (None, "", "   "):
        read(unreadable)
        assert state.cell_packs[0].serial_mismatch and store.revision == revision + 1
    read("S1")                                                      # the stored serial is back
    assert not state.cell_packs[0].serial_mismatch and store.revision == revision + 2
    assert store.snapshot()[3]["packs"]["0"]["serial_mismatch"] is False


def test_unreadable_serial_with_no_data_changes_nothing(tmp_path):
    store, state, _ = _guard(tmp_path)
    pack_state = types.SimpleNamespace(address=0, pack=types.SimpleNamespace(info=None), last_data=None)
    revision = store.revision
    api.check_identity(state, pack_state)
    pack_state.pack.info = {"normal": {"bms_serial_number": "OTHER"}}            # the cached device info is used too
    api.check_identity(state, pack_state)
    assert state.cell_packs[0].serial_mismatch and store.revision == revision + 1


def test_identity_guard_learns_a_missing_serial_and_skips_unassigned_packs(tmp_path):
    store, state, read = _guard(tmp_path, stored_serial=None)
    read("FIRST")
    assert (state.cell_packs[0].bms_serial, state.cell_packs[0].serial_mismatch) == ("FIRST", False)
    assert store.snapshot()[3]["packs"]["0"]["bms_serial"] == "FIRST"
    read("SECOND")
    assert state.cell_packs[0].serial_mismatch
    store, state, read = _guard(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path, assigned=False)
    revision = store.revision
    read("WHATEVER")
    assert store.revision == revision and not state.cell_packs[0].serial_mismatch


def test_identity_guard_writes_nothing_over_a_newer_save(tmp_path):
    store, state, read = _guard(tmp_path)
    doc = store.snapshot()[3]
    doc["packs"]["0"]["bms_serial"] = "CONFIRMED"                   # the user confirmed meanwhile, not adopted yet
    store.commit(doc, store.revision)
    revision = store.revision
    read("OTHER")
    assert store.revision == revision and store.snapshot()[3]["packs"]["0"]["serial_mismatch"] is False
    assert state.cell_packs[0].serial_mismatch                      # the running guard stays conservative
    assert 0 not in state.identity_unsaved                          # nothing to retry: the save wins at adoption


def test_state_adopts_cells_only_when_asked(caplog):
    state = app.AppState(config=api.config())
    config = MonitoringConfig.from_dict({
        "connection": {"transport": "tcp", "host": "h"}, "packs": {"0": pack(None, 1)},
        "balancer_profiles": [profile()], "next_profile_id": 2,
    })
    state.request_cells(config)
    assert state.cell_packs == {} and state.balancer_profiles == ()          # the web thread never changes the active ones
    with caplog.at_level(logging.INFO, logger="SeplosBMS"):
        assert state.adopt_cells() is True
        assert state.adopt_cells() is False
    assert state.cell_packs == config.packs and state.balancer_profiles == config.balancer_profiles
    assert caplog.text.count("Cell assignments updated: 1 pack(s), 1 balancer profile(s)") == 1
    state.request_cells(MonitoringConfig.from_dict({"connection": {"transport": "tcp", "host": "h"}}))
    assert state.adopt_cells() and state.cell_packs == {}


def test_main_loop_adopts_saved_cells_at_the_cycle_boundary(monkeypatch, caplog):
    packs = {0: PackAssignment(0, balancer_profile=None, bms_serial="S0")}
    with caplog.at_level(logging.INFO, logger="SeplosBMS"):
        api.run_main(monkeypatch, _multi_pack_bms(), number_of_packs=2, telemetry_polls=8, cells_after_polls=[(2, (packs, ()))])
    state = api.LAST_RUN["states"][0]
    assert state.cell_packs == packs and not api.LAST_RUN["execs"]            # no restart
    assert caplog.text.count("Cell assignments updated: 1 pack(s), 0 balancer profile(s)") == 1


def test_main_loop_without_saved_cells_keeps_none(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger="SeplosBMS"):
        api.run_main(monkeypatch, _multi_pack_bms(), number_of_packs=2, telemetry_polls=6)
    assert api.LAST_RUN["states"][0].cell_packs == {}
    assert "Cell assignments updated" not in caplog.text


def test_load_cells_at_startup(tmp_path):
    store = cells_store(tmp_path, packs={"3": pack(snapshot())}, **{"balancer_profiles": [profile()], "next_profile_id": 2})
    packs, profiles = app.load_cells(store)
    assert list(packs) == [3] and [p.id for p in profiles] == [1]
    assert app.load_cells(JsonStore(tmp_path / "none.json")) == ({}, ())


# ---------------------------------------------------------------- review fixes


def _rewrite(store, change):
    doc = store.snapshot()[3]
    change(doc)
    store.commit(doc, store.revision)


def _stored(store) -> dict:
    return store.snapshot()[3]["packs"]["0"]


def test_guard_result_survives_a_queued_unrelated_save(tmp_path):
    store, state, read = _guard(tmp_path)
    _rewrite(store, lambda doc: doc.update(cell_types=[own_cell()]))               # an unrelated save ...
    state.request_cells(MonitoringConfig.from_dict(store.snapshot()[3]))           # ... queued with the old identity
    read("NEW")                                                                    # the guard finds a different pack first
    assert state.cell_packs[0].serial_mismatch and _stored(store)["serial_mismatch"] is True
    assert state.adopt_cells()
    read("NEW")
    assert state.cell_packs[0].serial_mismatch and state.cell_packs[0].bms_serial == "S1"
    assert _stored(store)["serial_mismatch"] is True


def test_identity_write_failure_keeps_the_running_guard_and_is_retried(tmp_path, monkeypatch):
    store, state, read = _guard(tmp_path)
    commit = store.commit

    def broken(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(store, "commit", broken)
    read("NEW")
    assert state.cell_packs[0].serial_mismatch and 0 in state.identity_unsaved       # running state is safe
    assert _stored(store)["serial_mismatch"] is False
    monkeypatch.setattr(store, "commit", commit)
    read("NEW")                                                                      # nothing changed, but the write is retried
    assert _stored(store)["serial_mismatch"] is True and 0 not in state.identity_unsaved
    revision = store.revision
    read("NEW")
    assert store.revision == revision


def test_confirm_identity_after_a_mismatch_clears_running_and_stored_state(tmp_path):
    store, state, read = _guard(tmp_path)
    read("NEW")
    assert state.cell_packs[0].serial_mismatch and _stored(store)["serial_mismatch"] is True

    def confirm(doc):
        doc["packs"]["0"].update(bms_serial="NEW", serial_mismatch=False)
    _rewrite(store, confirm)
    state.request_cells(MonitoringConfig.from_dict(store.snapshot()[3]))
    assert state.adopt_cells()
    revision = store.revision
    read("NEW")
    assert (state.cell_packs[0].bms_serial, state.cell_packs[0].serial_mismatch) == ("NEW", False)
    assert (_stored(store)["bms_serial"], _stored(store)["serial_mismatch"]) == ("NEW", False)
    assert store.revision == revision


def test_same_id_edits_do_not_need_the_catalog_entry(tmp_path, monkeypatch):
    cells = api.bundled_cells()
    api.use_bundle(monkeypatch, cells)
    bid = cells[0]["id"]
    store = cells_store(tmp_path, **{"balancer_profiles": [profile()], "next_profile_id": 2})
    app_, _ = make_app(store, {0: "S0"})

    async def scenario(client):
        res = await call(client, "PUT", "/api/packs", {"packs": {"0": {"cell": {"id": bid, "parallel": 1}, "balancer_profile": None}}},
                         store.revision)
        rev = (await res.json())["revision"]
        api.use_bundle(monkeypatch, cells[1:])                                      # catalog B no longer has the type
        first = (await (await client.get("/api/packs")).json())["packs"][0]
        assert first["cell"]["valid"] is True and first["cell"]["source"] is None
        res = await call(client, "PUT", "/api/packs", {"packs": {"0": {"cell": {"id": bid, "parallel": 2}, "balancer_profile": None}}}, rev)
        body = await res.json()
        assert res.status == 200 and body["packs"][0]["cell"]["parallel"] == 2
        res = await call(client, "PUT", "/api/packs", {"packs": {"0": {"cell": {"id": bid, "parallel": 2}, "balancer_profile": 1}}},
                         body["revision"])
        first = (await res.json())["packs"][0]
        assert res.status == 200 and first["balancer_profile"] == 1 and first["cell"]["entry"] == cells[0]
        # a new or changed id is still resolved against the catalog
        res = await call(client, "PUT", "/api/packs", {"packs": {"1": {"cell": {"id": bid, "parallel": 1}, "balancer_profile": None}}},
                         (await (await client.get("/api/packs")).json())["revision"])
        assert [(f["path"], f["key"]) for f in (await error_of(res))["params"]["fields"]] == [("packs.1.cell.id", "value")]

    run_with(app_, scenario)


def test_an_integral_float_entry_version_is_stored_as_an_int(tmp_path, monkeypatch):
    api.no_sleep(monkeypatch)
    store = cells_store(tmp_path)
    app_, _ = make_app(store, {0: "S0"})
    entry = own_cell()
    entry["entry_version"] = 2.0

    async def scenario(client):
        res = await call(client, "PUT", "/api/cells", {"cells": [entry]}, store.revision)
        body = await res.json()
        assert res.status == 200 and type(body["cells"][-1]["entry"]["entry_version"]) is int
        res = await call(client, "PUT", "/api/packs", {"packs": {"0": {"cell": {"id": "my-cell", "parallel": 1}, "balancer_profile": None}}},
                         body["revision"])
        body = await res.json()
        assert res.status == 200 and body["packs"][0]["cell"]["entry_version"] == 2
        res = await call(client, "PUT", "/api/cells", {"cells": [{**entry, "manufacturer": "Other"}]}, body["revision"])
        body = await res.json()
        assert body["cells"][-1]["entry"]["entry_version"] == 3
        offer = (await (await client.get("/api/packs")).json())["packs"][0]["cell"]
        assert offer["update_available"] is True and offer["entry_version"] == 2

    run_with(app_, scenario)
    stored = disk(tmp_path)
    assert type(stored["cell_types"][0]["entry_version"]) is int and type(stored["packs"]["0"]["cell"]["entry"]["schema_version"]) is int


def test_accept_update_with_a_raw_stored_float_version(tmp_path):
    own = own_cell()
    own["entry_version"] = 2.0                                      # as an earlier version of the add-on could have stored it
    store = cells_store(tmp_path, cell_types=[own], packs={"0": pack(snapshot())})
    assert store.snapshot()[3]["cell_types"][0]["entry_version"] == 2.0 and isinstance(
        store.snapshot()[3]["cell_types"][0]["entry_version"], float)
    app_, _ = make_app(store)

    async def scenario(client):
        offer = (await (await client.get("/api/packs")).json())["packs"][0]["cell"]
        assert offer["update_available"] is True
        res = await call(client, "POST", "/api/packs/accept-update", {"addresses": [0]}, store.revision)
        body = await res.json()
        assert res.status == 200
        assert body["packs"][0]["cell"]["entry_version"] == 2 and body["packs"][0]["cell"]["update_available"] is False

    run_with(app_, scenario)
    cell = disk(tmp_path)["packs"]["0"]["cell"]
    assert type(cell["entry_version"]) is int and type(cell["entry"]["entry_version"]) is int


@pytest.mark.parametrize("bad_id", [[], {}, [1], {"a": 1}])
def test_unhashable_ids_are_a_normal_validation_error(tmp_path, bad_id):
    store = cells_store(tmp_path, cell_types=[own_cell()], **{"balancer_profiles": [profile()], "next_profile_id": 2})
    app_, _ = make_app(store)

    async def scenario(client):
        res = await call(client, "PUT", "/api/cells", {"cells": [{**own_cell(), "id": bad_id}]}, store.revision)
        err = await error_of(res)
        assert (res.status, err["key"]) == (422, "invalid_fields")
        assert {"path": "cell_types[0].id", "key": "type"} in err["params"]["fields"]
        res = await call(client, "PUT", "/api/balancer-profiles", {"profiles": [{**profile(), "id": bad_id}]}, store.revision)
        err = await error_of(res)
        assert (res.status, err["key"]) == (422, "invalid_fields")
        assert {"path": "balancer_profiles[0].id", "key": "type"} in err["params"]["fields"]

    run_with(app_, scenario)


def test_huge_integers_are_a_range_problem_not_a_crash(tmp_path):
    huge = own_cell(capacity={"nominal_ah": 10**500})
    store = cells_store(tmp_path)
    app_, _ = make_app(store)

    async def scenario(client):
        res = await call(client, "PUT", "/api/cells", {"cells": [huge]}, store.revision)
        err = await error_of(res)
        assert (res.status, err["key"]) == (422, "invalid_fields")
        assert err["params"]["fields"] == [{"path": "cell_types[0].capacity.nominal_ah", "key": "range"}]

    run_with(app_, scenario)


def test_parallel_has_no_small_ceiling(tmp_path):
    store = cells_store(tmp_path)
    app_, _ = make_app(store)

    async def scenario(client):
        for parallel, status in ((65, 200), (10_000, 200), (10_001, 422)):
            res = await call(client, "PUT", "/api/packs", {"packs": {"0": {"cell": {"id": bundled_id(), "parallel": parallel},
                                                                          "balancer_profile": None}}},
                             (await (await client.get("/api/packs")).json())["revision"])
            assert res.status == status, parallel

    run_with(app_, scenario)
