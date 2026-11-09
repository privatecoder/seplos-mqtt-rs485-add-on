"""Monitoring store, model, migration from the add-on options and the bridge to Config."""
from __future__ import annotations

import dataclasses
import json
import os
import threading

import pytest

from seplos_mqtt.config import Config, ConfigError
from seplos_mqtt.monitoring import store as store_mod
from seplos_mqtt.monitoring.migration import from_legacy_env, load_or_migrate
from seplos_mqtt.monitoring.model import (
    ConfigValidationError,
    ConnectionSettings,
    MonitoringConfig,
)
from seplos_mqtt.monitoring.store import JsonStore, LoadStatus, StaleRevision, StoreCorrupt

# ---------------------------------------------------------------- store


def _write(path, **raw):
    path.write_text(json.dumps(raw), encoding="utf-8")


def test_store_missing(tmp_path):
    s = JsonStore(tmp_path / "m.json")
    assert s.status is LoadStatus.MISSING
    assert s.revision == 0
    assert s.data == {}
    assert s.problem is None


def test_store_round_trip_and_revisions(tmp_path):
    path = tmp_path / "m.json"
    s = JsonStore(path)
    s.commit({"a": 1}, 0)
    assert (s.revision, s.status, s.data) == (1, LoadStatus.VALID, {"a": 1})
    s.commit({"a": 2}, 1)
    assert s.revision == 2
    again = JsonStore(path)
    assert (again.status, again.revision, again.data) == (LoadStatus.VALID, 2, {"a": 2})
    on_disk = json.loads(path.read_text())
    assert on_disk == {"schema_version": 1, "revision": 2, "data": {"a": 2}}


def test_store_stale_revision(tmp_path):
    s = JsonStore(tmp_path / "m.json")
    s.commit({"a": 1}, 0)
    with pytest.raises(StaleRevision) as e:
        s.commit({"a": 2}, 0)
    assert e.value.current == 1
    assert s.data == {"a": 1}
    assert JsonStore(tmp_path / "m.json").data == {"a": 1}


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        ("{not json", "invalid JSON"),
        (json.dumps({"schema_version": 2, "revision": 1, "data": {}}), "unknown schema"),
        (json.dumps([1]), "unknown schema"),
        (json.dumps({"schema_version": 1, "data": {}}), "invalid revision"),
        (json.dumps({"schema_version": 1, "revision": "1", "data": {}}), "invalid revision"),
        (json.dumps({"schema_version": 1, "revision": True, "data": {}}), "invalid revision"),
        (json.dumps({"schema_version": 1, "revision": 0, "data": {}}), "invalid revision"),
        (json.dumps({"schema_version": 1, "revision": 1, "data": []}), "invalid content"),
        ('{"schema_version": true, "revision": 1, "data": {}}', "unknown schema"),
        ('{"schema_version": 1.0, "revision": 1, "data": {}}', "unknown schema"),
        ('{"schema_version": "1", "revision": 1, "data": {}}', "unknown schema"),
        ('{"revision": 1, "data": {}}', "unknown schema"),
    ],
)
def test_store_corrupt(tmp_path, content, problem):
    path = tmp_path / "m.json"
    path.write_text(content, encoding="utf-8")
    s = JsonStore(path)
    assert s.status is LoadStatus.CORRUPT
    assert s.corrupt
    assert s.problem == problem
    assert s.revision == 0
    assert path.read_text(encoding="utf-8") == content


def test_store_unreadable(tmp_path):
    path = tmp_path / "m.json"
    path.write_bytes(b"\xff\xfe\x00")
    s = JsonStore(path)
    assert s.corrupt
    assert s.problem == "unreadable"


def test_store_validator_rejects(tmp_path):
    path = tmp_path / "m.json"
    _write(path, schema_version=1, revision=1, data={"a": 1})
    s = JsonStore(path, validate=lambda body: "b" in body)
    assert s.corrupt
    assert s.problem == "invalid content"
    ok = JsonStore(path, validate=lambda body: "a" in body)
    assert ok.status is LoadStatus.VALID


def test_store_failed_replace_changes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "m.json"
    s = JsonStore(path)
    s.commit({"a": 1}, 0)
    before = path.read_bytes()

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(store_mod.os, "replace", boom)
    with pytest.raises(OSError):
        s.commit({"a": 2}, 1)
    assert s.data == {"a": 1}
    assert s.revision == 1
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["m.json"]


def test_store_reset_keeps_corrupt_file(tmp_path):
    path = tmp_path / "m.json"
    path.write_text("{broken", encoding="utf-8")
    s = JsonStore(path)
    assert s.corrupt
    s.reset({"a": 1})
    assert s.status is LoadStatus.VALID
    assert s.revision == 1
    assert (tmp_path / "m.json.corrupt").read_text(encoding="utf-8") == "{broken"
    assert JsonStore(path).data == {"a": 1}


def test_store_expected_revision_type(tmp_path):
    s = JsonStore(tmp_path / "m.json")
    for bad in (True, 0.0, "0", None):
        with pytest.raises(TypeError):
            s.commit({"a": 1}, bad)
    assert s.status is LoadStatus.MISSING


def test_store_commit_and_save_rejected_when_corrupt(tmp_path):
    path = tmp_path / "m.json"
    path.write_bytes(b"{broken")
    s = JsonStore(path)
    with pytest.raises(StoreCorrupt):
        s.commit({"a": 1}, 0)
    with pytest.raises(StoreCorrupt):
        s.save()
    assert (s.status, s.revision, s.problem) == (LoadStatus.CORRUPT, 0, "invalid JSON")
    assert path.read_bytes() == b"{broken"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["m.json"]


def test_store_semantic_corruption_rejects_commit(tmp_path):
    path = tmp_path / "monitoring.json"
    _write(path, schema_version=1, revision=3, data={"connection": {"transport": "tcp"}})
    before = path.read_bytes()
    cfg, s = load_or_migrate(path, {})
    assert cfg is None
    with pytest.raises(StoreCorrupt):
        s.commit({"x": 1}, 0)
    with pytest.raises(StoreCorrupt):
        s.save()
    assert (s.status, s.revision) == (LoadStatus.CORRUPT, 0)
    assert path.read_bytes() == before


def test_store_reset_backup_failure_aborts(tmp_path, monkeypatch):
    path = tmp_path / "m.json"
    path.write_bytes(b"{broken")
    s = JsonStore(path)

    real_replace = os.replace
    calls = []

    def selective(src, dst):
        calls.append(str(dst))
        if str(dst).endswith(".corrupt"):
            raise OSError("no backup")
        real_replace(src, dst)

    monkeypatch.setattr(store_mod.os, "replace", selective)
    with pytest.raises(OSError):
        s.reset({"a": 1})
    # only the failed backup was attempted; the replacement write never started
    assert calls == [str(tmp_path / "m.json.corrupt")]
    assert path.read_bytes() == b"{broken"
    assert not (tmp_path / "m.json.corrupt").exists()
    assert (s.status, s.revision, s.problem) == (LoadStatus.CORRUPT, 0, "invalid JSON")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["m.json"]


def test_store_reset_write_failure(tmp_path, monkeypatch):
    path = tmp_path / "m.json"
    path.write_bytes(b"{broken")
    s = JsonStore(path)

    def boom(*a):
        raise OSError("disk full")

    monkeypatch.setattr(store_mod.os, "fsync", boom)
    with pytest.raises(OSError):
        s.reset({"a": 1})
    monkeypatch.undo()
    assert (tmp_path / "m.json.corrupt").read_bytes() == b"{broken"
    assert (s.status, s.revision, s.problem) == (LoadStatus.CORRUPT, 0, "reset failed")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["m.json.corrupt"]
    s.reset({"a": 2})
    assert (s.status, s.revision, s.data) == (LoadStatus.VALID, 1, {"a": 2})


def test_store_reset_write_failure_keeps_valid_state(tmp_path, monkeypatch):
    path = tmp_path / "m.json"
    s = JsonStore(path)
    s.commit({"a": 1}, 0)
    s.commit({"a": 2}, 1)
    before = path.read_bytes()

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(store_mod.os, "replace", boom)
    with pytest.raises(OSError):
        s.reset({"b": 1})
    assert (s.status, s.revision, s.data) == (LoadStatus.VALID, 2, {"a": 2})
    assert path.read_bytes() == before


@pytest.mark.parametrize("failing", ["fsync", "write"])
def test_store_failed_write_leaves_no_temp(tmp_path, monkeypatch, failing):
    path = tmp_path / "m.json"
    s = JsonStore(path)
    s.commit({"a": 1}, 0)
    before = path.read_bytes()

    if failing == "fsync":

        def boom(*a):
            raise OSError("io")

        monkeypatch.setattr(store_mod.os, "fsync", boom)
    else:
        real_fdopen = os.fdopen

        class FailingFile:
            def __init__(self, fh):
                self._fh = fh

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._fh.close()

            def write(self, text):
                raise OSError("io")

        monkeypatch.setattr(store_mod.os, "fdopen", lambda *a, **k: FailingFile(real_fdopen(*a, **k)))
    with pytest.raises(OSError):
        s.commit({"a": 2}, 1)
    monkeypatch.undo()
    assert (s.data, s.revision) == ({"a": 1}, 1)
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["m.json"]


def test_store_concurrent_commits_one_wins(tmp_path, monkeypatch):
    path = tmp_path / "m.json"
    s = JsonStore(path)
    s.commit({"a": 0}, 0)
    barrier = threading.Barrier(2, timeout=0.5)
    real_replace = os.replace

    def slow_replace(src, dst):
        try:
            barrier.wait()  # the second thread must not get here while the first is inside
        except threading.BrokenBarrierError:
            pass
        real_replace(src, dst)

    monkeypatch.setattr(store_mod.os, "replace", slow_replace)
    results = []

    def worker(n):
        try:
            s.commit({"a": n}, 1)
            results.append("ok")
        except StaleRevision as e:
            results.append(e.current)

    threads = [threading.Thread(target=worker, args=(n,)) for n in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results, key=str) == [2, "ok"]
    assert s.revision == 2
    assert json.loads(path.read_text())["revision"] == 2


def test_store_snapshot_is_coherent_copy(tmp_path):
    s = JsonStore(tmp_path / "m.json")
    assert s.snapshot() == (LoadStatus.MISSING, None, 0, {})
    s.commit({"a": {"b": 1}}, 0)
    status, problem, revision, data = s.snapshot()
    assert (status, problem, revision, data) == (LoadStatus.VALID, None, 1, {"a": {"b": 1}})
    data["a"]["b"] = 99
    assert s.data == {"a": {"b": 1}}
    (tmp_path / "c.json").write_bytes(b"{x")
    assert JsonStore(tmp_path / "c.json").snapshot() == (LoadStatus.CORRUPT, "invalid JSON", 0, {})


def test_store_snapshot_waits_for_adoption(tmp_path, monkeypatch):
    s = JsonStore(tmp_path / "m.json")
    s.commit({"a": 1}, 0)
    in_adopt, release = threading.Event(), threading.Event()

    def slow_adopt(new, revision):
        s.data = {"a": 2}  # data first ...
        in_adopt.set()
        release.wait(5)  # ... revision only after the release
        s.revision = revision
        s.status = LoadStatus.VALID
        s.problem = None

    monkeypatch.setattr(s, "_adopt", slow_adopt)
    committer = threading.Thread(target=lambda: s.commit({"a": 2}, 1))
    committer.start()
    assert in_adopt.wait(5)
    result = []
    reader = threading.Thread(target=lambda: result.append(s.snapshot()))
    reader.start()
    reader.join(0.3)
    assert reader.is_alive()  # blocked by the store lock, not reading a torn state
    assert result == []
    release.set()
    reader.join(5)
    committer.join(5)
    assert result == [(LoadStatus.VALID, None, 2, {"a": 2})]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_store_rejects_non_finite_numbers(tmp_path, bad):
    path = tmp_path / "m.json"
    s = JsonStore(path)
    s.commit({"a": 1}, 0)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        s.commit({"a": {"deep": [bad]}}, 1)
    assert (s.data, s.revision) == ({"a": 1}, 1)
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["m.json"]


# ---------------------------------------------------------------- model


def _tcp(**kw):
    return {"transport": "tcp", "host": "10.0.0.5", **kw}


def _serial(**kw):
    return {"transport": "serial", "device": "/dev/ttyUSB0", **kw}


def test_model_defaults():
    c = ConnectionSettings.from_dict(_tcp())
    assert c == ConnectionSettings(transport="tcp", host="10.0.0.5")
    assert (c.port, c.baud, c.pack_count, c.maint_firmware) == (4196, "auto", 1, False)
    assert (c.min_cell_voltage, c.max_cell_voltage, c.update_interval) == (2.5, 3.65, 1)
    s = ConnectionSettings.from_dict(_serial())
    assert s.device == "/dev/ttyUSB0"


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({}, [("connection.transport", "required")]),
        ({"transport": "udp"}, [("connection.transport", "value")]),
        ({"transport": 5}, [("connection.transport", "value")]),
        ({"transport": "tcp"}, [("connection.host", "required")]),
        (_tcp(host=""), [("connection.host", "format")]),
        (_tcp(host="a b"), [("connection.host", "format")]),
        (_tcp(host=3), [("connection.host", "type")]),
        (_tcp(port=0), [("connection.port", "range")]),
        (_tcp(port=65536), [("connection.port", "range")]),
        (_tcp(port="4196"), [("connection.port", "type")]),
        (_tcp(port=True), [("connection.port", "type")]),
        ({"transport": "serial"}, [("connection.device", "required")]),
        (_serial(device="ttyUSB0"), [("connection.device", "format")]),
        (_serial(device=1), [("connection.device", "type")]),
        (_tcp(baud=4800), [("connection.baud", "value")]),
        (_tcp(baud="fast"), [("connection.baud", "value")]),
        (_tcp(baud=9600.0), [("connection.baud", "type")]),
        (_tcp(pack_count=0), [("connection.pack_count", "range")]),
        (_tcp(pack_count=17), [("connection.pack_count", "range")]),
        (_tcp(pack_count="2"), [("connection.pack_count", "type")]),
        (_tcp(pack_count=True), [("connection.pack_count", "type")]),
        (_tcp(pack_count=2.0), [("connection.pack_count", "type")]),
        (_tcp(maint_firmware=1), [("connection.maint_firmware", "type")]),
        (_tcp(maint_firmware="true"), [("connection.maint_firmware", "type")]),
        (_tcp(min_cell_voltage="2.5"), [("connection.min_cell_voltage", "type")]),
        (_tcp(max_cell_voltage=True), [("connection.max_cell_voltage", "type")]),
        (
            _tcp(min_cell_voltage=3.7),
            [("connection.min_cell_voltage", "range"), ("connection.max_cell_voltage", "range")],
        ),
        (_tcp(min_cell_voltage=0), [("connection.min_cell_voltage", "range")]),
        (_tcp(max_cell_voltage=5.1), [("connection.max_cell_voltage", "range")]),
        (_tcp(max_cell_voltage=float("nan")), [("connection.max_cell_voltage", "range")]),
        (_tcp(max_cell_voltage=float("inf")), [("connection.max_cell_voltage", "range")]),
        (_tcp(max_cell_voltage=10**400), [("connection.max_cell_voltage", "range")]),
        (
            _tcp(min_cell_voltage="bad", max_cell_voltage=7),
            [("connection.min_cell_voltage", "type"), ("connection.max_cell_voltage", "range")],
        ),
        (_tcp(nominal_cell_voltage=0), [("connection.nominal_cell_voltage", "range")]),
        (_tcp(nominal_cell_voltage=5.1), [("connection.nominal_cell_voltage", "range")]),
        (_tcp(nominal_cell_voltage="x"), [("connection.nominal_cell_voltage", "type")]),
        (_tcp(nominal_cell_voltage=True), [("connection.nominal_cell_voltage", "type")]),
        (_tcp(nominal_cell_voltage=float("nan")), [("connection.nominal_cell_voltage", "range")]),
        (_tcp(nominal_cell_voltage=10**400), [("connection.nominal_cell_voltage", "range")]),
        (_tcp(update_interval=-1), [("connection.update_interval", "range")]),
        (_tcp(update_interval=1.5), [("connection.update_interval", "type")]),
        (_tcp(update_interval=False), [("connection.update_interval", "type")]),
    ],
)
def test_model_invalid(data, expected):
    with pytest.raises(ConfigValidationError) as e:
        ConnectionSettings.from_dict(data)
    assert e.value.problems == expected


def test_model_lists_all_problems():
    with pytest.raises(ConfigValidationError) as e:
        MonitoringConfig.from_dict(
            {"connection": _tcp(host="", port=0, baud=1, pack_count=99, update_interval=-5)}
        )
    assert e.value.problems == [
        ("connection.host", "format"),
        ("connection.port", "range"),
        ("connection.baud", "value"),
        ("connection.pack_count", "range"),
        ("connection.update_interval", "range"),
    ]


def test_model_section_problems():
    with pytest.raises(ConfigValidationError) as e:
        MonitoringConfig.from_dict({})
    assert e.value.problems == [("connection", "required")]
    with pytest.raises(ConfigValidationError) as e:
        MonitoringConfig.from_dict({"connection": []})
    assert e.value.problems == [("connection", "type")]
    with pytest.raises(ConfigValidationError) as e:
        MonitoringConfig.from_dict([])
    assert e.value.problems == [("", "type")]


def test_model_nominal_cell_voltage():
    c = ConnectionSettings.from_dict(_tcp())              # a document without the key
    assert c.nominal_cell_voltage == 3.2
    assert c.to_dict()["nominal_cell_voltage"] == 3.2     # serialized
    c = ConnectionSettings.from_dict(_tcp(nominal_cell_voltage=3))
    assert c.nominal_cell_voltage == 3.0 and isinstance(c.nominal_cell_voltage, float)
    # independent of min/max: no ordering check (nominal below min or above max is accepted)
    ConnectionSettings.from_dict(_tcp(min_cell_voltage=3.0, max_cell_voltage=3.5, nominal_cell_voltage=2.0))
    ConnectionSettings.from_dict(_tcp(min_cell_voltage=3.0, max_cell_voltage=3.5, nominal_cell_voltage=4.0))
    # reported next to the other problems
    with pytest.raises(ConfigValidationError) as e:
        ConnectionSettings.from_dict(_tcp(pack_count=0, nominal_cell_voltage=9))
    assert e.value.problems == [("connection.pack_count", "range"), ("connection.nominal_cell_voltage", "range")]


def test_model_int_accepted_for_float():
    c = ConnectionSettings.from_dict(_tcp(min_cell_voltage=3, max_cell_voltage=4))
    assert (c.min_cell_voltage, c.max_cell_voltage) == (3.0, 4.0)
    assert isinstance(c.min_cell_voltage, float)


def test_model_accepts_numeric_baud():
    assert ConnectionSettings.from_dict(_tcp(baud=9600)).baud == 9600
    assert ConnectionSettings.from_dict(_tcp(baud=19200)).baud == 19200


def test_model_unknown_keys_survive():
    doc = {"connection": _tcp(port=5000), "future": {"x": [1, 2]}, "other": 3}
    cfg = MonitoringConfig.from_dict(doc)
    out = cfg.to_dict()
    assert out["future"] == {"x": [1, 2]}
    assert out["other"] == 3
    assert MonitoringConfig.from_dict(out) == cfg
    assert out["connection"]["port"] == 5000


def test_model_nested_connection_extras_survive():
    extras = {"future_scalar": "uart1", "future_obj": {"a": {"b": 1}}, "future_list": [1, {"c": 2}]}
    doc = {"connection": {**_serial(), **extras}, "future": {"v": 1}}
    cfg = MonitoringConfig.from_dict(doc)
    out = cfg.to_dict()
    for k, v in extras.items():
        assert out["connection"][k] == v
    # input and output are independent objects
    assert out["connection"]["future_obj"] is not doc["connection"]["future_obj"]
    assert out["connection"]["future_list"] is not doc["connection"]["future_list"]
    doc["connection"]["future_obj"]["a"]["b"] = 99
    assert cfg.to_dict()["connection"]["future_obj"] == {"a": {"b": 1}}
    out["connection"]["future_list"].append(3)
    assert cfg.to_dict()["connection"]["future_list"] == [1, {"c": 2}]
    # a change of a known field keeps them
    changed = dataclasses.replace(cfg.connection, pack_count=5)
    assert changed.to_dict()["pack_count"] == 5
    assert changed.to_dict()["future_scalar"] == "uart1"
    assert changed.to_dict()["future_obj"] == {"a": {"b": 1}}
    assert changed.to_dict()["future_list"] == [1, {"c": 2}]
    # known but inapplicable keys are canonicalised away, not kept as extras
    cfg2 = MonitoringConfig.from_dict({"connection": {**_serial(), "host": "h", "port": 5}})
    assert "host" not in cfg2.to_dict()["connection"]
    assert "port" not in cfg2.to_dict()["connection"]


def test_model_to_dict_round_trip():
    for conn in (_tcp(port=5000, baud=9600, pack_count=3), _serial(maint_firmware=True)):
        cfg = MonitoringConfig.from_dict({"connection": conn})
        assert MonitoringConfig.from_dict(cfg.to_dict()) == cfg


# ---------------------------------------------------------------- migration


def test_migration_tcp_with_and_without_port():
    cfg = from_legacy_env({"RS485_REMOTE_IP": " 192.168.1.9 ", "RS485_REMOTE_PORT": "5000"})
    assert (cfg.connection.transport, cfg.connection.host, cfg.connection.port) == ("tcp", "192.168.1.9", 5000)
    cfg = from_legacy_env({"RS485_REMOTE_IP": "192.168.1.9", "RS485_REMOTE_PORT": ""})
    assert cfg.connection.port == 4196
    cfg = from_legacy_env({"RS485_REMOTE_IP": "gw.local"})
    assert cfg.connection.port == 4196


def test_migration_serial():
    cfg = from_legacy_env({"SERIAL_INTERFACE": "/dev/ttyUSB1", "RS485_REMOTE_IP": ""})
    assert (cfg.connection.transport, cfg.connection.device) == ("serial", "/dev/ttyUSB1")
    assert from_legacy_env({}).connection.device == "/tmp/vcom0"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "auto"), ("", "auto"), ("auto", "auto"), ("AUTO", "auto"), ("9600", 9600), ("19200", 19200)],
)
def test_migration_baud(value, expected):
    env = {} if value is None else {"SERIAL_BAUDRATE": value}
    assert from_legacy_env(env).connection.baud == expected


def test_migration_nominal_cell_voltage():
    assert from_legacy_env({"NOMINAL_CELL_VOLTAGE": "3.3"}).connection.nominal_cell_voltage == 3.3
    assert from_legacy_env({"NOMINAL_CELL_VOLTAGE": ""}).connection.nominal_cell_voltage == 3.2
    assert from_legacy_env({}).connection.nominal_cell_voltage == 3.2
    with pytest.raises(ConfigError):
        from_legacy_env({"NOMINAL_CELL_VOLTAGE": "high"})
    with pytest.raises(ConfigValidationError) as e:
        from_legacy_env({"NOMINAL_CELL_VOLTAGE": "9"})
    assert ("connection.nominal_cell_voltage", "range") in e.value.problems


def test_load_or_migrate_document_without_nominal_loads_default(tmp_path):
    path = tmp_path / "monitoring.json"
    _write(path, schema_version=1, revision=1, data={"connection": _tcp()})   # written before beta8
    cfg, store = load_or_migrate(path, {"NOMINAL_CELL_VOLTAGE": "3.3"})
    assert cfg is not None and cfg.connection.nominal_cell_voltage == 3.2     # the env is not consulted again
    assert store.revision == 1


def test_migration_other_values():
    cfg = from_legacy_env(
        {
            "NUMBER_OF_PACKS": "4",
            "MAINT_FIRMWARE": "true",
            "MIN_CELL_VOLTAGE": "2.8",
            "MAX_CELL_VOLTAGE": "3.6",
            "MQTT_UPDATE_INTERVAL": "10",
        }
    )
    c = cfg.connection
    assert (c.pack_count, c.maint_firmware, c.min_cell_voltage, c.max_cell_voltage, c.update_interval) == (
        4,
        True,
        2.8,
        3.6,
        10,
    )
    assert from_legacy_env({"MQTT_UPDATE_INTERVAL": ""}).connection.update_interval == 1


@pytest.mark.parametrize(
    "env",
    [
        {"NUMBER_OF_PACKS": "two"},
        {"MAINT_FIRMWARE": "maybe"},
        {"MIN_CELL_VOLTAGE": "low"},
        {"MQTT_UPDATE_INTERVAL": "1.5"},
        {"RS485_REMOTE_IP": "h", "RS485_REMOTE_PORT": "x"},
        {"SERIAL_BAUDRATE": "4800"},
    ],
)
def test_migration_malformed_raises(env):
    with pytest.raises(ConfigError):
        from_legacy_env(env)


def test_migration_invalid_results_raise():
    with pytest.raises(ConfigValidationError) as e:
        from_legacy_env({"MIN_CELL_VOLTAGE": "3.7", "MAX_CELL_VOLTAGE": "3.6"})
    assert ("connection.min_cell_voltage", "range") in e.value.problems
    with pytest.raises(ConfigValidationError) as e:
        from_legacy_env({"NUMBER_OF_PACKS": "17", "SERIAL_INTERFACE": "relative"})
    assert ("connection.pack_count", "range") in e.value.problems
    assert ("connection.device", "format") in e.value.problems


def test_load_or_migrate_first_then_unchanged(tmp_path):
    path = tmp_path / "monitoring.json"
    env = {"RS485_REMOTE_IP": "10.1.1.1", "NUMBER_OF_PACKS": "3"}
    cfg, store = load_or_migrate(path, env)
    assert cfg is not None
    assert store.revision == 1
    assert json.loads(path.read_text())["revision"] == 1
    before = path.read_bytes()
    mtime = os.stat(path).st_mtime_ns

    cfg2, store2 = load_or_migrate(path, {"NUMBER_OF_PACKS": "9", "RS485_REMOTE_IP": "other"})
    assert cfg2 == cfg
    assert store2.revision == 1
    assert path.read_bytes() == before
    assert os.stat(path).st_mtime_ns == mtime


def test_load_or_migrate_corrupt_left_alone(tmp_path):
    path = tmp_path / "monitoring.json"
    path.write_bytes(b"{garbage")
    cfg, store = load_or_migrate(path, {})
    assert cfg is None
    assert store.corrupt
    assert path.read_bytes() == b"{garbage"


def test_load_or_migrate_invalid_content_is_corrupt(tmp_path):
    path = tmp_path / "monitoring.json"
    _write(path, schema_version=1, revision=1, data={"connection": {"transport": "tcp"}})
    before = path.read_bytes()
    cfg, store = load_or_migrate(path, {})
    assert cfg is None
    assert store.corrupt
    assert store.problem == "invalid content"
    assert path.read_bytes() == before


def test_load_or_migrate_huge_integer_is_corrupt(tmp_path):
    path = tmp_path / "monitoring.json"
    huge = "1" + "0" * 400
    path.write_text(
        '{"schema_version": 1, "revision": 1, "data": {"connection": '
        '{"transport": "tcp", "host": "h", "max_cell_voltage": ' + huge + "}}}",
        encoding="utf-8",
    )
    before = path.read_bytes()
    cfg, store = load_or_migrate(path, {})
    assert cfg is None
    assert store.corrupt
    assert store.problem == "invalid content"
    assert path.read_bytes() == before


def test_load_or_migrate_invalid_env_writes_nothing(tmp_path):
    path = tmp_path / "monitoring.json"
    with pytest.raises(ConfigError):
        load_or_migrate(path, {"NUMBER_OF_PACKS": "x"})
    assert not path.exists()


# ---------------------------------------------------------------- Config.from_monitoring

_MQTT = {
    "MQTT_HOST": "broker",
    "MQTT_PORT": "1884",
    "MQTT_USERNAME": "u",
    "MQTT_PASSWORD": "p",
    "MQTT_TOPIC": "bms",
    "ENABLE_HA_DISCOVERY_CONFIG": "false",
    "HA_DISCOVERY_PREFIX": "ha",
    "INVERT_HA_DIS_CHARGE_MEASUREMENTS": "false",
    "LOGGING_LEVEL": "debug",
    "STATE_FILE": "/tmp/state.json",
}


def test_from_monitoring_matches_from_env_tcp():
    legacy = {
        **_MQTT,
        "RS485_REMOTE_IP": "10.0.0.5",
        "SERIAL_INTERFACE": "/tmp/vcom0",
        "SERIAL_BAUDRATE": "9600",
        "NUMBER_OF_PACKS": "4",
        "MAINT_FIRMWARE": "true",
        "MIN_CELL_VOLTAGE": "2.8",
        "MAX_CELL_VOLTAGE": "3.6",
        "NOMINAL_CELL_VOLTAGE": "3.3",
        "MQTT_UPDATE_INTERVAL": "7",
    }
    cfg = Config.from_monitoring(from_legacy_env(legacy), legacy)
    assert cfg == Config.from_env(legacy)
    assert cfg.nominal_cell_voltage == 3.3


def test_from_monitoring_matches_from_env_serial_auto():
    legacy = {**_MQTT, "SERIAL_INTERFACE": "/dev/ttyUSB0", "SERIAL_BAUDRATE": "auto", "MQTT_UPDATE_INTERVAL": "1"}
    cfg = Config.from_monitoring(from_legacy_env(legacy), legacy)
    assert cfg == Config.from_env(legacy)
    assert cfg.serial_baudrate == 0
    assert cfg.serial_interface == "/dev/ttyUSB0"


def test_from_monitoring_tcp_uses_pty_path():
    mon = MonitoringConfig.from_dict({"connection": _tcp()})
    cfg = Config.from_monitoring(mon, {})
    assert cfg.serial_interface == "/tmp/vcom0"
    assert cfg.number_of_packs == 1


def test_from_monitoring_invalid_mqtt_port_raises():
    mon = MonitoringConfig.from_dict({"connection": _tcp()})
    with pytest.raises(ConfigError):
        Config.from_monitoring(mon, {"MQTT_PORT": "abc"})
    with pytest.raises(ConfigError):
        Config.from_monitoring(mon, {"MQTT_PORT": "70000"})


# ---------------------------------------------------------------- bus choice, bus quiet


def test_bus_defaults_from_pack_count():
    assert ConnectionSettings.from_dict(_serial(pack_count=3)).bus == "uart1"
    s = ConnectionSettings.from_dict(_serial(pack_count=1))
    assert (s.bus, s.bus_quiet_ms) == ("uart0", 1000)


def test_bus_and_quiet_round_trip():
    s = ConnectionSettings.from_dict(_serial(pack_count=1, bus="uart1", bus_quiet_ms=300))
    assert (s.bus, s.bus_quiet_ms) == ("uart1", 300)
    assert ConnectionSettings.from_dict(s.to_dict()) == s


def test_several_packs_are_always_on_uart1():
    assert ConnectionSettings.from_dict(_serial(pack_count=2, bus="uart0")).bus == "uart1"


@pytest.mark.parametrize(("bus", "kind"), [("can", "value"), (1, "value")])
def test_bus_value(bus, kind):
    with pytest.raises(ConfigValidationError) as e:
        ConnectionSettings.from_dict(_serial(bus=bus))
    assert ("connection.bus", kind) in e.value.problems


@pytest.mark.parametrize(("ms", "problem"), [(199, "range"), (200, None), (2000, None), (2001, "range"),
                                             (1.5, "type"), (True, "type")])
def test_bus_quiet_range(ms, problem):
    if problem is None:
        assert ConnectionSettings.from_dict(_serial(bus_quiet_ms=ms)).bus_quiet_ms == ms
    else:
        with pytest.raises(ConfigValidationError) as e:
            ConnectionSettings.from_dict(_serial(bus_quiet_ms=ms))
        assert ("connection.bus_quiet_ms", problem) in e.value.problems


def test_config_bus_from_monitoring():
    mon = MonitoringConfig.from_dict({"connection": _serial(pack_count=1, bus="uart1", bus_quiet_ms=500)})
    c = Config.from_monitoring(mon, {})
    assert (c.bus, c.can_bus, c.multi_pack, c.baudrate, c.bus_quiet_ms) == ("uart1", True, False, 9600, 500)


def test_config_legacy_bus_follows_pack_count():
    c = Config.from_env({"NUMBER_OF_PACKS": "1"})
    assert (c.can_bus, c.baudrate, c.bus_quiet_ms) == (False, 19200, 1000)
    c = Config.from_env({"NUMBER_OF_PACKS": "3"})
    assert (c.can_bus, c.baudrate) == (True, 9600)
