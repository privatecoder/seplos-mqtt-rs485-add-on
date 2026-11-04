"""CID2 0x44 telesignalization (alarms, switches, balancing, system status)."""
from __future__ import annotations

from dataclasses import dataclass

# 24 byte alarms: one status byte per value
BYTE_ALARM_STATES = {0: "OK", 1: "Alarm (low)", 2: "Alarm (high)"}
BYTE_ALARM_OTHER = "Alarm (other)"

# key: (offset, amount)
BYTE_ALARMS: dict[str, tuple[int, int]] = {
    "cell_voltage_alarm":          (3, 16),  # doc: "Cell 1..M alarm"
    "cell_temperature_alarm":      (20, 4),  # doc: "Cell temperature alarm 1..4"
    "ambient_temperature_alarm":   (24, 1),  # doc: "Environment temperature alarm"
    "component_temperature_alarm": (25, 1),  # doc: "Power temperature alarm"
    "dis_charging_current_alarm":  (26, 1),  # doc: "Charge/discharge current alarm"
    "pack_voltage_alarm":          (27, 1),  # doc: "Total battery voltage alarm"
}

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
    offset: int
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
    # Alarm event 1 (offset 29): sensor/switch faults
    BitField("voltage_sensing_failure", 29, "fault_normal", 0),          # "Voltage sensor fault"
    BitField("temperature_sensing_failure", 29, "fault_normal", 1),      # "Temperature sensor fault"
    BitField("current_sensing_failure", 29, "fault_normal", 2),          # "Current sensor fault"
    BitField("power_switch_failure", 29, "fault_normal", 3),             # "Key switch fault"
    BitField("cell_voltage_difference_fault", 29, "fault_normal", 4),    # "Cell voltage dropout fault", delta >= P60
    BitField("charging_switch_failure", 29, "fault_normal", 5),          # "Charge switch fault"
    BitField("discharging_switch_failure", 29, "fault_normal", 6),       # "Discharge switch fault"
    BitField("current_limit_switch_failure", 29, "fault_normal", 7),     # "Current limit switch fault"
    # Alarm event 2 (offset 30): voltage, bit pairs alarm/protection
    BitField("cell_overvoltage", 30, "protection_alarm_normal", 0, 1),
    BitField("cell_voltage_low", 30, "protection_alarm_normal", 2, 3),
    BitField("pack_overvoltage", 30, "protection_alarm_normal", 4, 5),
    BitField("pack_voltage_low", 30, "protection_alarm_normal", 6, 7),
    # Alarm event 3 (offset 31): cell temperature
    BitField("charging_temperature_high", 31, "protection_alarm_normal", 0, 1),
    BitField("charging_temperature_low", 31, "protection_alarm_normal", 2, 3),
    BitField("discharging_temperature_high", 31, "protection_alarm_normal", 4, 5),
    BitField("discharging_temperature_low", 31, "protection_alarm_normal", 6, 7),
    # Alarm event 4 (offset 32): ambient/power temperature; bit 4 = power protection, bit 5 = power alarm
    BitField("ambient_temperature_high", 32, "protection_alarm_normal", 0, 1),
    BitField("ambient_temperature_low", 32, "protection_alarm_normal", 2, 3),
    BitField("component_temperature_high", 32, "protection_alarm_normal", 5, 4),
    BitField("low_temperature_heating", 32, "on_off", 6),                # "Cell low temperature heating"
    # Alarm event 5 (offset 33): current; bit 4/5 protection, bit 6/7 their lockouts
    BitField("charging_overcurrent", 33, "protection_alarm_normal", 0, 1),
    BitField("discharging_overcurrent", 33, "protection_alarm_normal", 2, 3),
    BitField("transient_overcurrent", 33, "lockout_protection_normal", 4, 6),
    BitField("output_short_circuit", 33, "lockout_protection_normal", 5, 7),
    # Alarm event 6 (offset 34)
    BitField("charging_high_voltage_protection", 34, "protection_normal", 0),   # charger > P18
    BitField("intermittent_power_supplement", 34, "warning_normal", 1),         # intermittent recharge waiting
    BitField("soc_low", 34, "protection_alarm_normal", 2, 3),                   # residual capacity alarm/protection
    BitField("cell_low_voltage_forbidden_charging", 34, "protection_normal", 4),  # cell < P9
    BitField("output_reverse_polarity_protection", 34, "protection_normal", 5),
    BitField("output_connection_failure", 34, "fault_normal", 6),
    # On-off state (offset 35)
    BitField("discharge_switch", 35, "on_off", 0),
    BitField("charge_switch", 35, "on_off", 1),
    BitField("current_limit_switch", 35, "on_off", 2),
    BitField("heating_switch", 35, "on_off", 3),
    # Equilibrium state 1/2 (offsets 36/37): cells 1-8 / 9-16
    BitField("balancer_cell", 36, "on_off", 0, amount=8),
    BitField("balancer_cell", 37, "on_off", 0, amount=8, start=8),
    # offset 38: system status (decode_system_status)
    # offsets 39/40: cell disconnection - not evaluated, the firmware always sends 0x00
    # Alarm event 7 (offset 41)
    BitField("auto_charging_wait", 41, "warning_normal", 4),
    BitField("manual_charging_wait", 41, "warning_normal", 5),
    # Alarm event 8 (offset 42)
    BitField("eep_storage_failure", 42, "fault_normal", 0),
    BitField("rtc_clock_failure", 42, "fault_normal", 1),
    BitField("no_calibration_of_voltage", 42, "warning_normal", 2),
    BitField("no_calibration_of_current", 42, "warning_normal", 3),
    BitField("no_calibration_of_null_point", 42, "warning_normal", 4),
]

SYSTEM_STATUS_OFFSET = 38


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
    number_of_cells = info[2]
    lists: dict[str, list[str]] = {}
    for key, (offset, amount) in BYTE_ALARMS.items():
        count = number_of_cells if key == "cell_voltage_alarm" else amount
        values = [BYTE_ALARM_STATES.get(info[offset + i], BYTE_ALARM_OTHER) for i in range(count)]
        if amount > 1:
            lists[key] = values
            for i, value in enumerate(values):
                normal[f"{key}_{i + 1}"] = value
        else:
            normal[key] = values[0]

    normal["any_cell_voltage_alarm"] = "Alarm" if any(v != "OK" for v in lists["cell_voltage_alarm"]) else "OK"
    normal["any_cell_temperature_alarm"] = "Alarm" if any(v != "OK" for v in lists["cell_temperature_alarm"]) else "OK"

    for field in BIT_FIELDS:
        byte = info[field.offset]
        if field.amount > 1:
            for i in range(field.amount):
                binary[f"{field.key}_{field.start + i + 1}"] = bit_state(byte, field, field.bit + i)
        elif field.is_binary:
            binary[field.key] = bit_state(byte, field)
        else:
            normal[field.key] = bit_state(byte, field)

    normal["system_status"] = decode_system_status(info[SYSTEM_STATUS_OFFSET])
    return {"normal": normal, "binary": binary}
