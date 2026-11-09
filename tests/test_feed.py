"""OpenBMS Monitor feed: raw answers with read times, failure reasons, messages, round order, switch."""
from __future__ import annotations

import pytest

import frames as f
from fakes import FakeBms, FakeSerial
from seplos_mqtt import timeutil
from seplos_mqtt.pack import TELEMETRY_LEN
from seplos_mqtt.transport import BusBusy, SerialBus

# ---------------------------------------------------------------- SerialBus: read time, failure reason, gap


def _bus(bms, clock=None):
    clock = clock if clock is not None else {"t": 0.0}

    def sleep(seconds):
        clock["t"] += seconds

    return SerialBus(FakeSerial(bms), clock=lambda: clock["t"], sleep=sleep)


def test_now_ms(monkeypatch):
    import time
    monkeypatch.setattr(time, "time", lambda: 1234.5678)
    assert timeutil.now_ms() == 1234567


def test_last_at_after_answer(monkeypatch):
    monkeypatch.setattr(timeutil, "now_ms", lambda: 1234)
    bms = FakeBms()
    bms.ascii[(0, 0x42)] = f.telemetry_info()
    bus = _bus(bms)
    assert bus.request(0, 0x42, TELEMETRY_LEN, "Telemetry") is not None
    assert (bus.last_at, bus.last_failure) == (1234, None)


def test_last_failure_failed():
    bus = _bus(FakeBms())
    assert bus.request(0, 0x42, TELEMETRY_LEN, "Telemetry", retries=2) is None
    assert bus.last_failure == "failed"


def test_last_failure_busy():
    bms = FakeBms()
    clock = {"t": 0.0}

    class Noisy(FakeSerial):
        @property
        def in_waiting(self):
            self.rx += b"\x00"
            return len(self.rx)

    bus = SerialBus(Noisy(bms), clock=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s))
    with pytest.raises(BusBusy):
        bus.request(0, 0x42, TELEMETRY_LEN, "Telemetry")
    assert bus.last_failure == "busy"


def test_last_failure_unsupported_modbus():
    bms = FakeBms()
    bms.modbus[(0x03, 0x1000, 21)] = ("exc", 0x02)
    bus = _bus(bms)
    bus.read_modbus(0x03, 0x1000, 21)
    assert bus.last_failure == "unsupported"


def test_set_gap():
    bms = FakeBms()
    bms.ascii[(0, 0x51)] = f.device_info_info()
    clock = {"t": 0.0}
    writes = []

    class Timed(FakeSerial):
        def write(self, data):
            writes.append(clock["t"])
            return super().write(data)

    bus = SerialBus(Timed(bms), clock=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s))
    bus.request_rtn(0, 0x51)
    bus.set_gap(0.2)
    bus.request_rtn(0, 0x51)
    assert writes[1] - writes[0] == pytest.approx(0.2)


# ---------------------------------------------------------------- pack: core/extras split, raw answers

import api  # noqa: E402
from test_characterization import _maint_responder, _multi_pack_bms, _single_pack_bms  # noqa: E402


def test_status_failure_keeps_telemetry(monkeypatch):
    api.no_sleep(monkeypatch)
    with api.mode(1):
        bms = _single_pack_bms()
        pack = api.make_pack(0)
        data, ok = api.poll(pack, bms)
        assert ok
        bms.ascii[(0, 0x42)] = f.telemetry_info(cycles=77)
        bms.ascii[(0, 0x44)] = None
        data, ok = api.poll(pack, bms)
    assert not ok and data is not None
    assert data["telemetry"]["normal"]["charging_cycles"] == 77
    # the last known status groups with every value unknown, the alarm summary unknown
    assert data["telesignalization"] and all(
        v is None for group in data["telesignalization"].values() for v in group.values())
    assert data["alarm_summary"]["normal"]["bms_alarm"] == "unknown"


def test_raw_parts(monkeypatch):
    api.no_sleep(monkeypatch)
    monkeypatch.setattr(timeutil, "now_ms", lambda: 5000)
    with api.mode(2):
        bms = _multi_pack_bms()
        pack = api.make_pack(0)
        api.poll(pack, bms)
        assert pack.raw["r42"] == {"t": 5000, "hex": f.telemetry_info(adr=0).hex()}
        assert pack.raw["r44"] == {"t": 5000, "hex": f.telesignal_info(adr=0).hex()}
        bms.ascii[(0, 0x42)] = None
        api.poll(pack, bms)
    assert pack.raw == {"r42": {"t": 5000, "err": "failed"}}


def test_raw_ids_uart0(monkeypatch):
    api.no_sleep(monkeypatch)
    monkeypatch.setattr(timeutil, "now_ms", lambda: 7000)
    with api.mode(1):
        pack = api.make_pack(0)
        api.poll(pack, _single_pack_bms())
    ids = pack.raw_ids
    assert ids["r51"] == {"t": 7000, "hex": f.device_info_info().hex()}
    assert ids["a2"] == {"t": 7000, "hex": f.serial_info().hex()} and len(ids["a2"]["hex"]) == 60
    assert ids["maint"] is None
    assert pack.raw47 == {"t": 7000, "hex": f.parameter_info().hex()} and len(pack.raw47["hex"]) == 338


def test_raw_ids_and_raw47_uart1_maint(monkeypatch):
    api.no_sleep(monkeypatch)
    monkeypatch.setattr(timeutil, "now_ms", lambda: 9000)
    with api.mode(2, MAINT_FIRMWARE=True):
        bms = _multi_pack_bms()
        bms.ascii[(0, 0x64)] = _maint_responder()
        bms.binary[(0, 0x47)] = f.parameter_info()[1:]
        bms.binary[(0, 0xA2)] = f.serial_info("BAT-SN-0")
        bms.binary[(0, 0xA4)] = f.serial_info("BMS-SN-0")
        bms.echo_binary = True
        pack = api.make_pack(0)
        api.poll(pack, bms)
    ids = pack.raw_ids
    assert ids["a2"]["hex"] == f.serial_info("BAT-SN-0").hex() and len(ids["a2"]["hex"]) == 60
    assert ids["maint"]["hex"] == f.MAINT_FULL_IDENTITY.hex()
    assert pack.raw47["hex"] == (bytes([0]) + f.parameter_info()[1:]).hex() and len(pack.raw47["hex"]) == 338


def test_raw_ids_uart1_oem_has_no_a2(monkeypatch):
    api.no_sleep(monkeypatch)
    with api.mode(2):
        pack = api.make_pack(0)
        api.poll(pack, _multi_pack_bms())
    assert pack.raw_ids["a2"] is None and pack.raw_ids["r51"] is not None


def test_status_failure_keeps_maint_energy_source(monkeypatch, tmp_path):
    api.no_sleep(monkeypatch)
    with api.mode(1, MAINT_FIRMWARE=True):
        bms = _single_pack_bms()
        bms.ascii[(0, 0x42)] = f.telemetry_info(energy_charged=6500)
        bms.ascii[(0, 0x64)] = _maint_responder()
        pack = api.make_pack(0, state_file=tmp_path / "state.json")
        data, ok = api.poll(pack, bms)
        assert ok and data["telemetry"]["normal"]["energy_charged"] == 7000.1
        bms.ascii[(0, 0x44)] = None
        data, ok = api.poll(pack, bms)
    assert not ok
    assert data["telemetry"]["normal"]["energy_charged"] in (7000.1, None)      # never the narrow 650.0


# ---------------------------------------------------------------- feed messages, round order, system raw

from unittest.mock import ANY  # noqa: E402

from seplos_mqtt import feed, topics  # noqa: E402
from seplos_mqtt.monitoring.model import ParallelSet  # noqa: E402


def test_feed_topics():
    assert topics.feed_pack("seplos", 3) == "seplos/openbms/v1/pack/3"
    assert topics.feed_system("seplos") == "seplos/openbms/v1/system"
    assert topics.feed_meta("seplos") == "seplos/openbms/v1/meta"


def test_round_order_sets_first_ends():
    s = ParallelSet(id=1, name="A", packs=(2, 3, 4), wiring="diagonal", plus=4, minus=2)
    assert feed.round_order(6, [s]) == [[0], [1], [4, 2, 3], [5]]


def test_round_order_no_sets():
    assert feed.round_order(3, []) == [[0], [1], [2]]


def test_round_order_ignores_missing_pack():
    assert feed.round_order(2, [ParallelSet(id=1, name="A", packs=(1, 5))]) == [[0], [1]]


def test_round_order_pair_without_ends():
    assert feed.round_order(4, [ParallelSet(id=1, name="A", packs=(3, 1))]) == [[0], [3, 1], [2]]


def test_pack_message_shape():
    fd = feed.Feed(boot="ab")
    m = fd.pack_message(0, {"r42": {"t": 1, "hex": "00"}}, None)
    assert m == {"v": 1, "boot": "ab", "seq": 1, "sent_at": ANY, "addr": 0, "r42": {"t": 1, "hex": "00"}}
    m = fd.pack_message(1, {"r42": {"t": 2, "err": "failed"}}, {"t": 0, "hex": "11"})
    assert m["seq"] == 2 and m["r47"] == {"t": 0, "hex": "11"}


def test_seq_is_global():
    fd = feed.Feed(boot="ab")
    seqs = [fd.pack_message(0, {}, None)["seq"], fd.system_message({})["seq"], fd.pack_message(1, {}, None)["seq"]]
    assert seqs == [1, 2, 3]


def test_boot_is_random_hex():
    a, b = feed.Feed(), feed.Feed()
    assert a.boot != b.boot and len(a.boot) == 16 and int(a.boot, 16) >= 0


def test_meta_contains_sets_and_cache(monkeypatch):
    monkeypatch.setenv("ADDON_VERSION", "2.0.0-test")
    api.no_sleep(monkeypatch)
    with api.mode(2):
        pack = api.make_pack(0)
        api.poll(pack, _multi_pack_bms())
        s = ParallelSet(id=7, name="A", packs=(0, 1), wiring="diagonal", plus=0, minus=1)
        fd = feed.Feed(boot="ab", on=True)
        m = fd.meta_message(api.config(), [pack], [s])
    assert set(m) == {"v", "on", "auto_off", "boot", "sent_at", "bus", "addon", "packs", "sets"}
    assert (m["on"], m["auto_off"], m["bus"], m["addon"]) == (True, None, "uart1", "2.0.0-test")
    assert m["packs"] == [{"addr": 0, **pack.raw_ids, "r47": pack.raw47}]
    assert m["sets"] == [{"id": 7, "packs": [0, 1], "wiring": "diagonal", "plus": 0, "minus": 1}]


def test_system_raw(monkeypatch):
    api.no_sleep(monkeypatch)
    monkeypatch.setattr(timeutil, "now_ms", lambda: 4242)
    bms = _multi_pack_bms()
    reader = api.new_system_reader()
    api.read_system_with(reader, bms)
    regs = f.system_registers()
    assert reader.raw["r1000"] == {"t": 4242, "hex": b"".join(r.to_bytes(2, "big") for r in regs).hex()}
    assert reader.raw["r0074"] == {"t": 4242, "hex": "00c0"}
    del bms.modbus[(0x03, 0x0074, 1)]
    api.read_system_with(reader, bms)
    assert reader.raw["r0074"]["err"] == "failed" and "hex" in reader.raw["r1000"]
