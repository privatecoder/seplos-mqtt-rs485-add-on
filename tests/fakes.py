"""Hardware-free fakes for the serial bus and the MQTT client."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

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


def parse_request(raw: bytes) -> Request:
    if raw[:1] == b"~":
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

    def respond(self, raw: bytes) -> bytes:
        req = parse_request(raw)
        self.requests.append(req)
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

    def publish(self, topic: str, payload=None, qos: int = 0, retain: bool = False):
        if isinstance(payload, str) and payload[:1] in ("{", "["):
            payload = json.loads(payload)
        self.published.append(Published(topic, payload, qos, retain))
        return _Info()

    def is_connected(self) -> bool:
        return self.connected

    def as_records(self) -> list[dict]:
        return [{"topic": p.topic, "payload": p.payload, "qos": p.qos, "retain": p.retain} for p in self.published]
