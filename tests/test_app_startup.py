"""Startup configuration (monitoring store), restart on a saved connection change, bridge supervision."""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

import pytest

import api
import frames as f
from fakes import FakeBms
from seplos_mqtt import app
from seplos_mqtt.config import Config

ENV = {"MQTT_HOST": "broker", "MQTT_TOPIC": "seplos", "MQTT_UPDATE_INTERVAL": "1"}


def single_pack_bms() -> FakeBms:
    bms = FakeBms()
    bms.ascii[(0, 0x42)] = f.telemetry_info(adr=0)
    bms.ascii[(0, 0x44)] = f.telesignal_info(adr=0)
    bms.ascii[(0, 0x51)] = f.device_info_info()
    bms.ascii[(0, 0xA2)] = f.serial_info()
    bms.ascii[(0, 0xAA)] = None
    bms.ascii[(0, 0x47)] = f.parameter_info()
    bms.ascii[(0, 0x5A)] = lambda req: f.ascii_frame(0, 0x5A, f.intra_pack_info())
    bms.ascii[(0, 0x4D)] = bytes.fromhex("07EA0B040B3B1E")
    return bms


# ---------------------------------------------------------------- startup_config


def test_missing_file_migrates_tcp(tmp_path):
    path = tmp_path / "monitoring.json"
    env = {**ENV, "MONITORING_FILE": str(path), "RS485_REMOTE_IP": "10.0.0.5", "RS485_REMOTE_PORT": "5000", "NUMBER_OF_PACKS": "2"}
    config, store = app.startup_config(env)
    assert store is not None and store.revision == 1
    assert path.exists()
    assert (config.remote_host, config.remote_port, config.number_of_packs) == ("10.0.0.5", 5000, 2)
    assert config.serial_interface == "/tmp/vcom0"
    assert config.mqtt_host == "broker"


def test_missing_file_migrates_serial(tmp_path):
    env = {**ENV, "MONITORING_FILE": str(tmp_path / "m.json"), "SERIAL_INTERFACE": "/dev/ttyUSB0", "SERIAL_BAUDRATE": "9600"}
    config, store = app.startup_config(env)
    assert store is not None and store.revision == 1
    assert (config.remote_host, config.serial_interface, config.baudrate) == ("", "/dev/ttyUSB0", 9600)


def test_valid_file_wins_over_changed_env(tmp_path):
    env = {**ENV, "MONITORING_FILE": str(tmp_path / "m.json"), "NUMBER_OF_PACKS": "2", "RS485_REMOTE_IP": "10.0.0.5"}
    app.startup_config(env)
    changed = {**env, "NUMBER_OF_PACKS": "5", "RS485_REMOTE_IP": "", "SERIAL_INTERFACE": "/dev/other", "MQTT_HOST": "other-broker"}
    config, store = app.startup_config(changed)
    assert store is not None and store.revision == 1
    assert (config.number_of_packs, config.remote_host) == (2, "10.0.0.5")
    assert config.mqtt_host == "other-broker"     # MQTT settings always come from the environment


def test_corrupt_file_falls_back_to_env(tmp_path):
    path = tmp_path / "m.json"
    path.write_bytes(b"{garbage")
    env = {**ENV, "MONITORING_FILE": str(path), "NUMBER_OF_PACKS": "3"}
    config, store = app.startup_config(env)
    assert config == Config.from_env(env)
    assert store is not None and store.corrupt
    assert path.read_bytes() == b"{garbage"


def test_invalid_options_exit_1(tmp_path):
    env = {**ENV, "MONITORING_FILE": str(tmp_path / "m.json"), "NUMBER_OF_PACKS": "x"}
    with pytest.raises(SystemExit) as e:
        app.startup_config(env)
    assert e.value.code == 1
    assert not (tmp_path / "m.json").exists()
    env = {**ENV, "MONITORING_FILE": str(tmp_path / "m.json"), "MIN_CELL_VOLTAGE": "3.7", "MAX_CELL_VOLTAGE": "3.6"}
    with pytest.raises(SystemExit):
        app.startup_config(env)


def test_unwritable_location_falls_back_without_store(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        env = {**ENV, "MONITORING_FILE": str(locked / "m.json"), "NUMBER_OF_PACKS": "2"}
        config, store = app.startup_config(env)
    finally:
        locked.chmod(0o700)
    assert store is None
    assert config == Config.from_env(env)


# ---------------------------------------------------------------- logging


def test_first_start_logs_the_migration_under_seplos_settings(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("MONITORING_FILE", str(tmp_path / "monitoring.json"))
    monkeypatch.delenv("RS485_REMOTE_IP", raising=False)
    monkeypatch.delenv("INGRESS_PORT", raising=False)
    root = logging.getLogger("SeplosBMS")
    old_level = root.level
    root.setLevel(logging.WARNING)                  # as if nothing had configured the level yet
    try:
        caplog.set_level(logging.DEBUG)
        api.run_main(monkeypatch, single_pack_bms(), number_of_packs=1, telemetry_polls=2, startup=True)
    finally:
        root.setLevel(old_level)
    messages = [(r.name, r.levelno, r.getMessage()) for r in caplog.records]
    created = ("SeplosBMS.settings", logging.INFO, "created monitoring.json from the add-on options")
    assert created in messages
    # logging was configured before the startup config ran: the line comes before the first start message
    assert messages.index(created) < messages.index(("SeplosBMS", logging.INFO, "Starting Seplos BMS Data Fetcher"))


def test_every_logger_is_below_seplosbms():
    src = Path(app.__file__).parent
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "getLogger(__name__)" not in text, path.name          # would not inherit the add-on's level
        for name in re.findall(r'getLogger\("([^"]+)"\)', text):
            assert name == "SeplosBMS" or name.startswith("SeplosBMS."), f"{path.name}: {name}"


def test_setup_logging_sets_the_level_of_the_whole_tree():
    root = logging.getLogger("SeplosBMS")
    old = root.level
    try:
        app.setup_logging("debug")
        assert logging.getLogger("SeplosBMS.settings").getEffectiveLevel() == logging.DEBUG
        assert logging.getLogger("SeplosBMS.web").getEffectiveLevel() == logging.DEBUG
        app.setup_logging("nonsense")
        assert logging.getLogger("SeplosBMS.web").getEffectiveLevel() == logging.INFO
    finally:
        root.setLevel(old)


# ---------------------------------------------------------------- restart


def test_saved_connection_restarts_in_place(monkeypatch):
    client, port = api.run_main(monkeypatch, single_pack_bms(), number_of_packs=1, telemetry_polls=10, restart_after_polls=2)
    records = client.as_records()
    assert records[-2]["topic"] == "seplos/availability" and records[-2]["payload"] == "offline"   # graceful exit
    assert port.is_open is False
    assert len(api.LAST_RUN["execs"]) == 1
    path, argv = api.LAST_RUN["execs"][0]
    assert path == sys.executable
    assert argv[:2] == [sys.executable, "-u"]
    assert argv[2].endswith("src/fetch_bms_data.py")


def test_no_execv_without_a_request(monkeypatch):
    api.run_main(monkeypatch, single_pack_bms(), number_of_packs=1, telemetry_polls=3)
    assert api.LAST_RUN["execs"] == []


def test_no_execv_on_sigterm(monkeypatch):
    api.run_main(monkeypatch, single_pack_bms(), number_of_packs=1, telemetry_polls=10, signal_after_polls=2)
    assert api.LAST_RUN["execs"] == []


def test_request_restart_is_a_flag():
    state = app.AppState(config=api.config())
    assert not state.restart_event.is_set()
    state.request_restart(object())
    assert state.restart_event.is_set()
    assert app.wait_or_restart(state, 5) is True      # returns at once


# ---------------------------------------------------------------- bridge


class FakeBridge:
    instances: list = []
    is_alive = True

    def __init__(self, host, port, link):
        self.args = (host, port, link)
        self.starts = 0
        self.stops = 0
        FakeBridge.instances.append(self)

    def start(self):
        self.starts += 1

    def alive(self):
        return type(self).is_alive

    def stop(self):
        self.stops += 1


class DeadBridge(FakeBridge):
    is_alive = False


class FakeWeb:
    """Stands in for WebThread: stop() answers from a script (an Exception instance is raised)."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def stop(self, timeout=10.0):
        self.calls.append(timeout)
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


def standalone(monkeypatch, tmp_path, bridge=None, web=None):
    """Environment for main() without a config: monitoring file, TCP gateway, optional fakes."""
    FakeBridge.instances.clear()
    monkeypatch.setattr(app, "SocatBridge", bridge or FakeBridge)
    monkeypatch.setenv("MONITORING_FILE", str(tmp_path / "monitoring.json"))
    monkeypatch.setenv("RS485_REMOTE_IP", "10.0.0.5")
    monkeypatch.setenv("RS485_REMOTE_PORT", "4196")
    if web is None:
        monkeypatch.delenv("INGRESS_PORT", raising=False)
    else:
        monkeypatch.setenv("INGRESS_PORT", "8099")
        monkeypatch.setattr(app, "start_web", lambda state, store, port: setattr(state, "web", web))


def offline_published(client) -> bool:
    return any(r["topic"] == "seplos/availability" and r["payload"] == "offline" for r in client.as_records())


def test_bridge_is_started_once_and_stopped_after_cleanup(monkeypatch, tmp_path):
    standalone(monkeypatch, tmp_path)
    client, _ = api.run_main(monkeypatch, single_pack_bms(), number_of_packs=1, telemetry_polls=4, seconds_per_tick=10, startup=True)
    (bridge,) = FakeBridge.instances
    assert bridge.args == ("10.0.0.5", 4196, "/tmp/vcom0")
    assert (bridge.starts, bridge.stops) == (1, 1)     # never restarted in place
    assert api.LAST_RUN["execs"] == []                 # alive the whole time: no restart


def test_dead_bridge_restarts_the_process_once(monkeypatch, tmp_path):
    standalone(monkeypatch, tmp_path, bridge=DeadBridge)
    client, port = api.run_main(
        monkeypatch, single_pack_bms(), number_of_packs=1, telemetry_polls=40, seconds_per_tick=10, startup=True
    )
    (bridge,) = FakeBridge.instances
    assert (bridge.starts, bridge.stops) == (1, 1)     # not restarted in place, stopped in the cleanup
    assert len(api.LAST_RUN["execs"]) == 1
    assert api.LAST_RUN["states"][0].restart_reason == "bridge"
    assert offline_published(client)
    assert port.is_open is False


def _bridge_state(alive):
    state = app.AppState(config=api.config())
    flag = {"alive": alive}
    state.bridge = type("B", (), {"alive": lambda self: flag["alive"]})()
    return state, flag


def test_check_bridge_waits_for_the_deadline():
    state, _ = _bridge_state(False)
    app.check_bridge(state, 100.0)
    assert state.bridge_dead_since == 100.0
    app.check_bridge(state, 100.0 + app.BRIDGE_DEAD_RESTART_S - 0.1)
    assert not state.restart_event.is_set()
    app.check_bridge(state, 100.0 + app.BRIDGE_DEAD_RESTART_S)
    assert state.restart_event.is_set()
    assert state.restart_reason == "bridge"
    assert state.bridge_dead_since == 100.0              # not reset by later checks


def test_check_bridge_timer_resets_when_alive_again():
    state, flag = _bridge_state(False)
    app.check_bridge(state, 100.0)
    flag["alive"] = True
    app.check_bridge(state, 120.0)
    assert state.bridge_dead_since is None
    flag["alive"] = False
    app.check_bridge(state, 125.0)                        # a new outage starts counting from here
    app.check_bridge(state, 125.0 + app.BRIDGE_DEAD_RESTART_S - 1)
    assert not state.restart_event.is_set()


def test_check_bridge_never_restarts_a_living_bridge():
    state, _ = _bridge_state(True)
    for now in (0.0, 100.0, 10_000.0):
        app.check_bridge(state, now)
    assert not state.restart_event.is_set()
    assert state.bridge_dead_since is None
    app.check_bridge(app.AppState(config=api.config()), 5.0)     # no bridge at all: nothing to do


def test_first_restart_reason_is_kept():
    state, _ = _bridge_state(False)
    state.request_restart(object())
    app.check_bridge(state, 0.0)
    app.check_bridge(state, 100.0)
    assert state.restart_reason == "connection"
    state2 = app.AppState(config=api.config())
    state2.request_restart(reason="bridge")
    state2.request_restart()
    assert state2.restart_reason == "bridge"


# ---------------------------------------------------------------- web shutdown before exec


def test_stop_web_retries_until_stopped():
    web = FakeWeb(False, False, True)
    clock = iter(range(100))
    assert app.stop_web(web, deadline_s=30.0, clock=lambda: next(clock)) is True
    assert web.calls == [5.0, 5.0, 5.0]


def test_stop_web_gives_up_at_the_deadline():
    web = FakeWeb(False)
    t = {"now": 0.0}

    def clock():
        t["now"] += 10.0
        return t["now"]

    assert app.stop_web(web, deadline_s=30.0, clock=clock) is False
    assert len(web.calls) >= 3


def test_restart_waits_for_a_slow_web_shutdown(monkeypatch, tmp_path):
    web = FakeWeb(False, False, True)
    standalone(monkeypatch, tmp_path, web=web)
    client, _ = api.run_main(monkeypatch, single_pack_bms(), number_of_packs=1, telemetry_polls=10, restart_after_polls=2, startup=True)
    assert len(web.calls) == 3
    assert len(api.LAST_RUN["execs"]) == 1          # exec only after the third (successful) stop
    assert offline_published(client)
    assert FakeBridge.instances[0].stops == 1


def test_no_exec_while_the_web_server_does_not_stop(monkeypatch, tmp_path):
    web = FakeWeb(False)
    standalone(monkeypatch, tmp_path, web=web)
    client, _ = api.run_main(monkeypatch, single_pack_bms(), number_of_packs=1, telemetry_polls=10, restart_after_polls=2, startup=True)
    assert api.LAST_RUN["execs"] == []
    assert api.LAST_RUN["exit"] == 1
    assert offline_published(client)                 # the cleanup still ran
    assert FakeBridge.instances[0].stops == 1


def test_web_exception_does_not_skip_the_other_cleanup(monkeypatch, tmp_path):
    web = FakeWeb(RuntimeError("boom"))
    standalone(monkeypatch, tmp_path, web=web)
    client, port = api.run_main(monkeypatch, single_pack_bms(), number_of_packs=1, telemetry_polls=10, restart_after_polls=2, startup=True)
    assert offline_published(client)
    assert port.is_open is False
    assert FakeBridge.instances[0].stops == 1
    assert api.LAST_RUN["execs"] == []               # an unknown web state must not be exec'ed over
    assert api.LAST_RUN["exit"] == 1


def test_sigterm_cleans_up_without_exec(monkeypatch, tmp_path):
    web = FakeWeb(True)
    standalone(monkeypatch, tmp_path, web=web)
    client, port = api.run_main(monkeypatch, single_pack_bms(), number_of_packs=1, telemetry_polls=10, signal_after_polls=2, startup=True)
    assert len(web.calls) == 1
    assert offline_published(client)
    assert port.is_open is False
    assert FakeBridge.instances[0].stops == 1
    assert api.LAST_RUN["execs"] == []
    assert api.LAST_RUN["exit"] == 0


def test_bridge_error_at_startup_exits(monkeypatch, tmp_path):
    from seplos_mqtt.bridge import BridgeError

    class Failing(FakeBridge):
        def start(self):
            raise BridgeError("socat missing")

    monkeypatch.setattr(app, "SocatBridge", Failing)
    monkeypatch.setenv("MONITORING_FILE", str(tmp_path / "monitoring.json"))
    monkeypatch.setenv("RS485_REMOTE_IP", "10.0.0.5")
    monkeypatch.delenv("INGRESS_PORT", raising=False)
    client, port = api.run_main(monkeypatch, single_pack_bms(), number_of_packs=1, telemetry_polls=4, startup=True)
    assert not [r for r in client.as_records() if r["topic"].endswith("/sensors")]   # never polled



# ---------------------------------------------------------------- additional startup coverage


def test_unreadable_file_falls_back_to_env(tmp_path, monkeypatch):
    path = tmp_path / "m.json"
    env = {**ENV, "MONITORING_FILE": str(path), "NUMBER_OF_PACKS": "2"}
    app.startup_config(env)                           # creates the valid file
    before = path.read_bytes()

    real = type(path).read_text

    def denied(self, *a, **k):
        if self == path:
            raise PermissionError("denied")
        return real(self, *a, **k)

    monkeypatch.setattr(type(path), "read_text", denied)
    config, store = app.startup_config({**env, "NUMBER_OF_PACKS": "3"})
    assert config == Config.from_env({**env, "NUMBER_OF_PACKS": "3"})
    assert store is not None and store.corrupt and store.problem == "unreadable"
    assert path.read_bytes() == before


def test_valid_envelope_with_invalid_content_falls_back_to_env(tmp_path):
    path = tmp_path / "m.json"
    path.write_text('{"schema_version": 1, "revision": 4, "data": {"connection": {"transport": "tcp"}}}')
    before = path.read_bytes()
    env = {**ENV, "MONITORING_FILE": str(path), "NUMBER_OF_PACKS": "3"}
    config, store = app.startup_config(env)
    assert config == Config.from_env(env)
    assert store is not None and store.corrupt and store.problem == "invalid content"
    assert path.read_bytes() == before


def test_main_exits_1_on_invalid_options(monkeypatch, tmp_path):
    monkeypatch.setenv("MONITORING_FILE", str(tmp_path / "m.json"))
    monkeypatch.setenv("NUMBER_OF_PACKS", "x")
    with pytest.raises(SystemExit) as e:
        app.main()
    assert e.value.code == 1


class _Boom(Exception):
    pass


def test_web_start_failure_keeps_polling(monkeypatch, tmp_path, caplog):
    from seplos_mqtt.web import server

    def boom(self):
        raise _Boom("bind failed")

    monkeypatch.setattr(server.WebThread, "start", boom)
    standalone(monkeypatch, tmp_path)
    monkeypatch.setenv("INGRESS_PORT", "8099")
    client, _ = api.run_main(monkeypatch, single_pack_bms(), number_of_packs=1, telemetry_polls=3, startup=True)
    assert [r for r in client.as_records() if r["topic"] == "seplos/pack-0/sensors"]
    assert api.LAST_RUN["states"][0].web is None
    assert "Settings app could not be started" in caplog.text


def test_saved_callback_requests_a_restart(monkeypatch, tmp_path):
    from seplos_mqtt.monitoring.store import JsonStore
    from seplos_mqtt.web import server

    captured = {}

    class FakeThread:
        def __init__(self, web_app):
            captured["app"] = web_app

        def start(self):
            return 8099

    monkeypatch.setattr(server, "WebThread", FakeThread)
    state = app.AppState(config=api.config())
    app.start_web(state, JsonStore(tmp_path / "m.json"), "8099")
    assert isinstance(state.web, FakeThread)
    assert not state.restart_event.is_set()
    captured["app"]._on_connection_saved(object())    # what the web app calls after a save
    assert state.restart_event.is_set()
    assert state.restart_reason == "connection"
