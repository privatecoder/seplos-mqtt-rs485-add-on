"""Hardware-free fakes for the serial bus and the MQTT client."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from frames import ascii_frame, modbus_crc, modbus_exception, modbus_response

# A responder gets the decoded request and returns the raw bytes to put on the wire (or b"" for silence)
Responder = Callable[["Request"], bytes]


@dataclass
class Request:
    raw: bytes
    kind: str                 # "ascii" or "modbus"
    adr: int | None = None
    cid2: int | None = None
    info: bytes = b""         # decoded binary INFO
    fc: int | None = None
    register: int | None = None
    count: int | None = None


class ProtocolError(AssertionError):
    pass


def check_ascii_request(raw: bytes) -> None:
    """Independent validation of an outbound Seplos ASCII request (SOI/EOI, VER, CID1, LENGTH, CHKSUM)."""
    if raw[:1] != b"~" or raw[-1:] != b"\r":
        raise ProtocolError(f"bad SOI/EOI: {raw!r}")
    body = raw[1:-5]
    if (-sum(body)) & 0xFFFF != int(raw[-5:-1], 16):
        raise ProtocolError(f"bad checksum: {raw!r}")
    if raw[1:3] != b"20" or raw[5:7] != b"46":
        raise ProtocolError(f"bad VER/CID1: {raw!r}")
    length = int(raw[9:13], 16)
    lenid = length & 0x0FFF
    info = raw[13:-5]
    if lenid != len(info):
        raise ProtocolError(f"LENGTH {lenid} != INFO {len(info)}: {raw!r}")
    if lenid:
        nibble = (-(lenid & 0xF) - ((lenid >> 4) & 0xF) - ((lenid >> 8) & 0xF)) & 0xF
        if length >> 12 != nibble:
            raise ProtocolError(f"bad LENGTH check nibble: {raw!r}")
    elif length != 0:
        raise ProtocolError(f"bad empty LENGTH: {raw!r}")
    bytes.fromhex(info.decode())


def parse_request(raw: bytes) -> Request:
    if raw[:1] == b"\x55":
        import binascii
        assert len(raw) == raw[3] + 7 and raw[-1] == 0xAA, "bad binary request"
        assert binascii.crc_hqx(raw[1:-3], 0) == int.from_bytes(raw[-3:-1], "big"), "bad binary CRC"
        return Request(raw=raw, kind="binary", adr=raw[1], cid2=raw[2], info=raw[4:-3])
    if raw[:1] == b"~":
        check_ascii_request(raw)
        info_ascii = raw[13:-5]
        return Request(raw=raw, kind="ascii", adr=int(raw[3:5], 16), cid2=int(raw[7:9], 16),
                       info=bytes.fromhex(info_ascii.decode()))
    assert modbus_crc(raw[:-2]) == int.from_bytes(raw[-2:], "little"), "bad modbus CRC in request"
    return Request(raw=raw, kind="modbus", adr=raw[0], fc=raw[1],
                   register=int.from_bytes(raw[2:4], "big"), count=int.from_bytes(raw[4:6], "big"))


class FakeBms:
    """
    Answers requests like a set of packs would.
    `ascii[(adr, cid2)]` = binary INFO (RTN 00) | ("rtn", code) | None (silence) | callable(req)->bytes
    `modbus[(fc, register, count)]` = list of registers | ("exc", code) | None
    """

    def __init__(self) -> None:
        self.ascii: dict[tuple[int, int], object] = {}
        self.modbus: dict[tuple[int, int, int], object] = {}
        self.requests: list[Request] = []
        self.binary: dict[tuple[int, int], object] = {}   # (adr, cmd) -> DATA | ("rej", code) | callable
        self.echo_binary = False
        self.protocol_errors: list[str] = []
        self.noise: bytes = b""        # bytes put on the wire before the next answer (e.g. intra-pack traffic)

    def respond(self, raw: bytes) -> bytes:
        try:
            req = parse_request(raw)
        except (ProtocolError, ValueError, AssertionError) as e:
            self.protocol_errors.append(str(e))
            return b""
        noise, self.noise = self.noise, b""
        return noise + self._answer(req)

    def _answer(self, req: Request) -> bytes:
        self.requests.append(req)
        if req.kind == "binary":
            from frames import binary_frame
            entry = self.binary.get((req.adr, req.cid2))
            echo = req.raw if self.echo_binary else b""
            if entry is None:
                return echo
            if callable(entry):
                return echo + entry(req)
            if isinstance(entry, tuple) and entry[0] == "rej":
                return echo + binary_frame(req.adr, entry[1])
            return echo + binary_frame(req.adr, req.cid2, entry)
        if req.kind == "ascii":
            entry = self.ascii.get((req.adr, req.cid2))
            if entry is None:
                return b""
            if callable(entry):
                return entry(req)
            if isinstance(entry, tuple) and entry[0] == "rtn":
                return ascii_frame(req.adr, entry[1], b"")
            if isinstance(entry, tuple) and entry[0] == "raw_rtn":   # (raw_rtn, rtn, info)
                return ascii_frame(req.adr, entry[1], entry[2])
            return ascii_frame(req.adr, 0x00, entry)
        entry = self.modbus.get((req.fc, req.register, req.count))
        if entry is None:
            return b""
        if isinstance(entry, tuple) and entry[0] == "exc":
            return modbus_exception(req.adr, req.fc, entry[1])
        if isinstance(entry, tuple) and entry[0] == "raw":
            return entry[1]
        return modbus_response(req.adr, req.fc, entry)


class FakeSerial:
    """Minimal pyserial stand-in: every write is answered by the FakeBms into an RX buffer."""

    def __init__(self, bms: FakeBms) -> None:
        self.bms = bms
        self.rx = bytearray()
        self.is_open = True
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        self.rx += self.bms.respond(bytes(data))
        return len(data)

    def read_until(self, terminator: bytes = b"\n", size: int | None = None) -> bytes:
        idx = self.rx.find(terminator)
        end = len(self.rx) if idx < 0 else idx + len(terminator)
        out = bytes(self.rx[:end])
        del self.rx[:end]
        return out

    def read(self, size: int = 1) -> bytes:
        out = bytes(self.rx[:size])
        del self.rx[:size]
        return out

    def flushInput(self) -> None:  # noqa: N802 - pyserial legacy name
        self.rx.clear()

    def flushOutput(self) -> None:  # noqa: N802
        pass

    reset_input_buffer = flushInput

    def reset_output_buffer(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False


@dataclass
class Published:
    topic: str
    payload: object
    qos: int
    retain: bool


class _Info:
    rc = 0
    mid = 0

    def is_published(self) -> bool:
        return True

    def wait_for_publish(self, timeout: float | None = None) -> None:
        return None


@dataclass
class FakeMqtt:
    published: list[Published] = field(default_factory=list)
    connected: bool = True

    fail_next: int = 0
    subscriptions: list = field(default_factory=list)

    def publish(self, topic: str, payload=None, qos: int = 0, retain: bool = False):
        if self.fail_next > 0:
            self.fail_next -= 1
            info = _Info()
            info.rc = 4   # MQTT_ERR_NO_CONN
            return info
        if isinstance(payload, str) and payload[:1] in ("{", "["):
            payload = json.loads(payload)
        self.published.append(Published(topic, payload, qos, retain))
        return _Info()

    def subscribe(self, topic, qos=0):
        self.subscriptions.append(topic)
        return (0, 1)

    # lifecycle spies
    calls: list[str] = field(default_factory=list)

    def disconnect(self, *args, **kwargs):
        self.calls.append("disconnect")
        self.connected = False

    def loop_stop(self, *args, **kwargs):
        self.calls.append("loop_stop")

    def is_connected(self) -> bool:
        return self.connected

    def as_records(self) -> list[dict]:
        return [{"topic": p.topic, "payload": p.payload, "qos": p.qos, "retain": p.retain} for p in self.published]


class FakeConnection:
    """Stand-in for MqttConnection around a FakeMqtt."""

    def __init__(self, client: FakeMqtt) -> None:
        import threading
        self.client = client
        self.resync = threading.Event()

    def is_connected(self) -> bool:
        return self.client.connected

    def stop(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()
