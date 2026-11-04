"""
Characterization of the add-on's public output before refactoring (step 0).

Explicit asserts are hand-checked against the protocol documentation; the golden files pin the
complete dictionaries / discovery publishes so that a structural refactoring cannot change them
unnoticed. Known audited defects are characterized as-is and marked with their audit id; the
fix commit for that id updates the expectation.
"""
from __future__ import annotations

import json

import api
import frames as f
from fakes import FakeBms


# --- decoders --------------------------------------------------------------------------------

def test_telemetry_decoder(golden):
    out = api.decode_telemetry(f.telemetry_info())
    n = out["normal"]
    assert n["voltage_cell_1"] == 3.3 and n["voltage_cell_16"] == 3.315
    assert n["cell_temperature_1"] == 21.0 and n["cell_temperature_4"] == 23.4
    assert n["ambient_temperature"] == 19.5 and n["components_temperature"] == 25.1
    assert n["dis_charge_current"] == -12.34 and n["current"] == -12.34
    assert n["total_pack_voltage"] == 52.93 and n["port_voltage"] == 52.9
    assert n["residual_capacity"] == 150.0 and n["state_of_charge"] == 53.6
    assert n["charging_cycles"] == 42 and n["current_offset"] == -15
    assert n["energy_charged"] == 1000.0 and n["energy_discharged"] == 900.0
    assert n["lowest_cell"] == 1 and n["highest_cell"] == 16 and n["delta_cell_voltage"] == 0.015
    assert n["delta_cell_temperature"] == 3.4
    assert n["dis_charge_power"] == -653.16
    assert n["pack_voltage_setting_min"] == 40.0 and n["pack_voltage_setting_max"] == 58.4
    # F12 (characterized): 0.01 Ah raw values are rounded to 0.1 Ah
    assert n["full_capacity"] == 280.0 and n["rated_capacity"] == 280.0
    assert "current_idle" not in n
    golden("decode_telemetry", out)


def test_telemetry_idle_current_merge():
    out = api.decode_telemetry(f.telemetry_info(current_10ma=0, current_idle_ma=-372))
    assert out["normal"]["dis_charge_current"] == 0
    assert out["normal"]["current"] == -0.372


def test_telemetry_energy_fallback_within_process():
    pack = api.make_pack(0)
    first = pack.decode_telemetry_feedback_frame(f.telemetry_info(energy_charged=64999).hex().upper().encode())
    second = pack.decode_telemetry_feedback_frame(f.telemetry_info(energy_charged=6500).hex().upper().encode())
    assert first["normal"]["energy_charged"] == 6499.9
    assert second["normal"]["energy_charged"] == 6499.9


def test_telemetry_energy_fallback_after_restart_characterized():
    # F03 (characterized): a fresh process publishes the 650.0 fallback as a valid total
    out = api.decode_telemetry(f.telemetry_info(energy_charged=6500))
    assert out["normal"]["energy_charged"] == 650.0


def test_telesignal_decoder(golden):
    cell_alarms = [0] * 16
    cell_alarms[2] = 1
    out = api.decode_telesignal(f.telesignal_info(
        cell_alarms=cell_alarms,
        temp_alarms=(0, 2, 0, 0),
        events_1_6=(0x00, 0x03, 0x00, 0x10, 0x50, 0x02),
        on_off=0x03,
        balancing=(0x01, 0x80),
        system_status=0x02,
        event7=0x10,
        event8=0x01,
    ))
    n, b = out["normal"], out["binary"]
    assert n["cell_voltage_alarm_3"] == "Alarm (low)" and n["cell_voltage_alarm_1"] == "OK"
    assert n["cell_temperature_alarm_2"] == "Alarm (high)"
    assert n["any_cell_voltage_alarm"] == "Alarm" and n["any_cell_temperature_alarm"] == "Alarm"
    assert n["cell_overvoltage"] == "Protection"
    assert n["component_temperature_high"] == "Protection"   # event 4 bit 4 = protection
    assert n["transient_overcurrent"] == "Lockout"
    assert n["system_status"] == "Charging"
    assert b["intermittent_power_supplement"] == "Warning"
    assert b["auto_charging_wait"] == "Warning" and b["manual_charging_wait"] == "OK"
    assert b["eep_storage_failure"] == "Fault"
    assert b["discharge_switch"] == "ON" and b["charge_switch"] == "ON" and b["heating_switch"] == "OFF"
    assert b["balancer_cell_1"] == "ON" and b["balancer_cell_16"] == "ON" and b["balancer_cell_2"] == "OFF"
    # F08 (characterized): reverse polarity entity still decoded
    assert b["output_reverse_polarity_protection"] == "OK"
    golden("decode_telesignal", out)


def test_telesignal_precedence_pairs():
    """Alarm+protection and protection+lockout combinations: the more severe state wins."""
    out = api.decode_telesignal(f.telesignal_info(events_1_6=(0, 0x0C, 0x30, 0x03, 0xF0, 0)))
    n = out["normal"]
    assert n["cell_voltage_low"] == "Protection"
    assert n["discharging_temperature_high"] == "Protection"
    assert n["ambient_temperature_high"] == "Protection"
    assert n["transient_overcurrent"] == "Lockout" and n["output_short_circuit"] == "Lockout"
    out = api.decode_telesignal(f.telesignal_info(events_1_6=(0, 0x01, 0, 0x20, 0x10, 0)))
    n = out["normal"]
    assert n["cell_overvoltage"] == "Alarm"
    assert n["component_temperature_high"] == "Alarm"     # event 4 bit 5 = alarm
    assert n["transient_overcurrent"] == "Protection"


def test_system_status_priority():
    for status, expected in ((0x22, "Off"), (0x03, "Charging"), (0x11, "Discharging"), (0x10, "Standby"), (0x00, "Unknown")):
        assert api.decode_telesignal(f.telesignal_info(system_status=status))["normal"]["system_status"] == expected


def test_device_info_and_serial(golden):
    info = api.decode_device_info(f.device_info_info())
    assert info == {"normal": {"device_name": "SP16S020", "firmware_version": "16.06", "can_protocol": "Pylon"}}
    serial = api.decode_serial(f.serial_info())
    assert serial == {"normal": {"serial_number": "SN-BATTERY-0001"}}


def test_parameters_decoder(golden):
    out = api.decode_parameters(f.parameter_info(switches=bytes([0xFE, 0xFF, 0xFF, 0x7F, 0xFF, 0xFF, 0x0F, 0x80])))
    assert out["cell_high_voltage_alarm_v"] == 3.55
    assert out["pack_high_voltage_alarm_v"] == 56.8
    assert out["rated_capacity_ah"] == 280.0 and out["remaining_capacity_setting_ah"] == 140.0
    assert out["cell_difference_fault_v"] == 0.2
    assert out["cells_in_series"] == 16
    assert out["compensation_point_2_impedance_mohm"] == 1.5
    assert out["function_switches"] == "FE FF FF 7F FF FF 0F 80"
    assert out["balancing_enabled"] is True and out["charge_current_limit_mode"] == "warning - 10 A"
    # F02 (characterized): switch byte 0 is treated as a mask of event 1
    assert out["disabled_alarm_bits"] == {"alarm_event_1": [0], "alarm_event_4": [7]}
    assert out["device_name"] == "SP16S020"
    assert len(out) == 95   # F19 (characterized): attribute count, used as the Parameters state
    golden("decode_parameters", out)


def test_extended_status_decoder(golden):
    out = api.decode_extended_status(f.intra_pack_info())
    assert out["normal"] == {"dis_charge_current_unfiltered": 0.37, "highest_cell_temperature": 23.4, "lowest_cell_temperature": 20.0}
    assert out["binary"]["balancing_active"] == "ON" and out["binary"]["charge_forbidden"] == "ON"
    assert out["binary"]["balancing_blocked"] == "OFF"
    golden("decode_extended_status", out)


def test_system_decoder(golden):
    out = api.decode_system(f.system_registers())
    n, b = out["normal"], out["binary"]
    assert n["system_state"] == "Charging"
    assert n["system_voltage"] == 52.9 and n["system_current"] == -15.3 and n["system_soc"] == 54
    assert n["highest_cell_temperature"] == 23.4 and n["highest_cell_temperature_pack"] == 1
    assert n["lowest_cell_temperature"] == -1.5 and n["lowest_cell_temperature_pack"] == 0
    assert n["highest_cell_voltage"] == 3.315 and n["lowest_cell_voltage"] == 3.3
    assert n["charge_current_limit"] == 100.0 and n["discharge_current_limit"] == 150.0
    assert n["total_capacity"] == 560
    assert b == {"charge_allowed": "ON", "discharge_allowed": "ON", "pack_communication_fault": "ON"}
    golden("decode_system", out)


# --- complete pack poll against a fake bus ----------------------------------------------------

def _single_pack_bms(adr: int = 0) -> FakeBms:
    bms = FakeBms()
    bms.ascii[(adr, 0x42)] = f.telemetry_info()
    bms.ascii[(adr, 0x44)] = f.telesignal_info()
    bms.ascii[(adr, 0x51)] = f.device_info_info()
    bms.ascii[(adr, 0xA2)] = f.serial_info()
    bms.ascii[(adr, 0xAA)] = None          # 16.06.13+: silent
    bms.ascii[(adr, 0x47)] = f.parameter_info()
    bms.ascii[(adr, 0x5A)] = lambda req: f.ascii_frame(adr, 0x5A, f.intra_pack_info())
    return bms


def test_single_pack_poll(monkeypatch, golden):
    api.no_sleep(monkeypatch)
    with api.mode(1):
        pack = api.make_pack(0)
        bms = _single_pack_bms()
        data, ok = api.poll(pack, bms)
        assert ok and data is not None
        assert data["info"]["normal"]["firmware_patch"] == "16.06.13+"
        assert data["info"]["normal"]["serial_number"] == "SN-BATTERY-0001"
        assert set(data) == {"telemetry", "telesignalization", "info", "status", "parameters"}
        golden("poll_single_pack", data)
        # unchanged second poll publishes nothing but counts as success
        data2, ok2 = api.poll(pack, bms)
        assert data2 is None and ok2
        cids = [r.cid2 for r in bms.requests if r.kind == "ascii"]
        assert cids.count(0x42) == 2 and cids.count(0x47) == 1 and cids.count(0x51) >= 1


def test_single_pack_poll_failed_5a_keeps_stale_status(monkeypatch):
    # F04 (characterized): a failed 0x5A keeps the last status without expiry
    api.no_sleep(monkeypatch)
    with api.mode(1):
        pack = api.make_pack(0)
        bms = _single_pack_bms()
        data, _ = api.poll(pack, bms)
        bms.ascii[(0, 0x5A)] = None
        bms.ascii[(0, 0x42)] = f.telemetry_info(cycles=43)
        data2, ok = api.poll(pack, bms)
        assert ok and data2["status"] == data["status"]


def test_multi_pack_poll(monkeypatch, golden):
    api.no_sleep(monkeypatch)
    with api.mode(2):
        bms = FakeBms()
        for adr in (0, 1):
            bms.ascii[(adr, 0x42)] = f.telemetry_info()
            bms.ascii[(adr, 0x44)] = f.telesignal_info()
            bms.ascii[(adr, 0x51)] = f.device_info_info()
        bms.ascii[(0x02, 0x60)] = ("rtn", 0x00)
        bms.modbus[(0x03, 0x001F, 9)] = [0] * 9
        results = {}
        for adr in (0, 1):
            data, ok = api.poll(api.make_pack(adr), bms)
            assert ok
            results[adr] = data
        assert results[0]["info"]["normal"]["firmware_patch"] == "16.06.13+"
        assert results[1]["info"]["normal"]["firmware_patch"] == "16.06 (unknown, only derivable for the master)"
        assert "serial_number" not in results[0]["info"]["normal"]
        assert set(results[0]) == {"telemetry", "telesignalization", "info"}
        assert not any(r.cid2 in (0xA2, 0x47, 0x5A) for r in bms.requests if r.kind == "ascii")
        golden("poll_multi_pack", results)


def test_patch_probe_pack_bus_old_firmware(monkeypatch):
    api.no_sleep(monkeypatch)
    with api.mode(1):
        bms = _single_pack_bms()
        bms.ascii[(0, 0xAA)] = ("rtn", 0x04)
        data, _ = api.poll(api.make_pack(0), bms)
        assert data["info"]["normal"]["firmware_patch"] == "16.06.03-16.06.12"


def test_patch_probe_pack_bus_other_rtn_characterized(monkeypatch):
    # F16 (characterized): RTN 02 on 0xAA is not RTN 04 and the pack answers 0x51 -> classified 16.06.13+
    api.no_sleep(monkeypatch)
    with api.mode(1):
        bms = _single_pack_bms()
        bms.ascii[(0, 0xAA)] = ("rtn", 0x02)
        data, _ = api.poll(api.make_pack(0), bms)
        assert data["info"]["normal"]["firmware_patch"] == "16.06.13+"


def test_poll_fails_on_bad_checksum(monkeypatch):
    api.no_sleep(monkeypatch)
    with api.mode(1):
        bms = _single_pack_bms()
        good = f.ascii_frame(0, 0, f.telemetry_info())
        bms.ascii[(0, 0x42)] = lambda req: good[:-5] + b"0000\r"
        data, ok = api.poll(api.make_pack(0), bms)
        assert data is None and not ok


def test_system_read(monkeypatch):
    api.no_sleep(monkeypatch)
    with api.mode(2):
        bms = FakeBms()
        bms.modbus[(0x03, 0x1000, 21)] = f.system_registers()
        out = api.read_system(api.make_pack(0), bms)
        assert out == api.decode_system(f.system_registers())


# --- discovery ----------------------------------------------------------------------------------

def _discovery(records):
    """
    Retained discovery configs are independent of their publish order, except that the full device
    block travels with the first config of each device. Assert that, then compare order-insensitively.
    """
    first_dev = {}
    for r in records:
        dev = r["payload"].get("dev") if isinstance(r["payload"], dict) else None
        if dev and dev["ids"] not in first_dev:
            first_dev[dev["ids"]] = dev
    for ids, dev in first_dev.items():
        assert "name" in dev and "mf" in dev, f"first config of {ids} lacks the full device block"
    return sorted(records, key=lambda r: (r["topic"], json.dumps(r["payload"], sort_keys=True)))


def test_discovery_single_pack(golden):
    disc, client = api.discovery(invert=False)
    api.discovery_startup(disc, 1)
    records = client.as_records()
    assert all(r["retain"] and r["qos"] == 1 for r in records)
    configs = [r for r in records if r["payload"]]
    assert any(r["topic"] == "homeassistant/sensor/seplos-mqtt-pack-0/voltage_cell_1/config" for r in configs)
    golden("discovery_single_pack", _discovery(records))


def test_discovery_multi_pack(golden):
    disc, client = api.discovery(invert=False)
    api.discovery_startup(disc, 2)
    golden("discovery_multi_pack", _discovery(client.as_records()))


def test_discovery_inverted(golden):
    disc, client = api.discovery(invert=True)
    api.discovery_startup(disc, 2)
    golden("discovery_multi_pack_inverted", _discovery(client.as_records()))


def test_discovery_firmware_refresh(golden):
    disc, client = api.discovery(invert=False)
    api.discovery_startup(disc, 2)
    client.published.clear()
    api.firmware_refresh(disc, 0, {"normal": {"firmware_version": "16.06", "firmware_patch": "16.06.13+"}})
    api.firmware_refresh(disc, 1, {"normal": {"firmware_version": "16.06", "firmware_patch": "16.06 (unknown, only derivable for the master)"}})
    golden("discovery_firmware_refresh", client.as_records())


# --- main loop: runtime topics, heartbeat, availability, exit ------------------------------------

def _runtime_records(client):
    """Main-loop publishes without the discovery configs (covered by their own goldens)."""
    return [r for r in client.as_records() if not r["topic"].startswith("homeassistant/")]


def test_main_loop_single_pack(monkeypatch, golden):
    client = api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=2)
    records = _runtime_records(client)
    topics = [r["topic"] for r in records]
    assert "seplos/pack-0/sensors" in topics and "seplos/pack-0/heartbeat" in topics
    assert topics[-2:] == ["seplos/availability", "seplos/pack-0/availability"]   # graceful exit
    assert records[-2]["payload"] == "offline" and records[-2]["retain"] is True
    assert any(r["topic"].startswith("homeassistant/") for r in client.as_records())
    golden("main_loop_single_pack", records)


def test_main_loop_multi_pack(monkeypatch, golden):
    bms = FakeBms()
    for adr in (0, 1):
        bms.ascii[(adr, 0x42)] = f.telemetry_info()
        bms.ascii[(adr, 0x44)] = f.telesignal_info()
        bms.ascii[(adr, 0x51)] = f.device_info_info()
    bms.ascii[(0x02, 0x60)] = ("rtn", 0x00)
    bms.modbus[(0x03, 0x001F, 9)] = [0] * 9
    bms.modbus[(0x03, 0x1000, 21)] = f.system_registers()
    client = api.run_main(monkeypatch, bms, number_of_packs=2, telemetry_polls=4)
    records = _runtime_records(client)
    topics = [r["topic"] for r in records]
    assert "seplos/system/sensors" in topics and "seplos/system/availability" in topics
    golden("main_loop_multi_pack", records)
    # discovery as published by main() equals the startup sequence + firmware refresh
    disc_records = [r for r in client.as_records() if r["topic"].startswith("homeassistant/")]
    golden("main_loop_multi_pack_discovery", _discovery(disc_records))
