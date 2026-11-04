"""
Stable test facade over the production code. Characterization tests only call these functions,
so a structural refactoring changes this file, not the tests or golden files. The facade only
delegates; it must not re-implement production composition.
"""
from __future__ import annotations

import datetime as _dt
from contextlib import contextmanager

import fetch_bms_data as fb
from ha_auto_discovery import AutoDiscoveryConfig

from fakes import FakeBms, FakeMqtt, FakeSerial


def _ascii(info: bytes) -> bytes:
    return info.hex().upper().encode()


@contextmanager
def mode(number_of_packs: int, **overrides):
    """Run with a given pack count and Config overrides (attribute names of the legacy Config)."""
    values = {"NUMBER_OF_PACKS": number_of_packs, **overrides}
    old = {k: getattr(fb.Config, k) for k in values}
    for k, v in values.items():
        setattr(fb.Config, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(fb.Config, k, v)


def no_sleep(monkeypatch) -> None:
    monkeypatch.setattr(fb.time, "sleep", lambda _s: None)


# --- framing ----------------------------------------------------------------------------------

def encode_request(address: int, cid2: int, info: bytes = b"01") -> bytes:
    return fb.SeplosBatteryPack(address).encode_cmd(address=address, cid2=cid2, info=info)


def modbus_crc(data: bytes) -> int:
    return fb.SeplosBatteryPack.modbus_crc16(data)


# --- decoders ---------------------------------------------------------------------------------

def decode_telemetry(info: bytes) -> dict:
    return fb.SeplosBatteryPack(0).decode_telemetry_feedback_frame(_ascii(info))


def decode_telemetry_sequence(infos: list[bytes]) -> list[dict]:
    """Several 0x42 answers decoded by the same pack (stateful energy handling)."""
    pack = fb.SeplosBatteryPack(0)
    return [pack.decode_telemetry_feedback_frame(_ascii(i)) for i in infos]


def decode_telesignal(info: bytes) -> dict:
    return fb.SeplosBatteryPack(0).decode_telesignalization_feedback_frame(_ascii(info))


def decode_device_info(info: bytes) -> dict:
    return fb.SeplosBatteryPack.decode_device_info_frame(_ascii(info))


def decode_serial(info: bytes) -> dict:
    return fb.SeplosBatteryPack.decode_serial_number_frame(_ascii(info))


def decode_parameters(info: bytes) -> dict:
    return fb.SeplosBatteryPack.decode_parameter_frame(_ascii(info))


def decode_intra_pack_record(info: bytes) -> dict:
    return fb.SeplosBatteryPack.decode_intra_pack_frame(_ascii(info))


def decode_extended_status(info: bytes) -> dict:
    return fb.SeplosBatteryPack.decode_extended_status(_ascii(info))


def decode_system(regs: list[int]) -> dict:
    return fb.SystemData.decode(regs)


# --- polling ----------------------------------------------------------------------------------

def make_pack(address: int):
    return fb.SeplosBatteryPack(address)


def poll(pack, bms: FakeBms):
    """One read_serial_data() call against the fake bus; outbound frames must be valid."""
    fb.app_state.serial_instance = FakeSerial(bms)
    result = pack.read_serial_data()
    assert not bms.protocol_errors, bms.protocol_errors
    return result


def read_system(master, bms: FakeBms):
    fb.app_state.serial_instance = FakeSerial(bms)
    return fb.SystemData().read(master)


# --- discovery --------------------------------------------------------------------------------

def discovery(invert: bool = False, topic: str = "seplos", prefix: str = "homeassistant") -> tuple[AutoDiscoveryConfig, FakeMqtt]:
    client = FakeMqtt()
    disc = AutoDiscoveryConfig(
        mqtt_topic=topic,
        discovery_prefix=prefix,
        invert_ha_dis_charge_measurements=invert,
        mqtt_client=client,
    )
    return disc, client


def discovery_startup(disc: AutoDiscoveryConfig, number_of_packs: int) -> None:
    """The discovery calls main() makes at startup."""
    multi = number_of_packs > 1
    for pack_no in range(number_of_packs):
        disc.create_autodiscovery_sensors(pack_no=pack_no, pack_bus=not multi)
    if multi:
        disc.create_system_sensors()
    else:
        disc.remove_system_sensors()


def firmware_refresh(disc: AutoDiscoveryConfig, pack_no: int, info: dict) -> None:
    disc.update_firmware_version(pack_no, fb.device_firmware(info))


# --- initialization -----------------------------------------------------------------------------

class SerialSpy:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.is_open = True


def init_serial(monkeypatch, number_of_packs: int) -> dict:
    monkeypatch.setattr(fb.serial, "Serial", SerialSpy)
    with mode(number_of_packs):
        return fb.initialize_serial().kwargs


class MqttClientSpy:
    instances: list["MqttClientSpy"] = []

    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.calls: list[tuple] = []
        MqttClientSpy.instances.append(self)

    def username_pw_set(self, *a, **k):
        self.calls.append(("username_pw_set", a, k))

    def will_set(self, *a, **k):
        self.calls.append(("will_set", a, k))

    def connect(self, *a, **k):
        self.calls.append(("connect", a, k))

    def loop_start(self):
        self.calls.append(("loop_start", (), {}))


def init_mqtt(monkeypatch) -> dict:
    MqttClientSpy.instances.clear()
    monkeypatch.setattr(fb.mqtt, "Client", MqttClientSpy)
    client = fb.initialize_mqtt()
    return {
        "calls": [[name, list(a), k] for name, a, k in client.calls],
        "on_connect": client.on_connect.__name__,
        "on_disconnect": client.on_disconnect.__name__,
    }


# --- main loop ----------------------------------------------------------------------------------

class StopLoop(BaseException):
    """Raised from the fake bus to leave main()'s endless loop (not caught by `except Exception`)."""


def run_main(
    monkeypatch,
    bms: FakeBms,
    number_of_packs: int,
    telemetry_polls: int,
    discovery: bool = True,
    topic: str = "seplos",
    prefix: str = "homeassistant",
    seconds_per_tick: float = 0.5,
) -> tuple[FakeMqtt, FakeSerial]:
    """
    Run main() against the fakes until the bus has answered `telemetry_polls` 0x42 requests
    (answered or not), then stop it on the next one. Returns the fake MQTT client and serial port.
    """
    from frames import ascii_frame

    client = FakeMqtt()
    port = FakeSerial(bms)
    counter = {"n": 0}

    def gate(adr, entry):
        def respond(req):
            counter["n"] += 1
            if counter["n"] > telemetry_polls:
                raise StopLoop()
            if entry is None:
                return b""
            return entry(req) if callable(entry) else ascii_frame(adr, 0x00, entry)
        return respond

    for adr in range(number_of_packs):
        bms.ascii[(adr, 0x42)] = gate(adr, bms.ascii.get((adr, 0x42)))

    class FixedDatetime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 4, 12, 0, 0, tzinfo=tz)

    clock = {"t": 1_000_000.0}

    def fake_time():
        clock["t"] += seconds_per_tick
        return clock["t"]

    monkeypatch.setattr(fb.time, "sleep", lambda _s: None)
    monkeypatch.setattr(fb.time, "time", fake_time)
    monkeypatch.setattr(fb, "datetime", FixedDatetime)
    monkeypatch.setattr(fb, "initialize_mqtt", lambda: client)
    monkeypatch.setattr(fb, "initialize_serial", lambda: port)
    monkeypatch.setattr(fb, "_start_health_server", lambda: None)
    monkeypatch.setattr(fb.sys, "exit", lambda *_a: None)
    monkeypatch.setenv("MQTT_TOPIC", topic)
    with mode(number_of_packs, ENABLE_HA_DISCOVERY_CONFIG=discovery, MQTT_TOPIC=topic, HA_DISCOVERY_PREFIX=prefix):
        try:
            fb.main()
        except StopLoop:
            pass
    assert not bms.protocol_errors, bms.protocol_errors
    return client, port
