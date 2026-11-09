"""
Stable test facade over the production code. Characterization tests only call these functions,
so a structural refactoring changes this file, not the tests or golden files. The facade only
delegates; it must not re-implement production composition.
"""
from __future__ import annotations

import dataclasses
import types
from contextlib import contextmanager

from fakes import FakeBms, FakeConnection, FakeMqtt, FakeSerial
from seplos_mqtt import app
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
    "MAINT_FIRMWARE": "maint_firmware",
    "BUS": "bus",
    "BUS_QUIET_MS": "bus_quiet_ms",
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
    import datetime as _datetime

    from seplos_mqtt import timeutil
    monkeypatch.setattr(timeutil, "now_iso", lambda: FIXED_NOW)
    monkeypatch.setattr(timeutil, "local_now", lambda: _datetime.datetime(2026, 11, 4, 12, 0, 0))


# --- framing ----------------------------------------------------------------------------------

def encode_request(address: int, cid2: int, info: bytes = b"\x01") -> bytes:
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
    """time.time() and time.monotonic() under test control: clock["t"] += seconds."""
    import time
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(time, "time", lambda: clock["t"])
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
    return clock


def decode_telesignal(info: bytes) -> dict:
    return alarms.decode(info)


def alarm_summary(telesignalization, maint_binary=None) -> dict:
    return alarms.summarize(telesignalization, maint_binary)


def alarm_rules() -> types.SimpleNamespace:
    """The severity table and the decoder metadata it must cover."""
    return types.SimpleNamespace(
        severity=alarms.SEVERITY, bit_fields=alarms.BIT_FIELDS, byte_alarms=tuple(alarms.BYTE_ALARMS),
        two_bit_modes=alarms.TWO_BIT_MODES, tiered=alarms.TIERED, severities=alarms.SEVERITIES,
        ok=alarms.OK, alarm=alarms.ALARM, protection=alarms.PROTECTION, fault=alarms.FAULT)


def alarm_entity_config(scope: int = 0) -> dict:
    """The rendered discovery config of the pack's ``bms_alarm`` entity."""
    from seplos_mqtt.discovery.catalog import PACK_ENTITIES
    disc, _client = discovery()
    return disc.render(next(e for e in PACK_ENTITIES if e.key == "bms_alarm"), scope)


def cell_limits(entry, parallel: int) -> dict | None:
    from seplos_mqtt.cells import checks
    return checks.limits(entry, parallel)


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

def make_pack(address: int, state_file=None):
    return BatteryPack(address, None, config(), StateStore(str(state_file)) if state_file else None)


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

def discovery(invert: bool = False, topic: str = "seplos", prefix: str = "homeassistant",
              maint_enabled: bool = False) -> tuple[Discovery, FakeMqtt]:
    client = FakeMqtt()
    return Discovery(client, topic, prefix, invert, maint_enabled=maint_enabled), client


def discovery_startup(disc: Discovery, number_of_packs: int) -> None:
    """The discovery calls main() makes at startup."""
    multi = config().can_bus if config().number_of_packs == number_of_packs else number_of_packs > 1
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
    from seplos_mqtt import mqtt_client
    monkeypatch.setattr(mqtt_client.mqtt, "Client", MqttClientSpy)
    client = app.initialize_mqtt(config()).client
    return {
        "calls": [[name, list(a), k] for name, a, k in client.calls],
        "on_connect": callable(client.on_connect),
        "on_disconnect": callable(client.on_disconnect),
    }


# --- main loop ----------------------------------------------------------------------------------

class StopLoop(BaseException):
    """Raised from the fake bus to leave main()'s endless loop (not caught by `except Exception`)."""


# execv calls ((path, argv) tuples) and AppState instances of the last run_main()
LAST_RUN: dict = {"execs": [], "states": [], "exit": None, "job": None}   # "exit": SystemExit code, None if none


def run_main(
    monkeypatch,
    bms: FakeBms,
    number_of_packs: int,
    telemetry_polls: int,
    discovery: bool = True,
    topic: str = "seplos",
    prefix: str = "homeassistant",
    seconds_per_tick: float = 0.5,
    resync_after_polls: int | None = None,
    signal_after_polls: int | None = None,
    restart_after_polls: int | None = None,
    startup: bool = False,
    job_after_polls: int | None = None,
    sets_after_polls: list[tuple[int, tuple]] | None = None,
    serial_error: str | None = None,
    cells_after_polls: list[tuple[int, tuple]] | None = None,
    cell_types_after_polls: tuple = (),
    cell_packs: dict | None = None,
    balancer_profiles: tuple = (),
) -> tuple[FakeMqtt, FakeSerial]:
    """
    Run main() against the fakes until the bus has answered `telemetry_polls` 0x42 requests
    (answered or not), then stop it on the next one. Returns the fake MQTT client and serial port.
    `restart_after_polls` simulates a saved connection change (AppState.request_restart); the
    execv calls and the AppState instances of the run are left in LAST_RUN. `startup=True` runs
    main() without a config (startup_config(os.environ), bridge): the caller sets the environment.
    `job_after_polls` submits a "detect" job (like the web thread would); it is left in LAST_RUN["job"].
    `sets_after_polls=[(n, sets), ...]` hands over saved parallel sets like the web thread would (AppState.request_sets);
    `cells_after_polls=[(n, (packs, profiles)), ...]` does the same for cell assignments (AppState.request_cells).
    """
    from frames import ascii_frame

    client = FakeMqtt()
    port = FakeSerial(bms)
    connection = FakeConnection(client)
    counter = {"n": 0}

    def gate(adr, entry):
        def respond(req):
            counter["n"] += 1
            if counter["n"] > telemetry_polls:
                raise StopLoop()
            if resync_after_polls is not None and counter["n"] == resync_after_polls + 1:
                connection.resync.set()
            if signal_after_polls is not None and counter["n"] == signal_after_polls + 1:
                handlers["SIGTERM"](15, None)
            if restart_after_polls is not None and counter["n"] == restart_after_polls + 1:
                LAST_RUN["states"][0].request_restart()
            if job_after_polls is not None and counter["n"] == job_after_polls + 1:
                LAST_RUN["job"] = LAST_RUN["states"][0].jobs.submit("detect")
            for after, handed_over in sets_after_polls or []:
                if counter["n"] == after + 1:
                    LAST_RUN["states"][0].request_sets(types.SimpleNamespace(parallel_sets=handed_over))
            for after, (packs, profiles) in cells_after_polls or []:
                if counter["n"] == after + 1:
                    LAST_RUN["states"][0].request_cells(
                        types.SimpleNamespace(packs=packs, balancer_profiles=profiles, cell_types=cell_types_after_polls))
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
    monkeypatch.setattr(time, "monotonic", fake_time)
    no_sleep(monkeypatch)
    monkeypatch.setattr(app, "initialize_mqtt", lambda _config: connection)
    def open_serial(_config):
        if serial_error is not None:
            raise app.ConnectionUnavailable(serial_error)
        return port

    monkeypatch.setattr(app, "initialize_serial", open_serial)
    monkeypatch.setattr(app, "start_health_server", lambda _state: None)
    handlers: dict = {}
    monkeypatch.setattr(app.signal, "signal", lambda sig, handler: handlers.__setitem__(sig.name, handler))
    # no real waiting: the interruptible sleep only reports a requested restart
    monkeypatch.setattr(app, "wait_or_restart", lambda state, _s: state.restart_event.is_set())
    LAST_RUN["execs"].clear()
    LAST_RUN["states"].clear()
    LAST_RUN["job"] = None
    monkeypatch.setattr(app, "_execv", lambda path, argv: LAST_RUN["execs"].append((path, argv)))

    @dataclasses.dataclass
    class SpyState(app.AppState):      # a dataclass again, so the generated __init__ calls __post_init__
        def __post_init__(self):
            if cell_packs:
                self.cell_packs = dict(cell_packs)       # assignments as if loaded from the monitoring document
            self.balancer_profiles = tuple(balancer_profiles)
            LAST_RUN["states"].append(self)

    monkeypatch.setattr(app, "AppState", SpyState)
    with mode(number_of_packs, ENABLE_HA_DISCOVERY_CONFIG=discovery, MQTT_TOPIC=topic, HA_DISCOVERY_PREFIX=prefix):
        LAST_RUN["exit"] = None
        try:
            app.main(None if startup else config())
        except StopLoop:
            pass
        except SystemExit as e:
            LAST_RUN["exit"] = e.code
    assert not bms.protocol_errors, bms.protocol_errors
    return client, port


# --- cell catalog ---------------------------------------------------------------------------------

def bundled_cells() -> list[dict]:
    """Deep copies of the bundled cell entries."""
    import copy

    from seplos_mqtt.cells import catalog
    return copy.deepcopy(list(catalog.current().entries))


def use_bundle(monkeypatch, entries: list[dict]) -> None:
    """Simulate another bundle (a catalog update): same schema, these entries."""
    from seplos_mqtt.cells import catalog
    monkeypatch.setattr(catalog, "_CATALOG", dataclasses.replace(catalog.current(), entries=tuple(entries)))


def cell_structural_problems(entry) -> list:
    from seplos_mqtt.cells import validate
    return validate.structural_problems(entry)


def cell_semantic_problems(entry) -> list:
    from seplos_mqtt.cells import validate
    return validate.semantic_problems(entry)


def cell_problems(entry, path: str = "") -> list:
    from seplos_mqtt.cells import validate
    return validate.entry_problems(entry, path)


def cell_schema() -> dict:
    from seplos_mqtt.cells import catalog
    return catalog.current().schema


def check_identity(state, pack_state) -> None:
    app.check_identity(state, pack_state)


def length_field(n: int) -> int:
    return af.length_field(n)


def with_checksum(raw: bytes) -> bytes:
    """Recompute the character checksum of a (deliberately modified) frame."""
    body = raw[1:-5]
    return b"~" + body + f"{(-sum(body)) & 0xFFFF:04X}".encode() + b"\r"


def bus_trace_poll(monkeypatch, bms: FakeBms, flaky_first_42: bool = False) -> list[tuple[str, float]]:
    """One poll on a bus with a simulated clock; returns (write|read, time) events."""
    import datetime as _datetime

    from seplos_mqtt import timeutil
    monkeypatch.setattr(timeutil, "now_iso", lambda: FIXED_NOW)
    monkeypatch.setattr(timeutil, "local_now", lambda: _datetime.datetime(2026, 11, 4, 12, 0, 0))
    clock = {"t": 0.0}
    trace: list[tuple[str, float]] = []

    class TracingSerial(FakeSerial):
        def write(self, data):
            trace.append(("write", clock["t"]))
            clock["t"] += 0.02
            return super().write(data)

        def read_until(self, terminator=b"\n", size=None):
            clock["t"] += 0.1
            trace.append(("read", clock["t"]))
            return super().read_until(terminator, size)

        def read(self, size=1):
            clock["t"] += 0.1
            trace.append(("read", clock["t"]))
            return super().read(size)

    if flaky_first_42:
        from frames import ascii_frame, telemetry_info
        calls = {"n": 0}

        def flaky(req):
            calls["n"] += 1
            return b"garbage\r" if calls["n"] == 1 else ascii_frame(0, 0, telemetry_info())
        bms.ascii[(0, 0x42)] = flaky

    def sleep(seconds):
        clock["t"] += seconds

    bus = SerialBus(TracingSerial(bms), clock=lambda: clock["t"], sleep=sleep)
    pack = BatteryPack(0, bus, config())
    pack.read()
    return trace


def publisher():
    from seplos_mqtt.mqtt_client import Publisher
    client = FakeMqtt()
    return Publisher(client), client


class _Message:
    def __init__(self, topic, payload):
        self.topic, self.payload = topic, payload


def message(topic: str, payload: bytes):
    return _Message(topic, payload)


class _ReasonCode:
    """Paho 2.x style reason code."""
    def __init__(self, value):
        self.value = value
        self.is_failure = value >= 0x80 or value not in (0,)


def reason_code(value: int):
    return _ReasonCode(value)


def mqtt_connection():
    from seplos_mqtt import mqtt_client
    conn = mqtt_client.MqttConnection.__new__(mqtt_client.MqttConnection)
    conn.config = config()
    conn.connected = False
    import threading
    conn.resync = threading.Event()
    conn._connects = 0
    conn.birth_topic = f"{config().ha_discovery_prefix}/status"
    return conn, FakeMqtt()


def config_from(env: dict) -> Config:
    import os
    return Config.from_env({**os.environ, **env})


def binary_encode(address: int, command: int, data: bytes = b"") -> bytes:
    from seplos_mqtt.framing import binary
    return binary.encode(address, command, data)


def decode_maint_identity(info: bytes):
    from seplos_mqtt.decoders import maint
    return maint.decode_identity(info)


def capabilities(pack) -> set:
    return pack.capabilities


def set_capabilities(disc: Discovery, pack_no: int, caps: set, pack_bus: bool) -> bool:
    return disc.set_capabilities(pack_no, caps, pack_bus)


def fake_monotonic(monkeypatch) -> dict:
    import time
    clock = {"t": 1_000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
    return clock


def system_reader(bms: FakeBms):
    reader = SystemReader()
    bus = SerialBus(FakeSerial(bms))
    return reader, lambda: reader.read(bus)


def decode_group0(group0: int, identity):
    from seplos_mqtt.decoders import maint
    return maint.decode_group0(group0, identity)


def decode_marker(info: bytes) -> dict:
    from seplos_mqtt.decoders import maint
    return maint.decode_marker(info)




FrameError = af.FrameError


def _bus_busy():
    from seplos_mqtt.transport import BusBusy
    return BusBusy


BusBusy = _bus_busy()


def parse_frame(raw: bytes):
    return af.parse(raw)


def decode_storage(info: bytes) -> dict:
    from seplos_mqtt.decoders import maint
    return maint.decode_storage(info)


def quiet_gap_trace(noise_at: float | None = None, noise_forever: bool = False) -> list[tuple[str, float]]:
    """Two requests on a bus with a simulated clock; foreign bytes arrive at `noise_at` (or always)."""
    clock = {"t": 0.0}
    events: list[tuple[str, float]] = []
    bms = FakeBms()

    class NoisySerial(FakeSerial):
        @property
        def in_waiting(self):
            if noise_forever or (noise_at is not None and not any(k == "noise" for k, _ in events) and clock["t"] >= noise_at):
                events.append(("noise", clock["t"]))
                self.rx += b"\x00"
            return len(self.rx)

        def write(self, data):
            events.append(("write", clock["t"]))
            return super().write(data)

    def sleep(seconds):
        clock["t"] += seconds

    bus = SerialBus(NoisySerial(bms), clock=lambda: clock["t"], sleep=sleep)
    bus.request_rtn(0, 0x51)
    clock["t"] += 0.0
    if noise_at is not None:
        clock["t"] = max(clock["t"], noise_at - 0.5)
    bus.request_rtn(0, 0x51)
    return events


def availability_state(monkeypatch):
    client = FakeMqtt()
    from seplos_mqtt.mqtt_client import Publisher
    state = app.AppState(config=config())
    state.publisher = Publisher(client)
    pack_state = app.PackState(BatteryPack(0, None, config()), 0, last_success=0.0, availability="online")
    state.packs = [pack_state]
    return state, pack_state, client


def publish_availability(state, pack_state, now: float) -> None:
    app.publish_pack_availability(state, pack_state, now)


def health_progress_during_slow_poll(monkeypatch) -> bool:
    """Run a pack poll whose optional reads take long; check health right after the core reads."""
    import time
    clock = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
    no_sleep(monkeypatch)
    from frames import device_info_info, telemetry_info, telesignal_info
    bms = FakeBms()
    bms.ascii[(0, 0x42)] = telemetry_info()
    bms.ascii[(0, 0x44)] = telesignal_info()
    bms.ascii[(0, 0x51)] = device_info_info()
    state = app.AppState(config=config())
    pack = BatteryPack(0, SerialBus(FakeSerial(bms)), config())
    state.packs = [app.PackState(pack, 0)]
    state.last_poll = 0.0
    seen = {}
    original = pack._fetch_device_info

    def slow_optional():
        clock["t"] += 55                      # optional/static work takes long (startup grace alone: 65 s > 60 s)
        seen["healthy"] = state.max_age() > clock["t"] - state.last_progress()
        original()
    pack._fetch_device_info = slow_optional
    clock["t"] = 10.0
    pack.read()
    return seen["healthy"]


def capabilities_resolved(pack) -> bool:
    return pack.capabilities_resolved


def poll_pack(state, pack_state) -> None:
    app.poll_pack(state, pack_state)


def poll_after_idle_with_buffered_byte() -> list[tuple[str, float]]:
    """A completed exchange at t=1, idle until t=10, a foreign byte buffered at t=9.999, then a poll."""
    clock = {"t": 0.0}
    events: list[tuple[str, float]] = []
    from frames import telemetry_info, telesignal_info
    bms = FakeBms()
    bms.ascii[(0, 0x42)] = telemetry_info()
    bms.ascii[(0, 0x44)] = telesignal_info()

    class TracingSerial(FakeSerial):
        def write(self, data):
            events.append(("write", clock["t"]))
            return super().write(data)

    port = TracingSerial(bms)
    bus = SerialBus(port, clock=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s))
    bus._last_activity = 1.0
    clock["t"] = 10.0
    port.rx += b"\x00"                       # received at 9.999, still buffered
    bus._last_activity = 1.0
    events.clear()
    pack = BatteryPack(0, bus, config())
    pack.info, pack.info_attempts = {"normal": {}}, 99
    pack.read()
    return events


def simulated_bank_cycle(packs: int, seconds_per_pack: float) -> list[int]:
    """First polling cycle of a bank where each pack poll takes long; returns packs published offline."""
    import time as _time
    clock = {"t": 100.0}
    client = FakeMqtt()
    from seplos_mqtt.mqtt_client import Publisher
    cfg = dataclasses.replace(config(), number_of_packs=packs)
    state = app.AppState(config=cfg)
    state.publisher = Publisher(client)

    class SlowPack:
        def __init__(self, address):
            self.address, self.cell_count, self.info, self.info_revision, self.capabilities_resolved = address, None, None, 0, False
            self.core_success = 0.0
            self.sample = None

        def parameter_sample(self):
            return None

        def read(self):
            clock["t"] += seconds_per_pack
            return {"telemetry": {"normal": {"a": self.address}}}, True

    state.packs = [app.PackState(SlowPack(i), i) for i in range(packs)]
    original = _time.monotonic
    _time.monotonic = lambda: clock["t"]
    try:
        state.cycle_start = clock["t"]
        for pack_state in state.packs:
            app.poll_pack(state, pack_state)
    finally:
        _time.monotonic = original
    return sorted({int(r.topic.split("pack-")[1].split("/")[0]) for r in client.published
                   if r.topic.endswith("/availability") and r.payload == "offline"})


def app_with_discovery(monkeypatch):
    from seplos_mqtt.mqtt_client import Publisher
    client = FakeMqtt()
    state = app.AppState(config=config())
    state.publisher = Publisher(client)
    state.discovery = Discovery(state.publisher, "seplos", "homeassistant", False, maint_enabled=True)
    state.packs = [app.PackState(BatteryPack(0, None, config()), 0)]
    return state, state.discovery, client


def retry_discovery(state) -> None:
    app.retry_discovery(state)


def state_store(path) -> StateStore:
    return StateStore(str(path))


# --- datasheet checks -----------------------------------------------------------------------------

def checks_sample(values: dict, at: float = 0.0, max_age: float | None = None):
    from seplos_mqtt.cells.checks import Sample
    return Sample(values, at, max_age)


def balancer_step(state: str, values: dict, sample, now: float = 0.0) -> str:
    from seplos_mqtt.cells import balancer
    return balancer.step(state, values, sample, now)


def balancer_rule(values: dict):
    from seplos_mqtt.cells import balancer
    return balancer.Rule.from_values(values)


def balancer_tracker():
    from seplos_mqtt.cells import balancer
    return balancer.Tracker()


def live_checks(entry, parallel, sample, now: float = 0.0, blocked: str | None = None) -> dict:
    from seplos_mqtt.cells import checks
    return checks.live_checks(entry, parallel, sample, now, blocked=blocked)


def parameter_checks(entry, parallel, parameters, series, now: float = 0.0, blocked: str | None = None) -> dict:
    from seplos_mqtt.cells import checks
    return checks.parameter_checks(entry, parallel, parameters, series, now, blocked=blocked)


def permitted_charge(entry, parallel, temps, soc=None):
    from seplos_mqtt.cells import checks
    return checks.permitted_charge_a(entry, parallel, temps, soc)


def permitted_discharge(entry, parallel, temps):
    from seplos_mqtt.cells import checks
    return checks.permitted_discharge_a(entry, parallel, temps)


def allowed_charge_rate(entry, temp, soc=None):
    from seplos_mqtt.cells import checks
    return checks.allowed_charge_rate(entry, temp, soc)


def charge_range(entry):
    from seplos_mqtt.cells import checks
    return checks.charge_range(entry)


def summarize_checks(results: list[str]) -> str:
    from seplos_mqtt.cells import checks
    return checks.summarize([{"result": r} for r in results])


def stored_assignments(packs: dict, cell_types: list | None = None, pack_count: int = 2) -> dict:
    """PackAssignment objects as the monitoring document would load them."""
    from seplos_mqtt.monitoring.model import MonitoringConfig
    doc = {"connection": {"transport": "tcp", "host": "h", "pack_count": pack_count}, "packs": packs}
    if cell_types:
        doc["cell_types"] = cell_types
    return dict(MonitoringConfig.from_dict(doc).packs)


def stored_cells(packs: dict, profiles: list, pack_count: int = 2):
    """The loaded monitoring document with pack assignments and balancer profiles."""
    from seplos_mqtt.monitoring.model import MonitoringConfig
    doc = {"connection": {"transport": "tcp", "host": "h", "pack_count": pack_count}, "packs": packs,
           "balancer_profiles": profiles, "next_profile_id": 100}
    return MonitoringConfig.from_dict(doc)


def reconcile_balancers(state, force: bool = False) -> None:
    app.reconcile_balancers(state, force=force)


def balancer_update(state, pack_state, now: float) -> None:
    from seplos_mqtt.cells import evaluate
    evaluate.balancer_update(state, pack_state, now)


def balancer_report(state, pack_state, now: float = 1000.0):
    from seplos_mqtt.cells import evaluate
    return evaluate.balancer_report(state, pack_state, now)


def pack_report(state, pack_state, now: float) -> dict:
    from seplos_mqtt.cells import evaluate
    return evaluate.pack_report(state, pack_state, now)


def total_full_energy(state, now: float):
    return app.total_full_energy(state, now)
