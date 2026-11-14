"""
Independent builders for synthetic Seplos wire frames.

Fields are laid out in protocol order from the knowledge base (seplos-ascii.md §5.1/§5.2/§5.4,
intra-pack.md, modbus-rtu.md) and do NOT import the production offset tables, so a wrong
offset in production code shows up as a test failure instead of being mirrored.
"""
from __future__ import annotations

import struct


def u8(v: int) -> bytes:
    return struct.pack(">B", v)


def u16(v: int) -> bytes:
    return struct.pack(">H", v)


def s16(v: int) -> bytes:
    return struct.pack(">h", v)


def kelvin(celsius: float) -> bytes:
    """Temperatures travel as 0.1 K with offset 2731."""
    return u16(round(celsius * 10) + 2731)


def ascii_frame(adr: int, cid2_or_rtn: int, info: bytes, ver: int = 0x20, cid1: int = 0x46) -> bytes:
    """Build a complete ASCII frame ~VER ADR CID1 CID2/RTN LENGTH INFO CHKSUM CR from binary INFO."""
    info_ascii = info.hex().upper().encode()
    lenid = len(info_ascii)
    if lenid:
        lchk = (-(lenid & 0xF) - ((lenid >> 4) & 0xF) - ((lenid >> 8) & 0xF)) & 0xF
        length = (lchk << 12) | lenid
    else:
        length = 0
    body = f"{ver:02X}{adr:02X}{cid1:02X}{cid2_or_rtn:02X}{length:04X}".encode() + info_ascii
    checksum = (-sum(body)) & 0xFFFF
    return b"~" + body + f"{checksum:04X}".encode() + b"\r"


def modbus_crc(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def modbus_response(slave: int, fc: int, regs: list[int]) -> bytes:
    body = bytes([slave, fc, 2 * len(regs)]) + b"".join(u16(r & 0xFFFF) for r in regs)
    return body + modbus_crc(body).to_bytes(2, "little")


def modbus_wide_count_response(slave: int, fc: int, value: int) -> bytes:
    """Seplos single-register answer with a 16-bit byte count (0x0033, 0x0070-0x0074)."""
    body = bytes([slave, fc, 0x00, 0x02]) + u16(value)
    return body + modbus_crc(body).to_bytes(2, "little")


def modbus_exception(slave: int, fc: int, code: int) -> bytes:
    body = bytes([slave, fc | 0x80, code])
    return body + modbus_crc(body).to_bytes(2, "little")


# --- 0x42 telemetry (75 bytes for 16 cells) ------------------------------------------------

DEFAULT_CELLS_MV = [3300 + i for i in range(16)]  # 3300..3315 mV, unique extrema


def telemetry_info(
    cells_mv: list[int] | None = None,
    adr: int = 0,
    cell_temps_c: tuple[float, float, float, float] = (21.0, 22.5, 20.0, 23.4),
    ambient_c: float = 19.5,
    power_c: float = 25.1,
    current_10ma: int = -1234,
    voltage_10mv: int = 5293,
    residual_10mah: int = 15000,
    full_10mah: int = 28001,
    soc_permille: int = 536,
    rated_10mah: int = 28003,
    cycles: int = 42,
    soh: int = 1000,
    port_10mv: int = 5290,
    current_offset_ma: int = -15,
    current_idle_ma: int = 0,
    energy_charged: int = 10000,
    energy_discharged: int = 9000,
) -> bytes:
    cells = DEFAULT_CELLS_MV if cells_mv is None else cells_mv
    out = u8(0x00) + u8(adr) + u8(len(cells))   # DATA FLAG, own address, M
    out += b"".join(u16(v) for v in cells)
    out += u8(6)  # number of temperatures: 4 cell + ambient + power
    out += b"".join(kelvin(t) for t in cell_temps_c)
    out += kelvin(ambient_c) + kelvin(power_c)
    out += s16(current_10ma) + u16(voltage_10mv) + u16(residual_10mah)
    out += u8(0x0A)  # custom count
    out += u16(full_10mah) + u16(soc_permille) + u16(rated_10mah) + u16(cycles) + u16(soh) + u16(port_10mv)
    out += s16(current_offset_ma) + s16(current_idle_ma) + u16(energy_charged) + u16(energy_discharged)
    return out


# --- 0x44 telesignalization (49 bytes for 16 cells) ---------------------------------------

def telesignal_info(
    cell_alarms: list[int] | None = None,
    adr: int = 0,
    temp_alarms: tuple[int, int, int, int] = (0, 0, 0, 0),
    ambient_alarm: int = 0,
    power_alarm: int = 0,
    current_alarm: int = 0,
    pack_voltage_alarm: int = 0,
    events_1_6: tuple[int, int, int, int, int, int] = (0, 0, 0, 0, 0, 0),
    on_off: int = 0x03,
    balancing: tuple[int, int] = (0x00, 0x00),
    system_status: int = 0x01,
    disconnection: tuple[int, int] = (0, 0),
    event7: int = 0,
    event8: int = 0,
) -> bytes:
    alarms = [0] * 16 if cell_alarms is None else cell_alarms
    out = u8(0x00) + u8(adr) + u8(len(alarms)) + bytes(alarms)   # DATA FLAG, own address, M
    out += u8(6) + bytes(temp_alarms) + u8(ambient_alarm) + u8(power_alarm)
    out += u8(current_alarm) + u8(pack_voltage_alarm)
    out += u8(20)  # custom count
    out += bytes(events_1_6) + u8(on_off) + bytes(balancing) + u8(system_status)
    out += bytes(disconnection) + u8(event7) + u8(event8)
    out += bytes(5) + u8(system_status & 0x33)  # reserved, then a masked copy of the system status
    return out


# --- 0x51 / 0xA2 -----------------------------------------------------------------------------

def device_info_info(name: str = "SP16S020", fw: tuple[int, int] = (16, 6), can: str = "Pylon") -> bytes:
    return name.encode().ljust(10, b"\x00") + bytes(fw) + can.encode().ljust(20, b"\x00")


def serial_info(serial: str = "SN-BATTERY-0001") -> bytes:
    return serial.encode().ljust(30, b" ")


# --- 0x47 parameters (169 bytes, ASCII path incl. address) --------------------------------

def parameter_values() -> tuple[list[int], list[int]]:
    """Distinguishable values P0..P59 (u16) and P60..P86 (u8)."""
    words = [
        3550, 3450, 2900, 3000, 3650, 3500, 2700, 2900, 3400, 2000,   # P0-P9 mV
        5680, 5520, 4640, 4800, 5840, 5600, 4320, 4640, 5900, 5800,   # P10-P19 10mV
    ]
    words += [2731 + 450 + i for i in range(36)]                       # P20-P55 0.1K / mixed
    words[50:56] = [15000, 14500, 15500, 15000, 16000, 16500]           # P50-P55 10mA
    words[52:54] = [(-15000) & 0xFFFF, (-14500) & 0xFFFF]                # P52/P53 discharge thresholds are negative
    words += [30000, 300, 28000, 14000]                                 # P56 10mA, P57 raw, P58/P59 10mAh
    bytes_ = [20, 15, 30, 10, 12, 16, 5, 6, 30, 60, 4, 10, 30, 12, 3, 10, 60, 24, 10, 5, 80, 70, 50, 8, 12, 9, 15]
    assert len(words) == 60 and len(bytes_) == 27
    return words, bytes_


def parameter_info(
    switches: bytes = bytes([0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x0F, 0x80]),
    name: str = "SP16S020",
    adr: int = 0,
) -> bytes:
    words, bytes_ = parameter_values()
    out = u8(adr) + u8(0x3C) + b"".join(u16(w) for w in words)
    out += u8(0x1B) + bytes(bytes_)
    out += u8(0x08) + switches
    out += name.encode().ljust(10, b"\x00")
    assert len(out) == 169
    return out


# --- 0x5A intra-pack record (32 bytes) ------------------------------------------------------

def intra_pack_info(
    max_mv: int = 3315, min_mv: int = 3300, max_t: float = 23.4, min_t: float = 20.0,
    current_10ma: int = 37, voltage_10mv: int = 5293, residual: int = 15000, full: int = 28001,
    soc: int = 536, port: int = 5290, events_1_6: bytes = bytes(6), on_off: int = 0x03,
    event7: int = 0x01, event8: int = 0x40, status: int = 0x02,
) -> bytes:
    out = u8(0x00) + u8(0x01) + u16(max_mv) + u16(min_mv) + kelvin(max_t) + kelvin(min_t)
    out += s16(current_10ma) + u16(voltage_10mv) + u16(residual) + u16(full) + u16(soc) + u16(port)
    out += events_1_6 + u8(on_off) + u8(event7) + u8(event8) + u8(status)
    assert len(out) == 32
    return out


# --- Modbus FC03 0x1000 x21 system block -----------------------------------------------------

def system_registers() -> list[int]:
    return [
        0x02,          # 0 state: charging
        0x03,          # 1 charge+discharge allowed
        0x0101,        # 2 alarm word current/voltage
        0x8004,        # 3 alarm word temperature/misc (+ comm fault)
        529,           # 4 system voltage 0.1 V
        (-153) & 0xFFFF,  # 5 system current 0.1 A, signed
        54,            # 6 SOC %
        0,             # 7 unused
        234,           # 8 highest cell temp 0.1 °C
        2,             # 9 pack (1-based)
        (-15) & 0xFFFF,  # 10 lowest cell temp
        1,             # 11 pack
        3315,          # 12 highest cell voltage mV
        2,             # 13
        3300,          # 14 lowest cell voltage
        1,             # 15
        568,           # 16 charge voltage limit 0.1 V
        1000,          # 17 CCL 0.1 A
        1500,          # 18 DCL 0.1 A
        448,           # 19 discharge voltage limit 0.1 V
        560,           # 20 total capacity Ah
    ]


# --- EMU1101-EXT (identity EXT05) ---------------------------------------------------------------

# KB openbms-monitor.md "Firmware identity: ASCII 64/03": EXT, format 05, OEM base, patch version (3 bytes),
# build ID, image flags, read-capability mask (u16 BE); synthetic build IDs
EXT_FULL_FLAGS = 0xFE
EXT_LIGHT_FLAGS = 0x3A


def maint_identity(patch=(1, 6, 4), base=(16, 6, 13), flags=EXT_FULL_FLAGS, caps=0x000F, build="a8676b52bae281ff",
                   signature=b"EXT", info_format=5) -> bytes:
    return signature + u8(info_format) + bytes(base) + bytes(patch) + bytes.fromhex(build) + u8(flags) + u16(caps)


MAINT_FULL_IDENTITY = maint_identity()
MAINT_LIGHT_FLAGS = EXT_LIGHT_FLAGS


def maint_storage(classes=(1, 1, 1, 1, 1, 1), locks=(0, 0), cache=0x06) -> bytes:
    return u8(1) + bytes(classes) + bytes(locks) + u8(cache)


def _soc_record(remaining=15000, full=28000, charged=70000, discharged=69000, cycles=42) -> bytes:
    le = lambda v, n=2: v.to_bytes(n, "little")  # noqa: E731
    body = b"\xa0\x14" + le(remaining) + le(full) + le(charged, 4) + le(discharged, 4) + le(cycles) + le(500) + le(30)
    return body + le(0xBEEF)


def maint_energy(charged=70001, discharged=69002, classes=(1, 1), comparison=1, crc_mask=3, boot=1) -> bytes:
    out = u8(1) + bytes(classes) + u8(comparison) + u8(crc_mask) + u8(boot)
    out += u16(28000) + u16(14000) + struct.pack(">I", charged) + struct.pack(">I", discharged)
    out += u16(15000) + u16(28000) + u16(536)
    out += _soc_record() + _soc_record()
    assert len(out) == 68
    return out


def binary_frame(adr: int, cmd: int, data: bytes = b"") -> bytes:
    import binascii
    body = bytes([adr, cmd, len(data)]) + data
    return b"\x55" + body + binascii.crc_hqx(body, 0).to_bytes(2, "big") + b"\xaa"
