"""
Stable test facade over the production code. Characterization tests only call these functions,
so a structural refactoring changes this file, not the tests or golden files.
"""
from __future__ import annotations

from contextlib import contextmanager

import fetch_bms_data as fb
from ha_auto_discovery import AutoDiscoveryConfig

from fakes import FakeBms, FakeMqtt, FakeSerial


def _ascii(info: bytes) -> bytes:
    return info.hex().upper().encode()


@contextmanager
def mode(number_of_packs: int):
    old = fb.Config.NUMBER_OF_PACKS
    fb.Config.NUMBER_OF_PACKS = number_of_packs
    try:
        yield
    finally:
        fb.Config.NUMBER_OF_PACKS = old


def no_sleep(monkeypatch) -> None:
    monkeypatch.setattr(fb.time, "sleep", lambda _s: None)


def decode_telemetry(info: bytes) -> dict:
    return fb.SeplosBatteryPack(0).decode_telemetry_feedback_frame(_ascii(info))


def decode_telesignal(info: bytes) -> dict:
    return fb.SeplosBatteryPack(0).decode_telesignalization_feedback_frame(_ascii(info))


def decode_device_info(info: bytes) -> dict:
    return fb.SeplosBatteryPack.decode_device_info_frame(_ascii(info))


def decode_serial(info: bytes) -> dict:
    return fb.SeplosBatteryPack.decode_serial_number_frame(_ascii(info))


def decode_parameters(info: bytes) -> dict:
    return fb.SeplosBatteryPack.decode_parameter_frame(_ascii(info))


def decode_extended_status(info: bytes) -> dict:
    return fb.SeplosBatteryPack.decode_extended_status(_ascii(info))


def decode_system(regs: list[int]) -> dict:
    return fb.SystemData.decode(regs)


def make_pack(address: int):
    return fb.SeplosBatteryPack(address)


def poll(pack, bms: FakeBms):
    """One read_serial_data() call against the fake bus."""
    fb.app_state.serial_instance = FakeSerial(bms)
    return pack.read_serial_data()


def read_system(master, bms: FakeBms):
    fb.app_state.serial_instance = FakeSerial(bms)
    return fb.SystemData().read(master)


def discovery(invert: bool = False) -> tuple[AutoDiscoveryConfig, FakeMqtt]:
    client = FakeMqtt()
    disc = AutoDiscoveryConfig(
        mqtt_topic="seplos",
        discovery_prefix="homeassistant",
        invert_ha_dis_charge_measurements=invert,
        mqtt_client=client,
    )
    return disc, client


def discovery_startup(disc: AutoDiscoveryConfig, number_of_packs: int) -> None:
    """What main() publishes at startup."""
    multi = number_of_packs > 1
    for pack_no in range(number_of_packs):
        disc.create_autodiscovery_sensors(pack_no=pack_no, pack_bus=not multi)
    if multi:
        disc.create_system_sensors()
    else:
        disc.remove_system_sensors()


def firmware_refresh(disc: AutoDiscoveryConfig, pack_no: int, info: dict) -> None:
    disc.update_firmware_version(pack_no, fb.device_firmware(info))


class StopLoop(BaseException):
    """Raised from the fake bus to leave main()'s endless loop (not caught by `except Exception`)."""


def run_main(monkeypatch, bms: FakeBms, number_of_packs: int, telemetry_polls: int, discovery: bool = True) -> FakeMqtt:
    """
    Run main() against the fakes until the bus has answered `telemetry_polls` 0x42 requests,
    then stop it on the next 0x42. Returns the fake MQTT client with every publish.
    """
    import datetime as _dt

    client = FakeMqtt()
    counter = {"n": 0}
    original = dict(bms.ascii)

    def gate(adr):
        entry = original.get((adr, 0x42))

        def respond(req):
            counter["n"] += 1
            if counter["n"] > telemetry_polls:
                raise StopLoop()
            return frames_ascii(adr, entry)
        return respond

    from frames import ascii_frame as frames_ascii_frame

    def frames_ascii(adr, info):
        return frames_ascii_frame(adr, 0x00, info)

    for (adr, cid2) in list(bms.ascii):
        if cid2 == 0x42:
            bms.ascii[(adr, cid2)] = gate(adr)

    class FixedDatetime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 4, 12, 0, 0, tzinfo=tz)

    clock = {"t": 1_000_000.0}

    def fake_time():
        clock["t"] += 0.5
        return clock["t"]

    monkeypatch.setattr(fb.time, "sleep", lambda _s: None)
    monkeypatch.setattr(fb.time, "time", fake_time)
    monkeypatch.setattr(fb, "datetime", FixedDatetime)
    monkeypatch.setattr(fb, "initialize_mqtt", lambda: client)
    monkeypatch.setattr(fb, "initialize_serial", lambda: FakeSerial(bms))
    monkeypatch.setattr(fb, "_start_health_server", lambda: None)
    monkeypatch.setattr(fb.Config, "ENABLE_HA_DISCOVERY_CONFIG", discovery)
    monkeypatch.setattr(fb.sys, "exit", lambda *_a: None)
    client.loop_stop = lambda: None
    client.disconnect = lambda: None
    with mode(number_of_packs):
        try:
            fb.main()
        except StopLoop:
            pass
    return client
