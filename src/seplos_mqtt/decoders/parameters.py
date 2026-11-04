"""CID2 0x47 parameters: 60 x u16 (P0-P59), 27 x u8 (P60-P86), 8 function switch bytes, device name."""
from __future__ import annotations

from .common import KELVIN_OFFSET, signed16, text

# (name, kind) for parameter index 0..86, kind defines the raw unit
PARAMETER_DEFINITIONS: list[tuple[str, str]] = [
    ("cell_high_voltage_alarm", "mV"), ("cell_high_voltage_recovery", "mV"),
    ("cell_low_voltage_alarm", "mV"), ("cell_low_voltage_recovery", "mV"),
    ("cell_overvoltage_protection", "mV"), ("cell_overvoltage_recovery", "mV"),
    ("cell_undervoltage_protection", "mV"), ("cell_undervoltage_recovery", "mV"),
    ("balancing_start_voltage", "mV"), ("cell_low_voltage_charging_forbidden", "mV"),
    ("pack_high_voltage_alarm", "10mV"), ("pack_high_voltage_recovery", "10mV"),
    ("pack_low_voltage_alarm", "10mV"), ("pack_low_voltage_recovery", "10mV"),
    ("pack_overvoltage_protection", "10mV"), ("pack_overvoltage_recovery", "10mV"),
    ("pack_undervoltage_protection", "10mV"), ("pack_undervoltage_recovery", "10mV"),
    ("charger_overvoltage_protection", "10mV"), ("charger_overvoltage_recovery", "10mV"),
    ("charging_high_temperature_alarm", "0.1K"), ("charging_high_temperature_recovery", "0.1K"),
    ("charging_low_temperature_alarm", "0.1K"), ("charging_low_temperature_recovery", "0.1K"),
    ("charging_over_temperature_protection", "0.1K"), ("charging_over_temperature_recovery", "0.1K"),
    ("charging_under_temperature_protection", "0.1K"), ("charging_under_temperature_recovery", "0.1K"),
    ("discharging_high_temperature_alarm", "0.1K"), ("discharging_high_temperature_recovery", "0.1K"),
    ("discharging_low_temperature_alarm", "0.1K"), ("discharging_low_temperature_recovery", "0.1K"),
    ("discharging_over_temperature_protection", "0.1K"), ("discharging_over_temperature_recovery", "0.1K"),
    ("discharging_under_temperature_protection", "0.1K"), ("discharging_under_temperature_recovery", "0.1K"),
    ("cell_heating_on", "0.1K"), ("cell_heating_off", "0.1K"),
    ("ambient_high_temperature_alarm", "0.1K"), ("ambient_high_temperature_recovery", "0.1K"),
    ("ambient_low_temperature_alarm", "0.1K"), ("ambient_low_temperature_recovery", "0.1K"),
    ("ambient_over_temperature_protection", "0.1K"), ("ambient_over_temperature_recovery", "0.1K"),
    ("ambient_under_temperature_protection", "0.1K"), ("ambient_under_temperature_recovery", "0.1K"),
    ("power_high_temperature_alarm", "0.1K"), ("power_high_temperature_recovery", "0.1K"),
    ("power_over_temperature_protection", "0.1K"), ("power_over_temperature_recovery", "0.1K"),
    ("charging_overcurrent_alarm", "10mA"), ("charging_overcurrent_recovery", "10mA"),
    ("discharging_overcurrent_alarm", "10mA"), ("discharging_overcurrent_recovery", "10mA"),
    ("charging_overcurrent_protection", "10mA"), ("discharging_overcurrent_protection", "10mA"),
    ("transient_overcurrent_protection", "10mA"), ("output_soft_start_delay_ms", "raw"),
    ("rated_capacity", "10mAh"), ("remaining_capacity_setting", "10mAh"),
    # byte parameters 60..86
    ("cell_difference_fault", "10mV"), ("cell_difference_fault_recovery", "10mV"),
    ("balancing_start_difference", "mV"), ("balancing_stop_difference", "mV"),
    ("static_balancing_time_h", "raw"), ("cells_in_series", "raw"),
    ("charging_overcurrent_delay_s", "raw"), ("discharging_overcurrent_delay_s", "raw"),
    ("transient_overcurrent_delay_ms", "raw"), ("overcurrent_recovery_delay_s", "raw"),
    ("overcurrent_recovery_attempts", "raw"), ("charge_current_limit_delay_min", "raw"),
    ("charge_activation_delay_min", "raw"), ("charge_activation_interval_h", "raw"),
    ("charge_activation_attempts", "raw"), ("work_record_interval_min", "raw"),
    ("standby_record_interval_min", "raw"), ("standby_shutdown_delay_h", "raw"),
    ("remaining_capacity_alarm_percent", "raw"), ("remaining_capacity_protection_percent", "raw"),
    ("intermittent_charge_capacity_percent", "raw"), ("cycle_cumulative_capacity_percent", "raw"),
    # impedances: unit 0.1 mOhm derived from the BatteryMonitor export (one decimal), not verified in code
    ("connection_fault_impedance_mohm", "0.1"), ("compensation_point_1_position", "raw"),
    ("compensation_point_1_impedance_mohm", "0.1"), ("compensation_point_2_position", "raw"),
    ("compensation_point_2_impedance_mohm", "0.1"),
]

# Offsets in the parameter block WITHOUT the leading address byte of the ASCII answer
WORD_MARKER, WORD_COUNT = 0, 0x3C
BYTE_MARKER, BYTE_COUNT = 121, 0x1B
SWITCH_MARKER, SWITCH_COUNT = 149, 0x08
NAME_OFFSET = 158
BLOCK_LENGTH = 168


def _scaled(name: str, kind: str, value: int) -> tuple[str, object]:
    if kind == "mV":
        return f"{name}_v", round(value / 1000, 3)
    if kind == "10mV":
        return f"{name}_v", round(value / 100, 2)
    if kind == "0.1K":
        return f"{name}_c", round((value - KELVIN_OFFSET) / 10, 1)
    if kind == "10mA":
        return f"{name}_a", round(signed16(value) / 100, 2)
    if kind == "10mAh":
        return f"{name}_ah", round(value / 100, 2)
    if kind == "0.1":
        return name, round(value / 10, 1)
    return name, value


def decode_block(block: bytes) -> dict:
    """
    Decode the 168 byte parameter block: 0x3C + 60 x u16, 0x1B + 27 x u8, 0x08 + 8 switch bytes,
    10 byte device name. (The ASCII 0x47 answer prefixes it with the address byte.)
    """
    if block[WORD_MARKER] != WORD_COUNT or block[BYTE_MARKER] != BYTE_COUNT or block[SWITCH_MARKER] != SWITCH_COUNT:
        raise ValueError("unexpected parameter frame markers")

    values = [int.from_bytes(block[1 + 2 * i:3 + 2 * i], "big") for i in range(60)]
    values += list(block[BYTE_MARKER + 1:SWITCH_MARKER])

    parameters: dict = {}
    for (name, kind), value in zip(PARAMETER_DEFINITIONS, values, strict=True):
        key, scaled = _scaled(name, kind, value)
        parameters[key] = scaled

    switches = block[SWITCH_MARKER + 1:NAME_OFFSET]
    parameters["function_switches"] = switches.hex(" ").upper()
    # switch byte k (k = 0..5) masks alarm event k+1, bytes 6/7 hold functions
    parameters["balancing_enabled"] = bool(switches[6] & 0x01)
    parameters["balancing_in_idle"] = bool(switches[6] & 0x02)
    parameters["balancing_time_limit_enabled"] = bool(switches[6] & 0x04)
    parameters["balancing_temperature_block_enabled"] = bool(switches[6] & 0x08)
    parameters["charge_current_limit_mode"] = "warning - 10 A" if switches[7] & 0x80 else "warning / 2"
    disabled = {
        f"alarm_event_{k + 1}": [bit for bit in range(8) if not switches[k] & (1 << bit)]
        for k in range(6)
    }
    parameters["disabled_alarm_bits"] = {k: v for k, v in disabled.items() if v}
    parameters["device_name"] = text(block[NAME_OFFSET:NAME_OFFSET + 10])
    return parameters


def decode_ascii_answer(raw: bytes) -> dict:
    """ASCII 0x47 answer INFO (169 bytes): address byte + parameter block."""
    return decode_block(raw[1:])
