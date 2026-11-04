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


def test_poll_fails_when_44_fails(monkeypatch):
    api.no_sleep(monkeypatch)
    with api.mode(1):
        bms = _single_pack_bms()
        bms.ascii[(0, 0x44)] = None
        assert api.poll(api.make_pack(0), bms) == (None, False)


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
    assert api.init_serial(monkeypatch, 1) == {"port": "/tmp/vcom0", "baudrate": 19200, "timeout": 0.5}
    assert api.init_serial(monkeypatch, 2) == {"port": "/tmp/vcom0", "baudrate": 9600, "timeout": 0.5}


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
    assert api.init_serial(monkeypatch, 2, SERIAL_BAUDRATE="12345")["baudrate"] == 9600
