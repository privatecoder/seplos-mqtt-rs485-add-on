"""socat bridge with a fake process: argv, link wait, early exit, timeout, stop."""
from __future__ import annotations

import subprocess

import pytest

from seplos_mqtt import bridge as bridge_mod
from seplos_mqtt.bridge import BridgeError, SocatBridge


class FakeProc:
    def __init__(self, on_poll=None, ignore_terminate=False):
        self.returncode = None
        self.calls = []
        self.on_poll = on_poll
        self.ignore_terminate = ignore_terminate

    def poll(self):
        if self.on_poll:
            self.on_poll(self)
        return self.returncode

    def terminate(self):
        self.calls.append("terminate")
        if not self.ignore_terminate:
            self.returncode = -15

    def kill(self):
        self.calls.append("kill")
        self.returncode = -9

    def wait(self, timeout=None):
        self.calls.append(("wait", timeout))
        if self.returncode is None:
            raise subprocess.TimeoutExpired("socat", timeout)
        return self.returncode


class Popen:
    def __init__(self, proc):
        self.proc = proc
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return self.proc


@pytest.fixture
def clock(monkeypatch):
    t = {"now": 0.0}
    monkeypatch.setattr(bridge_mod.time, "monotonic", lambda: t["now"])
    monkeypatch.setattr(bridge_mod.time, "sleep", lambda s: t.__setitem__("now", t["now"] + s))
    return t


def test_start_spawns_socat_and_waits_for_link(tmp_path, clock):
    link = tmp_path / "vcom0"
    proc = FakeProc(on_poll=lambda p: link.touch() if clock["now"] >= 0.3 else None)
    popen = Popen(proc)
    b = SocatBridge("192.168.1.9", 4196, str(link), popen=popen)
    b.start()
    argv, kwargs = popen.calls[0]
    assert argv == ["socat", f"pty,link={link},raw", "tcp:192.168.1.9:4196,retry,interval=.2,forever"]
    assert kwargs == {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "start_new_session": True}
    assert b.alive()
    assert clock["now"] >= 0.3


def test_process_exits_early(tmp_path, clock):
    proc = FakeProc()
    proc.returncode = 1
    b = SocatBridge("gw.local", 4196, str(tmp_path / "vcom0"), popen=Popen(proc))
    with pytest.raises(BridgeError, match="exited"):
        b.start()
    assert not b.alive()


def test_timeout_terminates_the_process(tmp_path, clock):
    proc = FakeProc()
    b = SocatBridge("gw.local", 4196, str(tmp_path / "vcom0"), popen=Popen(proc), wait_s=1.0)
    with pytest.raises(BridgeError, match="terminated"):
        b.start()
    assert "terminate" in proc.calls
    assert not b.alive()
    assert clock["now"] >= 1.0


def test_popen_failure_is_a_bridge_error(tmp_path):
    def broken(argv, **kwargs):
        raise FileNotFoundError("socat")

    with pytest.raises(BridgeError, match="could not start"):
        SocatBridge("gw.local", 4196, str(tmp_path / "vcom0"), popen=broken).start()


def test_stop_terminates_then_kills(tmp_path, clock):
    link = tmp_path / "vcom0"
    proc = FakeProc(on_poll=lambda p: link.touch(), ignore_terminate=True)
    b = SocatBridge("gw.local", 4196, str(link), popen=Popen(proc))
    b.start()
    b.stop()
    assert proc.calls[0] == "terminate"
    assert ("wait", 3) in proc.calls
    assert "kill" in proc.calls
    b.stop()  # idempotent
    assert proc.calls.count("kill") == 1
    assert not b.alive()


def test_stop_never_raises(tmp_path, clock):
    link = tmp_path / "vcom0"
    proc = FakeProc(on_poll=lambda p: link.touch())

    def boom():
        raise OSError("gone")

    proc.terminate = boom
    b = SocatBridge("gw.local", 4196, str(link), popen=Popen(proc))
    b.start()
    b.stop()


def test_alive_reflects_the_process(tmp_path, clock):
    link = tmp_path / "vcom0"
    proc = FakeProc(on_poll=lambda p: link.touch())
    b = SocatBridge("gw.local", 4196, str(link), popen=Popen(proc))
    assert not b.alive()
    b.start()
    assert b.alive()
    proc.returncode = 1
    assert not b.alive()


def test_restart_removes_a_stale_link(tmp_path, clock):
    target = tmp_path / "pts"
    target.touch()
    link = tmp_path / "vcom0"
    link.symlink_to(target)
    created = []

    def popen(argv, **kwargs):
        assert not link.is_symlink()  # the stale link is gone before socat is spawned
        link.touch()
        created.append(argv)
        return FakeProc()

    SocatBridge("gw.local", 4196, str(link), popen=popen).start()
    assert len(created) == 1


def test_stale_link_removal_failure_is_an_error_without_spawn(tmp_path, monkeypatch):
    target = tmp_path / "pts"
    target.touch()
    link = tmp_path / "vcom0"
    link.symlink_to(target)
    popen = Popen(FakeProc())

    def denied(path):
        raise PermissionError("no")

    monkeypatch.setattr(bridge_mod.os, "unlink", denied)
    with pytest.raises(BridgeError, match="stale link"):
        SocatBridge("gw.local", 4196, str(link), popen=popen).start()
    assert popen.calls == []
    assert link.is_symlink()


def test_existing_regular_file_is_not_replaced(tmp_path):
    link = tmp_path / "vcom0"
    link.write_text("not ours")
    popen = Popen(FakeProc())
    with pytest.raises(BridgeError, match="refusing to replace"):
        SocatBridge("gw.local", 4196, str(link), popen=popen).start()
    assert popen.calls == []
    assert link.read_text() == "not ours"


@pytest.mark.parametrize(
    ("host", "address"),
    [
        ("::1", "[::1]"),
        ("[fe80::1]", "[fe80::1]"),
        ("2001:db8::10", "[2001:db8::10]"),
        ("192.168.1.10", "192.168.1.10"),
        ("gw.local", "gw.local"),
    ],
)
def test_socat_address_rendering(tmp_path, clock, host, address):
    link = tmp_path / "vcom0"
    popen = Popen(FakeProc(on_poll=lambda p: link.touch()))
    SocatBridge(host, 4196, str(link), popen=popen).start()
    assert popen.calls[0][0][2] == f"tcp:{address}:4196,retry,interval=.2,forever"


@pytest.mark.parametrize("host", ["bad::host::x", "1:2", "[::1", "::1]", "[gw]", "gw]", "12345::1::"])
def test_invalid_ipv6_or_brackets_rejected(host):
    with pytest.raises(BridgeError):
        SocatBridge(host, 4196)


@pytest.mark.parametrize("host", ["", "a b", "x;rm -rf /", "host,fork", "a" * 254, "gw/../x", "gw\n"])
def test_invalid_host_rejected(host):
    with pytest.raises(BridgeError):
        SocatBridge(host, 4196)


@pytest.mark.parametrize("host", ["192.168.1.9", "gw.local", "fe80::1", "[::1]", "my-gw"])
def test_valid_hosts(host):
    SocatBridge(host, 4196)
