"""Status snapshot: build_status, its production by the main loop, GET /api/status."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import tempfile
import types
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

import api
import frames as f
from fakes import FakeBms
from seplos_mqtt import app
from seplos_mqtt.monitoring.migration import load_or_migrate
from seplos_mqtt.pack import BatteryPack
from seplos_mqtt.status import build_status, with_pending
from seplos_mqtt.web.server import WebApp, WebSettings

PACK_KEYS = {
    "address", "availability", "last_success_age_s", "device_name", "firmware_version", "firmware_patch", "cell_count",
    "bms_serial", "maint", "maint_version", "state_of_charge", "total_pack_voltage", "current", "highest_cell_voltage", "lowest_cell_voltage",
    "rated_capacity", "cells", "balancer",
}
SYSTEM_KEYS = {"system_soc", "system_voltage", "system_current", "system_power", "total_capacity", "total_full_energy", "system_state"}


def single_bms() -> FakeBms:
    bms = FakeBms()
    bms.ascii[(0, 0x42)] = f.telemetry_info(adr=0)
    bms.ascii[(0, 0x44)] = f.telesignal_info(adr=0)
    bms.ascii[(0, 0x51)] = f.device_info_info()
    return bms


def multi_bms() -> FakeBms:
    bms = FakeBms()
    for adr in (0, 1):
        bms.ascii[(adr, 0x42)] = f.telemetry_info(adr=adr)
        bms.ascii[(adr, 0x44)] = f.telesignal_info(adr=adr)
        bms.ascii[(adr, 0x51)] = f.device_info_info()
    bms.ascii[(0x02, 0x60)] = ("rtn", 0x00)
    bms.modbus[(0x03, 0x001F, 9)] = [0] * 9
    bms.modbus[(0x03, 0x1000, 21)] = f.system_registers()
    bms.modbus[(0x03, 0x0074, 1)] = ("raw", f.modbus_wide_count_response(1, 3, 0xC0))
    return bms


def fresh_state(packs: int) -> app.AppState:
    cfg = dataclasses.replace(api.config(), number_of_packs=packs)
    state = app.AppState(config=cfg)
    state.packs = [app.PackState(BatteryPack(i, None, cfg), i) for i in range(packs)]
    return state


def test_saved_assignment_is_pending_until_the_cycle_boundary_adopts_it():
    from test_cell_checks import assigned
    state = fresh_state(2)

    def served():
        return with_pending(state, build_status(state, 100.0, "t"))["packs"]

    before = served()
    assert [p["cells"]["pending"] for p in before] == [False, False]
    assert before[0]["cells"]["live"]["reason"] == "no_assignment"

    new = assigned(address=0)
    state.request_cells(types.SimpleNamespace(packs=new, balancer_profiles=(), cell_types=()))
    saved = served()
    assert [p["cells"]["pending"] for p in saved] == [True, False]       # only the pack whose assignment changed
    assert saved[0]["cells"]["live"]["reason"] == saved[0]["cells"]["parameters"]["reason"] == "pending"
    assert saved[1]["cells"]["live"]["reason"] == "no_assignment"

    assert state.adopt_cells()
    adopted = served()
    assert adopted[0]["cells"]["pending"] is False
    assert adopted[0]["cells"]["live"]["reason"] != "pending"            # now really evaluated (no sample yet)
    assert adopted[0]["cells"]["assignment"]["id"] == new[0].cell.id


def test_a_save_that_changes_nothing_for_a_pack_is_not_pending():
    from test_cell_checks import assigned
    state = fresh_state(1)
    new = assigned(address=0)
    state.request_cells(types.SimpleNamespace(packs=new, balancer_profiles=(), cell_types=()))
    assert state.adopt_cells()
    state.request_cells(types.SimpleNamespace(packs=new, balancer_profiles=(), cell_types=()))   # e.g. a profile edit
    assert with_pending(state, build_status(state, 100.0, "t"))["packs"][0]["cells"]["pending"] is False
    assert with_pending(state, None) is None


# ---------------------------------------------------------------- build_status


def test_before_the_first_poll_everything_is_unknown():
    status = build_status(fresh_state(1), 100.0, "2026-11-04T12:00:00+01:00")
    assert status["generated_at"] == "2026-11-04T12:00:00+01:00"
    assert status["mode"] == "single"
    assert status["mqtt_connected"] is False
    assert status["bridge"] == "none"
    assert status["last_cycle_s"] is None
    assert status["system"] is None
    (pack,) = status["packs"]
    assert set(pack) == PACK_KEYS
    assert pack["address"] == 0 and pack["availability"] == "offline"
    assert pack["maint"] == "disabled"
    assert all(v is None for k, v in pack.items() if k not in ("address", "availability", "maint", "cells"))
    assert pack["balancer"] is None
    assert pack["cells"]["assignment"] is None
    assert pack["cells"]["live"] == {"summary": "not_evaluated", "reason": "no_assignment", "checks": [],
                                     "charge_limit_a": None, "discharge_limit_a": None}
    assert pack["cells"]["parameters"] == {"summary": "not_evaluated", "reason": "no_assignment", "checks": []}
    assert pack["maint_version"] is None
    json.dumps(status)                                   # JSON-ready


def test_values_after_a_poll_come_from_the_published_data(monkeypatch):
    client, _ = api.run_main(monkeypatch, single_bms(), number_of_packs=1, telemetry_polls=2)
    state = api.LAST_RUN["states"][0]
    assert state.status is not None
    (pack,) = state.status["packs"]
    data = state.packs[0].last_data
    normal = data["telemetry"]["normal"]
    assert pack["state_of_charge"] == normal["state_of_charge"]
    assert pack["total_pack_voltage"] == normal["total_pack_voltage"]
    assert pack["current"] == normal["current"]                    # canonical, not inverted
    assert pack["highest_cell_voltage"] == normal["highest_cell_voltage"]
    assert pack["lowest_cell_voltage"] == normal["lowest_cell_voltage"]
    assert pack["device_name"] == "SP16S020"
    assert pack["firmware_version"] == "16.06"
    assert pack["firmware_patch"]                                  # derived after the info was read
    assert pack["cell_count"] == 16
    assert pack["availability"] == "online"
    assert isinstance(pack["last_success_age_s"], float) and pack["last_success_age_s"] >= 0
    assert round(pack["last_success_age_s"], 1) == pack["last_success_age_s"]
    assert state.status["mode"] == "single" and state.status["system"] is None
    assert state.status["last_cycle_s"] is not None
    assert state.status["mqtt_connected"] is True
    json.dumps(state.status)


def test_system_values_in_multi_pack_mode_only(monkeypatch):
    api.run_main(monkeypatch, multi_bms(), number_of_packs=2, telemetry_polls=4)
    state = api.LAST_RUN["states"][0]
    status = state.status
    assert status["mode"] == "multi"
    assert [p["address"] for p in status["packs"]] == [0, 1]
    assert set(status["system"]) == SYSTEM_KEYS
    assert status["system"]["system_soc"] is not None
    assert status["system"]["total_full_energy"] is not None
    # the same system data does not show up in single pack mode
    single = fresh_state(1)
    single.system_data = state.system_data
    assert build_status(single, 0.0, "t")["system"] is None


def test_multi_pack_system_is_none_before_the_first_read():
    status = build_status(fresh_state(2), 0.0, "t")
    assert status["mode"] == "multi" and status["system"] is None


def test_bridge_and_maint_fields():
    state = fresh_state(1)
    state.bridge = type("B", (), {"alive": lambda self: True})()
    assert build_status(state, 0.0, "t")["bridge"] == "alive"
    state.bridge = type("B", (), {"alive": lambda self: False})()
    assert build_status(state, 0.0, "t")["bridge"] == "dead"
    cfg = dataclasses.replace(api.config(), maint_firmware=True)
    state = app.AppState(config=cfg)
    pack = BatteryPack(0, None, cfg)
    state.packs = [app.PackState(pack, 0)]
    for maint_state, expected in (("pending", "pending"), ("present", "present"), ("absent", "absent"), ("unknown", "pending")):
        pack.maint.state = maint_state
        assert build_status(state, 0.0, "t")["packs"][0]["maint"] == expected
    pack.maint.state = "present"
    pack.maint.identity = type("I", (), {"name": "EMU1101-EXT Full 1.6.4"})()
    assert build_status(state, 0.0, "t")["packs"][0]["maint_version"] == "EMU1101-EXT Full 1.6.4"
    pack.maint.state = "absent"
    assert build_status(state, 0.0, "t")["packs"][0]["maint_version"] is None


def test_a_defect_in_the_snapshot_does_not_stop_the_polling(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(app, "build_status", broken)
    client, _ = api.run_main(monkeypatch, single_bms(), number_of_packs=1, telemetry_polls=3)
    assert [r for r in client.as_records() if r["topic"] == "seplos/pack-0/sensors"]
    assert api.LAST_RUN["states"][0].status is None


# ---------------------------------------------------------------- GET /api/status


def _get_status(provider):
    async def main():
        with tempfile.TemporaryDirectory() as tmp:
            _cfg, store = load_or_migrate(Path(tmp) / "monitoring.json", {"RS485_REMOTE_IP": "10.0.0.1"})
            kwargs = {} if provider is None else {"status_provider": provider}
            web_app = WebApp(WebSettings(host="127.0.0.1", allowed_peers=frozenset({"127.0.0.1"})), store, **kwargs)
            async with TestServer(web_app.make_app()) as server, TestClient(server) as client:
                res = await client.get("/api/status")
                return res.status, await res.json(), res.headers.get("Cache-Control")

    return asyncio.run(main())


def test_api_status_without_a_provider():
    status, body, cache = _get_status(None)
    assert (status, body) == (200, {"status": None})
    assert cache == "no-store"


def test_api_status_with_a_provider():
    snapshot = {"generated_at": "t", "mode": "single", "packs": []}
    status, body, _ = _get_status(lambda: snapshot)
    assert (status, body) == (200, {"status": snapshot})


def test_api_status_is_not_a_mutation_route():
    # reading needs no identity: the status carries no secrets
    status, body, _ = _get_status(lambda: None)
    assert status == 200 and body["status"] is None
