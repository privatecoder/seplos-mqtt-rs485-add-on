"""
Stable test facade over the production code. Characterization tests only call these functions,
so a structural refactoring changes this file, not the tests or golden files. The facade only
delegates; it must not re-implement production composition.
"""
from __future__ import annotations

import dataclasses
from contextlib import contextmanager

from fakes import FakeBms, FakeMqtt, FakeSerial
from seplos_mqtt import app
from seplos_mqtt import pack as pack_module
from seplos_mqtt.config import Config
from seplos_mqtt.decoders import alarms, identity, intrapack, parameters, system
from seplos_mqtt.decoders.identity import device_firmware
from seplos_mqtt.discovery import Discovery
from seplos_mqtt.framing import ascii as af
from seplos_mqtt.framing import modbus
from seplos_mqtt.pack import BatteryPack
from seplos_mqtt.state import StateStore
from seplos_mqtt.system import SystemReader
from seplos_mqtt.transport import SerialBus

_CONFIG = {"config": Config.from_env()}

# legacy Config attribute names used by the tests -> Config fields
_FIELDS = {
    "NUMBER_OF_PACKS": "number_of_packs",
    "ENABLE_HA_DISCOVERY_CONFIG": "enable_ha_discovery",
    "MQTT_TOPIC": "mqtt_topic",
    "HA_DISCOVERY_PREFIX": "ha_discovery_prefix",
}


def config() -> Config:
    return _CONFIG["config"]


@contextmanager
def mode(number_of_packs: int, **overrides):
    """Run with a given pack count and Config overrides (legacy attribute names)."""
    old = _CONFIG["config"]
    values = {"NUMBER_OF_PACKS": number_of_packs, **overrides}
    _CONFIG["config"] = dataclasses.replace(old, **{_FIELDS[k]: v for k, v in values.items()})
    try:
        yield
    finally:
        _CONFIG["config"] = old


FIXED_NOW = "2026-11-04T12:00:00+01:00"


def no_sleep(monkeypatch) -> None:
    """No real delays and a fixed wall-clock timestamp."""
    import time
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    monkeypatch.setattr(pack_module, "now_iso", lambda: FIXED_NOW)


# --- framing ----------------------------------------------------------------------------------

def encode_request(address: int, cid2: int, info: bytes = b"01") -> bytes:
    return af.encode(address, cid2, info)


def modbus_crc(data: bytes) -> int:
    return modbus.crc16(data)


# --- decoders ---------------------------------------------------------------------------------

def decode_telemetry(info: bytes) -> dict:
    return decode_telemetry_sequence([info])[0]


def decode_telemetry_sequence(infos: list[bytes], state_file=None, flush_before: bool = False) -> list[dict]:
    """Several 0x42 answers decoded by the same pack (stateful energy handling, optional state file)."""
    store = StateStore(str(state_file)) if state_file else None
    pack = BatteryPack(0, None, config(), store)
    out = [pack._decode_telemetry(i) for i in infos]
    if store is not None:
        store.flush()
    return out


def fake_clock(monkeypatch) -> dict:
    """time.time() under test control: clock["t"] += seconds."""
    import time
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(time, "time", lambda: clock["t"])
    return clock


def decode_telesignal(info: bytes) -> dict:
    return alarms.decode(info)


def decode_device_info(info: bytes) -> dict:
    return identity.decode_device_info(info)


def decode_serial(info: bytes) -> dict:
    return identity.decode_serial_number(info)


def decode_parameters(info: bytes) -> dict:
    return parameters.decode_ascii_answer(info)


def decode_intra_pack_record(info: bytes) -> dict:
    return intrapack.decode_record(info)


def decode_extended_status(info: bytes) -> dict:
    return intrapack.decode_extended_status(info)


def decode_system(regs: list[int]) -> dict:
    return system.decode(regs)


# --- polling ----------------------------------------------------------------------------------

def make_pack(address: int):
    return BatteryPack(address, None, config())


def poll(pack, bms: FakeBms):
    """One poll against the fake bus; outbound frames must be valid."""
    pack.bus = SerialBus(FakeSerial(bms))
    pack.config = config()
    result = pack.read()
    assert not bms.protocol_errors, bms.protocol_errors
    return result


def read_system(master, bms: FakeBms):
    return SystemReader().read(SerialBus(FakeSerial(bms)))


# --- discovery --------------------------------------------------------------------------------

def discovery(invert: bool = False, topic: str = "seplos", prefix: str = "homeassistant") -> tuple[Discovery, FakeMqtt]:
    client = FakeMqtt()
    return Discovery(client, topic, prefix, invert), client


def discovery_startup(disc: Discovery, number_of_packs: int) -> None:
    """The discovery calls main() makes at startup."""
    multi = number_of_packs > 1
    for pack_no in range(number_of_packs):
        disc.publish_pack(pack_no, pack_bus=not multi)
    if multi:
        disc.publish_system()
    else:
        disc.remove_system()


def cell_count(disc: Discovery, pack_no: int, cells: int) -> None:
    disc.update_cell_count(pack_no, cells)


def firmware_refresh(disc: Discovery, pack_no: int, info: dict) -> None:
    disc.update_firmware(pack_no, device_firmware(info))


# --- initialization -----------------------------------------------------------------------------

class SerialSpy:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.is_open = True


def init_serial(monkeypatch, number_of_packs: int, **env) -> dict:
    monkeypatch.setattr(app.serial, "Serial", SerialSpy)
    import os
    cfg = Config.from_env({**os.environ, **env})
    cfg = dataclasses.replace(cfg, number_of_packs=number_of_packs)
    return app.initialize_serial(cfg).kwargs


class MqttClientSpy:
    instances: list[MqttClientSpy] = []

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
    monkeypatch.setattr(app.mqtt, "Client", MqttClientSpy)
    client = app.initialize_mqtt(app.AppState(config=config()))
    return {
        "calls": [[name, list(a), k] for name, a, k in client.calls],
        "on_connect": callable(client.on_connect),
        "on_disconnect": callable(client.on_disconnect),
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

    clock = {"t": 1_000_000.0}

    def fake_time():
        clock["t"] += seconds_per_tick
        return clock["t"]

    import time
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    monkeypatch.setattr(time, "time", fake_time)
    monkeypatch.setattr(app, "now_iso", lambda: FIXED_NOW)
    monkeypatch.setattr(pack_module, "now_iso", lambda: FIXED_NOW)
    monkeypatch.setattr(app, "initialize_mqtt", lambda _state: client)
    monkeypatch.setattr(app, "initialize_serial", lambda _config: port)
    monkeypatch.setattr(app, "start_health_server", lambda _state: None)
    monkeypatch.setattr(app.signal, "signal", lambda *_a: None)
    with mode(number_of_packs, ENABLE_HA_DISCOVERY_CONFIG=discovery, MQTT_TOPIC=topic, HA_DISCOVERY_PREFIX=prefix):
        try:
            app.main(config())
        except StopLoop:
            pass
    assert not bms.protocol_errors, bms.protocol_errors
    return client, port
