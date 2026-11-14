"""
Characterization of the add-on's public output before refactoring (step 0).

Explicit asserts are hand-checked against the protocol documentation; the golden files pin the
complete dictionaries / discovery publishes so that a structural refactoring cannot change them
unnoticed. Known audited defects are characterized as-is and marked with their audit id; the
fix commit for that id updates the expectation.
"""
from __future__ import annotations

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
    # F12: capacities keep their 0.01 Ah resolution
    assert n["full_capacity"] == 280.01 and n["rated_capacity"] == 280.03
    # G06: cell temperature extrema from the four 0x42 channels (both bus modes)
    assert n["highest_cell_temperature"] == 23.4 and n["lowest_cell_temperature"] == 20.0
    assert "current_idle" not in n
    golden("decode_telemetry", out)


def test_telemetry_idle_current_merge():
    out = api.decode_telemetry(f.telemetry_info(current_10ma=0, current_idle_ma=-372))
    assert out["normal"]["dis_charge_current"] == 0
    assert out["normal"]["current"] == -0.372


def test_telemetry_energy_fallback_within_process():
    first, second = api.decode_telemetry_sequence([f.telemetry_info(energy_charged=64999), f.telemetry_info(energy_charged=6500)])
    assert first["normal"]["energy_charged"] == 6499.9
    assert second["normal"]["energy_charged"] == 6499.9


def test_telemetry_energy_fallback_without_history_is_unknown():
    # F03: without any history a 650.0 can be the fallback, publish unknown instead of a false reset
    out = api.decode_telemetry(f.telemetry_info(energy_charged=6500))
    assert out["normal"]["energy_charged"] is None
    assert out["normal"]["energy_discharged"] == 900.0


def test_telemetry_energy_fallback_after_restart_with_state(tmp_path):
    # F03: the last valid value survives a restart via the state file
    path = tmp_path / "state.json"
    first = api.decode_telemetry_sequence([f.telemetry_info(energy_charged=64999)], state_file=path)
    restarted = api.decode_telemetry_sequence([f.telemetry_info(energy_charged=6500)], state_file=path, flush_before=True)
    assert first[0]["normal"]["energy_charged"] == 6499.9
    assert restarted[0]["normal"]["energy_charged"] == 6499.9


def test_telemetry_legit_650_with_history():
    out = api.decode_telemetry_sequence([f.telemetry_info(energy_charged=6499), f.telemetry_info(energy_charged=6500)])
    assert out[1]["normal"]["energy_charged"] == 650.0


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
    # F08: reverse polarity is never set by the firmware, not decoded anymore
    assert "output_reverse_polarity_protection" not in b
    # F01 / F09
    assert b["severe_fault"] == "OK" and b["rtc_time_invalid"] == "OK"
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
    assert out["balancing_enabled"] is True and out["ccl_base_mode"] == "warning - 10 A (if above 20 A)"   # F21
    # F02: BitGroup0 is no mask; groups 1-5 mask events 2-6
    assert out["disabled_alarm_bits"] == {"alarm_event_4": [7]}
    assert out["temperature_sensor_plausibility_check"] is True and out["severe_fault_reporting"] is False   # G10
    # F10: P70-P72 describe what they are
    assert out["transient_or_short_circuit_lockout_event_count"] == 4
    assert out["charge_current_limit_duration_min"] == 10 and out["charge_activation_window_min"] == 30
    assert out["device_name"] == "SP16S020"
    golden("decode_parameters", out)


def test_extended_status_decoder(golden):
    out = api.decode_extended_status(f.intra_pack_info())
    assert out["normal"] == {"dis_charge_current_unfiltered": 0.37}
    b = out["binary"]
    assert b["balancing_active"] == "ON" and b["charge_forbidden"] == "ON" and b["balancing_blocked"] == "OFF"
    # G01/G02 from the system status 0x02: charging, nothing else
    assert b["external_voltage_detected"] == "OFF" and b["charge_current_status"] == "OFF"
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

def _multi_pack_bms() -> FakeBms:
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


def _single_pack_bms(adr: int = 0) -> FakeBms:
    bms = FakeBms()
    bms.ascii[(adr, 0x42)] = f.telemetry_info(adr=adr)
    bms.ascii[(adr, 0x44)] = f.telesignal_info(adr=adr)
    bms.ascii[(adr, 0x51)] = f.device_info_info()
    bms.ascii[(adr, 0xA2)] = f.serial_info()
    bms.ascii[(adr, 0xAA)] = None          # 16.06.13+: silent
    bms.ascii[(adr, 0x47)] = f.parameter_info()
    bms.ascii[(adr, 0x5A)] = lambda req: f.ascii_frame(adr, 0x5A, f.intra_pack_info())
    bms.ascii[(adr, 0x4D)] = bytes.fromhex("07EA0B040B3B1E")    # 2026-11-04 11:59:30
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
        assert set(data) == {"telemetry", "telesignalization", "info", "status", "parameters", "clock", "alarm_summary"}
        assert data["clock"]["normal"] == {"bms_time": "2026-11-04T11:59:30", "clock_offset": -30}   # G09
        golden("poll_single_pack", data)
        # an unchanged second poll returns the same data; the publisher suppresses the publish
        data2, ok2 = api.poll(pack, bms)
        assert data2 == data and ok2
        cids = [r.cid2 for r in bms.requests if r.kind == "ascii"]
        assert cids.count(0x42) == 2 and cids.count(0x47) == 1 and cids.count(0x51) >= 1


def test_single_pack_poll_failed_5a_status_expires(monkeypatch):
    # F04: a failed 0x5A bridges a short gap, then the status becomes unknown
    api.no_sleep(monkeypatch)
    clock = api.fake_clock(monkeypatch)
    with api.mode(1):
        pack = api.make_pack(0)
        bms = _single_pack_bms()
        data, _ = api.poll(pack, bms)
        bms.ascii[(0, 0x5A)] = None
        bms.ascii[(0, 0x42)] = f.telemetry_info(cycles=43)
        clock["t"] += 10
        data2, ok = api.poll(pack, bms)
        assert ok and data2["status"] == data["status"]
        bms.ascii[(0, 0x42)] = f.telemetry_info(cycles=44)
        clock["t"] += 120
        data3, ok = api.poll(pack, bms)
        assert ok and data3["status"]["binary"]["charge_forbidden"] is None
        assert set(data3["status"]["binary"]) == set(data["status"]["binary"])


def test_multi_pack_poll(monkeypatch, golden):
    api.no_sleep(monkeypatch)
    with api.mode(2):
        bms = FakeBms()
        bms = _multi_pack_bms()
        results = {}
        for adr in (0, 1):
            data, ok = api.poll(api.make_pack(adr), bms)
            assert ok
            results[adr] = data
        assert results[0]["info"]["normal"]["firmware_patch"] == "16.06.13+"
        assert results[1]["info"]["normal"]["firmware_patch"] == "16.06 (unknown, only derivable for the master)"
        assert "serial_number" not in results[0]["info"]["normal"]
        assert set(results[0]) == {"telemetry", "telesignalization", "info", "alarm_summary"}
        assert not any(r.cid2 in (0xA2, 0x47, 0x5A) for r in bms.requests if r.kind == "ascii")
        golden("poll_multi_pack", results)


def test_patch_probe_pack_bus_old_firmware(monkeypatch):
    api.no_sleep(monkeypatch)
    with api.mode(1):
        bms = _single_pack_bms()
        bms.ascii[(0, 0xAA)] = ("rtn", 0x04)
        data, _ = api.poll(api.make_pack(0), bms)
        assert data["info"]["normal"]["firmware_patch"] == "16.06.03-16.06.12"


def test_patch_probe_pack_bus_other_rtn_is_unknown(monkeypatch):
    # F16: only silence counts for 16.06.13+, any other answer to 0xAA makes the result unknown
    api.no_sleep(monkeypatch)
    with api.mode(1):
        bms = _single_pack_bms()
        bms.ascii[(0, 0xAA)] = ("rtn", 0x02)
        data, _ = api.poll(api.make_pack(0), bms)
        assert data["info"]["normal"]["firmware_patch"] == "16.06 (unknown)"


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
        bms.modbus[(0x03, 0x0074, 1)] = ("raw", f.modbus_wide_count_response(1, 3, 0xE0))
        out = api.read_system(api.make_pack(0), bms)
        expected = api.decode_system(f.system_registers())
        expected["binary"].update({"force_charge_1": "ON", "force_charge_2": "OFF"})   # G07
        assert out == expected


def test_system_read_without_flags_register(monkeypatch):
    """Firmware < 16.06.11 answers 0x0074 with an exception: the flags are left out."""
    api.no_sleep(monkeypatch)
    bms = FakeBms()
    bms.modbus[(0x03, 0x1000, 21)] = f.system_registers()
    bms.modbus[(0x03, 0x0074, 1)] = ("exc", 0x02)
    out = api.read_system(api.make_pack(0), bms)
    expected = api.decode_system(f.system_registers())
    expected["binary"].update({"force_charge_1": None, "force_charge_2": None})   # review T02: unknown, not stale
    assert out == expected


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
    # stable sort: repeated retained writes to one topic keep their order (the last one wins)
    return sorted(records, key=lambda r: r["topic"])


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
    client, port = api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=2)
    records = _runtime_records(client)
    topics = [r["topic"] for r in records]
    assert "seplos/pack-0/sensors" in topics and "seplos/pack-0/heartbeat" in topics
    assert topics[-2:] == ["seplos/availability", "seplos/pack-0/availability"]   # graceful exit
    assert records[-2]["payload"] == "offline" and records[-2]["retain"] is True
    assert any(r["topic"].startswith("homeassistant/") for r in client.as_records())
    golden("main_loop_single_pack", records)


def test_main_loop_multi_pack(monkeypatch, golden):
    bms = _multi_pack_bms()
    client, port = api.run_main(monkeypatch, bms, number_of_packs=2, telemetry_polls=4)
    records = _runtime_records(client)
    topics = [r["topic"] for r in records]
    assert "seplos/system/sensors" in topics and "seplos/system/availability" in topics
    golden("main_loop_multi_pack", records)
    # discovery as published by main() equals the startup sequence + firmware refresh
    disc_records = [r for r in client.as_records() if r["topic"].startswith("homeassistant/")]
    golden("main_loop_multi_pack_discovery", _discovery(disc_records))


# --- review step 0 additions ------------------------------------------------------------------------

def test_fixture_layouts():
    assert len(f.telemetry_info()) == 75 and len(f.telesignal_info()) == 49
    assert len(f.parameter_info()) == 169 and len(f.intra_pack_info()) == 32
    assert f.ascii_frame(1, 0, f.telemetry_info())[9:13] == b"1096"
    assert f.ascii_frame(1, 0, f.telesignal_info())[9:13] == b"8062"
    assert f.telesignal_info(system_status=0x13)[48] == 0x13 & 0x33


def test_outbound_requests_match_kb_literals():
    # seplos-ascii.md examples (pack address 1)
    assert api.encode_request(1, 0x42) == b"~20014642E00201FD35\r"
    assert api.encode_request(1, 0x44) == b"~20014644E00201FD33\r"
    assert api.encode_request(1, 0x51, b"") == b"~200146510000FDAD\r"
    assert api.encode_request(1, 0x47, b"") == b"~200146470000FDA8\r"


def test_checksum_vectors():
    assert f.modbus_crc(b"123456789") == 0x4B37
    assert api.modbus_crc(b"123456789") == 0x4B37


def test_fake_rejects_invalid_ascii_request():
    from fakes import ProtocolError, check_ascii_request
    good = api.encode_request(0, 0x42)
    check_ascii_request(good)
    for bad in (good[:-5] + b"0000\r", good.replace(b"~20", b"~21"), good[:9] + b"F002" + good[13:]):
        try:
            check_ascii_request(bad)
        except ProtocolError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_intra_pack_record_decoder(golden):
    out = api.decode_intra_pack_record(f.intra_pack_info(events_1_6=bytes([1, 2, 3, 4, 5, 6]), status=0x12))
    assert out["highest_cell_voltage"] == 3.315 and out["lowest_cell_voltage"] == 3.3
    assert out["pack_voltage"] == 52.93 and out["full_capacity"] == 280.01 and out["state_of_charge"] == 53.6
    assert out["alarm_events_1_6"] == "01 02 03 04 05 06" and out["system_status"] == "12"
    golden("decode_intra_pack_record", out)


def test_extended_status_bits_cleared():
    out = api.decode_extended_status(f.intra_pack_info(event7=0x0E, event8=0x00, max_t=-5.0, min_t=-12.3, current_10ma=-250))
    assert out["normal"] == {"dis_charge_current_unfiltered": -2.5}
    assert out["binary"] == {"balancing_active": "OFF", "balancing_blocked": "ON", "balancing_time_limit": "ON",
                             "balancing_temperature_blocked": "ON", "limiter_charging_from_10a": "OFF",
                             "limiter_after_charge_overcurrent_warning": "OFF", "charge_forbidden": "OFF",
                             "precharge_running": "OFF", "external_voltage_detected": "OFF",
                             "discharge_current_status": "OFF", "charge_current_status": "OFF"}
    out = api.decode_extended_status(f.intra_pack_info(event7=0xC0, on_off=0x13, status=0xC4))
    b = out["binary"]
    assert b["limiter_charging_from_10a"] == "ON" and b["limiter_after_charge_overcurrent_warning"] == "ON"
    assert b["precharge_running"] == "ON" and b["external_voltage_detected"] == "ON"
    assert b["discharge_current_status"] == "ON" and b["charge_current_status"] == "ON"


def test_negative_temperatures_and_parameter_currents():
    out = api.decode_telemetry(f.telemetry_info(cell_temps_c=(-5.0, -0.1, 0.0, 1.5), ambient_c=-12.3))
    assert out["normal"]["cell_temperature_1"] == -5.0 and out["normal"]["cell_temperature_2"] == -0.1
    assert out["normal"]["ambient_temperature"] == -12.3 and out["normal"]["delta_cell_temperature"] == 6.5
    params = api.decode_parameters(f.parameter_info())
    assert params["discharging_overcurrent_alarm_a"] == -150.0 and params["discharging_overcurrent_recovery_a"] == -145.0


def test_every_alarm_field_individually(golden):
    """Set one byte alarm / one event byte at a time; the golden pins which keys react."""
    results = {}
    for name, kwargs in ([
        ("ambient", dict(ambient_alarm=2)), ("power", dict(power_alarm=1)), ("current", dict(current_alarm=2)),
        ("pack_voltage", dict(pack_voltage_alarm=1)), ("other", dict(power_alarm=0xF0)),
    ]):
        n = api.decode_telesignal(f.telesignal_info(**kwargs))["normal"]
        results[name] = {k: v for k, v in n.items() if v not in ("OK",) and k != "system_status"}
    for event in range(6):
        for bit in range(8):
            events = [0] * 6
            events[event] = 1 << bit
            out = api.decode_telesignal(f.telesignal_info(events_1_6=tuple(events)))
            changed = {k: v for grp in ("normal", "binary") for k, v in out[grp].items()
                       if v not in ("OK", "OFF") and k not in ("system_status", "discharge_switch", "charge_switch")}
            results[f"event{event + 1}_bit{bit}"] = changed
    for name, kwargs in (("event7", "event7"), ("event8", "event8"), ("on_off", "on_off")):
        for bit in range(8):
            out = api.decode_telesignal(f.telesignal_info(**{"on_off": 0, kwargs: 1 << bit}))
            results[f"{name}_bit{bit}"] = {k: v for grp in ("normal", "binary") for k, v in out[grp].items()
                                           if v not in ("OK", "OFF") and k != "system_status"}
    assert results["event1_bit0"] == {"voltage_sensing_failure": "Fault"}
    assert results["event6_bit4"] == {"cell_low_voltage_forbidden_charging": "Protection"}
    assert results["event8_bit4"] == {"no_calibration_of_null_point": "Warning"}
    assert results["other"] == {"component_temperature_alarm": "Alarm (other)"}
    golden("alarm_field_matrix", results)


def test_poll_changed_second_payload(monkeypatch):
    api.no_sleep(monkeypatch)
    with api.mode(1):
        pack = api.make_pack(0)
        bms = _single_pack_bms()
        api.poll(pack, bms)
        bms.ascii[(0, 0x42)] = f.telemetry_info(cycles=43)
        data, ok = api.poll(pack, bms)
        assert ok and data["telemetry"]["normal"]["charging_cycles"] == 43


def test_poll_is_no_success_when_44_fails(monkeypatch):
    api.no_sleep(monkeypatch)
    with api.mode(1):
        bms = _single_pack_bms()
        bms.ascii[(0, 0x44)] = None
        data, ok = api.poll(api.make_pack(0), bms)
        assert not ok and data["telemetry"]["normal"]["voltage_cell_1"] == 3.3


def test_poll_rejects_wrong_address_answer(monkeypatch):
    api.no_sleep(monkeypatch)
    with api.mode(1):
        bms = _single_pack_bms()
        bms.ascii[(0, 0x42)] = lambda req: f.ascii_frame(1, 0, f.telemetry_info(adr=1))
        assert api.poll(api.make_pack(0), bms) == (None, False)


def test_poll_skips_interleaved_intra_pack_frame(monkeypatch):
    api.no_sleep(monkeypatch)
    with api.mode(1):
        bms = _single_pack_bms()
        noise = f.ascii_frame(3, 0x5A, f.intra_pack_info())
        good = f.ascii_frame(0, 0, f.telemetry_info())
        bms.ascii[(0, 0x42)] = lambda req: noise + good
        data, ok = api.poll(api.make_pack(0), bms)
        assert ok and data["telemetry"]["normal"]["charging_cycles"] == 42


def test_initialize_serial(monkeypatch):
    assert api.init_serial(monkeypatch, 1) == {"port": "/tmp/vcom0", "baudrate": 19200, "timeout": 0.5, "write_timeout": 2}
    assert api.init_serial(monkeypatch, 2) == {"port": "/tmp/vcom0", "baudrate": 9600, "timeout": 0.5, "write_timeout": 2}


def test_initialize_mqtt(monkeypatch, golden):
    golden("initialize_mqtt", api.init_mqtt(monkeypatch))


def test_main_loop_cleanup(monkeypatch):
    client, port = api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=1)
    assert client.calls == ["disconnect", "loop_stop"]
    assert port.is_open is False


def test_main_loop_failing_pack_and_ageing(monkeypatch, golden):
    """Pack 1 never answers; large time steps age the availability of pack 0 between its polls."""
    bms = _multi_pack_bms()
    bms.ascii[(1, 0x42)] = None
    bms.modbus.pop((0x03, 0x1000, 21))
    client, _ = api.run_main(monkeypatch, bms, number_of_packs=2, telemetry_polls=6, seconds_per_tick=7)
    golden("main_loop_failing_pack", _runtime_records(client))


def test_main_loop_no_discovery_custom_topic(monkeypatch, golden):
    client, _ = api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=1,
                             discovery=False, topic="bms/garage", prefix="ha")
    records = client.as_records()
    assert not any(r["topic"].startswith(("ha/", "homeassistant/")) for r in records)
    assert all(r["topic"].startswith("bms/garage/") for r in records)
    golden("main_loop_custom_topic", records)


def test_discovery_custom_prefix_topic(golden):
    disc, client = api.discovery(topic="bms/garage", prefix="ha")
    api.discovery_startup(disc, 2)
    records = client.as_records()
    assert all(r["topic"].startswith("ha/") for r in records)
    golden("discovery_custom_prefix", _discovery(records))


# --- step 2: variable cell count, baud override, force charge flags ----------------------------------

def test_fifteen_cell_pack(monkeypatch, golden):
    """F05: the 0x42/0x44 layout follows the cell count M."""
    api.no_sleep(monkeypatch)
    cells = [3300 + i for i in range(15)]
    with api.mode(1):
        bms = _single_pack_bms()
        bms.ascii[(0, 0x42)] = f.telemetry_info(cells_mv=cells, cycles=77, energy_charged=1234)
        bms.ascii[(0, 0x44)] = f.telesignal_info(cell_alarms=[0] * 14 + [2], events_1_6=(0, 0, 0, 0x80, 0, 0), balancing=(0x01, 0x40))
        data, ok = api.poll(api.make_pack(0), bms)
    assert ok
    t = data["telemetry"]["normal"]
    assert t["voltage_cell_15"] == 3.314 and "voltage_cell_16" not in t
    assert t["charging_cycles"] == 77 and t["energy_charged"] == 123.4 and t["average_cell_voltage"] == 3.307
    assert t["pack_voltage_setting_max"] == 3.65 * 15
    s = data["telesignalization"]
    assert s["normal"]["cell_voltage_alarm_15"] == "Alarm (high)" and "cell_voltage_alarm_16" not in s["normal"]
    assert s["binary"]["severe_fault"] == "Fault"
    assert s["binary"]["balancer_cell_15"] == "ON" and "balancer_cell_16" not in s["binary"]
    golden("poll_fifteen_cells", data)


def test_discovery_removes_cells_above_count(golden):
    disc, client = api.discovery()
    api.discovery_startup(disc, 1)
    client.published.clear()
    api.cell_count(disc, 0, 15)
    api.cell_count(disc, 0, 15)   # idempotent
    topics = [r["topic"] for r in client.as_records()]
    assert topics == [
        "homeassistant/sensor/seplos-mqtt-pack-0/voltage_cell_16/config",
        "homeassistant/sensor/seplos-mqtt-pack-0/cell_voltage_alarm_16/config",
        "homeassistant/binary_sensor/seplos-mqtt-pack-0/balancer_cell_16/config",
    ]


def test_implausible_cell_count_rejected(monkeypatch):
    api.no_sleep(monkeypatch)
    with api.mode(1):
        bms = _single_pack_bms()
        bms.ascii[(0, 0x42)] = f.telemetry_info(cells_mv=[3300] * 17)
        assert api.poll(api.make_pack(0), bms) == (None, False)


def test_baudrate_override(monkeypatch):
    """F17: 16.04.x uses 9600 on RS485-1/2 as well."""
    assert api.init_serial(monkeypatch, 1, SERIAL_BAUDRATE="9600")["baudrate"] == 9600
    assert api.init_serial(monkeypatch, 1, SERIAL_BAUDRATE="auto")["baudrate"] == 19200
    import pytest

    from seplos_mqtt.config import ConfigError
    with pytest.raises(ConfigError):
        api.init_serial(monkeypatch, 2, SERIAL_BAUDRATE="1920O")


def test_inconsistent_cell_count_rejected(monkeypatch):
    """Review E01: a 16-cell body declaring 15 cells is malformed and must not count as a successful poll."""
    api.no_sleep(monkeypatch)
    with api.mode(1):
        for cid2, info in ((0x42, f.telemetry_info()), (0x44, f.telesignal_info())):
            bad = bytearray(info)
            bad[2] = 15
            bms = _single_pack_bms()
            bms.ascii[(0, cid2)] = bytes(bad)
            data, ok = api.poll(api.make_pack(0), bms)
            assert not ok, hex(cid2)
            assert (data is None) == (cid2 == 0x42), hex(cid2)     # a bad 0x44 keeps the valid 0x42


# --- step 3: bus ownership, timing, strict framing -------------------------------------------------

def test_bus_quiet_gap_on_every_transmission(monkeypatch):
    """R04: every write, incl. retries, starts >= 1 s after the last bus activity."""
    trace = api.bus_trace_poll(monkeypatch, _single_pack_bms(), flaky_first_42=True)
    writes = [t for op, t in trace if op == "write"]
    reads = [t for op, t in trace if op == "read"]
    assert len(writes) >= 8
    for w in writes[1:]:
        last_read = max(r for r in reads if r <= w)
        assert w - last_read >= 1.0 - 1e-9, trace


def test_strict_envelope_rejected(monkeypatch):
    """R03: checksum-valid frames with a wrong VER/CID1/LENGTH/EOI are not accepted."""
    api.no_sleep(monkeypatch)
    good = f.ascii_frame(0, 0, f.telemetry_info())
    variants = [
        f.ascii_frame(0, 0, f.telemetry_info(), ver=0x21),
        f.ascii_frame(0, 0, f.telemetry_info(), cid1=0x47),
        api.with_checksum(good[:9] + b"F096" + good[13:]),      # wrong LENGTH nibble
        good[:-1] + b"\n",                                       # wrong EOI
    ]
    with api.mode(1):
        for raw in variants:
            bms = _single_pack_bms()
            bms.ascii[(0, 0x42)] = lambda req, raw=raw: raw
            assert api.poll(api.make_pack(0), bms) == (None, False), raw


def test_length_field_wraps_correctly():
    assert api.length_field(46) == 0x002E and api.length_field(2) == 0xE002 and api.length_field(150) == 0x1096


# --- step 3: MQTT delivery, resync, lifecycle, configuration ---------------------------------------

def test_publisher_retries_failed_publish_and_replays():
    """R01: a state that failed to publish is sent again with the next unchanged poll and on replay."""
    pub, client = api.publisher()
    client.fail_next = 1
    assert pub.state("t/a", {"v": 1}) is False
    assert pub.state("t/a", {"v": 1}) is True          # same payload, but the first attempt failed
    assert pub.state("t/a", {"v": 1}) is False         # now suppressed
    client.published.clear()
    pub.replay()
    assert [r.topic for r in client.published] == ["t/a"]


def test_resync_on_ha_birth_and_reconnect():
    conn, raw = api.mqtt_connection()
    conn.on_connect(raw, None, {}, 0)
    assert raw.subscriptions == ["homeassistant/status"] and not conn.resync.is_set()
    conn.on_message(raw, None, api.message("homeassistant/status", b"online"))
    assert conn.resync.is_set()
    conn.resync.clear()
    conn.on_message(raw, None, api.message("homeassistant/status", b"offline"))
    assert not conn.resync.is_set()
    conn.on_disconnect(raw, None, 7)                    # Paho 1.x signature
    assert conn.connected is False
    conn.on_connect(raw, None, {}, api.reason_code(0), None)   # Paho 2.x signature, reconnect
    assert conn.connected and conn.resync.is_set()
    conn.on_connect(raw, None, {}, api.reason_code(5), None)
    assert conn.connected is False


def test_main_loop_resync_republishes(monkeypatch):
    client, _ = api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=2, resync_after_polls=1)
    sensors = [r for r in client.as_records() if r["topic"] == "seplos/pack-0/sensors"]
    configs = [r for r in client.as_records() if r["topic"] == "homeassistant/sensor/seplos-mqtt-pack-0/voltage_cell_2/config"]
    assert len(sensors) >= 2 and len(configs) == 2     # unchanged data replayed, discovery resent
    # the anchor config also carries the device block: startup (no firmware yet), the one update when the
    # device info arrives, then the resync. Nothing is republished a second time after the resync.
    anchors = [r["payload"]["dev"] for r in client.as_records()
               if r["topic"] == "homeassistant/sensor/seplos-mqtt-pack-0/voltage_cell_1/config"]
    assert [d["sw"] for d in anchors] == ["", "16.06.13+", "16.06.13+"]
    assert anchors[-1]["sn"] == "SN-BATTERY-0001"


def test_discovery_device_block_retried_after_failed_publish():
    """E04/R01: only the anchor config carries the full device block; if its publish fails, discovery is
    flagged and the next reconcile sends it again (no other config takes over the block)."""
    disc, client = api.discovery()
    client.fail_next = 1
    api.discovery_startup(disc, 1)
    assert disc.failed
    second = next(r for r in client.as_records() if r["topic"].endswith("/voltage_cell_2/config"))
    assert second["payload"]["dev"] == {"ids": "seplos_bms_pack_0"}
    client.published.clear()
    disc.publish_pack(0, pack_bus=True)
    anchor = next(r for r in client.as_records() if r["topic"].endswith("/voltage_cell_1/config"))
    assert "mf" in anchor["payload"]["dev"]


def test_signal_handler_exits_through_cleanup(monkeypatch):
    client, port = api.run_main(monkeypatch, _single_pack_bms(), number_of_packs=1, telemetry_polls=5, signal_after_polls=1)
    assert client.calls == ["disconnect", "loop_stop"] and port.is_open is False
    assert client.as_records()[-2] == {"topic": "seplos/availability", "payload": "offline", "qos": 0, "retain": True}


def test_config_validation():
    import pytest

    from seplos_mqtt.config import ConfigError
    for env in ({"NUMBER_OF_PACKS": "17"}, {"MQTT_TOPIC": "bms/#"}, {"MIN_CELL_VOLTAGE": "3.7"}, {"MQTT_PORT": "0"},
                {"NOMINAL_CELL_VOLTAGE": "0"}, {"NOMINAL_CELL_VOLTAGE": "x"}):
        with pytest.raises(ConfigError):
            api.config_from(env)
    assert "mqtt_password" not in api.config_from({}).loggable()
    assert api.config_from({}).nominal_cell_voltage == 3.2


def test_full_energy():
    from seplos_mqtt.decoders.telemetry import full_energy
    assert full_energy(280.0, 16, 3.2) == 14.34
    assert full_energy(3360, 16, 3.2) == 172.03
    assert full_energy(None, 16, 3.2) is None and full_energy(280.0, None, 3.2) is None


def test_health_decision():
    from seplos_mqtt import health
    assert health.max_poll_age(1, 1, False) == 60.0
    assert health.max_poll_age(4, 60, True) == (4 * 3 + 60 + 3) * 3 + 10
    assert health.is_healthy(True, True, 5, 30) and not health.is_healthy(True, True, 31, 30)
    assert not health.is_healthy(False, True, 5, 30) and not health.is_healthy(True, False, 5, 30)


# --- EMU1101-EXT stage 1 ------------------------------------------------------------------------------------

def _maint_responder(identity=None, storage=None, energy=None):
    identity = f.MAINT_FULL_IDENTITY if identity is None else identity
    storage = f.maint_storage() if storage is None else storage
    energy = f.maint_energy() if energy is None else energy

    def respond(req):
        payload = {b"\x03": identity, b"\x04": storage, b"\x06": energy}.get(req.info)
        return f.ascii_frame(req.adr, 0x00, payload) if payload is not None else f.ascii_frame(req.adr, 0x05, b"")
    return respond


def test_binary_frame_kb_literals():
    assert api.binary_encode(1, 0x47) == bytes.fromhex("550147 00A36BAA".replace(" ", ""))
    assert api.binary_encode(1, 0xA2) == bytes.fromhex("5501A2004C2CAA")
    assert api.binary_encode(1, 0xA4) == bytes.fromhex("5501A400E68AAA")
    assert api.binary_encode(11, 0xA4) == bytes.fromhex("550BA400214BAA")


def test_ext05_identity_decoded():
    ident = api.decode_maint_identity(f.MAINT_FULL_IDENTITY)
    assert len(f.MAINT_FULL_IDENTITY) == 21 and f.MAINT_FULL_IDENTITY[:4] == b"EXT\x05"
    assert (ident.info_format, ident.base_version, ident.patch_version) == (5, "16.06.13", "1.6.4")
    assert ident.build_id == "a8676b52bae281ff" and ident.image_flags == 0xFE and ident.capabilities == 0x000F
    assert ident.name == "EMU1101-EXT Full 1.6.4"
    light = api.decode_maint_identity(f.maint_identity(patch=(1, 5, 5), base=(16, 6, 0xFF), flags=f.EXT_LIGHT_FLAGS))
    assert (light.name, light.base_version, light.image_flags) == ("EMU1101-EXT Light 1.5.5", "16.06.?", 0x3A)
    assert api.decode_maint_identity(f.maint_identity(patch=(1, 7, 0))).name == "EMU1101-EXT 1.7.0"   # a later line


def test_unsupported_identities_not_decoded():
    import pytest
    rejected = {
        "unknown capability bit": f.maint_identity(caps=0x0020),
        "Light 1.5.3": f.maint_identity(patch=(1, 5, 3), flags=f.EXT_LIGHT_FLAGS),
        "Full 1.6.3": f.maint_identity(patch=(1, 6, 3)),
        "EXT format 06": f.maint_identity(info_format=6),
        "EXT05 too short": f.maint_identity()[:20],
        "EXT05 too long": f.maint_identity() + b"\x00",
        "MNT05": f.maint_identity(signature=b"MNT"),
        "MNT04": b"MNT\x04" + bytes([16, 6, 13, 1, 5, 2]) + bytes(8) + b"\x08",
        # TEMP(maint-1.6): only the one first MAINT 1.6 build, not another MNT format 3
        "other MNT03 build": b"MNT\x03" + bytes([16, 6, 13, 1, 6]) + bytes(8) + b"\x06",
    }
    for case, info in rejected.items():
        with pytest.raises(ValueError):
            api.decode_maint_identity(info)
            pytest.fail(case)


def test_unsupported_ext_version_gets_no_ext_reads(monkeypatch):
    """EXT05 below Light 1.5.4: unsupported patch firmware, the OEM reads stay, no EMU1101-EXT reads."""
    api.no_sleep(monkeypatch)
    with api.mode(1, MAINT_FIRMWARE=True):
        bms = _single_pack_bms()
        bms.ascii[(0, 0x64)] = _maint_responder(identity=f.maint_identity(patch=(1, 5, 3), flags=f.EXT_LIGHT_FLAGS))
        pack = api.make_pack(0)
        data, ok = api.poll(pack, bms)
    assert ok and "maint" not in data and api.capabilities(pack) == set()
    assert [r.info for r in bms.requests if r.cid2 == 0x64] == [b"\x03"]


def test_maint_single_pack(monkeypatch, golden):
    api.no_sleep(monkeypatch)
    with api.mode(1, MAINT_FIRMWARE=True):
        bms = _single_pack_bms()
        bms.ascii[(0, 0x42)] = f.telemetry_info(energy_charged=6500)    # 0x42 fallback 650.0
        bms.ascii[(0, 0x64)] = _maint_responder()
        pack = api.make_pack(0)
        data, ok = api.poll(pack, bms)
    assert ok
    t = data["telemetry"]["normal"]
    assert t["energy_charged"] == 7000.1 and t["energy_discharged"] == 6900.2   # full width from 64/06
    assert data["info"]["normal"]["firmware_patch"] == "16.06.13"               # reported, not probed
    assert not any(r.cid2 == 0xAA for r in bms.requests if r.kind == "ascii")
    assert data["maint"]["normal"]["maint_version"] == "EMU1101-EXT Full 1.6.4" and data["maint"]["binary"]["storage_problem"] == "OFF"
    assert data["maint"]["binary"]["maintenance_mode"] is None     # 64/02 not answered: unknown
    assert data["maint"]["attributes"]["checkpoint"]["boot_choice"] == "primary"
    assert api.capabilities(pack) == {"maint", "maint_storage", "maint_lock", "maint_marker", "maint_hard_stop"}
    golden("poll_maint_single_pack", data)


def test_maint_storage_problem_and_light_profile(monkeypatch):
    api.no_sleep(monkeypatch)
    with api.mode(1, MAINT_FIRMWARE=True):
        bms = _single_pack_bms()
        light = f.maint_identity(patch=(1, 5, 4), flags=f.MAINT_LIGHT_FLAGS)
        bms.ascii[(0, 0x64)] = _maint_responder(identity=light, storage=f.maint_storage(classes=(1, 2, 0, 1, 1, 1), locks=(0xFF, 0xFF), cache=0))
        data, ok = api.poll(api.make_pack(0), bms)
    assert ok and data["maint"]["binary"]["storage_problem"] == "ON"
    storage = data["maint"]["attributes"]["storage"]
    assert storage["parameters"] == {"primary": "valid", "backup": "invalid", "comparison": "not comparable"}
    assert storage["lock_recorded_primary"] is None and storage["lock_runtime"] is None   # 1.5.4: unavailable, not "unlocked"


def test_maint_oem_firmware_answers_rtn04(monkeypatch):
    api.no_sleep(monkeypatch)
    with api.mode(1, MAINT_FIRMWARE=True):
        bms = _single_pack_bms()
        bms.ascii[(0, 0x64)] = ("rtn", 0x04)
        pack = api.make_pack(0)
        data, ok = api.poll(pack, bms)
        api.poll(pack, bms)
    assert ok and "maint" not in data and api.capabilities(pack) == set()
    assert data["info"]["normal"]["firmware_patch"] == "16.06.13+"
    assert sum(1 for r in bms.requests if r.cid2 == 0x64) == 1     # detected once


def test_maint_silent_gives_up_after_three_polls(monkeypatch):
    api.no_sleep(monkeypatch)
    with api.mode(1, MAINT_FIRMWARE=True):
        bms = _single_pack_bms()
        pack = api.make_pack(0)
        for _ in range(5):
            data, ok = api.poll(pack, bms)
            assert ok
    assert sum(1 for r in bms.requests if r.cid2 == 0x64) == 6     # 3 polls x 2 attempts
    assert "maint" not in data


def test_maint_energy_failure_does_not_fall_back_to_oem(monkeypatch):
    api.no_sleep(monkeypatch)
    clock = api.fake_monotonic(monkeypatch)
    with api.mode(1, MAINT_FIRMWARE=True):
        bms = _single_pack_bms()
        bms.ascii[(0, 0x42)] = f.telemetry_info(energy_charged=6500)
        responder = _maint_responder()
        bms.ascii[(0, 0x64)] = responder
        pack = api.make_pack(0)
        api.poll(pack, bms)
        bms.ascii[(0, 0x64)] = lambda req: b"" if req.info == b"\x06" else responder(req)
        clock["t"] += 120
        data, _ = api.poll(pack, bms)
        assert data["telemetry"]["normal"]["energy_charged"] == 7000.1        # last wide value, still fresh
        clock["t"] += 700
        data, ok = api.poll(pack, bms)
    assert ok and data["telemetry"]["normal"]["energy_charged"] is None      # stale: unknown, never 650.0


def test_maint_multi_pack_mixed_bank(monkeypatch, golden):
    """UART1: pack 0 runs EMU1101-EXT (binary 47/A2/A4), pack 1 is OEM."""
    api.no_sleep(monkeypatch)
    with api.mode(2, MAINT_FIRMWARE=True):
        bms = _multi_pack_bms()
        bms.ascii[(0, 0x64)] = _maint_responder()
        bms.binary[(0, 0x47)] = f.parameter_info()[1:]
        bms.binary[(0, 0xA2)] = f.serial_info("BAT-SN-0")
        bms.binary[(0, 0xA4)] = f.serial_info("BMS-SN-0")
        bms.echo_binary = True
        master, slave = api.make_pack(0), api.make_pack(1)
        m, ok0 = api.poll(master, bms)
        s, ok1 = api.poll(slave, bms)
    assert ok0 and ok1
    assert m["parameters"]["cells_in_series"] == 16 and m["parameters"]["read_at"] == api.FIXED_NOW
    assert m["info"]["normal"]["serial_number"] == "BAT-SN-0" and m["info"]["normal"]["bms_serial_number"] == "BMS-SN-0"
    assert api.capabilities(master) == {"maint", "maint_storage", "maint_uart1", "maint_lock", "maint_marker", "maint_hard_stop",
                                        "maint_rtc"}
    assert "maint" not in s and "parameters" not in s and api.capabilities(slave) == set()
    golden("poll_maint_multi_pack", {"master": m, "slave": s})


def test_maint_read_errors_do_not_fail_poll(monkeypatch):
    api.no_sleep(monkeypatch)
    with api.mode(2, MAINT_FIRMWARE=True):
        bms = _multi_pack_bms()
        bms.ascii[(0, 0x64)] = _maint_responder(energy=bytes(68))      # unknown format
        bms.binary[(0, 0x47)] = ("rej", 0x81)
        data, ok = api.poll(api.make_pack(0), bms)
    assert ok and data["maint"]["normal"]["maint_version"] == "EMU1101-EXT Full 1.6.4" and "parameters" not in data


def test_discovery_publishes_maint_entities_per_capability(golden):
    disc, client = api.discovery()
    api.discovery_startup(disc, 2)
    client.published.clear()
    api.set_capabilities(disc, 0, {"maint", "maint_storage", "maint_uart1"}, pack_bus=False)
    records = client.as_records()
    configs = {r["topic"].split("/")[-2]: r["payload"] for r in records if r["topic"].split("/")[2] == "seplos-mqtt-pack-0"}
    for key in ("maint_version", "storage_problem", "parameters", "serial_number", "bms_serial_number"):
        assert configs[key], key
    assert configs["maint_version"]["json_attr_tpl"].startswith("{{ value_json.get('maint'")
    assert configs["balancing_active"] == ""          # 0x5A stays RS485-1/2 only
    assert not any("pack-1" in r["topic"] for r in records)
    golden("discovery_maint_capabilities", _discovery(records))


# --- review step 2 (T01-T07) --------------------------------------------------------------------------

def test_status_unknown_without_any_5a_after_restart(monkeypatch):
    """T01: no successful 0x5A in this process -> the status group is present with unknown values."""
    api.no_sleep(monkeypatch)
    with api.mode(1):
        bms = _single_pack_bms()
        bms.ascii[(0, 0x5A)] = None
        data, ok = api.poll(api.make_pack(0), bms)
    assert ok and data["status"]["binary"]["charge_forbidden"] is None
    assert data["status"]["normal"] == {"dis_charge_current_unfiltered": None}


def test_inverted_templates_keep_null_unknown():
    """T01: inverted current/power templates must not turn null into a template error."""
    disc, client = api.discovery(invert=True)
    api.discovery_startup(disc, 1)
    tpl = next(r["payload"]["val_tpl"] for r in client.as_records()
               if r["topic"].endswith("/dis_charge_current_unfiltered/config"))
    assert tpl == ("{{ none if value_json.status.normal.dis_charge_current_unfiltered is none "
                   "else (value_json.status.normal.dis_charge_current_unfiltered | float) * -1 }}")
    jinja2 = __import__("pytest").importorskip("jinja2")
    render = jinja2.Environment().from_string(tpl).render
    assert render(value_json={"status": {"normal": {"dis_charge_current_unfiltered": None}}}) == "None"
    assert render(value_json={"status": {"normal": {"dis_charge_current_unfiltered": 1.5}}}) == "-1.5"


def test_force_charge_flags_unknown_after_failure(monkeypatch):
    """T02: flags become unknown when 0x0074 stops answering; a busy exception doesn't disable them."""
    api.no_sleep(monkeypatch)
    bms = FakeBms()
    bms.modbus[(0x03, 0x1000, 21)] = f.system_registers()
    bms.modbus[(0x03, 0x0074, 1)] = ("raw", f.modbus_wide_count_response(1, 3, 0x20))
    reader, read = api.system_reader(bms)
    assert read()["binary"]["force_charge_1"] == "ON"
    bms.modbus[(0x03, 0x0074, 1)] = None
    assert read()["binary"]["force_charge_1"] is None
    bms.modbus[(0x03, 0x0074, 1)] = ("exc", 0x06)    # busy
    for _ in range(4):
        read()
    assert not reader.flags_unsupported
    bms.modbus[(0x03, 0x0074, 1)] = ("exc", 0x02)    # illegal data address
    for _ in range(3):
        read()
    assert reader.flags_unsupported


def test_system_busy_exception_does_not_disable(monkeypatch):
    api.no_sleep(monkeypatch)
    bms = FakeBms()
    bms.modbus[(0x03, 0x1000, 21)] = ("exc", 0x06)
    reader, read = api.system_reader(bms)
    for _ in range(4):
        assert read() is None
    assert not reader.unsupported


def test_cell_count_validation(monkeypatch):
    """T03: 7-16 cells, count markers, 0x42/0x44 must agree."""
    api.no_sleep(monkeypatch)
    with api.mode(1):
        cases = {
            "six cells": (f.telemetry_info(cells_mv=[3300] * 6), f.telesignal_info(cell_alarms=[0] * 6)),
            "42/44 disagree": (f.telemetry_info(), f.telesignal_info(cell_alarms=[0] * 15)),
        }
        bad_temp = bytearray(f.telemetry_info()); bad_temp[35] = 0          # noqa: E702
        bad_custom = bytearray(f.telemetry_info()); bad_custom[54] = 0      # noqa: E702
        cases["temperature count"] = (bytes(bad_temp), f.telesignal_info())
        cases["custom count"] = (bytes(bad_custom), f.telesignal_info())
        for name, (t, s) in cases.items():
            bms = _single_pack_bms()
            bms.ascii[(0, 0x42)], bms.ascii[(0, 0x44)] = t, s
            assert not api.poll(api.make_pack(0), bms)[1], name
        for m in (7, 16):
            bms = _single_pack_bms()
            bms.ascii[(0, 0x42)] = f.telemetry_info(cells_mv=[3300 + i for i in range(m)])
            bms.ascii[(0, 0x44)] = f.telesignal_info(cell_alarms=[0] * m)
            assert api.poll(api.make_pack(0), bms)[1], m


def test_cell_entities_grow_shrink_replay():
    """T04: per-cell entities follow the count in both directions and on a full republish."""
    disc, client = api.discovery()
    api.discovery_startup(disc, 1)

    def last(topic_key):
        recs = [r for r in client.as_records() if r["topic"] == f"homeassistant/sensor/seplos-mqtt-pack-0/{topic_key}/config"]
        return recs[-1]["payload"]
    api.cell_count(disc, 0, 15)
    assert last("voltage_cell_16") == ""
    api.cell_count(disc, 0, 16)
    assert last("voltage_cell_16")["name"] == "Voltage Cell 16"
    api.cell_count(disc, 0, 14)
    api.discovery_startup(disc, 1)                      # replay after the count is known
    assert last("voltage_cell_15") == "" and last("voltage_cell_14")["name"] == "Voltage Cell 14"


def test_state_file_invalid_content(tmp_path):
    """T07: an invalid cache degrades to 'no history' instead of failing the poll."""
    for content in ("[]", '{"pack0.energy_charged": "bad"}', '{"pack0.energy_charged": true}', "not json"):
        path = tmp_path / "state.json"
        path.write_text(content)
        out = api.decode_telemetry_sequence([f.telemetry_info(energy_charged=6500)], state_file=path)
        assert out[0]["normal"]["energy_charged"] is None, content


def test_energy_reset_sequences():
    def run(raws):
        return [o["normal"]["energy_charged"] for o in api.decode_telemetry_sequence([f.telemetry_info(energy_charged=r) for r in raws])]
    assert run([64999, 6500]) == [6499.9, 6499.9]
    assert run([6499, 6500, 6501]) == [649.9, 650.0, 650.1]
    assert run([64999, 0, 6500]) == [6499.9, 0.0, 650.0]      # observed reset (clear history) resets the baseline
    assert run([64999, 0, 1]) == [6499.9, 0.0, 0.1]


def test_silence_after_skipped_traffic_is_inconclusive(monkeypatch):
    """F16 detail: interleaved intra-pack traffic followed by nothing is not 'silence'."""
    api.no_sleep(monkeypatch)
    with api.mode(1):
        bms = _single_pack_bms()
        bms.ascii[(0, 0xAA)] = lambda req: f.ascii_frame(3, 0x5A, b"")
        data, _ = api.poll(api.make_pack(0), bms)
    assert data["info"]["normal"]["firmware_patch"] == "16.06 (unknown)"


# --- EMU1101-EXT stage 2 ------------------------------------------------------------------------------------

def test_maint_group0_settings():
    full = api.decode_maint_identity(f.MAINT_FULL_IDENTITY)                  # flags FE: lock, positive, hard stop config
    d = api.decode_group0(0xFF, full)
    assert d["uart1_power_rtc_allowed"] is True and d["extended_off_enabled"] is True
    assert d["pack_lock_recorded"] is False                                    # bit 5 = 1: no lock
    assert (d["recharge_hard_stop"], d["recharge_hard_stop_source"]) == (False, "image default")   # FE: default soft
    assert api.decode_group0(0x7F, full)["recharge_hard_stop"] is True          # explicit hard
    assert api.decode_group0(0x3F, full)["recharge_hard_stop_source"] == "parameter"
    assert api.decode_group0(0xDF, full)["pack_lock_recorded"] is True          # bit 5 = 0
    light = api.decode_maint_identity(f.maint_identity(patch=(1, 5, 4), flags=f.MAINT_LIGHT_FLAGS))
    d = api.decode_group0(0xFF, light)
    assert "pack_lock_recorded" not in d and d["recharge_hard_stop_source"] == "image"


def test_maint_marker_decoder():
    assert api.decode_marker(bytes.fromhex("FFFFFFFF03"))["maintenance_active"] is False
    assert api.decode_marker(bytes.fromhex("0000000000")) == {"maintenance_active": True, "marker": "0x00000000", "effective_slave_count": 0}
    assert api.decode_marker(bytes.fromhex("12345678FF"))["maintenance_active"] is None   # unresolved, never "active"


def test_maint_stage2_master(monkeypatch, golden):
    """UART1 master with EMU1101-EXT Full 1.6.4: lock state, maintenance marker, recharge stop mode, calibration, clock."""
    api.no_sleep(monkeypatch)
    responder = _maint_responder(storage=f.maint_storage(locks=(1, 1), cache=0x07))
    calibration = bytes([1, 0, 0x08, 16]) + bytes(60) + (250).to_bytes(4, "big", signed=True) + (-12).to_bytes(4, "big", signed=True)

    def respond(req):
        if req.info == b"\x02":
            return f.ascii_frame(req.adr, 0, bytes.fromhex("0000000000"))
        if req.info == b"\x05\x00":
            return f.ascii_frame(req.adr, 0, calibration)
        return responder(req)
    with api.mode(2, MAINT_FIRMWARE=True):
        bms = _multi_pack_bms()
        bms.ascii[(0, 0x64)] = respond
        bms.ascii[(0, 0x4D)] = bytes.fromhex("07EA0B040C0000")
        bms.binary[(0, 0x47)] = f.parameter_info(switches=bytes([0x5F, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x0F, 0x80]))[1:]
        bms.binary[(0, 0xA2)] = f.serial_info("BAT-SN-0")
        bms.binary[(0, 0xA4)] = f.serial_info("BMS-SN-0")
        pack = api.make_pack(0)
        api.poll(pack, bms)
        data, ok = api.poll(pack, bms)
    assert ok
    maint = data["maint"]
    assert maint["binary"] == {"storage_problem": "OFF", "pack_lock_active": "ON", "maintenance_mode": "ON"}
    assert maint["normal"]["effective_slave_count"] == 0 and maint["normal"]["recharge_stop_mode"] == "hard stop"
    assert maint["attributes"]["settings"]["pack_lock_recorded"] is True
    assert maint["attributes"]["calibration"] == {"invalid_calibration_flags": "0x08", "current_a_ma": 250, "current_b_ma": -12,
                                                  "read_at": api.FIXED_NOW}
    assert data["clock"]["normal"] == {"bms_time": "2026-11-04T12:00:00", "clock_offset": 0}
    assert {"maint_lock", "maint_marker", "maint_hard_stop"} <= api.capabilities(pack)
    golden("poll_maint_stage2_master", data)


def test_maint_slave_has_no_marker_on_uart1(monkeypatch):
    api.no_sleep(monkeypatch)
    with api.mode(2, MAINT_FIRMWARE=True):
        bms = _multi_pack_bms()
        bms.ascii[(1, 0x64)] = _maint_responder()
        data, ok = api.poll(api.make_pack(1), bms)
    assert ok and "maintenance_mode" not in data["maint"]["binary"]
    assert not any(r.cid2 == 0x64 and r.info == b"\x02" for r in bms.requests)


def test_oem_multi_pack_has_no_clock(monkeypatch):
    """OEM UART1 doesn't serve 0x4D: no clock request, no clock group."""
    api.no_sleep(monkeypatch)
    with api.mode(2):
        bms = _multi_pack_bms()
        data, ok = api.poll(api.make_pack(0), bms)
    assert ok and "clock" not in data and not any(r.cid2 == 0x4D for r in bms.requests)


# --- review step 3 + EMU1101-EXT 1 (M01-M09) ----------------------------------------------------------------

def test_wide_energy_source_survives_restart(monkeypatch, tmp_path):
    """M01: after EMU1101-EXT supplied the totals, a restart with failing identity yields unknown, not the 0x42 value."""
    api.no_sleep(monkeypatch)
    state_file = tmp_path / "state.json"
    with api.mode(1, MAINT_FIRMWARE=True):
        bms = _single_pack_bms()
        bms.ascii[(0, 0x42)] = f.telemetry_info(energy_charged=6500, energy_discharged=6500)
        bms.ascii[(0, 0x64)] = _maint_responder()
        data, _ = api.poll(api.make_pack(0, state_file=state_file), bms)
        assert data["telemetry"]["normal"]["energy_charged"] == 7000.1
        bms.ascii[(0, 0x64)] = None                                    # identity silent after the restart
        restarted = api.make_pack(0, state_file=state_file)
        for _ in range(4):
            data, ok = api.poll(restarted, bms)
            assert ok and data["telemetry"]["normal"]["energy_charged"] is None
            assert data["telemetry"]["normal"]["energy_discharged"] is None
        bms.ascii[(0, 0x64)] = ("rtn", 0x04)                          # confirmed OEM (pack replaced)
        data, _ = api.poll(api.make_pack(0, state_file=state_file), bms)
    assert data["telemetry"]["normal"]["energy_charged"] == 650.0 or data["telemetry"]["normal"]["energy_charged"] is None


def test_quiet_gap_restarts_on_received_noise():
    """M02: bytes arriving during the wait are discarded and restart the quiet timer."""
    events = api.quiet_gap_trace(noise_at=0.99)
    sends = [t for kind, t in events if kind == "write"]
    noise = [t for kind, t in events if kind == "noise"]
    assert noise and sends[-1] - noise[-1] >= 1.0 - 1e-9, events


def test_bus_busy_when_quiet_never_comes():
    import pytest
    with pytest.raises(api.BusBusy):
        api.quiet_gap_trace(noise_forever=True)


def test_failed_offline_publish_is_retried(monkeypatch):
    """M03: an availability change only counts as sent after the client accepted it."""
    state, pack_state, client = api.availability_state(monkeypatch)
    client.fail_next = 1
    api.publish_availability(state, pack_state, now=1000.0)          # pack aged out, publish fails
    assert pack_state.availability == "online"
    api.publish_availability(state, pack_state, now=1001.0)
    assert pack_state.availability == "offline"
    assert client.as_records()[-1] == {"topic": "seplos/pack-0/availability", "payload": "offline", "qos": 0, "retain": False}


def test_health_follows_core_progress_during_slow_first_poll(monkeypatch):
    """M04: core 0x42/0x44 success counts for health even while optional reads are still running."""
    assert api.health_progress_during_slow_poll(monkeypatch) is True


def test_discovery_keeps_maint_entities_while_detection_pending():
    """M05: unknown capabilities neither create nor delete EMU1101-EXT-dependent configs."""
    disc, client = api.discovery(maint_enabled=True)
    api.discovery_startup(disc, 2)
    topics = {r["topic"].split("/")[-2] for r in client.as_records() if "seplos-mqtt-pack-0" in r["topic"]}
    for key in ("maint_version", "storage_problem", "parameters", "serial_number"):
        assert key not in topics, key
    assert "balancing_active" in topics                                  # RS485-1/2-only entity is still removed
    client.published.clear()
    assert api.set_capabilities(disc, 0, set(), pack_bus=False)         # confirmed OEM: now removed
    removed = {r["topic"].split("/")[-2] for r in client.as_records() if r["payload"] == ""}
    assert {"maint_version", "parameters", "serial_number"} <= removed


def test_strict_parser_rejects_whitespace_and_short_headers():
    """M06."""
    import pytest
    for body in (b"20004600    ", b"20      0000"):
        with pytest.raises(api.FrameError):
            api.parse_frame(api.with_checksum(b"~" + body + b"0000\r"))


def test_maint_transient_rtn_retried_and_late_identity_refreshes_firmware(monkeypatch):
    """M08: RTN 02 is not 'no EMU1101-EXT'; a late identity replaces the heuristic firmware patch."""
    api.no_sleep(monkeypatch)
    with api.mode(1, MAINT_FIRMWARE=True):
        bms = _single_pack_bms()
        bms.ascii[(0, 0x64)] = ("rtn", 0x02)
        pack = api.make_pack(0)
        data, _ = api.poll(pack, bms)
        assert data["info"]["normal"]["firmware_patch"] == "16.06.13+" and pack.maint.state == "pending"
        revision = pack.info_revision
        bms.ascii[(0, 0x64)] = _maint_responder()
        data, _ = api.poll(pack, bms)
    assert pack.maint.state == "present" and data["info"]["normal"]["firmware_patch"] == "16.06.13"
    assert pack.info_revision > revision


def test_modbus_busy_is_not_feature_rejection(monkeypatch):
    """M08: a busy exception during the CAN-port patch probe is inconclusive, not 'older firmware'."""
    api.no_sleep(monkeypatch)
    with api.mode(2):
        bms = _multi_pack_bms()
        bms.modbus[(0x03, 0x001F, 9)] = ("exc", 0x06)
        bms.modbus[(0x03, 0x9000, 21)] = ("exc", 0x06)
        data, _ = api.poll(api.make_pack(0), bms)
    assert data["info"]["normal"]["firmware_patch"] == "16.06.12+"


def test_storage_lock_codes_validated():
    """M09: only 00/01/FF are valid recorded lock codes."""
    import pytest
    with pytest.raises(ValueError):
        api.decode_storage(f.maint_storage(locks=(2, 0)))
    with pytest.raises(ValueError):
        api.decode_storage(f.maint_storage(cache=0x08))


def test_config_rejects_malformed_values():
    import pytest

    from seplos_mqtt.config import ConfigError
    for env in ({"NUMBER_OF_PACKS": "two"}, {"ENABLE_HA_DISCOVERY_CONFIG": "maybe"}):
        with pytest.raises(ConfigError):
            api.config_from(env)
    assert api.config_from({"MQTT_TOPIC": "/seplos"}).mqtt_topic == "/seplos"     # leading slash stays legal


# --- final review (L01-L08) ---------------------------------------------------------------------------

def test_energy_source_on_disk_before_first_wide_value(monkeypatch, tmp_path):
    """L01: ordinary OEM totals write the store first; the source marker must still reach the disk at once."""
    api.no_sleep(monkeypatch)
    import json
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"pack0.energy_charged": 6499.9, "pack0.energy_discharged": 1234.5}))
    with api.mode(1, MAINT_FIRMWARE=True):
        bms = _single_pack_bms()
        bms.ascii[(0, 0x42)] = f.telemetry_info(energy_charged=6500, energy_discharged=12346)
        bms.ascii[(0, 0x64)] = _maint_responder(energy=f.maint_energy(charged=70001, discharged=12346))
        data, _ = api.poll(api.make_pack(0, state_file=state_file), bms)
        assert data["telemetry"]["normal"]["energy_charged"] == 7000.1
        assert json.loads(state_file.read_text())["pack0.energy_source"] == "maint"     # no graceful flush needed
        bms.ascii[(0, 0x64)] = None
        data, _ = api.poll(api.make_pack(0, state_file=state_file), bms)                # unclean restart
    assert data["telemetry"]["normal"]["energy_charged"] is None


def test_unsupported_identity_keeps_wide_source(monkeypatch, tmp_path):
    """L02: an unknown patch identity format or a non-empty RTN 04 is not 'OEM'."""
    api.no_sleep(monkeypatch)
    state_file = tmp_path / "state.json"
    with api.mode(1, MAINT_FIRMWARE=True):
        bms = _single_pack_bms()
        bms.ascii[(0, 0x42)] = f.telemetry_info(energy_charged=6500)
        bms.ascii[(0, 0x64)] = _maint_responder()
        api.poll(api.make_pack(0, state_file=state_file), bms)
        for answer in (_maint_responder(identity=f.maint_identity(caps=0x0020)),
                       lambda req: f.ascii_frame(0, 0x04, b"\x01")):
            bms.ascii[(0, 0x64)] = answer
            pack = api.make_pack(0, state_file=state_file)
            for _ in range(4):
                data, _ = api.poll(pack, bms)
            assert pack.maint.state in ("unsupported", "unknown") and not api.capabilities_resolved(pack)
            assert data["telemetry"]["normal"]["energy_charged"] is None


def test_poll_waits_full_gap_after_buffered_traffic():
    """L03: buffered foreign bytes at the start of a poll restart the quiet gap (no flush bypass)."""
    events = api.poll_after_idle_with_buffered_byte()
    first_write = next(t for kind, t in events if kind == "write")
    assert first_write - 9.999 >= 1.0 - 1e-9, events


def test_parameters_read_right_after_host_boot(monkeypatch):
    """L04: the first 0x47 read is due immediately, also when monotonic time is small."""
    api.no_sleep(monkeypatch)
    import time
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
    with api.mode(1):
        data, ok = api.poll(api.make_pack(0), _single_pack_bms())
    assert ok and "parameters" in data


def test_failed_online_publish_not_cached(monkeypatch):
    """L05."""
    state, pack_state, client = api.availability_state(monkeypatch)
    pack_state.availability = "offline"
    pack_state.pack.read = lambda: ({"telemetry": {}}, True)
    client.fail_next = 4                           # heartbeat, online, sensors and the availability retry rejected
    api.poll_pack(state, pack_state)
    assert pack_state.availability == "offline"


def test_bank_startup_keeps_successful_packs_online():
    """L06: a long first cycle (slow optional reads of other packs) doesn't age out successful packs."""
    offline = api.simulated_bank_cycle(packs=16, seconds_per_pack=19.5)
    assert offline == [], offline


def test_uart1_clock_needs_power_rtc_flag(monkeypatch):
    """L07: an EXT05 image without flag 0x02 -> no UART1 4D request, no clock entity."""
    api.no_sleep(monkeypatch)
    with api.mode(2, MAINT_FIRMWARE=True):
        bms = _multi_pack_bms()
        no_power = f.maint_identity(patch=(1, 5, 5), flags=f.EXT_LIGHT_FLAGS & ~0x02)
        bms.ascii[(0, 0x64)] = _maint_responder(identity=no_power)
        pack = api.make_pack(0)
        data, ok = api.poll(pack, bms)
    assert ok and "clock" not in data and "maint_rtc" not in api.capabilities(pack)
    assert not any(r.cid2 == 0x4D for r in bms.requests)


def test_failed_discovery_publish_is_reconciled(monkeypatch):
    """L08: a config the client rejected is published again by a later reconciliation."""
    state, disc, client = api.app_with_discovery(monkeypatch)
    client.fail_next = 1
    api.set_capabilities(disc, 0, {"maint"}, pack_bus=True)
    assert disc.failed
    api.retry_discovery(state)
    assert any(r["topic"].endswith("/maint_version/config") and r["payload"] for r in client.as_records())
    assert not disc.failed


def test_energy_source_write_failure_retried_and_unknown(monkeypatch, tmp_path):
    """C01: until the source marker is on disk the energy is unknown; the write is retried unchanged."""
    api.no_sleep(monkeypatch)
    import json
    import os
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"pack0.energy_charged": 6499.9, "pack0.energy_discharged": 1234.5}))
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:                    # the source marker write
            raise OSError("disk full")
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", flaky_replace)
    with api.mode(1, MAINT_FIRMWARE=True):
        bms = _single_pack_bms()
        bms.ascii[(0, 0x42)] = f.telemetry_info(energy_charged=6500, energy_discharged=12346)
        bms.ascii[(0, 0x64)] = _maint_responder(energy=f.maint_energy(charged=70001, discharged=12346))
        pack = api.make_pack(0, state_file=state_file)
        data, ok = api.poll(pack, bms)
        assert ok and data["telemetry"]["normal"]["energy_charged"] is None          # not on disk yet
        assert "pack0.energy_source" not in json.loads(state_file.read_text())
        data, ok = api.poll(pack, bms)                                               # unchanged values: retried
        assert data["telemetry"]["normal"]["energy_charged"] == 7000.1
        assert json.loads(state_file.read_text())["pack0.energy_source"] == "maint"


def test_durable_marker_unaffected_by_throttled_counters(tmp_path):
    """C01: a throttled ordinary update doesn't make an already durable marker 'failed'."""
    store = api.state_store(tmp_path / "state.json")
    assert store.set_durable("pack0.energy_source", "maint")
    store.set("pack0.energy_charged", 1.0)
    store.set("pack0.energy_charged", 2.0)        # throttled, store dirty
    assert store.set_durable("pack0.energy_source", "maint")


# --- TEMP(maint-1.6): the first MAINT 1.6 (MNT format 3, 18 bytes, inverse polarity) ----------------------

MNT03_LIVE = bytes.fromhex("4d4e540310060d010618daf61d8a93671d06")     # TEMP(maint-1.6): build 18daf61d8a93671d, flags 06


def test_mnt03_identity_live_frame():
    """TEMP(maint-1.6)"""
    ident = api.decode_maint_identity(MNT03_LIVE)
    assert len(MNT03_LIVE) == 18 and ident.name == "MAINT 1.6"
    assert (ident.info_format, ident.base_version, ident.patch_version) == (3, "16.06.13", "1.6")
    assert ident.build_id == "18daf61d8a93671d" and ident.image_flags == 0x06 and ident.capabilities == 0


def test_inverse_polarity_group0():
    """TEMP(maint-1.6)"""
    ident = api.decode_maint_identity(MNT03_LIVE)
    d = api.decode_group0(0xFF, ident)            # old FF parameters: power denied, extended off disabled, unlocked
    assert d["uart1_power_rtc_allowed"] is False and d["extended_off_enabled"] is False and d["pack_lock_recorded"] is False
    d = api.decode_group0(0xC7, ident)            # bits 3/4/5 = 0
    assert d["uart1_power_rtc_allowed"] is True and d["extended_off_enabled"] is True and d["pack_lock_recorded"] is True


def test_maint16_bank_master_and_slave(monkeypatch, golden):
    """TEMP(maint-1.6): identity, marker (master only), clock, binary 47/A2/A4, no 64/04/06."""
    api.no_sleep(monkeypatch)

    def respond(req):
        if req.info == b"\x03":
            return f.ascii_frame(req.adr, 0, MNT03_LIVE)
        if req.info == b"\x02" and req.adr == 0:
            return f.ascii_frame(req.adr, 0, bytes.fromhex("ffffffff0b"))
        return f.ascii_frame(req.adr, 0x05, b"") if req.adr == 0 else b""
    with api.mode(2, MAINT_FIRMWARE=True):
        bms = _multi_pack_bms()
        for adr, clock, bms_sn in ((0, "07ea0b0415172e", "SEPLOS-0000000001"), (1, "07ea0b04150d2f", "SEPLOS-0000000002")):
            bms.ascii[(adr, 0x64)] = respond
            bms.ascii[(adr, 0x4D)] = bytes.fromhex(clock)
            group0 = 0xDF if adr else 0xFF                     # slave: bit 5 = 0, individual lock active
            bms.binary[(adr, 0x47)] = f.parameter_info(switches=bytes([group0, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x0F, 0x80]))[1:]
            bms.binary[(adr, 0xA2)] = b"EMU11011OS-XXXX-20220120-00001"
            bms.binary[(adr, 0xA4)] = bms_sn.encode().ljust(30)
        master, slave = api.make_pack(0), api.make_pack(1)
        m, _ = api.poll(master, bms)
        s, _ = api.poll(slave, bms)
    assert m["maint"]["normal"]["maint_version"] == "MAINT 1.6" and m["info"]["normal"]["firmware_patch"] == "16.06.13"
    assert m["maint"]["binary"]["maintenance_mode"] == "OFF" and m["maint"]["normal"]["effective_slave_count"] == 11
    assert m["maint"]["binary"]["pack_lock_active"] == "OFF" and s["maint"]["binary"]["pack_lock_active"] == "ON"
    assert "maintenance_mode" not in s["maint"]["binary"] and "storage_problem" not in m["maint"]["binary"]
    assert m["clock"]["normal"]["bms_time"] == "2026-11-04T21:23:46" and m["clock"]["normal"]["clock_offset"] == 33826
    assert s["info"]["normal"]["bms_serial_number"] == "SEPLOS-0000000002"
    assert m["maint"]["normal"]["recharge_stop_mode"] == "soft stop"
    assert api.capabilities(master) == {"maint", "maint_uart1", "maint_lock", "maint_marker", "maint_hard_stop", "maint_rtc"}
    assert not any(r.cid2 == 0x64 and r.info in (b"\x04", b"\x06") for r in bms.requests)
    golden("poll_maint16_bank", {"master": m, "slave": s})


def test_single_pack_on_uart1_polls_like_the_can_socket(monkeypatch):
    api.no_sleep(monkeypatch)
    with api.mode(1, BUS="uart1"):
        assert api.config().baudrate == 9600
        bms = _multi_pack_bms()
        data, ok = api.poll(api.make_pack(0), bms)
        assert ok and "status" not in data and "parameters" not in data
        cids = {r.cid2 for r in bms.requests if r.kind == "ascii"}
        assert 0x5A not in cids and 0x47 not in cids and 0xA2 not in cids


def test_single_pack_on_uart1_discovery_has_the_system_device():
    disc, client = api.discovery()
    with api.mode(1, BUS="uart1"):
        api.discovery_startup(disc, 1)
    assert disc._system_active


def test_health_age_follows_bus_quiet():
    from seplos_mqtt import health
    assert health.max_poll_age(12, 0, True, 2.0) > health.max_poll_age(12, 0, True, 1.0)
    assert health.max_poll_age(4, 60, True, 1.0) == (4 * 3 + 60 + 3) * 3 + 10


# --- EMU1101-EXT Full 1.6.7: CCL decrease step (64/07, read-only) -----------------------------------------------

EXT_READS = {b"\x02", b"\x03", b"\x04", b"\x05\x00", b"\x06", b"\x07"}     # 64/08 (write) is never sent
FULL_167 = f.maint_identity(patch=(1, 6, 7), caps=0x001F)


def _ccl_responder(identity=FULL_167, step=None):
    """64/03-06 like _maint_responder; 64/07 answers `step` (INFO bytes, a callable or None for silence)."""
    base = _maint_responder(identity=identity)
    step = f.maint_ccl_step() if step is None else step

    def respond(req):
        if req.info == b"\x07":
            if callable(step):
                return step(req)
            return f.ascii_frame(req.adr, 0x00, step) if step != b"silent" else b""
        return base(req)
    return respond


def _ccl_requests(bms, adr=None):
    return [r for r in bms.requests if r.cid2 == 0x64 and r.info == b"\x07" and (adr is None or r.adr == adr)]


def _no_ext_write(bms):
    assert {r.info for r in bms.requests if r.kind == "ascii" and r.cid2 == 0x64} <= EXT_READS
    assert not any(r.kind == "ascii" and r.cid2 in (0xA1, 0xA3, 0xA5, 0x45, 0x4E) for r in bms.requests)


def test_ext_current_builds_decoded():
    """Full 1.6.7 (caps 001F, CCL configuration), Full 1.6.6 and Light 1.5.6 (caps 000F)."""
    full7 = api.decode_maint_identity(FULL_167)
    full6 = api.decode_maint_identity(f.maint_identity(patch=(1, 6, 6)))
    light6 = api.decode_maint_identity(f.maint_identity(patch=(1, 5, 6), flags=f.EXT_LIGHT_FLAGS))
    assert (full7.name, full7.capabilities, full7.image_flags) == ("EMU1101-EXT Full 1.6.7", 0x001F, 0xFE)
    assert (full6.name, full6.capabilities) == ("EMU1101-EXT Full 1.6.6", 0x000F)
    assert (light6.name, light6.capabilities, light6.image_flags) == ("EMU1101-EXT Light 1.5.6", 0x000F, 0x3A)
    assert full7.has_capability(0x0010) and not full6.has_capability(0x0010) and not light6.has_capability(0x0010)
    assert full7.as_dict()["capabilities"] == "0x001F"


def test_ccl_step_decoder_is_strict():
    import pytest
    for step in (1, 10, 20):
        assert api.decode_ccl_step(f.maint_ccl_step(step))["decrease_step"] == step
    assert api.decode_ccl_step(f.maint_ccl_step(10)) == {"decrease_step": 10, "increase_step": 1, "min": 1, "max": 20,
                                                            "unit": "A per online pack per update"}
    bad = [f.maint_ccl_step(0), f.maint_ccl_step(21), f.maint_ccl_step(0xFF), f.maint_ccl_step(fmt=2),
           f.maint_ccl_step(up=2), f.maint_ccl_step(low=0), f.maint_ccl_step(high=0x13), f.maint_ccl_step()[:4],
           f.maint_ccl_step() + b"\x00", b""]
    for info in bad:
        with pytest.raises(ValueError):
            api.decode_ccl_step(info)
            pytest.fail(info.hex())


def test_ccl_step_read_from_the_master_single_pack(monkeypatch):
    api.no_sleep(monkeypatch)
    with api.mode(1, MAINT_FIRMWARE=True):
        bms = _single_pack_bms()
        bms.ascii[(0, 0x64)] = _ccl_responder()
        pack = api.make_pack(0)
        data, ok = api.poll(pack, bms)
    assert ok
    [request] = _ccl_requests(bms)
    assert request.raw == b"~20004664E00207FD2C\r"          # VER 20, ADR 00, CID1 46, CID2 64, LENGTH E002, INFO 07
    assert data["maint"]["normal"]["ccl_decrease_step"] == 10
    assert data["maint"]["attributes"]["ccl_step"] == {"decrease_step": 10, "increase_step": 1, "min": 1, "max": 20,
                                                       "unit": "A per online pack per update", "read_at": api.FIXED_NOW}
    assert "maint_ccl_step" in api.capabilities(pack)
    _no_ext_write(bms)


def test_ccl_step_only_on_the_master_of_a_bank(monkeypatch):
    api.no_sleep(monkeypatch)
    with api.mode(2, MAINT_FIRMWARE=True):
        bms = _multi_pack_bms()
        for adr in (0, 1):
            bms.ascii[(adr, 0x64)] = _ccl_responder(step=f.maint_ccl_step(3))
        master, slave = api.make_pack(0), api.make_pack(1)
        m, ok0 = api.poll(master, bms)
        s, ok1 = api.poll(slave, bms)
    assert ok0 and ok1
    assert m["maint"]["normal"]["ccl_decrease_step"] == 3 and "maint_ccl_step" in api.capabilities(master)
    assert "ccl_decrease_step" not in s["maint"]["normal"] and "ccl_step" not in s["maint"]["attributes"]
    assert "maint_ccl_step" not in api.capabilities(slave)
    assert len(_ccl_requests(bms, 0)) == 1 and _ccl_requests(bms, 1) == []
    _no_ext_write(bms)


def test_no_ccl_step_without_the_capability(monkeypatch):
    api.no_sleep(monkeypatch)
    for identity in (f.maint_identity(patch=(1, 6, 6)), f.maint_identity(patch=(1, 5, 6), flags=f.EXT_LIGHT_FLAGS)):
        with api.mode(1, MAINT_FIRMWARE=True):
            bms = _single_pack_bms()
            bms.ascii[(0, 0x64)] = _ccl_responder(identity=identity)
            pack = api.make_pack(0)
            data, ok = api.poll(pack, bms)
        assert ok and data["maint"]["normal"]["maint_version"].startswith("EMU1101-EXT")
        assert "ccl_decrease_step" not in data["maint"]["normal"] and "maint_ccl_step" not in api.capabilities(pack)
        assert _ccl_requests(bms) == []


def test_invalid_ccl_step_answers_are_unavailable(monkeypatch):
    """RTN errors and wrong shapes never show a value (not the default 1) and never fail the poll."""
    api.no_sleep(monkeypatch)
    answers = {
        "RTN 05": lambda req: f.ascii_frame(req.adr, 0x05, b""),
        "RTN 04": lambda req: f.ascii_frame(req.adr, 0x04, b""),
        "RTN 00 empty": lambda req: f.ascii_frame(req.adr, 0x00, b""),
        "step 0": f.maint_ccl_step(0),
        "step 21": f.maint_ccl_step(21),
        "four bytes": f.maint_ccl_step()[:4],
        "other format": f.maint_ccl_step(fmt=2),
        "foreign address": lambda req: f.ascii_frame(5, 0x00, f.maint_ccl_step()),
        "bad checksum": lambda req: f.ascii_frame(req.adr, 0x00, f.maint_ccl_step())[:-2] + b"0\r",
        "silent": b"silent",
    }
    for case, answer in answers.items():
        with api.mode(1, MAINT_FIRMWARE=True):
            bms = _single_pack_bms()
            bms.ascii[(0, 0x64)] = _ccl_responder(step=answer)
            pack = api.make_pack(0)
            data, ok = api.poll(pack, bms)
        assert ok, case
        assert data["maint"]["normal"]["ccl_decrease_step"] is None, case
        assert "ccl_step" not in data["maint"]["attributes"], case
        assert "maint_ccl_step" in api.capabilities(pack), case       # the entity stays, unavailable
        _no_ext_write(bms)


def test_ccl_step_interval_staleness_and_definitive_errors(monkeypatch):
    api.no_sleep(monkeypatch)
    clock = api.fake_monotonic(monkeypatch)
    answer = {"step": f.maint_ccl_step(5)}
    with api.mode(1, MAINT_FIRMWARE=True):
        bms = _single_pack_bms()
        bms.ascii[(0, 0x64)] = _ccl_responder(step=lambda req: answer["step"](req) if callable(answer["step"])
                                              else (f.ascii_frame(req.adr, 0, answer["step"]) if answer["step"] else b""))
        pack = api.make_pack(0)
        data, _ = api.poll(pack, bms)
        assert data["maint"]["normal"]["ccl_decrease_step"] == 5 and len(_ccl_requests(bms)) == 1
        clock["t"] += 300
        data, _ = api.poll(pack, bms)                          # not due yet: no new read, value kept
        assert data["maint"]["normal"]["ccl_decrease_step"] == 5 and len(_ccl_requests(bms)) == 1
        answer["step"] = f.maint_ccl_step(12)                  # written elsewhere
        clock["t"] += 301
        data, _ = api.poll(pack, bms)
        assert data["maint"]["normal"]["ccl_decrease_step"] == 12 and len(_ccl_requests(bms)) == 2
        answer["step"] = None                                  # silence: the confirmed value stays until stale
        clock["t"] += 601
        data, _ = api.poll(pack, bms)
        assert data["maint"]["normal"]["ccl_decrease_step"] == 12
        clock["t"] += 1300
        data, ok = api.poll(pack, bms)
        assert ok and data["maint"]["normal"]["ccl_decrease_step"] is None
        answer["step"] = f.maint_ccl_step(7)
        clock["t"] += 61
        data, _ = api.poll(pack, bms)
        assert data["maint"]["normal"]["ccl_decrease_step"] == 7
        answer["step"] = lambda req: f.ascii_frame(req.adr, 0x05, b"")   # a definitive error clears at once
        clock["t"] += 601
        data, ok = api.poll(pack, bms)
    assert ok and data["maint"]["normal"]["ccl_decrease_step"] is None
    _no_ext_write(bms)


def test_discovery_ccl_step_entity():
    disc, client = api.discovery(maint_enabled=True)
    api.discovery_startup(disc, 2)
    client.published.clear()
    api.set_capabilities(disc, 0, {"maint", "maint_uart1", "maint_ccl_step"}, pack_bus=False)
    api.set_capabilities(disc, 1, {"maint", "maint_uart1"}, pack_bus=False)
    configs = {(r["topic"].split("/")[2], r["topic"].split("/")[-2]): r["payload"] for r in client.as_records()}
    config = configs[("seplos-mqtt-pack-0", "ccl_decrease_step")]
    assert config["name"] == "EXT CCL Decrease Step (Master, A per Online Pack per Update)"
    assert config["ent_cat"] == "diagnostic" and "dev_cla" not in config and "unit_of_meas" not in config
    assert config["val_tpl"] == "{{ value_json.maint.normal.ccl_decrease_step }}"
    assert config["json_attr_tpl"] == "{{ value_json.get('maint', {}).get('attributes', {}).get('ccl_step', {}) | tojson }}"
    assert config["default_entity_id"] == "sensor.seplos_bms_pack_0_ccl_decrease_step"
    assert configs[("seplos-mqtt-pack-1", "ccl_decrease_step")] == ""          # slave: removed
