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
