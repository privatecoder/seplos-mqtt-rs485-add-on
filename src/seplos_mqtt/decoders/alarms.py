"""CID2 0x44 telesignalization (alarms, switches, balancing, system status)."""
from __future__ import annotations

from dataclasses import dataclass

# 24 byte alarms: one status byte per value
BYTE_ALARM_STATES = {0: "OK", 1: "Alarm (low)", 2: "Alarm (high)"}
BYTE_ALARM_OTHER = "Alarm (other)"

# The layout depends on the cell count M: cell alarms at 3..2+M, all later fields relative to
# BASE = 3 + M (offsets below for 16 cells: BASE = 19).
# key: (offset relative to BASE, amount); the cell alarms are absolute at 3 with M entries.
CELL_ALARM_OFFSET = 3
BYTE_ALARMS: dict[str, tuple[int, int]] = {
    "cell_temperature_alarm":      (1, 4),   # doc: "Cell temperature alarm 1..4"
    "ambient_temperature_alarm":   (5, 1),   # doc: "Environment temperature alarm"
    "component_temperature_alarm": (6, 1),   # doc: "Power temperature alarm"
    "dis_charging_current_alarm":  (7, 1),   # doc: "Charge/discharge current alarm"
    "pack_voltage_alarm":          (8, 1),   # doc: "Total battery voltage alarm"
}

# event byte offsets relative to BASE
EVENT_1, EVENT_2, EVENT_3, EVENT_4, EVENT_5, EVENT_6 = 10, 11, 12, 13, 14, 15
ON_OFF, BALANCING_1, BALANCING_2, SYSTEM_STATUS = 16, 17, 18, 19
EVENT_7, EVENT_8 = 22, 23


def info_length(cell_count: int) -> int:
    """Binary INFO length of the 0x44 answer."""
    return 33 + cell_count


# Single bit modes: (active, inactive) payload
SINGLE_BIT_MODES = {
    "on_off": ("ON", "OFF"),
    "fault_normal": ("Fault", "OK"),
    "warning_normal": ("Warning", "OK"),
    "protection_normal": ("Protection", "OK"),
}
# Two bit modes: the more severe state (second bit) takes precedence, as the alarm bit usually
# stays set while the protection/lockout is active. (severe, mild)
TWO_BIT_MODES = {
    "protection_alarm_normal": ("Protection", "Alarm"),
    "lockout_protection_normal": ("Lockout", "Protection"),
}


@dataclass(frozen=True)
class BitField:
    key: str
    offset: int              # relative to BASE
    mode: str
    bit: int
    severe_bit: int | None = None
    amount: int = 1          # consecutive bits -> key_1..key_n
    start: int = 0           # index offset for key_<start+1>

    @property
    def is_binary(self) -> bool:
        """Two-bit states are multi-state sensors, everything else is published as a binary payload."""
        return self.mode not in TWO_BIT_MODES

    def payloads(self) -> tuple[str, str]:
        return SINGLE_BIT_MODES[self.mode]


BIT_FIELDS: list[BitField] = [
    # Alarm event 1: sensor/switch faults
    BitField("voltage_sensing_failure", EVENT_1, "fault_normal", 0),          # "Voltage sensor fault"
    BitField("temperature_sensing_failure", EVENT_1, "fault_normal", 1),      # "Temperature sensor fault"
    BitField("current_sensing_failure", EVENT_1, "fault_normal", 2),          # "Current sensor fault"
    BitField("power_switch_failure", EVENT_1, "fault_normal", 3),             # "Key switch fault"
    BitField("cell_voltage_difference_fault", EVENT_1, "fault_normal", 4),    # "Cell voltage dropout fault", delta >= P60
    BitField("charging_switch_failure", EVENT_1, "fault_normal", 5),          # "Charge switch fault"
    BitField("discharging_switch_failure", EVENT_1, "fault_normal", 6),       # "Discharge switch fault"
    BitField("current_limit_switch_failure", EVENT_1, "fault_normal", 7),     # "Current limit switch fault"
    # Alarm event 2: voltage, bit pairs alarm/protection
    BitField("cell_overvoltage", EVENT_2, "protection_alarm_normal", 0, 1),
    BitField("cell_voltage_low", EVENT_2, "protection_alarm_normal", 2, 3),
    BitField("pack_overvoltage", EVENT_2, "protection_alarm_normal", 4, 5),
    BitField("pack_voltage_low", EVENT_2, "protection_alarm_normal", 6, 7),
    # Alarm event 3: cell temperature
    BitField("charging_temperature_high", EVENT_3, "protection_alarm_normal", 0, 1),
    BitField("charging_temperature_low", EVENT_3, "protection_alarm_normal", 2, 3),
    BitField("discharging_temperature_high", EVENT_3, "protection_alarm_normal", 4, 5),
    BitField("discharging_temperature_low", EVENT_3, "protection_alarm_normal", 6, 7),
    # Alarm event 4: ambient/power temperature; bit 4 = power protection, bit 5 = power alarm
    BitField("ambient_temperature_high", EVENT_4, "protection_alarm_normal", 0, 1),
    BitField("ambient_temperature_low", EVENT_4, "protection_alarm_normal", 2, 3),
    BitField("component_temperature_high", EVENT_4, "protection_alarm_normal", 5, 4),
    BitField("low_temperature_heating", EVENT_4, "on_off", 6),                # "Cell low temperature heating"
    # Severe fault: latched until restart (sensor fault, cell >= P4 + 50 mV, cell <= P6 - 200 mV,
    # overcurrent beyond P54/P55); only reported with BitGroup3 bit 7
    BitField("severe_fault", EVENT_4, "fault_normal", 7),
    # Alarm event 5: current; bit 4/5 protection, bit 6/7 their lockouts
    BitField("charging_overcurrent", EVENT_5, "protection_alarm_normal", 0, 1),
    BitField("discharging_overcurrent", EVENT_5, "protection_alarm_normal", 2, 3),
    BitField("transient_overcurrent", EVENT_5, "lockout_protection_normal", 4, 6),
    BitField("output_short_circuit", EVENT_5, "lockout_protection_normal", 5, 7),
    # Alarm event 6 (& 0x7F on the wire); bit 5 "reverse polarity" is never set by the firmware
    BitField("charging_high_voltage_protection", EVENT_6, "protection_normal", 0),   # charger > P18
    BitField("intermittent_power_supplement", EVENT_6, "warning_normal", 1),         # intermittent recharge waiting
    BitField("soc_low", EVENT_6, "protection_alarm_normal", 2, 3),                   # residual capacity alarm/protection
    BitField("cell_low_voltage_forbidden_charging", EVENT_6, "protection_normal", 4),  # cell < P9
    BitField("output_connection_failure", EVENT_6, "fault_normal", 6),
    # On-off state (& 0x0F on the wire)
    BitField("discharge_switch", ON_OFF, "on_off", 0),
    BitField("charge_switch", ON_OFF, "on_off", 1),
    BitField("current_limit_switch", ON_OFF, "on_off", 2),
    BitField("heating_switch", ON_OFF, "on_off", 3),
    # Equilibrium state 1/2: cells 1-8 / 9-16
    BitField("balancer_cell", BALANCING_1, "on_off", 0, amount=8),
    BitField("balancer_cell", BALANCING_2, "on_off", 0, amount=8, start=8),
    # SYSTEM_STATUS: decode_system_status(); BASE+20/21 cell disconnection: always 0
    # Alarm event 7 (& 0x30 on the wire): a charge activation window (P72 minutes) is running,
    # undervoltage/empty protections get cleared so a charger can charge
    BitField("auto_charging_wait", EVENT_7, "warning_normal", 4),
    BitField("manual_charging_wait", EVENT_7, "warning_normal", 5),
    # Alarm event 8 (& 0x3F on the wire)
    BitField("eep_storage_failure", EVENT_8, "fault_normal", 0),
    BitField("rtc_clock_failure", EVENT_8, "fault_normal", 1),
    BitField("no_calibration_of_voltage", EVENT_8, "warning_normal", 2),
    BitField("no_calibration_of_current", EVENT_8, "warning_normal", 3),
    BitField("no_calibration_of_null_point", EVENT_8, "warning_normal", 4),
    BitField("rtc_time_invalid", EVENT_8, "warning_normal", 5),               # RTC register 2 bit 7
]


def bit_state(byte: int, field: BitField, bit: int | None = None) -> str:
    bit = field.bit if bit is None else bit
    if field.mode in SINGLE_BIT_MODES:
        active, inactive = SINGLE_BIT_MODES[field.mode]
        return active if byte & (1 << bit) else inactive
    severe, mild = TWO_BIT_MODES[field.mode]
    if field.severe_bit is not None and byte & (1 << field.severe_bit):
        return severe
    if byte & (1 << bit):
        return mild
    return "OK"


def decode_system_status(status_byte: int) -> str:
    """
    System state with a fixed priority, so the state never sticks when no bit is set:
    bit 0 discharging, bit 1 charging, bit 4 standby, bit 5 off. Bit 2 is masked out in 0x44.
    """
    if status_byte & 0x20:
        return "Off"
    if status_byte & 0x02:
        return "Charging"
    if status_byte & 0x01:
        return "Discharging"
    if status_byte & 0x10:
        return "Standby"
    return "Unknown"


def decode(info: bytes) -> dict:
    """Decode the 0x44 INFO into {"normal": {...}, "binary": {...}}."""
    normal: dict = {}
    binary: dict = {}
    cell_count = info[2]
    base = CELL_ALARM_OFFSET + cell_count

    cell_alarms = [BYTE_ALARM_STATES.get(b, BYTE_ALARM_OTHER) for b in info[CELL_ALARM_OFFSET:base]]
    for i, value in enumerate(cell_alarms):
        normal[f"cell_voltage_alarm_{i + 1}"] = value
    temp_alarms: list[str] = []
    for key, (offset, amount) in BYTE_ALARMS.items():
        values = [BYTE_ALARM_STATES.get(info[base + offset + i], BYTE_ALARM_OTHER) for i in range(amount)]
        if amount > 1:
            temp_alarms = values
            for i, value in enumerate(values):
                normal[f"{key}_{i + 1}"] = value
        else:
            normal[key] = values[0]

    normal["any_cell_voltage_alarm"] = "Alarm" if any(v != "OK" for v in cell_alarms) else "OK"
    normal["any_cell_temperature_alarm"] = "Alarm" if any(v != "OK" for v in temp_alarms) else "OK"

    for field in BIT_FIELDS:
        byte = info[base + field.offset]
        if field.amount > 1:
            for i in range(field.amount):
                index = field.start + i
                if index < cell_count:
                    binary[f"{field.key}_{index + 1}"] = bit_state(byte, field, field.bit + i)
        elif field.is_binary:
            binary[field.key] = bit_state(byte, field)
        else:
            normal[field.key] = bit_state(byte, field)

    normal["system_status"] = decode_system_status(info[base + SYSTEM_STATUS])
    return {"normal": normal, "binary": binary}
